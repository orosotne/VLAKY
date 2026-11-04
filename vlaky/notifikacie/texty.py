"""Texty správ pre dispečera (slovensky, čistý text – čitateľné v e-maile, Teams aj SMS)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..cas import fmt
from ..config import settings
from ..models import Recommendation


def _t(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return fmt(datetime.fromisoformat(iso))
    except (TypeError, ValueError):
        return str(iso)


def eur(value, digits: int = 0) -> str:
    """Suma v eurách po slovensky: tisíce oddelené medzerou, desatinná čiarka („1 593 €“, „12,50 €“) – ako na
    webe. Formát Pythonu „1,593 €“ by dispečer čítal ako 1,59 €."""
    try:
        text = f"{float(value or 0):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"
    return text.replace(",", " ").replace(".", ",") + " €"


def dec(value, digits: int = 1) -> str:
    """Desatinné číslo s desatinnou čiarkou („0,8“ h, „4,7“ h, „110,1“ km, „1,08“) – ako na webe; zápis Pythonu
    „0.8“ nie je slovenský."""
    try:
        return f"{float(value):.{digits}f}".replace(".", ",")
    except (TypeError, ValueError):
        return str(value if value is not None else "")


def hours(value) -> str:
    """Počet hodín limitu bez zbytočných desatinných miest („13“, „18“, „12,5“) – nie „13.0“."""
    try:
        return f"{float(value):g}".replace(".", ",")
    except (TypeError, ValueError):
        return str(value if value is not None else "")


def count(n: int, one: str, few: str, many: str) -> str:
    """Počet s tvarom podstatného mena po slovensky: „1 udalosť“, „3 udalosti“, „5 udalostí“ (aj „0 udalostí“)."""
    n = int(n or 0)
    return f"{n} {one if n == 1 else few if 2 <= n <= 4 else many}"


def number(value) -> str:
    """Číslo po slovensky: tisíce oddelené medzerou, desatinná čiarka, celé číslo bez desatinných miest („1 500“,
    „12,5“) – napr. hmotnosť a dĺžka vlaku v koncepte objednávky trasy."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "")
    text = f"{v:,.0f}" if abs(v - round(v)) < 1e-9 else f"{v:,.1f}"
    return text.replace(",", " ").replace(".", ",")


def _duty_h(crew: dict) -> str:
    try:
        return hours(float(crew.get("max_duty_h") or 13))
    except (TypeError, ValueError):
        return "13"


def relief_overdue(crew: dict | None) -> str:
    """Čas vystriedania rušňovodiča (``relieve_by``) už pri prepočte uplynul → „limit 13 h uplynie (uplynul) o …“,
    inak „“. Pokyn „vystriedať najneskôr o <minulý čas>“ by dispečer nemal ako splniť – treba „ihneď“."""
    crew = crew or {}
    left, limit, relieve = crew.get("left_min"), crew.get("limit"), crew.get("relieve_by")
    if not isinstance(left, (int, float)) or not limit or not relieve:
        return ""
    try:
        post = (datetime.fromisoformat(limit) - datetime.fromisoformat(relieve)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return ""
    if left > post:
        return ""
    return f"limit {_duty_h(crew)} h {'uplynul' if left <= 0 else 'uplynie'} o {_t(limit)}"


def crew_relief_text(crew: dict | None) -> str:
    """Kedy vystriedať rušňovodiča. Pri neznámom nástupe (``unknown_start``) len ako odhad – nikdy ako fakt.
    Ak čas vystriedania už uplynul, „vystriedať ihneď“ s časom limitu (:func:`relief_overdue`)."""
    crew = crew or {}
    late = relief_overdue(crew)
    if crew.get("unknown_start"):
        if crew.get("relieve_by"):
            if late:
                return f"odhad: vystriedať ihneď ({late}; nástup rušňovodiča nie je zadaný)"
            return f"odhad: vystriedať najneskôr o {_t(crew['relieve_by'])} (nástup rušňovodiča nie je zadaný)"
        return f"limit {_duty_h(crew)} h sa nedá overiť – zadajte nástup rušňovodiča"
    if crew.get("relieve_by"):
        if late:
            return f"vystriedať ihneď ({late})"
        return f"vystriedať najneskôr o {_t(crew['relieve_by'])}"
    return ""


def _decision_head(rec: Recommendation) -> str:
    """Rozhodnutie s časom odchodu (predmet e-mailu, SMS). Idúci vlak, ktorý pred cieľom čaká, kým sa neuvoľní vjazd
    (``chosen["until_entry"]``), čas odchodu nemá – je to len hranica výpočtu, posúva sa s každým prepočtom."""
    ch = rec.chosen or {}
    if ch.get("until_entry"):
        return f"{rec.decision} pred cieľom"
    if ch.get("depart") and rec.decision not in ("ESKALOVAŤ", "POKRAČOVAŤ"):
        return f"{rec.decision} {_t(ch['depart'])}"
    return rec.decision


def recommendation_subject(rec: Recommendation) -> str:
    train = rec.train
    rel = f"{train.origin} → {train.destination}" if train else ""
    head = _decision_head(rec)
    prefix = "[TIEŇ]" if settings().shadow_mode else ""
    return f"{prefix}[AGENT][{rec.priority}] Vlak {train.number if train else '?'} {rel}: {head} ({rec.color})"


_RANK = {"ZELENÁ": 0, "ORANŽOVÁ": 1, "ČERVENÁ": 2}


def _km(value) -> str:
    """Kilometre s desatinnou čiarkou („110,1 km“)."""
    return dec(value) if value is not None else ""


def _semafor_cap(rec: Recommendation) -> Optional[str]:
    """Obmedzenie semaforu odporúčania – rovnaké pravidlo ako web (vlaky/web/data.py semafor_cap): ak ZELENÁ nie je
    dovolená (zoznam ``data_age["obmedzenie"]`` od plánovača, zastarané dáta, neznámy nástup rušňovodiča, idúci
    vlak bez čerstvej polohy alebo mimo trasy, výluka mimo siete, farba nižšia ako zvolená varianta), vráti farbu,
    nad ktorú sa v správe nesmie ísť; inak None."""
    age = rec.data_age or {}
    crew = rec.crew or {}
    ch = rec.chosen or {}
    pos = age.get("position_min")
    mb = age.get("mailbox_min")
    capped = ((rec.color != "ZELENÁ" and ch.get("status") == "ZELENÁ") or bool(crew.get("unknown_start"))
              or (isinstance(mb, (int, float)) and mb > 30) or bool(age.get("obmedzenie"))
              or any("nemôže byť ZELEN" in str(r) or "nie je na plánovanej trase" in str(r)
                     or "Chýbajú čerstvé dáta o polohe vlaku" in str(r) for r in rec.reasons or [])
              or (rec.train is not None and rec.train.status == "v_jazde"
                  and not (isinstance(pos, (int, float)) and pos <= settings().position_stale_minutes)))
    if not capped:
        return None
    return rec.color if rec.color in ("ORANŽOVÁ", "ČERVENÁ") else "ORANŽOVÁ"


def _capped_status(status: str, cap: Optional[str]) -> str:
    if not cap or not status:
        return status
    return status if _RANK.get(status, 0) >= _RANK.get(cap, 0) else cap


def recommendation_body(rec: Recommendation) -> str:
    s = settings()
    lines = [rec.summary, ""]
    if rec.trigger:
        lines.append(f"Čo sa zmenilo: {rec.trigger}")
    ch = rec.chosen or {}
    if ch.get("v_cieli"):
        # idúci vlak v cieli (alebo podľa plánu už v cieli bez polohy) – trasa sa neprepočítava, nič „teraz“
        where = (f"Vlak je v cieli – príchod {_t(ch.get('prichod_v_cieli'))}." if ch.get("prichod_v_cieli") else
                 f"Podľa plánu by už mal byť v cieli (príchod podľa plánu {_t(ch.get('prichod_podla_planu'))}) – "
                 "aktuálna poloha nie je známa, overte ju.")
        lines += [f"Odporúčanie: {rec.decision} – semafor {rec.color}", where,
                  "Po príchode ukončite jazdu vlaku (stav „ukončený“)."]
    elif ch:
        lines += [
            f"Odporúčanie: {rec.decision} – semafor {rec.color}",
            f"Trasa: {ch.get('route', '')}",
            f"Odchod: {'po uvoľnení vjazdu do cieľa' if ch.get('until_entry') else _t(ch.get('depart'))}   "
            f"Príchod: {_t(ch.get('arrival_p50'))} (90 % do {_t(ch.get('arrival_p90'))})",
            f"P(včas): {ch.get('p_on_time', 0):.0%}   P(zadržania pred výlukou): {ch.get('p_hold', 0):.0%}",
            f"Očakávaný náklad: {eur(ch.get('expected_cost', 0))} (vzorové sadzby, ak nie sú nastavené vlastné)",
        ]
    pp = rec.path_proposal or {}
    # návrh s jedinou dopravňou (zvyšok jazdy pred cieľom) nie je trasa, ktorú by sa dalo objednať
    if len(pp.get("stations") or []) >= 2:
        lines += ["", f"NÁVRH TRASY ({pp.get('route', '')}, {_km(pp.get('km'))} km):"]
        if pp.get("needs_new_path"):
            win = pp.get("request_window")
            lines.append("  → Treba objednať (ad hoc) trasu u ŽSR"
                         + (f", odchod v okne {_t(win[0])} – {_t(win[1])}" if win else "") + ".")
        for st in pp["stations"]:
            if st.get("type") == "junction":
                continue
            lines.append(f"  {st['name']:<34} prích. {_t(st.get('arr')):>13}  odch. {_t(st.get('dep')):>13}"
                         f"  ({_km(st.get('km'))} km)")
    cap = _semafor_cap(rec)
    win = rec.windows or {}
    if win:
        # pri obmedzenom semafore sú okná len informáciou o výlukách, nie odporúčaním na odchod
        lines += ["", f"Okná odchodu podľa výluk – len informatívne (semafor {cap}, nie je to odporúčanie na odchod):"
                  if cap else "Okná bezpečného odchodu (ZELENÁ):"]
        for route, wins in win.items():
            txt = ", ".join(f"{_t(a)} – {_t(b)}" for a, b in wins) or "žiadne"
            lines.append(f"  {route}: {txt}")
    crew = rec.crew or {}
    if crew.get("unknown_start"):
        # nástup rušňovodiča nie je zadaný – časy sú len odhad, nie fakt
        lines += ["", f"Rušňovodič: {crew.get('driver') or '—'} – nástup nie je zadaný, pracovný čas sa nedá "
                      f"overiť – {crew.get('status', '')}", f"  → {crew_relief_text(crew)}."]
        if crew.get("start"):
            lines.append(f"  (odhad predpokladá nástup o {_t(crew.get('start'))} – odchod vlaku mínus príprava; "
                         f"zadajte skutočný nástup rušňovodiča)")
    elif crew and ch.get("v_cieli") and not ch.get("prichod_v_cieli"):
        lines += ["", f"Rušňovodič: {crew.get('driver') or '—'}; nástup {_t(crew.get('start'))}, limit "
                      f"{_duty_h(crew)} h do {_t(crew.get('limit'))}; koniec služby po ukončení jazdy "
                      f"– {crew.get('status', '')}"]
    elif crew:
        lines += ["", f"Rušňovodič: {crew.get('driver') or '—'}; nástup {_t(crew.get('start'))}, limit "
                      f"{_duty_h(crew)} h do {_t(crew.get('limit'))}; koniec {_t(crew.get('end_p50'))} "
                      f"(90 % do {_t(crew.get('end_p90'))}) – {crew.get('status', '')}"]
        if crew.get("status") in ("ORANŽOVÁ", "ČERVENÁ") and crew.get("relieve_by"):
            late = relief_overdue(crew)
            lines.append(f"  → Vystriedať ihneď ({late})." if late else
                         f"  → Vystriedať najneskôr o {_t(crew.get('relieve_by'))}.")
    if rec.variants:
        lines += ["", "Porovnanie variantov:"]
        for v in rec.variants:
            lines.append(f"  {v.get('route', '')[:30]:<30} {_t(v.get('depart')):>13} {v.get('kind', ''):<12} "
                         f"{_capped_status(v.get('status', ''), cap):<9} P(včas) {v.get('p_on_time', 0):.0%}  "
                         f"{eur(v.get('expected_cost', 0))}")
    if rec.reasons:
        lines += ["", "Dôvody a upozornenia:"] + [f"  - {r}" for r in rec.reasons]
    age = rec.data_age or {}
    if age:
        parts = []
        if age.get("position_min") is not None:
            parts.append(f"poloha pred {age['position_min']:.0f} min")
        if age.get("mailbox_min") is not None:
            parts.append(f"schránka skontrolovaná pred {age['mailbox_min']:.0f} min")
        if parts:
            lines += ["", "Čerstvosť dát: " + ", ".join(parts)]
    # idúci vlak (aj keď čaká pred výlukou či pred vjazdom do cieľa) už vyšiel – kontrolný zoznam pred odchodom
    # patrí len k odchodu (pri odstavení idúceho vlaku k neskoršiemu odchodu na novú trasu)
    running = rec.train is not None and rec.train.status == "v_jazde"
    if rec.decision in ("VYPRAVIŤ", "POČKAŤ", "ODSTAVIŤ", "PRESMEROVAŤ") and \
            not (running and rec.decision != "ODSTAVIŤ"):
        lines += ["", "Pred odchodom (kontrolný zoznam): vlak aktivovaný a súpis vlaku ND + správa o zostave "
                      "zadané v KIS/PIS (inak ŽSR vlak nevypraví), pripravenosť vlaku v KIS, skúška bŕzd, "
                      "doklady (CIM / CIM/SMGS), rušňovodič s platnou znalosťou trate."]
    drafts = (rec.path_proposal or {}).get("drafts") or {}
    if drafts:
        lines += ["", "KONCEPTY SPRÁV (agent ich neodosiela – skopírujte, upravte a odošlite sami):"]
        for key in ("zsr_path_request", "customer"):
            if drafts.get(key) and (key != "zsr_path_request" or (rec.path_proposal or {}).get("needs_new_path")):
                lines += ["", "-" * 60, drafts[key]]
        lines.append("-" * 60)
    lines += ["", f"Detail a potvrdenie: {s.public_url.rstrip('/')}/odporucania/{rec.id}",
              "", "Agent dispečer iba odporúča. Rozhoduje a koná dispečer. Správu nepreposielajte mimo firmy."]
    return "\n".join(lines)


def sms_text(rec: Recommendation) -> str:
    train = rec.train
    ch = rec.chosen or {}
    txt = f"AGENT {rec.priority}: vlak {train.number if train else '?'} {_decision_head(rec)}"
    crew = rec.crew or {}
    if crew.get("status") in ("ORANŽOVÁ", "ČERVENÁ"):
        if crew.get("unknown_start"):
            txt += f"; rušňovodič: {crew_relief_text(crew)}"
        elif crew.get("relieve_by"):
            late = relief_overdue(crew)
            txt += f"; vystriedať rušňovodiča ihneď ({late})" if late else \
                f"; vystriedať rušňovodiča do {_t(crew.get('relieve_by'))}"
    return (txt + f". Detail: {settings().public_url.rstrip('/')}/odporucania/{rec.id}")[:300]


def order_proposal_text(order, plan: dict) -> tuple:
    """Návrh prepravy pre novú objednávku (krok A.3 v príbehu vlaku)."""
    s = settings()
    subject = (f"{'[TIEŇ]' if s.shadow_mode else ''}[AGENT][DÔLEŽITÉ] Nová objednávka: "
               f"{order.origin} → {order.destination} ({order.customer or 'zákazník'})")
    if plan.get("error"):
        return subject, f"Objednávku sa nepodarilo naplánovať: {plan['error']}\n\nSkontrolujte údaje objednávky."
    lines = [f"Zákazník: {order.customer or '—'}", f"Relácia: {order.origin} → {order.destination}",
             f"Najskorší odchod: {fmt(order.earliest_departure_utc)}   Termín: {fmt(order.deadline_utc)}",
             f"Tovar: {order.cargo or '—'}   Vozne: {order.wagons or '—'}   "
             f"Hmotnosť: {number(order.weight_t) if order.weight_t else '—'} t   "
             f"Dĺžka: {number(order.length_m) if order.length_m else '—'} m{'   RID: áno' if order.rid else ''}", "",
             f"Odporúčanie agenta: {plan.get('decision', '')}"]
    lines += [f"  - {e}" for e in plan.get("explanation", [])]
    if plan.get("routes"):
        lines += ["", "Možné trasy:"]
        for r in plan["routes"]:
            via = ", ".join(r["stations"][1:-1][:8])
            lines.append(f"  {r['name']}: {_km(r['km'])} km, ~{r['minutes']} min (cez {via}…)")
    pp = plan.get("path_proposal") or {}
    if pp.get("stations"):
        win = pp.get("request_window")
        lines += ["", f"NÁVRH TRASY na objednanie u ŽSR ({pp.get('route')}):"
                      + (f" odchod v okne {_t(win[0])} – {_t(win[1])}" if win else "")]
        for st in pp["stations"]:
            if st.get("type") == "junction":
                continue
            lines.append(f"  {st['name']:<34} prích. {_t(st.get('arr')):>13}  odch. {_t(st.get('dep')):>13}")
    lines += ["", "Ďalší krok: dispečer potvrdí termín zákazníkovi a objedná trasu v IS KIS (ad hoc trasa "
                  "najneskôr 6 h pred odchodom). Agent nič neodosiela navonok.",
              f"Detail: {s.public_url.rstrip('/')}/vlaky"]
    return subject, "\n".join(lines)
