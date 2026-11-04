"""5. revízia – konsolidovaný korpus formulácií e-mailov ŽSR (kolá 3 – 5).

Korpus je v ``tests/data/posta_korpus``: e-maily (.eml) a krátke formulácie priamo v ``oracle.json`` spolu
s očakávanými udalosťami (druh, úsek, obdobie v miestnom čase, druh obmedzenia, číslo). Každý prípad prejde skutočnou
extrakciou ako v spracovaní pošty (``vlaky.posta.extrakcia.extract`` bez LLM – pravidlá; e-mail ``.eml`` cez
``parse_message``) a porovná sa s orákulom dvakrát:

* ``test_korpus_bezpecne`` – nikdy nie nebezpečný výsledok: stratená výluka či termín (bez poznámky „overte“),
  miernejší druh obmedzenia, zrušenie, ktoré e-mail neohlasuje, mimoriadnosť s koncom skorším než oznámený;
* ``test_korpus_presne`` – presná zhoda s orákulom. Prípady, ktoré presné ešte nie sú, ale ich výsledok je
  konzervatívny (potvrdenie s poznámkou, prísnejší druh, chýbajúce miesto s poistkou pre celý vozový park), majú
  v orákule ``xfail`` s dôvodom – ``pytest.mark.xfail(strict=True)``: oprava ich prepne a test na to upozorní.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

KORPUS = Path(__file__).resolve().parent / "data" / "posta_korpus"
TZ = ZoneInfo("Europe/Bratislava")
UTC = timezone.utc
CLOSURE = ("vyluka_nova", "vyluka_zmena", "pomala_jazda", "mimoriadnost")
SEV = {"uplna": 4, "mimoriadnost": 4, "trakcne_vedenie": 3, "kolaj": 2, "pomala_jazda": 1, "obmedzenie": 0}
DEFAULT_SENDER = "vyluky@or.zsr.example"


def _load() -> list[dict]:
    return json.loads((KORPUS / "oracle.json").read_text(encoding="utf-8"))["cases"]


CASES = _load()


@pytest.fixture(scope="module")
def korpus():
    """Nastavenia ako v testoch (povolení odosielatelia *.example, bez LLM) – bez databázy; výsledky extrakcie sa
    počítajú raz pre modul."""
    from vlaky import config as config_mod

    root = Path(__file__).resolve().parent.parent
    config_mod.override_settings(env="test", database_url="sqlite://", data_dir=root / "data", llm_provider="none",
                                 allowed_senders="zsr.example,partner.example,zakaznik.example,dopravca.example")
    cache: dict = {}
    yield cache
    config_mod._OVERRIDE.clear()


# ---------------------------------------------------------------------------------------------- spustenie extrakcie
def _local(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).astimezone(UTC)


def _doc(case: dict):
    from vlaky.posta.parser import parse_message
    from vlaky.posta.spracovanie import _auto_reason
    from vlaky.posta.udalosti import MailDocument

    received = _local(case.get("received")) or datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
    if case.get("eml"):
        path = KORPUS / case["eml"]
        pm = parse_message(path.read_bytes(), sandbox=False)
        atts = [SimpleNamespace(filename=a.filename, content_type=a.content_type, text=a.text) for a in pm.attachments]
        auto = _auto_reason(SimpleNamespace(raw_path=str(path), attachments=atts))
        return MailDocument(subject=pm.subject, body=pm.body_text, attachments=[(a.filename, a.text) for a in atts
                                                                                if a.text],
                            sender=pm.sender or case.get("sender", ""), received_at=received, auto_reason=auto)
    return MailDocument(subject=case.get("subject", ""), body=case.get("body", ""),
                        attachments=[tuple(a) for a in case.get("attachments", [])],
                        sender=case.get("sender", DEFAULT_SENDER), received_at=received, auto_reason=case.get("auto", ""))


def _run(korpus: dict, case: dict) -> list:
    if case["id"] not in korpus:
        from vlaky.posta.extrakcia import extract

        korpus[case["id"]] = extract(_doc(case), use_llm=False).items
    return korpus[case["id"]]


# ---------------------------------------------------------------------------------------------- porovnanie
def _station(name):
    from vlaky.posta.stanice import stations

    if not name:
        return None
    m = stations().match(str(name))
    return m.name if m else str(name)


def _place(it) -> frozenset:
    d = it.data
    a, b = _station(d.get("from_station")), _station(d.get("to_station"))
    return frozenset(x for x in (a, b or a) if x)


def _utc(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _fmt(dt):
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if dt else None


def _expand(start, end, dfrom, dto) -> list:
    if start is None or end is None or not (dfrom and dto):
        return [(start, end)]
    h1, m1 = (int(x) for x in dfrom.split(":"))
    h2, m2 = (int(x) for x in dto.split(":"))
    out, day = [], start.astimezone(TZ).date() - timedelta(days=1)
    while day <= end.astimezone(TZ).date() + timedelta(days=1):
        ws = datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(hours=h1, minutes=m1)
        we = datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(hours=h2, minutes=m2)
        if we <= ws:
            we += timedelta(days=1)
        ws, we = max(ws.astimezone(UTC), start), min(we.astimezone(UTC), end)
        if we > ws:
            out.append((ws, we))
        day += timedelta(days=1)
    return out


def _merge(ivs: list) -> list:
    out: list = []
    for s, e in sorted(ivs, key=lambda x: x[0] or datetime.min.replace(tzinfo=UTC)):
        if out and s is not None and (out[-1][1] is None or s <= out[-1][1]):
            if out[-1][1] is not None and (e is None or e > out[-1][1]):
                out[-1] = (out[-1][0], e)
            continue
        out.append((s, e))
    return out


def _intervals(it) -> list:
    d = it.data
    end = None if d.get("until_revoked") and not d.get("end") else _utc(d.get("end"))
    return _expand(_utc(d.get("start")), end, d.get("daily_from"), d.get("daily_to"))


def _covers(actual: list, expected: list) -> bool:
    for s, e in expected:
        if not any((a is None or (s is not None and a <= s)) and (b is None or (e is not None and b >= e))
                   for a, b in actual):
            return False
    return True


def _same_ivs(actual: list, expected: list) -> bool:
    if len(actual) != len(expected):
        return False
    for (a, b), (s, e) in zip(actual, expected):
        if (a is None) != (s is None) or (b is None) != (e is None):
            return False
        if a and abs((a - s).total_seconds()) > 60 or b and abs((b - e).total_seconds()) > 60:
            return False
    return True


def _ref(v) -> str:
    s = re.sub(r"[^0-9a-z/]", "", str(v or "").lower())
    m = re.search(r"(\d+)/(\d{2,4})", s)
    return f"{int(m.group(1))}/{('20' + m.group(2)) if len(m.group(2)) == 2 else m.group(2)}" if m else s


def _noted(items, start) -> bool:
    """Niektorá udalosť má poznámku „overte“ s dňom termínu – stratený termín je oznámený (konzervatívne)."""
    if start is None:
        return False
    loc = start.astimezone(TZ)
    marks = (f"{loc.day:02d}.{loc.month:02d}.", f"{loc.day}.{loc.month}.", f"{loc.day}.{loc.month:02d}.")
    return any("overte" in n.lower() and any(m in n for m in marks) for it in items for n in it.notes)


def _judge(case: dict, items: list) -> tuple[list, list]:
    """(nepresnosti, nebezpečné výsledky) prípadu."""
    inexact: list[str] = []
    unsafe: list[str] = []
    used: set = set()
    expect = [x for x in case["expect"] if x["kind"] != "ine"] or case["expect"]
    kinds = {x["kind"] for x in expect}
    if kinds <= {"none", "ine", "none_or_same", "quarantine"}:
        bad = [it for it in items if it.kind in CLOSURE or it.kind == "vyluka_zrusenie"]
        if bad and "none_or_same" not in kinds:
            inexact.append("e-mail nič neohlasuje, ale udalosti: " + ", ".join(it.kind for it in bad))
        if any(it.kind == "vyluka_zrusenie" for it in items):
            unsafe.append("zrušenie výluky z e-mailu, ktorý nič neruší")
        return inexact, unsafe
    for x in expect:
        k = x["kind"]
        if k in CLOSURE:
            _judge_closure(x, items, used, inexact, unsafe)
        elif k == "vyluka_zrusenie":
            cands = [it for it in items if it.kind == k and id(it) not in used and (
                (x.get("ref") and _ref(it.data.get("external_ref")) == _ref(x["ref"]))
                or (x.get("frm") and _place(it) == frozenset((x["frm"], x.get("to") or x["frm"]))))]
            if not cands:
                inexact.append(f"chýba zrušenie {x.get('ref') or ''} {x.get('frm') or ''}")
                continue
            used.add(id(cands[0]))
            if x.get("iv"):
                act = _merge([iv for it in cands[:1] for iv in _intervals(it)])
                exp = _merge([(_local(s), _local(e)) for s, e in x["iv"]])
                if not _same_ivs(act, exp):
                    inexact.append(f"rušený termín {[(_fmt(a), _fmt(b)) for a, b in act]} != {x['iv']}")
        elif k == "avizo":
            cands = [it for it in items if it.kind == "avizo" and str(it.data.get("train_number") or "") == x.get("train")]
            if not cands:
                inexact.append(f"chýba avízo vlaku {x.get('train')}")
            elif x.get("eta") and _fmt(_utc(cands[0].data.get("eta"))) != x["eta"]:
                inexact.append(f"ETA {_fmt(_utc(cands[0].data.get('eta')))} != {x['eta']}")
        elif k.startswith("trasa_"):
            cands = [it for it in items if it.kind == k and str(it.data.get("train_number") or "") == x.get("train")]
            if not cands:
                inexact.append(f"chýba {k} vlaku {x.get('train')}")
                continue
            d = cands[0].data
            if x.get("date") and d.get("date") != x["date"]:
                inexact.append(f"dátum jazdy {d.get('date')} != {x['date']}")
            pts = d.get("points") or []
            if x.get("dep") and (not pts or _fmt(_utc(pts[0].get("dep"))) != x["dep"]):
                inexact.append(f"odchod {pts[0].get('dep') if pts else None} != {x['dep']}")
            if x.get("arr") and (not pts or _fmt(_utc(pts[-1].get("arr"))) != x["arr"]):
                inexact.append(f"príchod {pts[-1].get('arr') if pts else None} != {x['arr']}")
        elif k == "objednavka":
            cands = [it for it in items if it.kind == "objednavka"]
            if not cands:
                inexact.append("chýba objednávka")
                continue
            d = cands[0].data
            for key, field in (("origin", "origin"), ("destination", "destination")):
                if x.get(key) and _station(d.get(field)) != x[key]:
                    inexact.append(f"{key} {d.get(field)} != {x[key]}")
            for key, field in (("earliest", "earliest_departure"), ("deadline", "deadline")):
                if x.get(key) and _fmt(_utc(d.get(field))) != x[key]:
                    inexact.append(f"{key} {_fmt(_utc(d.get(field)))} != {x[key]}")
    extra = [it for it in items if id(it) not in used and (it.kind in CLOSURE or it.kind == "vyluka_zrusenie")]
    for it in extra:
        if it.kind == "vyluka_zrusenie" and "vyluka_zrusenie" not in kinds:
            unsafe.append(f"zrušenie, ktoré e-mail neohlasuje: {it.data}")
        elif not case.get("extra_ok"):
            inexact.append(f"ďalšia udalosť {it.kind} {it.data.get('from_station')} – {it.data.get('to_station')}")
    return inexact, unsafe


def _judge_closure(x: dict, items: list, used: set, inexact: list, unsafe: list) -> None:
    secs = [frozenset((x["frm"], x.get("to") or x["frm"]))] if x.get("frm") else []
    secs += [frozenset(p) for p in x.get("alt", [])]
    daily = x.get("daily") or (None, None)
    exp = _merge([iv for s, e in x.get("iv", []) for iv in _expand(_local(s), _local(e) if e else None, *daily)])
    allowed = {x["kind"], *x.get("kinds_ok", ())} | ({"mimoriadnost"} if x["kind"] in ("vyluka_nova", "vyluka_zmena")
                                                      else set())
    if x.get("noplace"):
        cands = [it for it in items if it.kind in CLOSURE and id(it) not in used and not _place(it)]
    else:
        cands = [it for it in items if it.kind in CLOSURE and id(it) not in used and _place(it) in secs]
    # viac očakávaných udalostí na tom istom mieste (pomalá jazda a výluka, dva termíny s iným druhom): udalosti sa
    # priradia podľa druhu a podľa prekrytia s termínom; bez zhody ostávajú všetky (aby sa ukázal miernejší druh)
    narrowed = [it for it in cands if it.kind in allowed] or cands
    if exp and len(narrowed) > 1:
        overlap = [it for it in narrowed if any(
            (a is None or e is None or a < e) and (b is None or s is None or s < b)
            for a, b in _intervals(it) for s, e in exp)]
        narrowed = overlap or narrowed
    cands = narrowed
    label = f"{x['kind']} {x.get('frm')} – {x.get('to')}"
    if not cands:
        # bez miesta: poistka pre celý vozový park (konzervatívne), ak pokrýva termín; inak stratená výluka
        fleet = [it for it in items if it.kind in CLOSURE and not _place(it)]
        if fleet and (not exp or _covers(_merge([iv for it in fleet for iv in _intervals(it)]), exp)):
            inexact.append(f"{label}: udalosť bez miesta (poistka pre celý vozový park)")
        elif x["kind"] == "pomala_jazda" and not x.get("must"):
            inexact.append(f"{label}: pomalá jazda chýba")
        else:
            unsafe.append(f"{label}: udalosť chýba")
        return
    for it in cands:
        used.add(id(it))
    kinds = {it.kind for it in cands}
    if not kinds <= allowed:
        if "pomala_jazda" in kinds and x["kind"] != "pomala_jazda":
            unsafe.append(f"{label}: výluka zapísaná ako pomalá jazda")
        else:
            inexact.append(f"{label}: druh udalosti {sorted(kinds)}")
    want = set(x.get("rks") or ([x["rk"]] if x.get("rk") else []))
    if want and x["kind"] != "mimoriadnost" and not x.get("rk_any"):
        rks = {it.data.get("kind") if it.kind != "mimoriadnost" else "mimoriadnost" for it in cands}
        if rks != want:
            if min(SEV.get(r, 4) for r in rks) < min(SEV[r] for r in want):
                unsafe.append(f"{label}: miernejší druh {sorted(map(str, rks))} než {sorted(want)}")
            else:
                inexact.append(f"{label}: prísnejší druh {sorted(map(str, rks))} než {sorted(want)}")
    if x.get("ref") and {_ref(it.data.get("external_ref")) for it in cands} != {_ref(x["ref"])}:
        inexact.append(f"{label}: číslo {[it.data.get('external_ref') for it in cands]} != {x['ref']}")
    if x.get("speed") and {it.data.get("speed_kmh") for it in cands} != {x["speed"]}:
        inexact.append(f"{label}: rýchlosť {[it.data.get('speed_kmh') for it in cands]} != {x['speed']}")
    if not exp:
        return
    act = _merge([iv for it in cands for iv in _intervals(it)])
    if x.get("open"):
        exp = [(s, None) for s, _e in exp]
    if not _covers(act, exp):
        lost = [(s, e) for s, e in exp if not _covers(act, [(s, e)])]
        if all(_noted(items, s) for s, _e in lost):
            inexact.append(f"{label}: termín {[(_fmt(s), _fmt(e)) for s, e in lost]} len v poznámke „overte“")
        elif x.get("start_any") and all(e is None or any(b is None or b >= e for _a, b in act) for _s, e in lost):
            inexact.append(f"{label}: iný začiatok {[(_fmt(a), _fmt(b)) for a, b in act]}")
        else:
            unsafe.append(f"{label}: termín {[(_fmt(s), _fmt(e)) for s, e in lost]} nepokrytý "
                          f"(udalosti {[(_fmt(a), _fmt(b)) for a, b in act]})")
    elif not _same_ivs(act, exp) and not x.get("open"):
        inexact.append(f"{label}: obdobie {[(_fmt(a), _fmt(b)) for a, b in act]} != {x.get('iv')}")


# ---------------------------------------------------------------------------------------------- testy
def _ids(c: dict) -> str:
    return c["id"]


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_korpus_bezpecne(korpus, case):
    """Žiadny prípad korpusu nesmie dať nebezpečný výsledok (stratená výluka, miernejší druh, zrušenie navyše)."""
    items = _run(korpus, case)
    _inexact, unsafe = _judge(case, items)
    assert not unsafe, f"{case['id']} ({case.get('origin', '')}): " + "; ".join(unsafe) + " | udalosti: " + \
        "; ".join(f"{it.kind} {it.data}" for it in items)


@pytest.mark.parametrize("case", [pytest.param(c, marks=pytest.mark.xfail(strict=True, reason=c["xfail"]))
                                  if c.get("xfail") else c for c in CASES], ids=_ids)
def test_korpus_presne(korpus, case):
    """Výsledok extrakcie sa zhoduje s orákulom (známe konzervatívne odchýlky sú xfail s dôvodom)."""
    items = _run(korpus, case)
    inexact, unsafe = _judge(case, items)
    assert not inexact and not unsafe, f"{case['id']} ({case.get('origin', '')}): " + "; ".join(inexact + unsafe)


def test_korpus_rozsah():
    """Korpus zahŕňa e-maily aj formulácie kôl 3 – 5 (reviewerské sondy, korpusy, repro skripty)."""
    origins = {c.get("origin", "").split(":")[0] for c in CASES}
    assert len(CASES) >= 400
    assert {"r4-korpus", "r5-korpus", "r4-sondy", "r5-formulacie"} <= origins
