"""Odovzdávka služby dispečerov (modul M12)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..cas import ensure_utc, fmt, now_utc
from ..models import (Closure, Email, Handover, Recommendation, ShiftHandoverReport, Train)
from ..planovanie.planovac import DONE_STATES


def _latest_rec(session: Session, train_id: int):
    return session.scalar(select(Recommendation).where(Recommendation.train_id == train_id)
                          .order_by(Recommendation.id.desc()))


# stavy preberania po slovensky (ako vo webe – vlaky/web/common.py HANDOVER_STATUS)
HANDOVER_TEXT = {"ocakavany": "očakávaný", "prisiel": "prišiel na hranicu", "prevzaty": "prevzatý",
                 "odovzdany": "odovzdaný"}


def _iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def _minutes(value: float) -> str:
    m = int(round(value))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _duty_h(p) -> str:
    return f"{p.crew_max_duty_min / 60:g}".replace(".", ",")


def _crew_line(train: Train, crew: dict) -> str:
    """Riadok o rušňovodičovi blízko limitu. Pri chýbajúcom zázname o nástupe sú časy len odhad (nástup
    = odchod vlaku mínus príprava) – nikdy sa neuvádzajú ako fakt; bez odhadu sa limit nedá overiť."""
    from ..notifikacie.texty import crew_relief_text  # rovnaký text ako v SMS, e-maile a na webe

    text = crew_relief_text(crew) or "čas vystriedania nie je známy – overte nástup rušňovodiča"
    return f"  - vlak {train.number}: {text}"


def _crew_now(session: Session, train: Train, rec, now: datetime, p, near_min: float) -> tuple | None:
    """Pracovný čas rušňovodiča vlaku k času odovzdávky (nie zmrazený stav z posledného prepočtu – agent mohol
    stáť): ("prekroceny" | "blizko", riadok, poznámka) alebo None. Poznámka (inak prázdna) sa pripíše k vlaku
    v zozname ČERVENÝCH/ORANŽOVÝCH, keď čas vystriedania už uplynul – text odporúčania z prepočtu ho môže ešte
    uvádzať ako radu („vystriedať najneskôr o …“).

    Limit ako plánovač a strážnik: od zaznamenaného nástupu (zmena v službe, inak naplánovaná, ktorá je na rade,
    s nástupom najneskôr teraz); idúci vlak bez neho len ODHAD z odporúčania; inak predpoveď plánovača (rušňovodič,
    ktorý nastúpi). Prekročený limit je „PREKROČENÝ“, nie „blízko limitu“, a uplynulý čas vystriedania sa
    neponúka ako rada („vystriedať ihneď“)."""
    from ..planovanie.planovac import active_shift

    crew = (rec.crew or {}) if rec is not None else {}
    post = timedelta(minutes=p.crew_post_min)
    duty = _duty_h(p)
    sh = active_shift(session, train.id, now)
    name = ""
    if sh is not None and ensure_utc(sh.start_utc) <= now:
        start = ensure_utc(sh.start_utc)
        limit = start + timedelta(minutes=p.crew_max_duty_min)
        name = sh.driver.name if sh.driver else ""
        crew_start = _iso(crew.get("start"))
        predicted = (not crew.get("unknown_start") and crew_start is not None
                     and abs((crew_start - start).total_seconds()) <= 60
                     and crew.get("status") in ("ORANŽOVÁ", "ČERVENÁ"))
        estimate = False
    elif crew.get("unknown_start"):
        if train.status != "v_jazde":
            return None
        limit = _iso(crew.get("limit"))
        relieve = _iso(crew.get("relieve_by"))
        if limit is None and relieve is not None:
            limit = relieve + post
        if limit is None:
            return "blizko", _crew_line(train, crew), ""  # odchod nie je známy: limit sa nedá overiť
        predicted, estimate = True, True
    elif crew.get("status") in ("ORANŽOVÁ", "ČERVENÁ") and (crew.get("limit") or crew.get("relieve_by")):
        # predpoveď plánovača pre rušňovodiča, ktorý nastúpi (naplánovaná zmena, nový rušňovodič)
        limit = _iso(crew.get("limit"))
        relieve = _iso(crew.get("relieve_by"))
        if limit is None and relieve is not None:
            limit = relieve + post
        if limit is None:
            return None
        name = crew.get("driver") or ""
        predicted, estimate = True, False
    else:
        return None
    left = (limit - now).total_seconds() / 60.0
    relieve = limit - post
    who = f"; rušňovodič {name}" if name and not estimate else ""
    odhad = " (odhad – nástup nie je zadaný)" if estimate else ""
    if left <= 0:
        note = f"[teraz: rušňovodič – PREKROČENÝ limit {duty} h{odhad}, pozri Rušňovodiči nižšie]"
        if estimate:
            return "prekroceny", (f"  - vlak {train.number}: odhad – PREKROČENÝ limit {duty} h o {_minutes(-left)} "
                                  f"(odhad limitu {fmt(limit)}; nástup rušňovodiča nie je zadaný) – overte nástup "
                                  f"a rušňovodiča vystriedajte"), note
        return "prekroceny", (f"  - vlak {train.number}: PREKROČENÝ limit {duty} h o {_minutes(-left)} (limit "
                              f"{fmt(limit)}) – vystriedať ihneď{who}"), note
    if not (predicted or left <= near_min):
        return None
    if relieve > now:
        if estimate:
            return "blizko", _crew_line(train, crew), ""
        return "blizko", f"  - vlak {train.number}: vystriedať najneskôr o {fmt(relieve)}{who}", ""
    note = f"[teraz: rušňovodiča vystriedať ihneď{odhad}, pozri Rušňovodiči nižšie]"
    if estimate:
        return "blizko", (f"  - vlak {train.number}: odhad: vystriedať ihneď – do odhadovaného limitu {duty} h "
                          f"({fmt(limit)}) zostáva {_minutes(left)} (nástup rušňovodiča nie je zadaný)"), note
    return "blizko", (f"  - vlak {train.number}: vystriedať ihneď – do limitu {duty} h ({fmt(limit)}) zostáva "
                      f"{_minutes(left)}{who}"), note


def _track_part(track: str) -> str:
    """Koľaj výluky koľaje v texte za druhom výluky: „výluka koľaje 1“, „výluka koľají 1 a 2“, „výluka oboch koľají“,
    iný zápis z registra v zátvorke („výluka koľaje (1. TK)“); neuvedená koľaj „(koľaj neuvedená)“."""
    import re

    from ..planovanie.planovac import ALL_TRACKS, _track_numbers
    from ..siet.graf import norm

    t = " ".join((track or "").split())
    if not t:
        return "výluka koľaje (koľaj neuvedená)"
    nums = _track_numbers(t)
    if ALL_TRACKS in nums:
        return "výluka oboch koľají" if norm(t).startswith("ob") else "výluka všetkých koľají"
    if re.fullmatch(r"\d+", t):
        return f"výluka koľaje {t}"
    if re.fullmatch(r"\d+(?:\s*(?:,|a)\s*\d+)+", t):
        return f"výluka koľají {t}"
    return f"výluka koľaje ({t})"


def closure_line(c: Closure) -> str:
    """Výluka registra jedným riadkom (odovzdávka služby) – druh ako v registri výluk a vo fronte, pri výluke koľaje
    koľaj, denné okno a obdobie: „výluka koľaje 1, denne 08:00 – 14:00, od 23.11. 08:00 do odvolania (technický koniec
    23.12. 08:00)“, „úplná výluka úseku, 24.11. 08:00 – 24.11. 14:00“. Výluka do odvolania nemá koniec – technický
    koniec nie je koniec výluky (agent ho posúva, kým ju ŽSR neodvolá); denná výluka nie je celé obdobie zatvorená."""
    from ..notifikacie.texty import CLOSURE_KIND, number

    kind = _track_part(c.track) if c.kind == "kolaj" else \
        (CLOSURE_KIND.get(c.kind) or f"{c.kind} (neznámy druh – počíta sa ako úplná výluka)")
    if c.kind == "pomala_jazda" and c.speed_kmh:
        kind += f" (max {number(c.speed_kmh)} km/h)"
    elif c.electric_only and c.kind != "trakcne_vedenie":
        kind += " (len elektrická trakcia)"  # ako stĺpec Dopad v registri výluk
    parts = [kind]
    if c.daily_from or c.daily_to:
        parts.append(f"denne {c.daily_from or '?'} – {c.daily_to or '?'}")
    if c.until_revoked:
        parts.append(f"od {fmt(c.start_utc)} do odvolania (technický koniec {fmt(c.end_utc)})")
    else:
        parts.append(f"{'v období ' if len(parts) > 1 else ''}{fmt(c.start_utc)} – {fmt(c.end_utc)}")
    return ", ".join(parts)


def _stale_advice(fresh: dict) -> str:
    """Čo robiť, keď údaje agenta nie sú čerstvé – podľa dôvodu (zdravie.data_freshness „codes“). Výpadok (agent
    nebeží, schránka sa nekontroluje) odkáže na kapitolu 12; samotné dočítavanie nahromadenej pošty nie je výpadok –
    agent beží, len kým poštu nedočíta, nie je nič ZELENÉ."""
    if fresh.get("outage", True):
        return "Pred rozhodnutím overte stav – agent nebeží alebo nečíta schránku (príručka, kapitola 12)."
    return ("Agent beží a dočítava nahromadenú poštu – kým ju nedočíta, nie je nič ZELENÉ a výluky z ešte "
            "neprečítaných e-mailov nemusia byť zapísané; po dočítaní vlaky prepočíta. Pred rozhodnutím overte stav "
            "(ak dočítavanie trvá dlho, pozri Zdravie agenta).")


def build_report(session: Session, now: datetime | None = None, hours: int = 12) -> tuple:
    from ..config import settings
    from ..planovanie.planovac import current_params
    from .zdravie import data_freshness, queue_counts

    now = now or now_utc()
    since = now - timedelta(hours=hours)
    p = current_params(session)
    # odovzdávka vytvorená počas výpadku agenta (napr. dispečer na webe): rovnaké pravidlo ako web – bez čerstvých
    # údajov agenta nie je nič ZELENÉ a dokument to hneď na začiatku povie (PROMPT 12)
    fresh = data_freshness(session, now)
    trains = list(session.scalars(select(Train).where(Train.status.notin_(DONE_STATES)).order_by(Train.number)))
    lines = [f"ODOVZDÁVKA SLUŽBY – {fmt(now)}", ""]
    if fresh["stale"]:
        lines += [f"POZOR: {fresh['text']} Semafor a časy vlakov platia k času posledného prepočtu (uvedený pri "
                  f"vlaku); vlaky so ZELENOU sú uvedené ako ORANŽOVÉ. {_stale_advice(fresh)}", ""]
    data: dict = {"trains": [], "waiting": 0, "quarantined": 0, "closures_new": 0, "stale": fresh["stale"],
                  "data_note": fresh["text"]}
    red, orange, exceeded, near = [], [], [], []
    crew_notes: dict = {}  # vlak → poznámka k uplynulému času vystriedania (pripíše sa k textu odporúčania)
    near_min = max([int(x) for x in settings().csv(settings().crew_alert_minutes)] or [90])
    running = [t for t in trains if t.status == "v_jazde"]
    to_border = [t for t in trains if t.status == "na_ceste_k_hranici"]
    lines.append(f"Vlaky v jazde: {len(running)}; na ceste k hranici: {len(to_border)}; "
                 f"ostatné aktívne: {len(trains) - len(running) - len(to_border)}")
    for t in trains:
        rec = _latest_rec(session, t.id)
        color = rec.color if rec else ""
        downgraded = rec is not None and fresh["stale"] and color == "ZELENÁ"
        shown = "ORANŽOVÁ" if downgraded else color
        entry = {"number": t.number, "status": t.status, "decision": rec.decision if rec else "", "color": color,
                 "display_color": shown, "downgraded": downgraded}
        data["trains"].append(entry)
        if shown == "ČERVENÁ":
            red.append((t, rec, downgraded))
        elif shown == "ORANŽOVÁ":
            orange.append((t, rec, downgraded))
        state = _crew_now(session, t, rec, now, p, near_min)
        if state is not None:
            kind, line, note = state
            (exceeded if kind == "prekroceny" else near).append(line)
            entry["crew"] = kind
            if note:
                crew_notes[t.id] = note

    def item(t, rec, downgraded: bool) -> str:
        text = rec.summary
        if fresh["stale"]:
            when = _iso((rec.data_age or {}).get("computed_at")) or rec.created_at
            text = f"[prepočet {fmt(when)}] {text}"
        if downgraded:
            text += " (bez čerstvých údajov agenta – pôvodne ZELENÁ)"
        if crew_notes.get(t.id):
            text += f" {crew_notes[t.id]}"
        return f"  - {text}"

    for title, items in (("ČERVENÉ vlaky (vyžadujú rozhodnutie)", red), ("ORANŽOVÉ vlaky", orange)):
        lines += ["", title + ":"]
        lines += [item(t, r, d) for t, r, d in items] or ["  (žiadne)"]
    duty = _duty_h(p)
    lines += ["", f"Rušňovodiči s PREKROČENÝM limitom {duty} h (stav k {fmt(now)}):"]
    lines += exceeded or ["  (žiadni)"]
    lines += ["", f"Rušňovodiči blízko limitu {duty} h:"]
    lines += near or ["  (žiadni)"]
    data["crew_exceeded"] = len(exceeded)
    handovers = list(session.scalars(select(Handover).where(Handover.status.in_(("ocakavany", "prisiel")))))
    # číslo vlaku, nie interné id v databáze (preberajúci dispečer pozná vlaky podľa čísla)
    numbers = dict(session.execute(select(Train.id, Train.number).where(
        Train.id.in_({h.train_id for h in handovers}))).all()) if handovers else {}
    lines += ["", "Očakávané preberania na hraniciach:"]
    lines += [f"  - vlak {numbers.get(h.train_id, '?')}: {h.border.handover_station if h.border else '?'} avízo "
              f"{fmt(h.avizo_eta_utc)} ({HANDOVER_TEXT.get(h.status, h.status)})" for h in handovers] or ["  (žiadne)"]
    new_closures = list(session.scalars(select(Closure).where((Closure.created_at >= since) |
                                                              (Closure.updated_at >= since))
                                        .order_by(Closure.start_utc)))
    data["closures_new"] = len(new_closures)

    def _state(c) -> str:
        if c.status == "zrusene":
            return "ZRUŠENÁ"
        created = c.created_at.replace(tzinfo=since.tzinfo) if c.created_at.tzinfo is None else c.created_at
        kind = "nová" if created >= since else f"zmenená (verzia {c.version})"
        return f"{kind}, {'overená' if c.status == 'overene' else 'NEPOTVRDENÁ'}"

    lines += ["", f"Nové alebo zmenené výluky za {hours} h:"]
    lines += [f"  - {c.from_station} – {c.to_station}: {closure_line(c)} ({_state(c)})"
              for c in new_closures] or ["  (žiadne)"]
    # fronta rovnako ako na webe (/fronta, počítadlo v hlavičke): čakajúce udalosti, automaticky zapísané výluky,
    # ktoré dispečer ešte neoveril, aj potvrdené udalosti so zlyhaným zápisom
    queue = queue_counts(session)
    quarantined = session.scalars(select(Email).where(Email.verdict == "karantena",
                                                      Email.received_at >= since)).all()
    data["waiting"], data["quarantined"], data["auto_applied"] = queue["total"], len(quarantined), queue["auto"]
    lines += ["", f"Nepotvrdené udalosti z e-mailov: {queue['total']}"]
    if queue["auto"]:
        lines.append(f"  - z toho zapísané automaticky – overte: {queue['auto']}")
    lines += [f"E-maily v karanténe za {hours} h: {len(quarantined)}",
              "", "Otvorené úlohy: doplní odovzdávajúci dispečer."]
    return "\n".join(lines), data


def create_report(session: Session, now: datetime | None = None, notify: bool = True) -> ShiftHandoverReport:
    content, data = build_report(session, now)
    rep = ShiftHandoverReport(content=content, data=data, created_at=now or now_utc())
    session.add(rep)
    session.flush()
    if notify:
        send_report(session, rep)
    return rep


def send_report(session: Session, rep: ShiftHandoverReport, only: set | None = None) -> list:
    """Pošle odovzdávku dispečerom; pri zlyhaní ju agent pošle znova – tú istú správu s rovnakým kľúčom, len tým
    (only – dvojice (kanál, príjemca)), ktorým ju nepodarilo doručiť."""
    from ..notifikacie.odosielanie import send_internal

    return send_internal(session, "odovzdavka", "[AGENT] Odovzdávka služby", rep.content, "INFO",
                         dedup_key=f"odovzdavka:{rep.id}", channels=["email"], only=only)
