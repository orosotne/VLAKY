"""Validácia udalostí: normalizácia dopravní, časy, vlaky, hranice a pravidlo „na potvrdenie“."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vlaky.hranice import seed_borders
from vlaky.models import Train
from vlaky.posta.validacia import effective_start, find_train, parse_utc, validate_event

FAR = "2026-11-20T07:00:00+00:00"          # ďalej ako 72 h od fixed_now (2. 11. 2026 04:00 UTC)
FAR_END = "2026-11-20T13:00:00+00:00"


def closure(**kw) -> dict:
    d = {"from_station": "Poprad Tatry", "to_station": "Liptovsky Mikulas", "start": FAR, "end": FAR_END,
         "kind": "kolaj"}
    d.update(kw)
    return d


def test_station_names_are_normalized_and_original_text_kept(session, fixed_now):
    vr = validate_event(session, "vyluka_nova", closure(), confidence=0.95)
    assert vr.data["from_station"] == "Poprad-Tatry" and vr.data["to_station"] == "Liptovský Mikuláš"
    assert vr.stations["from_station"] == {"text": "Poprad Tatry", "name": "Poprad-Tatry", "ref": "PoTa",
                                           "method": "presne"}
    assert vr.source_text == {"from_station": "Poprad Tatry", "to_station": "Liptovsky Mikulas"}
    assert vr.section_km and 10 < vr.section_km < 60
    assert not vr.errors and not vr.needs_confirmation, vr.reasons


def test_original_text_survives_revalidation(session, fixed_now):
    first = validate_event(session, "vyluka_nova", closure(), confidence=0.95)
    again = validate_event(session, "vyluka_nova", first.data, confidence=0.95, previous=first.as_dict())
    assert again.source_text["from_station"] == "Poprad Tatry"


def test_declined_and_abbreviated_names(session, fixed_now):
    vr = validate_event(session, "vyluka_nova", closure(from_station="Bratislava hl. st.",
                                                        to_station="Bratislavy-Petržalky"), confidence=0.95)
    assert vr.data["from_station"] == "Bratislava hlavná stanica"
    assert vr.data["to_station"] == "Bratislava-Petržalka"
    assert vr.stations["to_station"]["method"] == "sklonovanie"


def test_unknown_station_is_an_error_and_needs_confirmation(session, fixed_now):
    vr = validate_event(session, "vyluka_nova", closure(to_station="Horná Dolná Lhota"), confidence=0.95)
    assert any("Neznáma dopravňa „Horná Dolná Lhota“" in e for e in vr.errors)
    assert vr.needs_confirmation and "validácia našla chyby" in vr.reasons
    assert vr.data["to_station"] == "Horná Dolná Lhota"          # neznámy názov sa nemení


def test_approximate_station_is_flagged(session, fixed_now):
    vr = validate_event(session, "vyluka_nova", closure(from_station="Zvolen", to_station="Hronská Dúbrava"),
                        confidence=0.95)
    assert vr.data["from_station"] == "Zvolen osobná stanica"
    assert any("približne" in w for w in vr.warnings) and vr.needs_confirmation


def test_time_logic(session, fixed_now):
    vr = validate_event(session, "vyluka_nova", closure(end=FAR), confidence=0.95)
    assert "koniec nie je po začiatku" in vr.blocking
    vr = validate_event(session, "vyluka_nova", closure(start="2031-11-20T07:00:00+00:00",
                                                        end="2031-11-20T08:00:00+00:00"), confidence=0.95)
    assert any("Nepravdepodobný rok" in e for e in vr.errors)
    vr = validate_event(session, "vyluka_nova", closure(start="zajtra"), confidence=0.95)
    assert any("Neplatný čas" in e for e in vr.errors) and vr.blocking
    vr = validate_event(session, "vyluka_nova", closure(end=None), confidence=0.95)
    assert "chýba koniec" in vr.blocking
    vr = validate_event(session, "pomala_jazda", closure(end=None, until_revoked=True, kind="pomala_jazda",
                                                         speed_kmh=30), confidence=0.95)
    assert not vr.blocking and any("do odvolania" in w for w in vr.warnings)
    vr = validate_event(session, "vyluka_nova", closure(daily_from="25:00", daily_to="14:00"), confidence=0.95)
    assert any("denný čas" in e for e in vr.errors)
    # slovenský zápis z formulára dispečera sa prijme ako miestny čas
    vr = validate_event(session, "vyluka_nova", closure(start="20.11.2026 08:00", end="20.11.2026 14:00"),
                        confidence=0.95)
    assert vr.data["start"] == FAR and not vr.errors


def test_dst_day_warning(session, fixed_now):
    vr = validate_event(session, "vyluka_nova", closure(start="2027-03-28T00:30:00+00:00",
                                                        end="2027-03-28T05:00:00+00:00"), confidence=0.95)
    assert any("letného/zimného" in w for w in vr.warnings)


def test_slow_running_requires_speed(session, fixed_now):
    vr = validate_event(session, "pomala_jazda", closure(kind="pomala_jazda"), confidence=0.95)
    assert any("rýchlosť" in e for e in vr.errors)
    vr = validate_event(session, "pomala_jazda", closure(kind="pomala_jazda", speed_kmh="50"), confidence=0.95)
    assert vr.data["speed_kmh"] == 50 and not vr.errors


def test_train_number_and_path(session, fixed_now):
    pts = [{"station": "Cadca", "arr": None, "dep": "2026-11-14T05:10:00+00:00"},
           {"station": "Zilina", "arr": "2026-11-14T05:52:00+00:00", "dep": "2026-11-14T06:05:00+00:00"}]
    vr = validate_event(session, "trasa_pridelena", {"train_number": "Nex 54321", "date": "2026-11-14",
                                                     "points": pts}, confidence=0.95)
    assert vr.data["train_number"] == "54321"
    assert [p["station"] for p in vr.data["points"]] == ["Čadca", "Žilina"]
    assert vr.data["points"][0]["station_text"] == "Cadca"
    assert any("zatiaľ nie je v systéme" in w for w in vr.warnings) and not vr.errors
    vr = validate_event(session, "trasa_pridelena", {"train_number": "54A", "date": "2026-11-14", "points": pts},
                        confidence=0.95)
    assert "neplatné číslo vlaku" in vr.blocking
    bad = [pts[1], pts[0]]
    vr = validate_event(session, "trasa_pridelena", {"train_number": "54321", "date": "2026-11-14", "points": bad},
                        confidence=0.95)
    assert any("chronologické" in e for e in vr.errors)
    vr = validate_event(session, "trasa_zrusena", {"train_number": "12345"}, confidence=0.95)
    assert "chýba dátum jazdy" in vr.blocking


def test_avizo_border_and_train(session, fixed_now):
    seed_borders(session)
    data = {"train_number": "47250", "border": "Čadce", "eta": "2026-11-03T12:35:00+00:00"}
    vr = validate_event(session, "avizo", dict(data), confidence=0.95)
    assert vr.data["border_code"] == "CADCA" and vr.border["name"].startswith("Čadca")
    assert any("Vlak 47250 nenájdený" in e for e in vr.errors) and vr.needs_confirmation
    session.add(Train(number="Nex 47250", run_date="2026-11-03", origin="Čadca", destination="Košice"))
    session.flush()
    vr = validate_event(session, "avizo", dict(data), confidence=0.95)
    assert not vr.errors and vr.train_id
    assert vr.needs_confirmation and any("72 h" in r for r in vr.reasons)       # ETA je o 32 h
    vr = validate_event(session, "avizo", {**data, "border": "Rajka", "border_code": None}, confidence=0.95)
    assert vr.data["border_code"] == "RUSOVCE"
    vr = validate_event(session, "avizo", {**data, "border": "Neexistujúca hranica"}, confidence=0.95)
    assert any("Hraničný priechod" in e for e in vr.errors)


def test_borders_are_seeded_automatically(session, fixed_now):
    vr = validate_event(session, "avizo", {"train_number": "47250", "border": "Štúrovo",
                                           "eta": "2026-11-03T12:35:00+00:00"}, confidence=0.95)
    assert vr.data["border_code"] == "STUROVO"


def test_order_validation(session, fixed_now):
    vr = validate_event(session, "objednavka", {"customer": "FIKTÍVNA s.r.o.", "origin": "Kuty", "destination":
                                                "Zilina-Teplicka", "earliest_departure": "2026-11-20T05:00:00+00:00",
                                                "deadline": "2026-11-21T13:00:00+00:00", "wagons": "22",
                                                "weight_t": "1 480", "rid": None}, confidence=0.95)
    assert vr.data["origin"] == "Kúty" and vr.data["destination"] == "Žilina-Teplička"
    assert vr.data["wagons"] == 22 and vr.data["weight_t"] == 1480
    assert any("RID" in w for w in vr.warnings) and not vr.errors
    vr = validate_event(session, "objednavka", {"customer": "X", "origin": "Ostrava hlavní nádraží",
                                                "destination": "Košice"}, confidence=0.95)
    assert any("Neznáma dopravňa" in e for e in vr.errors)


@pytest.mark.parametrize("kind", ["vyluka_zrusenie", "trasa_zrusena", "mimoriadnost"])
def test_always_confirm_kinds(session, fixed_now, kind):
    data = {"vyluka_zrusenie": {"external_ref": "1150/2026", "start": FAR},
            "trasa_zrusena": {"train_number": "12345", "date": "2026-11-25"},
            "mimoriadnost": {"from_station": "Žilina", "to_station": "Vrútky", "start": FAR,
                             "description": "porucha"}}[kind]
    vr = validate_event(session, kind, data, confidence=0.99)
    assert vr.needs_confirmation and any("vždy potvrdzuje" in r for r in vr.reasons)


def test_confirmation_rule_threshold_horizon_and_verdict(session, fixed_now, cfg):
    assert not validate_event(session, "vyluka_nova", closure(), confidence=0.95).needs_confirmation
    low = validate_event(session, "vyluka_nova", closure(), confidence=0.85)
    assert low.needs_confirmation and "pod prahom" in low.reasons[0]
    soon = (fixed_now + timedelta(hours=30)).isoformat()
    near = validate_event(session, "vyluka_nova", closure(start=soon, end=FAR_END), confidence=0.95)
    assert near.needs_confirmation and any("72 h" in r for r in near.reasons)
    started = validate_event(session, "vyluka_nova", closure(start="2026-11-01T07:00:00+00:00", end=FAR_END),
                             confidence=0.95)
    assert started.needs_confirmation                         # už platí
    sus = validate_event(session, "vyluka_nova", closure(), confidence=0.99, verdict="podozrivy")
    assert sus.needs_confirmation and any("podozrivý" in r for r in sus.reasons)
    ine = validate_event(session, "ine", {}, confidence=0.99)
    assert ine.needs_confirmation


def test_parse_utc_and_effective_start():
    assert parse_utc("2026-11-12T08:00") == datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc)
    assert parse_utc("2026-11-12T07:00:00Z") == datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc)
    assert parse_utc("nič") is None and parse_utc(None) is None
    assert effective_start("trasa_zrusena", {"date": "2026-11-15"}) == datetime(2026, 11, 14, 23, 0,
                                                                                tzinfo=timezone.utc)


def test_find_train_by_digits_and_day(session):
    session.add_all([Train(number="54321", run_date="2026-11-14", origin="Čadca", destination="Košice"),
                     Train(number="Nex 54321", run_date="2026-11-21", origin="Čadca", destination="Košice")])
    session.flush()
    assert find_train(session, "Nex 54321", "2026-11-14").run_date == "2026-11-14"
    assert find_train(session, "54321", "2026-11-22").run_date == "2026-11-21"     # ± 1 deň
    assert find_train(session, "54321", "2026-11-17") is None
    assert find_train(session, "54321") is None                                     # nejednoznačné
    assert find_train(session, "") is None
