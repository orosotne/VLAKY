"""Regresné testy k zlúčeniu opráv tretej revízie (súvislosti medzi oblasťami)."""
from __future__ import annotations

from datetime import timedelta

from test_web_spolocne import make_train

pytest_plugins = ["test_web_spolocne"]


def test_hlavicka_odkazu_rusnovodica_ukaze_zmenu_v_sluzbe_nie_vystriedanie(client, fixed_now):
    """Po nástupe prvého rušňovodiča (naplánované vystriedanie o 7 h) ukáže hlavička aplikácie v mobile zmenu,
    ktorá prebieha – nie neskoršie naplánované vystriedanie (planovac.link_shift)."""
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift, Train
    from vlaky.web.rusnovodic import make_driver_token

    train_id = make_train(number="47801", status="v_jazde")
    with session_scope() as s:
        first = Driver(name="Prvý rušňovodič (FIKTÍVNY)")
        relief = Driver(name="Vystriedanie (FIKTÍVNE)")
        s.add_all([first, relief])
        s.flush()
        s.add_all([
            DriverShift(driver_id=first.id, train_id=train_id, start_utc=fixed_now - timedelta(hours=1),
                        status="prebieha"),
            DriverShift(driver_id=relief.id, train_id=train_id, start_utc=fixed_now + timedelta(hours=7),
                        status="planovana"),
        ])
        token = make_driver_token(s.get(Train, train_id))
    info = client.post("/api/rusnovodic/info", json={"t": token}).json()
    assert info["driver_name"] == "Prvý rušňovodič (FIKTÍVNY)" and info["shift_status"] == "prebieha"


def test_chyba_prihlasenia_do_schranky_nesie_kod_bez_identifikatorov(cfg, session, monkeypatch):
    """[OD4] Chyba prihlásenia do schránky nesie kód na diagnostiku (Graph: invalid_client, AADSTS7000222; IMAP:
    AUTHENTICATIONFAILED) – nikdy celý popis servera (identifikátory aplikácie, Trace ID)."""
    import imaplib

    import httpx
    import pytest

    from vlaky import config as config_mod
    from vlaky.posta.zdroje import GraphSource, ImapSource, MailSourceError

    s = config_mod.override_settings(**{**cfg.model_dump(), "graph_tenant_id": "00000000-0000-0000-0000-000000000000",
                                        "graph_client_id": "11111111-1111-1111-1111-111111111111",
                                        "graph_client_secret": "vyprsane",
                                        "graph_mailbox": "agent.dispecing@dopravca.example", "mail_source": "imap",
                                        "imap_host": "imap.dopravca.example", "imap_user": "agent",
                                        "imap_password": "heslo"})
    assert s.imap_host == "imap.dopravca.example"

    def token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client",
                                         "error_description": "AADSTS7000222: The provided client secret keys for "
                                                              "app '11111111-1111' are expired. Trace ID: abc"})

    with pytest.raises(MailSourceError) as err:
        GraphSource(session, client=httpx.Client(transport=httpx.MockTransport(token))).fetch_new()
    text = str(err.value)
    assert "invalid_client" in text and "AADSTS7000222" in text
    assert "11111111" not in text and "Trace" not in text

    class BadLogin:
        def __init__(self, *a, **kw):
            pass

        def login(self, user, password):
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

        def logout(self):
            pass

    with pytest.raises(MailSourceError, match="AUTHENTICATIONFAILED") as err2:
        ImapSource(session, factory=BadLogin).fetch_new()
    assert "Invalid credentials" not in str(err2.value)


def test_hlasenie_problemu_pri_vypadku_neklame_ze_bolo_odoslane(client, fixed_now, monkeypatch):
    """[WR3] Keď sa upozornenie dispečerom nepodarí odoslať (výpadok servera správ), odpoveď pre mobil nesmie
    tvrdiť „Problém odoslaný dispečerovi.“ – hlásenie je uložené, aplikácia ukáže upozornenie (warning)."""
    from vlaky.db import session_scope
    from vlaky.models import Train
    import vlaky.web.rusnovodic as rusnovodic

    note = "Hlásenie je uložené, ale upozornenie dispečerom sa zatiaľ nepodarilo odoslať – zavolajte dispečerovi."
    monkeypatch.setattr(rusnovodic, "_notify_problem", lambda *a, **k: note)
    train_id = make_train(number="47802", status="v_jazde")
    with session_scope() as s:
        token = rusnovodic.make_driver_token(s.get(Train, train_id))
    r = client.post("/api/hlasenie", json={"t": token, "typ": "problem", "text": "Porucha bŕzd (FIKTÍVNE)"})
    assert r.status_code == 200
    data = r.json()
    assert data["warning"] is True and data["message"] == note
    monkeypatch.setattr(rusnovodic, "_notify_problem", lambda *a, **k: None)
    with session_scope() as s:
        token2 = rusnovodic.make_driver_token(s.get(Train, make_train(number="47803", status="v_jazde")))
    ok = client.post("/api/hlasenie", json={"t": token2, "typ": "problem", "text": "Iný problém (FIKTÍVNE)"}).json()
    assert ok["message"] == "Problém odoslaný dispečerovi." and "warning" not in ok


def test_graph_stiahne_inu_spravu_s_rovnakym_message_id_a_preskoci_opatovne_dorucenie(cfg, session, fixed_now,
                                                                                         monkeypatch):
    """[MR8] Microsoft Graph: iná správa KIS (zrušenie trasy), ktorá znova použije Message-ID pridelenia, sa stiahne
    a spracuje; tá istá správa s rovnakým časom prijatia sa druhýkrát nestiahne."""
    import httpx
    from sqlalchemy import select

    from test_revizia2_posta import drop_raw, mail
    from vlaky.models import ExtractedEvent
    from vlaky.posta import zdroje
    from vlaky.posta.spracovanie import process_mailbox

    monkeypatch.setattr(cfg, "require_dmarc_pass", False)
    kis = "KIS - ZSR <kis-noreply@zsr.example>"
    body1 = ("Typ správy: Pridelenie trasy\nVlak: Nex 54321\nDátum jazdy: 14.11.2026\n\n"
             "Dopravňa Príchod Odchod\nČadca – 06:10\nŽilina 06:52 –\n")
    drop_raw(cfg, "1.eml", mail("kis-54321", "KIS - pridelenie trasy Nex 54321", body1, sender=kis))
    process_mailbox(session, now=fixed_now)
    session.commit()
    body2 = ("Typ správy: Zrušenie trasy\nVlak: Nex 54321\nDeň jazdy: 14.11.2026\n\n"
             "Trasa vlaku Nex 54321 na deň 14.11.2026 bola zrušená.\n")
    raw2 = mail("kis-54321", "KIS - zrusenie trasy Nex 54321 na den 14.11.2026", body2, sender=kis)
    downloads: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2" in url:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if url.endswith("/$value"):
            downloads.append(url)
            return httpx.Response(200, content=raw2)
        return httpx.Response(200, json={"value": [{
            "id": "AAMk2", "internetMessageId": "<kis-54321@zsr.example>", "receivedDateTime": "2026-11-02T06:10:00Z",
            "subject": "KIS - zrusenie trasy", "from": {"emailAddress": {"address": "kis-noreply@zsr.example"}}}],
            "@odata.deltaLink": "https://graph.example/delta?token=2"})

    for k, v in {"mail_source": "graph", "graph_tenant_id": "t", "graph_client_id": "c", "graph_client_secret": "s",
                 "graph_mailbox": "agent.dispecing@dopravca.example"}.items():
        monkeypatch.setattr(cfg, k, v)
    real_init = zdroje.GraphSource.__init__

    def init(self, session=None, client=None, **kw):  # noqa: ANN001
        real_init(self, session, httpx.Client(transport=httpx.MockTransport(handler)), **kw)

    monkeypatch.setattr(zdroje.GraphSource, "__init__", init)
    process_mailbox(session, now=fixed_now)
    session.commit()
    kinds = [e.kind for e in session.scalars(select(ExtractedEvent).order_by(ExtractedEvent.id))]
    assert "trasa_zrusena" in kinds and len(downloads) == 1
    zdroje.kv_set(session, zdroje.GraphSource.STATE_KEY, None)  # tá istá správa znova (napr. nový deltaLink)
    session.commit()
    process_mailbox(session, now=fixed_now)
    assert len(downloads) == 1  # opätovné doručenie tej istej správy sa nestiahlo


def test_agent_once_vrati_chybovy_kod_pri_chybe_cyklu(cfg, monkeypatch):
    """`python -m vlaky agent --once` (cron, test): cyklus s chybou (schránka sa nedá prečítať) vráti kód 1,
    bezchybný cyklus 0."""
    from vlaky.agent import beh

    monkeypatch.setattr(cfg, "mail_source", "directory")
    assert beh.run_agent(once=True) == 0
    monkeypatch.setattr(cfg, "mail_source", "imap")
    monkeypatch.setattr(cfg, "imap_host", "")  # IMAP nie je nastavené → chyba čítania schránky v cykle
    assert beh.run_agent(once=True) == 1


def test_planovac_zapise_dovody_obmedzenia_a_email_neukaze_zelenu(session, fixed_now):
    """Plánovač zapíše, prečo ZELENÁ nie je dovolená (data_age["obmedzenie"]); e-mail odporúčania potom neukáže
    okná odchodu ako „bezpečné (ZELENÁ)“ ani ZELENÉ varianty – rovnaké pravidlo ako web (data.semafor_cap)."""
    from sqlalchemy import delete

    from vlaky.hranice import seed_borders
    from vlaky.models import KeyValue, Path, Train
    from vlaky.notifikacie.texty import recommendation_body
    from vlaky.planovanie.planovac import evaluate_train

    seed_borders(session)
    session.execute(delete(KeyValue).where(KeyValue.key == "mail_last_ok"))  # schránka neskontrolovaná
    t = Train(number="47900", run_date=fixed_now.date().isoformat(), origin="Čadca", destination="Žilina",
              traction="E", gauge="1435", vmax_kmh=100, status="planovany", ready_utc=fixed_now + timedelta(hours=1),
              deadline_utc=fixed_now + timedelta(hours=10))
    session.add(t)
    session.flush()
    session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                     departure_utc=fixed_now + timedelta(hours=1)))
    session.commit()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.color != "ZELENÁ"
    assert any("schránka" in x for x in rec.data_age.get("obmedzenie", [])), rec.data_age
    body = recommendation_body(rec)
    assert "Okná bezpečného odchodu (ZELENÁ)" not in body
    assert not any(" ZELENÁ " in line for line in body.splitlines() if "P(včas)" in line and line.startswith("  "))


def test_email_vlaku_v_cieli_neuvadza_odchod_ani_prichod_teraz(session, fixed_now):
    """E-mail odporúčania pre vlak v cieli: „Vlak je v cieli – príchod …“ (alebo príchod podľa plánu), nie
    „Odchod: teraz, Príchod: teraz“ a koniec služby z „teraz“."""
    from vlaky.models import Recommendation, Train
    from vlaky.notifikacie.texty import recommendation_body

    t = Train(number="47901", origin="Čadca", destination="Žilina", status="v_jazde")
    session.add(t)
    session.flush()
    arr = fixed_now - timedelta(minutes=20)
    rec = Recommendation(train_id=t.id, created_at=fixed_now, decision="POKRAČOVAŤ", color="ZELENÁ", priority="INFO",
                         summary="Vlak 47901: POKRAČOVAŤ – vlak je v cieli.",
                         chosen={"route": "hlavná", "depart": fixed_now.isoformat(), "kind": "POKRAČOVAŤ",
                                 "arrival_p50": fixed_now.isoformat(), "arrival_p90": fixed_now.isoformat(),
                                 "v_cieli": True, "prichod_v_cieli": arr.isoformat(), "prichod_podla_planu": None},
                         crew={}, reasons=[], data_age={})
    session.add(rec)
    session.flush()
    body = recommendation_body(rec)
    assert "Vlak je v cieli – príchod" in body and "Odchod:" not in body and "Príchod:" not in body
    rec.chosen = dict(rec.chosen, prichod_v_cieli=None, prichod_podla_planu=arr.isoformat())
    rec.crew = {"driver": "Rušňovodič (FIKTÍVNY)", "start": (fixed_now - timedelta(hours=5)).isoformat(),
                "limit": (fixed_now + timedelta(hours=8)).isoformat(), "end_p50": fixed_now.isoformat(),
                "end_p90": fixed_now.isoformat(), "status": "ZELENÁ", "max_duty_h": 13}
    body2 = recommendation_body(rec)
    assert "Podľa plánu by už mal byť v cieli" in body2 and "koniec služby po ukončení jazdy" in body2


def test_ostra_prevadzka_bez_prijemcov_je_chyba_nastavenia(cfg, monkeypatch):
    """V ostrej prevádzke bez interného zoznamu príjemcov agent neodošle nič (ani KRITICKÉ) – zdravie to musí
    hlásiť ako chybu; vo vývoji, testoch a ukážke je to len informácia."""
    from vlaky.agent.zdravie import config_status

    monkeypatch.setattr(cfg, "notify_emails", "")
    monkeypatch.setattr(cfg, "notify_phones", "")
    monkeypatch.setattr(cfg, "teams_webhook_url", "")
    ok, text = config_status()
    assert ok is True and "príjemca" in text  # test: stačí informácia
    monkeypatch.setattr(cfg, "env", "prod")
    ok, text = config_status()
    assert ok is False and "neodošle nič" in text
    monkeypatch.setattr(cfg, "notify_phones", "+421900000001")
    ok, text = config_status()
    assert ok is False and "VLAKY_NOTIFY_EMAILS" in text
