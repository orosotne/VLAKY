"""Regresné testy k 3. revízii – stabilita odporúčaní (oblasť P2, druhé kolo opráv): vlak, ktorý už stojí na koľajach
ŽSR, nie je „v zahraničí“ pre nedopísané preberanie; hlásenie rušňovodiča „pripravený“ pri nezapísanom príchode;
rozhodnutie dispečera (prijaté aj upravené odporúčanie) platí aj po medziodporúčaní; objednaná nová trasa len kým
ŽSR neodpovie a odchod na ňu bez pridelenia nikdy nie je ZELENÝ; web neponúka objednávku trasy pri vlastnej trase."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as
from vlaky.agent.zdravie import set_kv
from vlaky.cas import fmt, local, set_fake_now
from vlaky.hranice import find_border, seed_borders
from vlaky.models import Closure, Handover, Path, Recommendation, Train
from vlaky.planovanie import stav
from vlaky.planovanie.planovac import evaluate_train

pytest_plugins = ["test_web_spolocne"]

TOLERANCIA = timedelta(minutes=15)


def _train(session, now, path_at=None, **kw):
    data = dict(number="74000", run_date=now.date().isoformat(), origin="Čadca", destination="Košice",
                traction="E", gauge="1435", vmax_kmh=100, status="planovany", ready_utc=now,
                deadline_utc=now + timedelta(hours=30))
    data.update(kw)
    t = Train(**data)
    session.add(t)
    session.flush()
    if path_at is not None:
        session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                         departure_utc=now + timedelta(minutes=path_at)))
        session.flush()
    return t


def _abroad(session, now, avizo_h=-3.0, path_at=20, **kw):
    """Vlak na ceste k hranici (Čadca), vstupné preberanie očakávané (príchod nezapísaný)."""
    bc = find_border(session, "Čadca")
    data = dict(number="74010", status="na_ceste_k_hranici", entry_border_id=bc.id, ready_utc=None,
                deadline_utc=now + timedelta(hours=12))
    data.update(kw)
    t = _train(session, now, path_at=path_at, **data)
    ho = Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="ocakavany",
                  avizo_eta_utc=now + timedelta(hours=avizo_h) if avizo_h is not None else None)
    session.add(ho)
    session.flush()
    return t, ho, bc


def _closure(session, now, start_h, end_h, a="Liptovský Mikuláš", b="Poprad-Tatry"):
    c = Closure(kind="uplna", from_station=a, to_station=b, start_utc=now + timedelta(hours=start_h),
                end_utc=now + timedelta(hours=end_h), status="overene")
    session.add(c)
    session.flush()
    return c


def _recs(session, train_id) -> list:
    return list(session.scalars(select(Recommendation).where(Recommendation.train_id == train_id)
                                .order_by(Recommendation.id)))


def _depart(rec) -> datetime:
    return datetime.fromisoformat(rec.chosen["depart"])


def _mailbox(session, last_ok: datetime) -> None:
    """Čas poslednej úspešnej kontroly schránky agenta (výpadok schránky = starý čas)."""
    set_kv(session, "mail_last_ok", {"ts": last_ok.isoformat(), "fetched": 0, "test": True})
    session.flush()


def _lt(dt: datetime) -> str:
    return local(dt).strftime("%Y-%m-%dT%H:%M")


# ====================================================================== vlak už stojí na koľajach ŽSR
def test_prevzaty_vlak_s_nedopisanym_preberanim_nie_je_v_zahranici(client, session, fixed_now):
    """Dispečer zmení stav vlaku vo webe na „prevzatý“, preberanie na hranici ostalo „očakávané“ (príchod nezapísaný,
    avízo pred 3 h). Vlak stojí na koľajach ŽSR (limit 18 h mu beží) – nesmie dostať „Vlak ešte neprišiel na hranicu“,
    posunutú pripravenosť ani zahodenú pridelenú trasu o 20 min (nová trasa o 6 h)."""
    seed_borders(session)
    t, ho, _bc = _abroad(session, fixed_now, number="74001")
    session.commit()
    login_as(client, "disp1", "dispecer")
    token = csrf(client, f"/vlaky/{t.id}/upravit")
    form = {"csrf_token": token, "number": t.number, "run_date": t.run_date, "origin": t.origin,
            "destination": t.destination, "gauge": t.gauge, "traction": t.traction, "vmax_kmh": "100",
            "status": "prevzaty", "entry_border_id": str(t.entry_border_id), "deadline_utc": _lt(t.deadline_utc),
            "ready_utc": _lt(fixed_now - timedelta(minutes=30))}
    assert client.post(f"/vlaky/{t.id}/upravit", follow_redirects=False, data=form).status_code == 303
    session.expire_all()
    assert session.get(Train, t.id).status == "prevzaty" and session.get(Handover, ho.id).status == "ocakavany"
    rec = evaluate_train(session, t.id, fixed_now, "úprava vlaku")
    path = fixed_now + timedelta(minutes=20)
    assert not any("neprišiel" in r or "nestihne" in r or "nie je zapísaný" in r for r in rec.reasons)
    assert rec.chosen["needs_new_path"] is False and abs(_depart(rec) - path) <= TOLERANCIA
    assert rec.color == "ZELENÁ"


def test_odstaveny_vlak_s_nedopisanym_preberanim(session, fixed_now):
    """To isté pri odstavenom vlaku: ostáva odstavený do svojej pridelenej trasy (nie „pôvodnú trasu odrieknuť“) a
    limit 18 h sa počíta od pripravenosti teraz, nie od pripravenosti posunutej o dobu preberania."""
    seed_borders(session)
    t, _ho, _bc = _abroad(session, fixed_now, number="74002")
    stav.pin_standing(session, t, fixed_now - timedelta(minutes=50))
    stav.set_status(session, t, "odstaveny", fixed_now - timedelta(minutes=50), actual=False)
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert not any("neprišiel" in r or "nestihne" in r for r in rec.reasons)
    assert rec.chosen["needs_new_path"] is False and "odrieknuť" not in rec.summary
    assert any(r.startswith(f"Vlak stojí od {fmt(fixed_now - timedelta(minutes=50))} – do limitu 18 h (PPŽS) "
                            f"zostáva 17,2 h") for r in rec.reasons)


# ====================================================================== pripravenosť zapísaná po avíze
def test_hlasenie_pripraveny_pri_nezapisanom_prichode(client, session, fixed_now):
    """Rušňovodič nahlási „pripravený“ (PWA), dispečer ešte nezapísal príchod (avízo pred 3 h). Pridelená trasa o 20 min
    sa nesmie zahodiť (nová trasa o 6 h), text nesmie tvrdiť „Vlak ešte neprišiel“; kým príchod nie je zapísaný,
    vlak nie je ZELENÝ a dispečer vidí, čo by sa stalo, keby vlak neprišiel. Po zapísaní príchodu obmedzenie zmizne."""
    from vlaky.web.rusnovodic import make_driver_token

    login_as(client, "disp1", "dispecer")
    seed_borders(session)
    t, ho, _bc = _abroad(session, fixed_now, number="74003")
    token = make_driver_token(t)
    session.commit()
    assert client.post("/api/hlasenie", json={"t": token, "typ": "pripraveny"}).status_code == 200
    now = fixed_now + timedelta(minutes=1)
    set_fake_now(now)
    session.expire_all()
    assert session.get(Train, t.id).ready_utc is not None
    rec = evaluate_train(session, t.id, now, "hlásenie rušňovodiča: pripravený")
    path = fixed_now + timedelta(minutes=20)
    assert rec.chosen["needs_new_path"] is False and abs(_depart(rec) - path) <= TOLERANCIA
    assert rec.color == "ORANŽOVÁ"
    assert not any("Vlak ešte neprišiel" in r for r in rec.reasons)
    assert any(r.startswith(f"Príchod na hranicu (Čadca; avízo {fmt(ho.avizo_eta_utc)}") and "nie je zapísaný" in r
               for r in rec.reasons)
    assert any(r.startswith(f"Ak vlak ešte neprišiel, pridelenú trasu ({fmt(path)}) nestihne") for r in rec.reasons)
    # dispečer zapíše príchod (v čase avíza): vlak je na hranici, obmedzenie na ORANŽOVÚ zmizne
    session.commit()
    form = {"csrf_token": csrf(client, "/hranice"), "cas": _lt(ho.avizo_eta_utc)}
    assert client.post(f"/hranice/preberanie/{ho.id}/prisiel", follow_redirects=False, data=form).status_code == 303
    session.expire_all()
    later = evaluate_train(session, t.id, now + timedelta(minutes=1), "príchod na hranicu")
    assert later.color == "ZELENÁ" and later.chosen["needs_new_path"] is False
    assert not any("nie je zapísaný" in r for r in later.reasons)


def test_planovana_a_stara_pripravenost_pri_uplynutom_avize(session, fixed_now):
    """Pripravenosť zapísaná po avíze (napr. plán „avízo + 60 min“) sa pri uplynutom avíze použije, no nikdy nie skôr,
    ako by bol vlak pripravený pri príchode podľa avíza (posunutie avíza vlak „neurýchli“), a nie ako ZELENÁ.
    Pripravenosť staršia ako avízo je neplatná – vlak ešte neprišiel."""
    seed_borders(session)
    bc = find_border(session, "Čadca")
    dwell = timedelta(minutes=max(bc.dwell_p50_min, bc.dwell_p90_min))
    avizo = fixed_now - timedelta(minutes=45)
    t, _ho, _bc = _abroad(session, fixed_now, avizo_h=-0.75, path_at=None, number="74004",
                          ready_utc=avizo + timedelta(minutes=60))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.color != "ZELENÁ"
    assert any("nie je zapísaný; počíta sa s pripravenosťou podľa záznamu vlaku" in r for r in rec.reasons)
    assert not any("Vlak ešte neprišiel" in r for r in rec.reasons)
    assert _depart(rec) >= avizo + dwell - timedelta(seconds=1)
    t2, _ho2, _ = _abroad(session, fixed_now, avizo_h=-0.75, path_at=None, number="74005",
                          ready_utc=avizo - timedelta(minutes=60))
    stale = evaluate_train(session, t2.id, fixed_now, "test")
    assert stale.color != "ZELENÁ"
    assert any("Vlak ešte neprišiel na hranicu" in r and "je staršia ako avízo" in r for r in stale.reasons)
    assert _depart(stale) >= fixed_now + dwell - timedelta(seconds=1)


# ====================================================================== rozhodnutie dispečera po medziodporúčaní
def test_prijata_nova_trasa_plati_aj_po_medziodporucani(session, fixed_now):
    """Prijaté „ODSTAVIŤ …, nová trasa na 12:20“ (objednaná v lehote) a potom výpadok schránky (ORANŽOVÁ – nové
    odporúčanie): objednaná trasa sa nesmie zabudnúť – ani počas výpadku, ani po ňom sa čas trasy neposunie na
    teraz + 6 h; nové odporúčanie po výpadku nevyzýva trasu objednať znova."""
    seed_borders(session)
    t = _train(session, fixed_now, number="74020", path_at=30)
    _closure(session, fixed_now, -1, 8)
    first = evaluate_train(session, t.id, fixed_now, "test")
    dep = _depart(first)
    assert first.decision == "ODSTAVIŤ" and first.chosen["needs_new_path"] and dep - fixed_now > timedelta(hours=6)
    first.state = "prijate"
    session.flush()
    out = fixed_now + timedelta(hours=3, minutes=30)  # čas trasy je už v lehote 6 h
    _mailbox(session, out - timedelta(minutes=45))
    amber = evaluate_train(session, t.id, out, "výpadok schránky")
    assert amber.id != first.id and amber.color == "ORANŽOVÁ" and _depart(amber) == dep
    again = evaluate_train(session, t.id, out + timedelta(minutes=15), "výpadok schránky")
    assert again.id == amber.id and _depart(again) == dep
    _mailbox(session, datetime(2100, 1, 1, tzinfo=fixed_now.tzinfo))
    back = evaluate_train(session, t.id, out + timedelta(minutes=30), "schránka opäť v poriadku")
    assert back.color == "ZELENÁ" and _depart(back) == dep
    assert "podľa prijatého odporúčania" in back.summary and "Treba objednať" not in back.summary
    for minutes in (60, 120):
        rec = evaluate_train(session, t.id, out + timedelta(minutes=minutes), "pravidelný prepočet")
        assert rec.id == back.id and _depart(rec) == dep
    assert first.state == "prijate" and [_depart(r) for r in _recs(session, t.id)] == [dep, dep, dep]


def test_prijate_odstavenie_plati_aj_po_medziodporucani(session, fixed_now):
    """Prijaté ODSTAVIŤ (stav vlaku nezmenený), potom výpadok schránky (ORANŽOVÁ) a návrat (ZELENÁ): bod zlomu
    čakať/odstaviť sa nesmie vrátiť – žiadne POČKAŤ pre ten istý odchod, odstavenie je rozhodnuté."""
    seed_borders(session)
    t = _train(session, fixed_now, number="74021", path_at=330)
    first = evaluate_train(session, t.id, fixed_now, "test")
    assert first.decision == "ODSTAVIŤ" and first.chosen["needs_new_path"] is False
    first.state = "prijate"
    session.flush()
    _mailbox(session, fixed_now + timedelta(minutes=15))
    amber = evaluate_train(session, t.id, fixed_now + timedelta(minutes=60), "výpadok schránky")
    assert amber.color == "ORANŽOVÁ" and amber.decision == "ODSTAVIŤ"
    _mailbox(session, datetime(2100, 1, 1, tzinfo=fixed_now.tzinfo))
    for minutes in (90, 105, 120, 180, 240):
        rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=minutes), "pravidelný prepočet")
        assert rec.decision == "ODSTAVIŤ", (minutes, rec.decision, rec.summary)
    assert not any(r.decision == "POČKAŤ" for r in _recs(session, t.id))


def test_upravene_odporucanie_je_rozhodnutie_dispecera(session, fixed_now):
    """Odporúčanie prijaté s úpravou (stav „upravené“) je rozhodnutie dispečera ako prijaté: objednaná nová trasa sa
    v lehote P5 ponúka ďalej a nenahradí ju neskoršia trasa."""
    seed_borders(session)
    t = _train(session, fixed_now, number="74022", path_at=30)
    _closure(session, fixed_now, -1, 8)
    first = evaluate_train(session, t.id, fixed_now, "test")
    dep = _depart(first)
    first.state, first.dispatcher_note = "upravene", "Rušňovodič nastúpi o 15 min skôr (FIKTÍVNE)."
    session.flush()
    for minutes in (120, 240, 360):
        rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=minutes), "pravidelný prepočet")
        assert rec.id == first.id and _depart(rec) == dep


# ====================================================================== objednaná trasa a odpoveď ŽSR
def test_objednana_trasa_po_odpovedi_zsr_s_inym_casom(session, fixed_now):
    """ŽSR na objednávku pridelí iný čas (12:47 namiesto 12:20): odporúčanie sa hneď riadi pridelenou trasou – nie
    objednaným časom, a v čase objednanej trasy agent neodporučí odchod na trasu, ktorú ŽSR nepridelila."""
    seed_borders(session)
    t = _train(session, fixed_now, number="74030", path_at=30)
    _closure(session, fixed_now, -1, 8)
    first = evaluate_train(session, t.id, fixed_now, "test")
    dep = _depart(first)
    first.state = "prijate"
    session.flush()
    answer = dep + timedelta(minutes=27)
    session.add(Path(train_id=t.id, train_number=t.number, status="pridelena", departure_utc=answer))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now + timedelta(hours=2), "trasa pridelená")
    assert rec.id != first.id and rec.chosen["needs_new_path"] is False
    assert abs(_depart(rec) - answer) <= TOLERANCIA and "pridelenú trasu" in rec.summary
    due = evaluate_train(session, t.id, dep - timedelta(minutes=10), "pravidelný prepočet")
    assert due.chosen["needs_new_path"] is False and abs(_depart(due) - answer) <= TOLERANCIA


def test_odchod_na_objednanu_nepridelenu_trasu_nie_je_zeleny(session, fixed_now):
    """ŽSR na objednávku zatiaľ neodpovedala (trasa nie je zapísaná ako pridelená) a čas objednanej trasy nastal:
    VYPRAVIŤ na ňu nesmie byť ZELENÁ – dispečer najprv overí pridelenie."""
    seed_borders(session)
    t = _train(session, fixed_now, number="74031", path_at=30)
    _closure(session, fixed_now, -1, 8)
    first = evaluate_train(session, t.id, fixed_now, "test")
    dep = _depart(first)
    first.state = "prijate"
    session.flush()
    rec = evaluate_train(session, t.id, dep, "pravidelný prepočet")
    assert rec.decision == "VYPRAVIŤ" and rec.chosen["needs_new_path"] is True
    assert rec.color != "ZELENÁ"
    assert any(r.startswith(f"Trasa podľa prijatého odporúčania ({fmt(dep)}) nie je zapísaná ako pridelená")
               for r in rec.reasons)


# ====================================================================== koncept objednávky trasy na webe
def test_web_neponuka_objednavku_trasy_pri_odchode_na_vlastnu_trasu(client, session, fixed_now):
    """ODSTAVIŤ s odchodom na vlastnú pridelenú trasu: detail odporúčania na webe neponúka koncept „objednávka ad hoc
    trasy“ (ako ani e-mail); odporúčanie s novou trasou ho ponúka ďalej."""
    seed_borders(session)
    t = _train(session, fixed_now, number="74040", path_at=330)
    own = evaluate_train(session, t.id, fixed_now, "test")
    assert own.decision == "ODSTAVIŤ" and own.chosen["needs_new_path"] is False
    t2 = _train(session, fixed_now, number="74041", path_at=None)
    new = evaluate_train(session, t2.id, fixed_now, "test")
    assert new.chosen["needs_new_path"] is True
    session.commit()
    login_as(client, "disp1", "dispecer")
    page = client.get(f"/odporucania/{own.id}").text
    assert "Žiadosť o trasu pre ŽSR" not in page and "objednávka ad hoc trasy" not in page
    assert "Správa zákazníkovi" in page
    page_new = client.get(f"/odporucania/{new.id}").text
    assert "Žiadosť o trasu pre ŽSR" in page_new and "objednávka ad hoc trasy" in page_new
