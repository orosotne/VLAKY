"""Validácia vytiahnutých udalostí a rozhodnutie, či ich musí potvrdiť dispečer (M5, zámerne konzervatívne).

Kontroly: názvy dopravní proti registru siete (pôvodný text aj názov podľa ŽSR sa uložia), existencia
úseku v sieti, logika časov (koniec po začiatku, rozumný rok), formát čísla vlaku, hraničný priechod
(:func:`vlaky.hranice.find_border`), existencia vlaku pri avíze.

Udalosť ide na potvrdenie, ak: istota < ``confirm_threshold`` ALEBO validácia našla chybu ALEBO e-mail je
podozrivý ALEBO sa udalosť týka najbližších ``confirm_horizon_h`` hodín (alebo už začala) ALEBO ide o zrušenie
výluky, zrušenie trasy či mimoriadnosť.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..cas import UTC, ensure_utc, local, local_naive_to_utc, now_utc, tz
from ..config import settings
from ..models import BorderCrossing, Train
from .stanice import stations
from .udalosti import ALWAYS_CONFIRM, CLOSURE_KINDS, KIND_LABELS, PATH_KINDS, normalize_data

MAX_SECTION_KM = 150.0
_STATION_FIELDS = {
    "vyluka_nova": ("from_station", "to_station"), "vyluka_zmena": ("from_station", "to_station"),
    "pomala_jazda": ("from_station", "to_station"), "vyluka_zrusenie": ("from_station", "to_station"),
    "mimoriadnost": ("from_station", "to_station"), "objednavka": ("origin", "destination"),
}
_TIME_FIELDS = ("start", "end", "eta", "earliest_departure", "deadline", "accept_by")


@dataclass
class ValidationResult:
    data: dict
    errors: list = field(default_factory=list)       # chyby → vždy potvrdenie dispečerom
    blocking: list = field(default_factory=list)     # chyby, pre ktoré nemožno udalosť aplikovať
    warnings: list = field(default_factory=list)
    stations: dict = field(default_factory=dict)     # pole → {text, name, ref, method}
    border: Optional[dict] = None
    train_id: Optional[int] = None
    section_km: Optional[float] = None
    source_text: dict = field(default_factory=dict)   # pôvodné názvy dopravní z e-mailu
    needs_confirmation: bool = True
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"errors": self.errors, "blocking": self.blocking, "warnings": self.warnings,
                "stations": self.stations, "source_text": self.source_text, "section_km": self.section_km,
                "border": self.border, "train_id": self.train_id, "confirmation_reasons": self.reasons}


# ---------------------------------------------------------------------- pomocné funkcie
def parse_utc(value: Any) -> Optional[datetime]:
    """ISO reťazec (UTC alebo s posunom) → datetime v UTC; bez zóny = miestny čas.

    Prijme aj slovenský zápis z formulára („12.11.2026 08:00“) – ako miestny čas.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        from .datumy import parse_datetime_text

        dt = parse_datetime_text(text, now_utc().date()) if re.search(r"\d{1,2}\s?\.\s?\d{1,2}\s?\.", text) else None
        if dt is None:
            return None
    return local_naive_to_utc(dt) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_number(value: Any) -> Optional[float]:
    """Číslo aj z textu („1 480“, „22,5“); neplatné → None."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def train_digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


_DONE_TRAIN = ("ukonceny", "odovzdany")


def _run_day(t: Train) -> Optional[date]:
    try:
        return date.fromisoformat(t.run_date) if t.run_date else None
    except ValueError:
        return None


def _train_candidates(session: Session, digits: str) -> list:
    rows = session.scalars(select(Train).where(Train.number.contains(digits))).all()
    return [t for t in rows if train_digits(t.number) == digits and t.status not in ("zruseny",)]


def find_train(session: Session, number: Any, day: Optional[str] = None, *,
               avizo: bool = False) -> Optional[Train]:
    """Vlak podľa čísla (len číslice, napr. „Nex 54321“ = „54321“) a dňa jazdy (± 1 deň).

    ``avizo`` – vlak pre avízo (príchod vlaku na hranicu): ukončený či odovzdaný vlak sa nevyberie (plánovač ho už
    nevyhodnocuje – napr. minulotýždňová jazda vlaku s rovnakým číslom); ak žiadny aktívny vlak s týmto číslom nemá
    dátum jazdy v rozsahu, použije sa jediný aktívny vlak bez dátumu jazdy (napr. založený vo webe s prázdnym poľom
    „Dátum jazdy“, ako radí hláška k avízu pre vlak mimo systému). Viac takých vlakov je nejednoznačných – vlak sa
    nevyberie. Trasy sa vlaku bez dátumu jazdy takto nepriraďujú (patrí mu len trasa jeho najbližšej jazdy –
    plánovač, ``stav.allocated_paths``)."""
    digits = train_digits(number)
    if not digits:
        return None
    cands = _train_candidates(session, digits)
    if avizo:
        cands = [t for t in cands if t.status not in _DONE_TRAIN]
    if not cands:
        return None
    if day:
        exact = [t for t in cands if t.run_date == day]
        if exact:
            return exact[0]
        try:
            d = date.fromisoformat(day)
        except ValueError:
            d = None
        if d:
            near = []
            for t in cands:
                rd = _run_day(t)
                if rd is None:
                    continue
                diff = abs((rd - d).days)
                if diff <= 1:
                    near.append((diff, t))
            if near:
                return sorted(near, key=lambda x: x[0])[0][1]
            if avizo:
                free = [t for t in cands if _run_day(t) is None]
                if len(free) == 1:
                    return free[0]
        return None
    active = [t for t in cands if t.status not in _DONE_TRAIN]
    return active[0] if len(active) == 1 else None


def _days_text(days: list) -> str:
    return ", ".join(date.fromisoformat(x).strftime("%d.%m.%Y") for x in days[:5])


def train_mismatch(session: Session, number: Any, day: Optional[str] = None) -> str:
    """Prečo :func:`find_train` (``avizo=True``) vlak nenašiel – text pre dispečera s radou, čo urobiť („“ = vlak
    s týmto číslom v systéme nie je). Ukončená jazda s rovnakým číslom (napr. minulý týždeň) sa neopravuje –
    pre novú jazdu treba založiť nový vlak."""
    digits = train_digits(number)
    cands = _train_candidates(session, digits) if digits else []
    if not cands:
        return ""
    try:
        avizo_day = date.fromisoformat(str(day)) if day else None
    except ValueError:
        avizo_day = None
    shown_day = avizo_day.strftime("%d.%m.%Y") if avizo_day else ""
    new = "založte vlak pre túto jazdu" + (f" (dátum jazdy {shown_day})" if shown_day else "")
    active = [t for t in cands if t.status not in _DONE_TRAIN]
    undated = [t for t in active if _run_day(t) is None]
    if len(undated) > 1:
        return f"v systéme je {len(undated)}× vlak {digits} bez dátumu jazdy – doplňte dátum jazdy správnemu vlaku"
    dated = sorted({t.run_date for t in active if _run_day(t) is not None})
    if dated:
        return (f"v systéme je vlak {digits} len s iným dátumom jazdy ({_days_text(dated)}) – ak ide o tento vlak, "
                f"opravte jeho dátum jazdy alebo dátum v udalosti, inak {new}")
    if undated:
        return f"v systéme je vlak {digits} bez dátumu jazdy – doplňte mu dátum jazdy" + \
            (f" {shown_day}" if shown_day else "")
    done = sorted({t.run_date for t in cands if _run_day(t) is not None})
    same = [x for x in done if avizo_day and abs((date.fromisoformat(x) - avizo_day).days) <= 1]
    if same:
        # rovnaká jazda je už ukončená (alebo ju dispečer omylom ukončil) – avízo sa ukončenému vlaku nepriradí
        return (f"vlak {digits} s dátumom jazdy {_days_text(same)} je v systéme ukončený alebo odovzdaný – ak jazda "
                f"ešte len príde, zmeňte stav vlaku, inak {new}")
    when = f" ({_days_text(done)})" if done else ""
    return (f"vlak {digits} je v systéme len ako ukončená alebo odovzdaná jazda{when} – tú avízo nemení; {new}")


def ensure_borders(session: Session) -> None:
    """Ak je register hraničných priechodov prázdny, nahrá ho z data/hranice.json (ako init-db)."""
    if session.scalar(select(func.count(BorderCrossing.id))) == 0:
        from ..hranice import seed_borders

        try:
            seed_borders(session)
        except Exception:  # súbor chýba – validácia priechodu potom zlyhá s jasnou chybou
            pass


def resolve_border(session: Session, data: dict) -> Optional[BorderCrossing]:
    from ..hranice import find_border

    ensure_borders(session)
    for key in ("border_code", "border"):
        val = data.get(key)
        if val:
            bc = find_border(session, str(val))
            if bc:
                return bc
    text = data.get("border")
    if text:   # skloňovaný tvar („do Čadce“) – podľa vzorov názvov priechodov
        from .stanice import find_borders_in_text
        from .text import fold

        hits = find_borders_in_text(fold(str(text)))
        if hits:
            return session.scalar(select(BorderCrossing).where(BorderCrossing.code == hits[0][0].code))
    return None


def _section_km(net: Any, a: str, b: str) -> Optional[float]:
    """Dĺžka úseku po najkratšej ceste (bez ohľadu na trakciu); None = dopravne nie sú prepojené."""
    from ..siet.graf import TrainSpec

    for gauge in ("1435", "1520"):
        try:
            r = net.route(a, b, TrainSpec(traction="M", gauge=gauge, vmax_kmh=200))
        except Exception:
            r = None
        if r is not None:
            return r.length_m / 1000.0
    return None


def _dst_day(dt: datetime) -> bool:
    d = local(dt).date()
    z = tz()
    a = datetime(d.year, d.month, d.day, 0, 0, tzinfo=z).utcoffset()
    b = (datetime(d.year, d.month, d.day, 0, 0, tzinfo=z) + timedelta(hours=23, minutes=59)).utcoffset()
    return a != b


def effective_start(kind: str, data: dict) -> Optional[datetime]:
    """Čas, od ktorého udalosť ovplyvňuje prevádzku (na posúdenie horizontu potvrdenia)."""
    if kind in CLOSURE_KINDS or kind in ("mimoriadnost", "vyluka_zrusenie"):
        return parse_utc(data.get("start"))
    if kind in PATH_KINDS or kind == "trasa_zrusena":
        for p in data.get("points") or []:
            t = parse_utc(p.get("arr")) or parse_utc(p.get("dep"))
            if t:
                return t
        if data.get("date"):
            try:
                d = date.fromisoformat(data["date"])
                return local_naive_to_utc(datetime(d.year, d.month, d.day))
            except ValueError:
                return None
        return None
    if kind == "avizo":
        return parse_utc(data.get("eta"))
    if kind == "objednavka":
        return parse_utc(data.get("earliest_departure")) or parse_utc(data.get("deadline"))
    return None


# ---------------------------------------------------------------------- validácia
def validate_event(session: Session, kind: str, data: dict, *, confidence: float, verdict: str = "ok",
                   now: Optional[datetime] = None, previous: Optional[dict] = None) -> ValidationResult:
    """Skontroluje a znormalizuje dáta udalosti a určí, či ju musí potvrdiť dispečer."""
    now = ensure_utc(now) if now else now_utc()
    data = normalize_data(kind, data)
    res = ValidationResult(data=data)
    prev_src = dict((previous or {}).get("source_text") or {})
    st = stations()

    def station_field(fname: str, required: bool) -> None:
        val = data.get(fname)
        if not val:
            if required:
                res.errors.append(f"Chýba dopravňa ({_label(fname)}).")
                res.blocking.append(f"chýba {_label(fname)}")
            return
        m = st.match(str(val))
        if m is None and kind == "objednavka":
            bc = resolve_border(session, {"border": val})
            if bc:
                res.stations[fname] = {"text": prev_src.get(fname, val), "name": bc.sk_station, "ref": "",
                                       "method": "hranicny_priechod"}
                data[fname] = bc.sk_station
                return
        if m is None:
            res.errors.append(f"Neznáma dopravňa „{val}“ ({_label(fname)}) – nie je v registri siete.")
            res.stations[fname] = {"text": prev_src.get(fname, val), "name": None, "ref": "", "method": "nenajdena"}
            return
        res.stations[fname] = {"text": prev_src.get(fname, val), "name": m.name, "ref": m.ref, "method": m.method}
        if m.method == "priblizne":
            res.warnings.append(f"Dopravňa „{val}“ určená približne ako „{m.name}“ – overte.")
        data[fname] = m.name

    # --- dopravne
    if kind in ("vyluka_nova", "vyluka_zmena", "pomala_jazda"):
        station_field("from_station", True)
        station_field("to_station", True)
    elif kind in ("vyluka_zrusenie", "mimoriadnost"):
        station_field("from_station", kind == "mimoriadnost")
        station_field("to_station", kind == "mimoriadnost")
        if kind == "vyluka_zrusenie" and not data.get("external_ref") and not (
                data.get("from_station") and data.get("to_station")):
            res.errors.append("Zrušenie výluky neobsahuje číslo výluky ani úsek – nedá sa priradiť.")
            res.blocking.append("chýba číslo výluky aj úsek")
    elif kind == "objednavka":
        station_field("origin", True)
        station_field("destination", True)
    if data.get("via"):
        norm_via = []
        for v in data["via"]:
            m = st.match(str(v))
            norm_via.append(m.name if m else v)
            if not m:
                res.warnings.append(f"Medziľahlá dopravňa „{v}“ nie je v registri siete.")
        data["via"] = norm_via

    # --- úsek existuje v sieti
    a_ok = res.stations.get("from_station", {}).get("name")
    b_ok = res.stations.get("to_station", {}).get("name")
    if a_ok and b_ok and a_ok != b_ok and st.net is not None:
        km = _section_km(st.net, a_ok, b_ok)
        if km is None:
            res.errors.append(f"Úsek {a_ok} – {b_ok} sa v sieti nenašiel (dopravne nie sú prepojené).")
        else:
            res.section_km = round(km, 1)
            if km > MAX_SECTION_KM:
                res.warnings.append(f"Úsek {a_ok} – {b_ok} je neobvykle dlhý ({km:.0f} km) – overte.")

    # --- časy
    times: dict[str, datetime] = {}
    for f in _TIME_FIELDS:
        if f not in data or data.get(f) in (None, ""):
            continue
        t = parse_utc(data[f])
        if t is None:
            res.errors.append(f"Neplatný čas v poli {_label(f)}: „{data[f]}“.")
            res.blocking.append(f"neplatný čas {_label(f)}")
            continue
        data[f] = t.isoformat()
        times[f] = t
        if not (now.year - 1 <= local(t).year <= now.year + 2):
            res.errors.append(f"Nepravdepodobný rok v poli {_label(f)}: {local(t).year}.")
        if _dst_day(t):
            res.warnings.append(f"{_label(f).capitalize()} pripadá na deň zmeny letného/zimného času – overte čas.")
    if kind in CLOSURE_KINDS or kind == "mimoriadnost":
        if "start" not in times:
            res.errors.append("Chýba začiatok platnosti.")
            res.blocking.append("chýba začiatok")
        if "end" not in times:
            if data.get("until_revoked"):
                res.warnings.append("Platí do odvolania – koniec nie je určený.")
            elif kind == "mimoriadnost":
                res.warnings.append("Predpokladaný koniec mimoriadnosti nie je známy.")
            else:
                res.errors.append("Chýba koniec platnosti.")
                res.blocking.append("chýba koniec")
        if "start" in times and "end" in times:
            if times["end"] <= times["start"]:
                res.errors.append("Koniec je skôr ako začiatok (alebo rovnaký).")
                res.blocking.append("koniec nie je po začiatku")
            elif times["end"] - times["start"] > timedelta(days=400):
                res.warnings.append("Platnosť je dlhšia ako 400 dní – overte.")
        if "end" in times and times["end"] < now - timedelta(hours=1):
            res.warnings.append("Platnosť už skončila.")
        for f in ("daily_from", "daily_to"):
            v = data.get(f)
            if v and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d|24:00", str(v)):
                res.errors.append(f"Neplatný denný čas {f}: „{v}“ (očakáva sa HH:MM).")
        if bool(data.get("daily_from")) != bool(data.get("daily_to")):
            res.errors.append("Denné okno musí mať začiatok aj koniec.")
        sp = data.get("speed_kmh")
        if sp not in (None, ""):
            spf = parse_number(sp)
            if spf is None:
                res.errors.append(f"Neplatná rýchlosť „{sp}“.")
                res.blocking.append("neplatná rýchlosť")
            else:
                data["speed_kmh"] = int(spf) if spf.is_integer() else spf
        if kind == "pomala_jazda" or data.get("kind") == "pomala_jazda":
            spf = parse_number(data.get("speed_kmh"))
            if spf is None:
                res.errors.append("Pri pomalej jazde chýba rýchlosť (km/h).")
            elif not (5 <= spf <= 160):
                res.errors.append(f"Nepravdepodobná rýchlosť {data.get('speed_kmh')} km/h.")
        if data.get("kind") not in (None, "uplna", "kolaj", "trakcne_vedenie", "pomala_jazda", "obmedzenie"):
            res.errors.append(f"Neznámy druh obmedzenia „{data.get('kind')}“.")

    # --- vlaky a trasy
    if kind in PATH_KINDS or kind in ("trasa_zrusena", "avizo"):
        tn = data.get("train_number")
        digits = train_digits(tn)
        if not tn:
            res.errors.append("Chýba číslo vlaku.")
            res.blocking.append("chýba číslo vlaku")
        elif not re.fullmatch(r"\d{3,6}", digits or ""):
            res.errors.append(f"Neplatné číslo vlaku „{tn}“ (očakáva sa 3 – 6 číslic).")
            res.blocking.append("neplatné číslo vlaku")
        else:
            data["train_number"] = digits
    if kind in PATH_KINDS or kind == "trasa_zrusena":
        d = data.get("date")
        try:
            run_day = date.fromisoformat(str(d)) if d else None
        except ValueError:
            run_day = None
            res.errors.append(f"Neplatný dátum jazdy „{d}“.")
            res.blocking.append("neplatný dátum jazdy")
        if d and run_day is None:
            pass
        elif run_day is None:
            res.errors.append("Chýba dátum jazdy.")
            res.blocking.append("chýba dátum jazdy")
        elif not (now.year - 1 <= run_day.year <= now.year + 2):
            res.errors.append(f"Nepravdepodobný rok v dátume jazdy: {run_day.year}.")
        if kind in PATH_KINDS:
            pts = data.get("points") or []
            if len(pts) < 2:
                res.errors.append("Trasa nemá aspoň dve dopravne.")
            last = None
            for p in pts:
                m = st.match(str(p.get("station") or ""))
                if m is None:
                    res.errors.append(f"Neznáma dopravňa v trase: „{p.get('station')}“.")
                else:
                    p.setdefault("station_text", p.get("station"))
                    p["station"] = m.name
                for key in ("arr", "dep"):
                    t = parse_utc(p.get(key)) if p.get(key) else None
                    if p.get(key) and t is None:
                        res.errors.append(f"Neplatný čas v trase ({p.get('station')}).")
                        continue
                    if t is not None:
                        p[key] = t.isoformat()
                        if last is not None and t < last:
                            res.errors.append(f"Časy v trase nie sú chronologické ({p.get('station')}).")
                        last = t
        if data.get("train_number") and run_day:
            tr = find_train(session, data["train_number"], run_day.isoformat())
            if tr is None:
                res.warnings.append(f"Vlak {data['train_number']} dňa {run_day:%d.%m.%Y} zatiaľ nie je v systéme.")
            else:
                res.train_id = tr.id
    if kind == "avizo":
        if "eta" not in times:
            res.errors.append("Chýba predpokladaný čas príchodu (ETA).")
            res.blocking.append("chýba ETA")
        elif not (now - timedelta(days=2) <= times["eta"] <= now + timedelta(days=14)):
            res.warnings.append("Predpokladaný príchod je ďaleko od dnešného dňa – overte dátum.")
        bc = resolve_border(session, data)
        if bc is None:
            res.errors.append(f"Hraničný priechod „{data.get('border') or '?'}“ sa v registri nenašiel.")
        else:
            data["border_code"] = bc.code
            res.border = {"code": bc.code, "name": bc.name, "handover_station": bc.handover_station,
                          "sk_station": bc.sk_station}
        if data.get("train_number") and train_digits(data["train_number"]):
            day = data.get("date") or (local(times["eta"]).date().isoformat() if "eta" in times else None)
            tr = find_train(session, data["train_number"], day, avizo=True)
            if tr is None:
                res.errors.append(f"Vlak {data['train_number']} nenájdený – avízo nemožno priradiť.")
            else:
                res.train_id = tr.id
                if _run_day(tr) is None:
                    # jediný aktívny vlak s týmto číslom je bez dátumu jazdy – môže to byť aj staršia, neukončená
                    # jazda; dispečer to má vidieť skôr, ako avízo potvrdí
                    res.warnings.append(f"Avízo sa priradí vlaku {tr.number} (#{tr.id}), ktorý nemá dátum jazdy – "
                                        f"overte, že ide o jazdu z avíza, a doplňte vlaku dátum jazdy.")

    # --- objednávka
    if kind == "objednavka":
        if not data.get("customer"):
            res.errors.append("Chýba zákazník.")
        if "deadline" in times and "earliest_departure" in times and times["deadline"] <= times["earliest_departure"]:
            res.errors.append("Termín dodania je skôr ako najskorší odchod.")
        checks = (("wagons", 1, 120, "počet vozňov"), ("weight_t", 1, 6000, "hmotnosť"),
                  ("length_m", 10, 1500, "dĺžka"))
        for f, lo, hi, label in checks:
            v = data.get(f)
            if v in (None, ""):
                continue
            num = parse_number(v)
            if num is None:
                res.errors.append(f"Neplatná hodnota – {label}: „{v}“.")
                data[f] = None
                continue
            data[f] = int(num) if (f == "wagons" or num.is_integer()) else num
            if not (lo <= num <= hi):
                res.warnings.append(f"Nepravdepodobná hodnota – {label}: {v}.")
        if isinstance(data.get("rid"), str):
            from .text import fold

            data["rid"] = fold(data["rid"]).strip() in ("ano", "a", "true", "1", "yes")
        if data.get("rid") is None:
            res.warnings.append("Nie je uvedené, či ide o nebezpečný tovar (RID).")

    # --- pôvodný text dopravní (z prvej extrakcie sa nemení ani po úpravách)
    src = dict(prev_src)
    for f, info in res.stations.items():
        if f not in src and info.get("text"):
            src[f] = info["text"]
    res.source_text = src
    res.data = data
    res.needs_confirmation, res.reasons = confirmation_needed(kind, data, confidence, verdict, res, now)
    return res


def confirmation_needed(kind: str, data: dict, confidence: float, verdict: str, res: ValidationResult,
                        now: datetime) -> tuple[bool, list[str]]:
    s = settings()
    reasons: list[str] = []
    if confidence < s.confirm_threshold:
        reasons.append(f"istota {confidence:.2f} je pod prahom {s.confirm_threshold:.2f}")
    if res.errors:
        reasons.append("validácia našla chyby")
    if any("približne" in w for w in res.warnings):
        reasons.append("dopravňa určená len približne")
    if kind == "avizo" and any("nemá dátum jazdy" in w for w in res.warnings):
        # vlak bez dátumu jazdy môže byť aj staršia, neukončená jazda s rovnakým číslom – avízo sa mu nepriradí
        # automaticky (rovnako ako pred zavedením tohto priradenia, keď sa avízo takému vlaku nepriradilo vôbec)
        reasons.append("avízo sa priradí vlaku bez dátumu jazdy – overte, že ide o túto jazdu")
    if res.section_km is not None and res.section_km > MAX_SECTION_KM:
        # výluka či pomalá jazda na stovkách km je skôr zle určená dopravňa (napr. iná obec s podobným menom)
        reasons.append(f"úsek je neobvykle dlhý ({res.section_km:.0f} km) – skontrolujte dopravne")
    if verdict == "podozrivy":
        reasons.append("podozrivý e-mail (možný pokus o prompt injection) – nikdy sa neaplikuje automaticky")
    elif verdict != "ok":
        reasons.append(f"e-mail má verdikt {verdict}")
    if kind in ALWAYS_CONFIRM:
        reasons.append(f"{KIND_LABELS.get(kind, kind)} vždy potvrdzuje dispečer")
    if kind == "ine":
        reasons.append("nerozpoznaný e-mail – skontrolujte originál")
    start = effective_start(kind, data)
    if kind != "ine":
        if start is None:
            reasons.append("nie je známy čas, odkedy udalosť platí")
        elif start <= now + timedelta(hours=s.confirm_horizon_h):
            reasons.append(f"týka sa najbližších {s.confirm_horizon_h} h (alebo už platí)")
    return bool(reasons), reasons


def _label(fname: str) -> str:
    return {"from_station": "od dopravne", "to_station": "do dopravne", "origin": "odkiaľ", "destination": "kam",
            "start": "začiatok", "end": "koniec", "eta": "ETA", "earliest_departure": "najskorší odchod",
            "deadline": "termín dodania", "accept_by": "lehota na akceptáciu"}.get(fname, fname)
