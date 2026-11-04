"""Regresné testy k 4. revízii – poradie súbežných prepočtov toho istého vlaku (QU2, oblasť P4 „poistky“).

Dispečer založí vlak (/vlaky/novy – prepočet „nový vlak“ na pozadí z údajov BEZ avíza) a hneď potvrdí avízo
(prepočet všetkých vlakov S avízom). Starší prepočet sa uloží až po novšom – nesmie sa stať aktuálnym odporúčaním
(ani prepísať čísla novšieho, ani ho označiť za zastarané). Poradie určuje skutočný začiatok prepočtu
(``data_age["eval_started"]``) a ukladá sa pod zámkom vlaku (PostgreSQL – poradný zámok, SQLite – zámok zápisu).

Testy bežia na SQLite (súbor) aj na PostgreSQL (VLAKY_TEST_DATABASE_URL). Všetky vlaky sú fiktívne.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, select

from vlaky.db import session_scope
from vlaky.hranice import seed_borders
from vlaky.models import Driver, DriverShift, Path, Recommendation, Train
from vlaky.planovanie import planovac


class _Clock:
    """Skutočný čas začiatku prepočtu pod kontrolou testu: každé volanie o 1 s neskôr (aj z iného vlákna)."""

    def __init__(self, monkeypatch):
        self.t = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
        self.lock = threading.Lock()
        monkeypatch.setattr(planovac, "_wall_clock", self, raising=False)

    def __call__(self) -> datetime:
        with self.lock:
            self.t += timedelta(seconds=1)
            return self.t


def _train(session, now, number) -> int:
    """Vlak Žilina → Košice s pridelenou trasou o 40 min, pripravený o 30 min, rušňovodič naplánovaný."""
    seed_borders(session)
    ready = now + timedelta(minutes=30)
    t = Train(number=number, run_date=ready.date().isoformat(), origin="Žilina", destination="Košice",
              traction="E", gauge="1435", vmax_kmh=100, status="planovany", ready_utc=ready,
              deadline_utc=ready + timedelta(hours=10), length_m=500, weight_t=1500)
    session.add(t)
    session.flush()
    session.add(Path(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena",
                     departure_utc=now + timedelta(minutes=40)))
    d = Driver(name=f"R. {number} FIKTÍVNY")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=ready - timedelta(minutes=80),
                            status="planovana"))
    session.flush()
    return t.id


def _set_ready(tid: int, ready: datetime) -> None:
    with session_scope() as s:
        s.get(Train, tid).ready_utc = ready


def _recs(tid: int) -> list:
    with session_scope() as s:
        return [(r.id, r.state, r.decision, r.trigger, dict(r.data_age or {}), r.summary)
                for r in s.scalars(select(Recommendation).where(Recommendation.train_id == tid)
                                   .order_by(Recommendation.id))]


def _hook_once(monkeypatch, action):
    """Pri prvom volaní closure_windows (prepočet A už prečítal vlak a jeho pripravenosť) spustí ``action`` – novší
    prepočet B v inej relácii, ktorý svoj výsledok uloží a potvrdí (commit)."""
    real = planovac.closure_windows
    done = []

    def hooked(session, start, end):
        if not done:
            done.append(True)
            action()
        return real(session, start, end)

    monkeypatch.setattr(planovac, "closure_windows", hooked)


def test_qu2_starsi_prepocet_sa_po_novsom_neulozi(cfg, fixed_now, monkeypatch):
    """[QU2] Prepočet A („nový vlak“) začne, prečíta vlak (pripravený o 30 min) a kým počíta, avízo zmení
    pripravenosť na +8 h a novší prepočet B svoj výsledok uloží. A je starší: neuloží nič, B neoznačí za zastarané a
    vráti B; aktuálne ostáva odporúčanie z novších údajov."""
    with session_scope() as s:
        tid = _train(s, fixed_now, "47250")
    clock = _Clock(monkeypatch)
    out = {}

    def newer():
        _set_ready(tid, fixed_now + timedelta(hours=8))         # potvrdené avízo: vlak príde neskôr
        with session_scope() as sb:
            rec = planovac.evaluate_train(sb, tid, fixed_now, "potvrdená udalosť z e-mailu")
            out["b"] = (rec.id, rec.data_age.get("eval_started"))

    _hook_once(monkeypatch, newer)
    with session_scope() as sa:
        rec_a = planovac.evaluate_train(sa, tid, fixed_now, "nový vlak")
        a_id = rec_a.id
    rows = _recs(tid)
    assert [(r[0], r[1], r[3]) for r in rows] == [(out["b"][0], "nove", "potvrdená udalosť z e-mailu")], rows
    assert a_id == out["b"][0]
    assert rows[0][4].get("eval_started") == out["b"][1] == (datetime(2026, 11, 4, 10, 0, 2, tzinfo=timezone.utc)
                                                            .isoformat())
    assert clock.t == datetime(2026, 11, 4, 10, 0, 2, tzinfo=timezone.utc)   # A začal (1 s) pred B (2 s)


def test_qu2_novsi_prepocet_po_starsom_nahradi(cfg, fixed_now, monkeypatch):
    """[QU2] Obvyklé poradie: starší prepočet uloží prvý, novší (z nových údajov) ho nahradí – nové odporúčanie
    s časom začiatku novšieho prepočtu, staršie je zastarané. Novší prepočet bez zmeny aktualizuje to isté
    odporúčanie a zapíše svoj začiatok."""
    with session_scope() as s:
        tid = _train(s, fixed_now, "47251")
    _Clock(monkeypatch)
    with session_scope() as sa:
        first = planovac.evaluate_train(sa, tid, fixed_now, "nový vlak")
        first_id, first_started = first.id, first.data_age.get("eval_started") or ""
    _set_ready(tid, fixed_now + timedelta(hours=8))
    with session_scope() as sb:
        second = planovac.evaluate_train(sb, tid, fixed_now, "potvrdená udalosť z e-mailu")
        second_id = second.id
    rows = _recs(tid)
    assert second_id != first_id and [(r[0], r[1]) for r in rows] == [(first_id, "zastarane"), (second_id, "nove")]
    assert first_started and (rows[1][4].get("eval_started") or "") > first_started
    with session_scope() as sc:
        third = planovac.evaluate_train(sc, tid, fixed_now + timedelta(minutes=1), "pravidelný prepočet")
        assert third.id == second_id                                       # bez zmeny – to isté odporúčanie
        assert third.data_age["eval_started"] > rows[1][4]["eval_started"]


def test_qu2_starsi_prepocet_neprepise_cisla_novsieho(cfg, fixed_now, monkeypatch):
    """[QU2] Starší prepočet s rovnakým výsledkom (rovnaký odtlačok) by inak „nepodstatnú zmenu“ zapísal do novšieho
    odporúčania – staršie čísla (čas prepočtu, súhrn) nesmú prepísať novšie."""
    with session_scope() as s:
        tid = _train(s, fixed_now, "47252")
    _Clock(monkeypatch)
    later = fixed_now + timedelta(minutes=5)
    out = {}

    def newer():
        with session_scope() as sb:
            rec = planovac.evaluate_train(sb, tid, later, "pravidelný prepočet")
            out["b"] = (rec.id, rec.summary, rec.data_age["computed_at"])

    _hook_once(monkeypatch, newer)
    with session_scope() as sa:
        assert planovac.evaluate_train(sa, tid, fixed_now, "nový vlak").id == out["b"][0]
    (row,) = _recs(tid)
    assert row[1] == "nove" and row[5] == out["b"][1]
    assert row[4]["computed_at"] == out["b"][2] == later.isoformat()


def test_qu2_novsi_prepocet_potvrdil_prijate_odporucanie(cfg, fixed_now, monkeypatch):
    """[QU2] Dispečer prijal odporúčanie. Zmena údajov spustí prepočet A; kým A počíta, zmena sa vráti späť a novší
    prepočet B prijaté odporúčanie potvrdí bez zmeny (zapíše len svoj začiatok). Starší A (zo zmenených údajov)
    potom nové odporúčanie nevytvorí – platí prijaté."""
    with session_scope() as s:
        tid = _train(s, fixed_now, "47253")
    _Clock(monkeypatch)
    with session_scope() as s0:
        accepted = planovac.evaluate_train(s0, tid, fixed_now, "nový vlak")
        accepted.state = "prijate"
        acc_id = accepted.id
    _set_ready(tid, fixed_now + timedelta(hours=8))
    out = {}

    def newer():
        _set_ready(tid, fixed_now + timedelta(minutes=30))     # zmena odvolaná
        with session_scope() as sb:
            rec = planovac.evaluate_train(sb, tid, fixed_now, "oprava údajov")
            out["b"] = (rec.id, rec.data_age.get("eval_started"))

    _hook_once(monkeypatch, newer)
    with session_scope() as sa:
        assert planovac.evaluate_train(sa, tid, fixed_now, "zmena pripravenosti").id == acc_id
    rows = _recs(tid)
    assert [(r[0], r[1]) for r in rows] == [(acc_id, "prijate")], rows
    assert out["b"][0] == acc_id and rows[0][4].get("eval_started") == out["b"][1] is not None


def test_qu2_zaznam_o_chybe_sa_riadi_poradim(cfg, fixed_now, monkeypatch):
    """[QU2] Prepočet všetkých vlakov zachytí začiatok prepočtu vlaku ešte pred výpočtom. Ak výpočet zlyhá a novší
    prepočet (začal neskôr, napr. z webu) medzitým uložil odporúčanie, záznam o chybe (ESKALOVAŤ – posúďte bez
    agenta) sa neuloží – platí novšie odporúčanie. Bez novšieho prepočtu je záznam o chybe nové odporúčanie so
    začiatkom zlyhaného prepočtu."""
    with session_scope() as s:
        tid = _train(s, fixed_now, "47254")
    clock = _Clock(monkeypatch)
    base = clock.t
    clock.t = base + timedelta(minutes=10)          # novší prepočet B začal neskôr …
    with session_scope() as sb:
        newer_id = planovac.evaluate_train(sb, tid, fixed_now, "potvrdená udalosť z e-mailu").id
    clock.t = base                                  # … ako prepočet všetkých vlakov, ktorý pri vlaku zlyhal

    def broken(session, train_id, now=None, trigger=""):
        raise RuntimeError("FIKTÍVNA chyba výpočtu")

    monkeypatch.setattr(planovac, "evaluate_train", broken)
    with session_scope() as sa:
        assert [r.id for r in planovac.evaluate_all(sa, fixed_now, "pravidelný prepočet")] == [newer_id]
    assert [(r[0], r[1]) for r in _recs(tid)] == [(newer_id, "nove")]
    clock.t = base + timedelta(minutes=20)
    with session_scope() as sc:
        (failed,) = planovac.evaluate_all(sc, fixed_now + timedelta(minutes=1), "pravidelný prepočet")
        assert failed.decision == "ESKALOVAŤ" and "nedokázal prepočítať" in failed.summary
        assert failed.data_age.get("eval_started") == (base + timedelta(minutes=20, seconds=1)).isoformat()
    assert [r[1] for r in _recs(tid)] == ["zastarane", "nove"]


def test_qu2_prepocet_vsetkych_vlakov_v_poradi_id(cfg, fixed_now, monkeypatch):
    """[QU2] Prepočet všetkých vlakov ide v poradí podľa id (zámky vlakov tak súbežné prepočty berú v rovnakom
    poradí – bez vzájomného čakania) – aj keď databáza vráti riadky v inom poradí (PostgreSQL po zmene riadku)."""
    with session_scope() as s:
        ids = [_train(s, fixed_now, f"4726{k}") for k in range(3)]
    with session_scope() as s:
        s.get(Train, ids[0]).notes = "zmenený záznam (riadok sa v PostgreSQL presunie na koniec tabuľky)"
    seen, statements = [], []
    monkeypatch.setattr(planovac, "evaluate_train", lambda session, tid, now=None, trigger="": seen.append(tid))

    def spy(conn, cursor, statement, parameters, context, executemany):
        if "FROM trains" in statement:
            statements.append(statement)

    with session_scope() as sc:
        eng = sc.get_bind()
        event.listen(eng, "before_cursor_execute", spy)
        try:
            planovac.evaluate_all(sc, fixed_now, "pravidelný prepočet")
        finally:
            event.remove(eng, "before_cursor_execute", spy)
    assert seen == sorted(ids)
    assert statements and "ORDER BY trains.id" in statements[0], statements


def test_qu2_subezne_ulozenie_caka_na_zamok_vlaku(cfg, fixed_now, monkeypatch):
    """[QU2] Skutočne súbežne (dve vlákna, dve relácie): prepočet A začne a počíta; novší prepočet B uloží výsledok,
    ale ešte ho nepotvrdil (drží zámok vlaku). A pri ukladaní počká na zámok (nečíta „posledné odporúčanie“ pred
    potvrdením B), potom zistí, že B je novší, a neuloží nič. Bez zámku by A uložil druhé „nové“ odporúčanie
    s vyšším id – zo starších údajov – a to by bolo aktuálne."""
    with session_scope() as s:
        tid = _train(s, fixed_now, "47255")
    a_reads, b_stored = threading.Event(), threading.Event()
    real = planovac.closure_windows
    result: dict = {}

    def hooked(session, start, end):
        if threading.current_thread().name == "prepocet-A":
            a_reads.set()
            assert b_stored.wait(60), "prepočet B sa neuložil"
        return real(session, start, end)

    monkeypatch.setattr(planovac, "closure_windows", hooked)

    def run_a():
        try:
            with session_scope() as sa:
                rec = planovac.evaluate_train(sa, tid, fixed_now, "nový vlak")
                result["a"] = rec.id
                result["a_done"] = time.monotonic()
        except Exception as exc:  # noqa: BLE001 – test vyhodnotí
            result["error"] = repr(exc)

    worker = threading.Thread(target=run_a, name="prepocet-A")
    worker.start()
    try:
        assert a_reads.wait(60)
        _set_ready(tid, fixed_now + timedelta(hours=8))
        with session_scope() as sb:
            rec_b = planovac.evaluate_train(sb, tid, fixed_now, "potvrdená udalosť z e-mailu")
            result["b"] = rec_b.id
            b_stored.set()
            time.sleep(1.0)                    # A medzitým dopočíta a čaká na zámok vlaku
            result["b_commit"] = time.monotonic()
    finally:
        b_stored.set()
        worker.join(120)
    assert not worker.is_alive() and "error" not in result, result
    assert result["a"] == result["b"]
    assert result["a_done"] >= result["b_commit"]          # A dokončil až po potvrdení B (čakal na zámok)
    rows = _recs(tid)
    assert [(r[0], r[1], r[3]) for r in rows] == [(result["b"], "nove", "potvrdená udalosť z e-mailu")], rows


def test_qu2_web_novy_vlak_a_hned_potvrdene_avizo(cfg, fixed_now, monkeypatch):
    """[QU2] Tok z webu: dispečer založí vlak (prepočet „nový vlak“ na pozadí) a hneď potvrdí avízo (prepočet všetkých
    vlakov na pozadí). Prepočet „nový vlak“ začal skôr, ale ukladá až po prepočte s avízom – aktuálne ostáva
    odporúčanie s avízom (vlak príde o 30 h) a každý dispečer dostane o vlaku jedinú správu."""
    from collections import Counter

    from vlaky.models import Notification
    from vlaky.notifikacie.odosielanie import SENT_STATES
    from vlaky.web import prepocet

    with session_scope() as s:
        tid = _train(s, fixed_now, "47256")
    done = []
    real = planovac.closure_windows

    def hooked(session, start, end):
        if not done:
            done.append(True)
            # kým „nový vlak“ počíta: dispečer potvrdí avízo (vlak príde neskôr) – prepočet všetkých vlakov z webu
            _set_ready(tid, fixed_now + timedelta(hours=30))
            prepocet.evaluate_all_bg("potvrdená udalosť z e-mailu #11")
        return real(session, start, end)

    monkeypatch.setattr(planovac, "closure_windows", hooked)
    prepocet.evaluate_train_bg(tid, "nový vlak (zadal dispecer)")
    rows = _recs(tid)
    assert [(r[1], r[3]) for r in rows] == [("nove", "potvrdená udalosť z e-mailu #11")], rows
    with session_scope() as s:
        sent = [n for n in s.scalars(select(Notification).where(Notification.kind == "odporucanie"))
                if n.status in SENT_STATES]
        assert sent and {n.recommendation_id for n in sent} == {rows[0][0]}
        assert set(Counter((n.channel, n.recipient) for n in sent).values()) == {1}
