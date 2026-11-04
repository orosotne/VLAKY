"""Regresné testy k 5. revízii (oblasť C – agent a upozornenia): opakovanie upozornení, tieňová prevádzka, stráženie
pracovného času rušňovodiča.

R5O0 – upozornenie „Na potvrdenie čaká …“ (kľúč ``neista_udalost:``) sa pri výpadku servera správ nezopakovalo; pri
udalosti bez miesta je to jediná správa o nej a zdravie hlásilo OK.
R5O1 – kontrola nastavení ignorovala tieňovú prevádzku (prázdny VLAKY_SHADOW_EMAILS = OK, chýbajúci
VLAKY_NOTIFY_EMAILS pri funkčnom pilote = trvalá chyba).
R5O2 – upozornenia na pracovný čas (13 h) a státie sa po chybe brány (5xx, časový limit) posielali znova v každom
cykle – rovnaká KRITICKÁ SMS každú minútu.
R5O4/R5U1 – KRITICKÉ upozornenie radilo „Vystriedať najneskôr o <čas, ktorý už uplynul>“.

Údaje sú fiktívne; SMTP je falošný server v procese, SMS brána je náhrada httpx.post (nič neodchádza mimo test).
"""
from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select

from test_revizia3_prevadzka3 import D1, D2, FAR, Relay, _drop, _eml, _setup
from vlaky import config as config_mod
from vlaky.agent import beh
from vlaky.agent.strazenie import crew_alerts
from vlaky.agent.zdravie import config_status, health_status, set_kv
from vlaky.cas import fmt, set_fake_now
from vlaky.db import session_scope
from vlaky.models import Driver, DriverShift, ExtractedEvent, Notification, Recommendation, Train
from vlaky.notifikacie import odosielanie

INCIDENT = ("Oznámenie o mimoriadnosti: výpadok napájania trakčného vedenia, prevádzka je zastavená "
            "od 02.11.2026 05:40 do 02.11.2026 12:00. FIKTÍVNE.")
ALERT = "[AGENT][DÔLEŽITÉ] Na potvrdenie čaká"


def _alert_rows() -> list:
    with session_scope() as s:
        return [(n.recipient, n.status) for n in s.scalars(
            select(Notification).where(Notification.dedup_key.like("neista_udalost:%")).order_by(Notification.id))]


# ------------------------------------------------------------------ R5O0
def test_r5o0_upozornenie_na_cakajucu_udalost_sa_po_vypadku_zopakuje(cfg, fixed_now, monkeypatch):
    """[R5O0] Mimoriadnosť bez miesta príde v cykle, keď server správ nefunguje: upozornenie „Na potvrdenie čaká …“
    agent zopakuje (každému dispečerovi práve raz) a kým nepríde, zdravie ho hlási ako nedoručené – aj keď tým istým
    dispečerom medzitým prišla iná správa."""
    relay = Relay()
    relay.down = True
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
    _drop(cfg, "mimoriadnost.eml", _eml("r5-c-neista-1@zsr.example", "Mimoriadnosť (FIKTÍVNE)", INCIDENT))
    beh.run_cycle(fixed_now)
    rows = _alert_rows()
    assert rows and {st for _r, st in rows} == {"chyba"}, rows  # predpoklad testu: upozornenie neodišlo
    relay.down = False
    set_fake_now(fixed_now + timedelta(minutes=2))
    with session_scope() as s:  # iná správa tým istým dispečerom prejde – upozornenie je stále nedoručené
        odosielanie.send_internal(s, "test", "[AGENT] Skúšobná správa", "FIKTÍVNE", "INFO", channels=["email"])
    ok, detail = health_status()
    assert not ok and "nedoručených" in detail["dorucenie"], detail
    for k in range(3, 14):
        t = fixed_now + timedelta(minutes=k)
        set_fake_now(t)
        beh.run_cycle(t)
    got = sorted(r for r, subject in relay.sent if subject.startswith(ALERT))
    assert got == [D1, D2], relay.sent  # každému práve raz
    _ok, detail = health_status()
    assert detail["dorucenie"] == "ok", detail


def test_r5o0_po_zamietnuti_udalosti_sa_upozornenie_neopakuje(cfg, fixed_now, monkeypatch):
    """[R5O0] Dispečer udalosť medzitým zamietol (alebo potvrdil): upozornenie už neplatí – neopakuje sa a zdravie ho
    nepočíta ako nedoručené."""
    from vlaky.posta.spracovanie import reject_event

    relay = Relay()
    relay.down = True
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
    _drop(cfg, "mimoriadnost.eml", _eml("r5-c-neista-2@zsr.example", "Mimoriadnosť (FIKTÍVNE)", INCIDENT))
    beh.run_cycle(fixed_now)
    assert {st for _r, st in _alert_rows()} == {"chyba"}
    with session_scope() as s:
        ev = s.scalars(select(ExtractedEvent).order_by(ExtractedEvent.id)).first()
        reject_event(s, ev.id, "dispecer.test", "FIKTÍVNE – netýka sa nás")
    relay.down = False
    for k in range(3, 14):
        t = fixed_now + timedelta(minutes=k)
        set_fake_now(t)
        beh.run_cycle(t)
    assert not [r for r, subject in relay.sent if subject.startswith(ALERT)], relay.sent
    _ok, detail = health_status()
    assert detail["dorucenie"] == "ok", detail


# ------------------------------------------------------------------ R5O1
def test_r5o1_tienova_prevadzka_bez_tienoveho_zoznamu_je_chyba(cfg, monkeypatch):
    """[R5O1] Tieňová prevádzka (ostrá) s prázdnym VLAKY_SHADOW_EMAILS: agent neodošle nič – je to chyba nastavenia,
    hoci VLAKY_NOTIFY_EMAILS je nastavený (v tieňovej prevádzke sa nepoužíva)."""
    for k, v in dict(env="prod", shadow_mode=True, shadow_emails="", smtp_host="relay.example",
                     notify_emails=D1).items():
        monkeypatch.setattr(cfg, k, v)
    assert not any(odosielanie.allowed_recipients().values())
    ok, text = config_status()
    assert ok is False and "VLAKY_SHADOW_EMAILS" in text and "neodošle nič" in text, text


def test_r5o1_tienovy_pilot_bez_zoznamu_dispecerov_je_v_poriadku(cfg, monkeypatch):
    """[R5O1] Tieňová prevádzka s tieňovým zoznamom a serverom správ, bez VLAKY_NOTIFY_*: správy chodia vedúcemu –
    žiadna trvalá chyba, len poznámka pre prechod do ostrej prevádzky."""
    for k, v in dict(env="prod", shadow_mode=True, shadow_emails="veduci@dopravca.example",
                     smtp_host="relay.example", notify_emails="", notify_phones="", teams_webhook_url="").items():
        monkeypatch.setattr(cfg, k, v)
    ok, text = config_status()
    assert ok is True and "neodošle nič" not in text and "VLAKY_NOTIFY_EMAILS" in text, text
    monkeypatch.setattr(cfg, "smtp_host", "")  # tieňový zoznam bez servera správ je chyba (ako v ostrej prevádzke)
    ok, text = config_status()
    assert ok is False and "server správ" in text, text


# ------------------------------------------------------------------ R5O2
class _Gateway:
    """SMS brána za proxy: správu doručí, no odpovie 504 (časový limit) – agent to berie ako výpadok."""

    def __init__(self) -> None:
        self.delivered: list = []

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.delivered.append((json or {}).get("to"))
        return httpx.Response(504, request=httpx.Request("POST", url))


def _shift(start, number: str = "96500") -> int:
    with session_scope() as s:
        t = Train(number=number, origin="Žilina", destination="Košice", status="v_jazde", traction="E")
        s.add(t)
        s.flush()
        d = Driver(name="Rušňovodič R (fiktívny)")
        s.add(d)
        s.flush()
        sh = DriverShift(driver_id=d.id, train_id=t.id, start_utc=start, status="prebieha")
        s.add(sh)
        s.flush()
        return sh.id


def test_r5o2_krititicka_sms_rusnovodica_sa_po_chybe_brany_neopakuje_kazdy_cyklus(cfg, fixed_now, monkeypatch):
    """[R5O2] Do limitu 13 h zostáva 25 min (KRITICKÉ – e-mail aj SMS). Brána SMS doručí, ale odpovie 504: agent pri
    cykloch každú minútu nepošle tú istú SMS každú minútu – zopakuje ju s odstupom ako ostatné KRITICKÉ správy (pri
    výpadku kanála v spoločnom 5-min okne). E-mail prišiel a ďalej sa nepotláča ako nový."""
    config_mod.override_settings(**{**cfg.model_dump(), "sms_gateway_url": "https://sms.example/api"})
    gw = _Gateway()
    monkeypatch.setattr(odosielanie.httpx, "post", gw.post)
    _shift(fixed_now - timedelta(hours=12, minutes=35))
    for minute in range(11):  # cykly agenta každú minútu (VLAKY_MAIL_POLL_SECONDS=60)
        now = fixed_now + timedelta(minutes=minute)
        set_fake_now(now)
        with session_scope() as s:
            crew_alerts(s, now)
    assert 1 <= len(gw.delivered) <= 3, gw.delivered  # pred opravou: 11× rovnaká KRITICKÁ SMS
    with session_scope() as s:
        mails = [n for n in s.scalars(select(Notification).where(Notification.channel == "email",
                                                                 Notification.dedup_key.like("crew:%")))
                 if n.status in odosielanie.SENT_STATES]
    assert sorted(n.recipient for n in mails) == [D1, D2]


def test_r5o2_dolezite_upozornenie_s_odstupom(cfg, fixed_now, monkeypatch):
    """[R5O2] send_internal(backoff=True): príjemcovi, ktorému posledný pokus zlyhal (nie výpadok – odmietnutie),
    sa správa zopakuje až po 5, potom 10 min – nie v každom volaní."""
    calls: list = []

    def refuse(recipient, subject, body):
        calls.append(recipient)
        raise RuntimeError("SMTP odmietol príjemcu (FIKTÍVNE)")

    monkeypatch.setattr(odosielanie, "_send_email", refuse)
    for minute in range(17):
        set_fake_now(fixed_now + timedelta(minutes=minute))
        with session_scope() as s:
            odosielanie.send_internal(s, "upozornenie", "[AGENT][DÔLEŽITÉ] Vlak 96501 stojí 25 min (FIKTÍVNE)",
                                      "FIKTÍVNE", "DÔLEŽITÉ", dedup_key="standing:1:202611020400",
                                      channels=["email"], backoff=True)
    # prvý pokus, po odstupe 5 min a po ďalších 10 min (s tolerancou 90 s pre správy toho istého kola) – každému
    # dispečerovi trikrát za 17 min, nie 17-krát
    assert sorted(calls) == sorted([D1, D2] * 3), calls


# ------------------------------------------------------------------ R5O4 / R5U1
def _alert_body(prefix: str) -> list:
    with session_scope() as s:
        return [n.body for n in s.scalars(select(Notification).where(Notification.dedup_key.like(prefix),
                                                                     Notification.channel == "email")
                                          .order_by(Notification.id))]


def test_r5o4_kriticke_upozornenie_na_rusnovodica_vystriedat_ihned(cfg, fixed_now):
    """[R5O4/R5U1] Pri 30 min do limitu (čas vystriedania – limit mínus odovzdanie rušňa – už uplynul) aj pri
    prekročení limitu znie pokyn „vystriedať ihneď (limit 13 h uplynie/uplynul o …)“ ako v odporúčaní a odovzdávke,
    nie „Vystriedať najneskôr o <minulý čas>“. Pri 90 min ostáva „Vystriedať najneskôr o …“."""
    start = fixed_now - timedelta(hours=12, minutes=37)  # do limitu zostáva 23 min
    sid = _shift(start)
    limit = start + timedelta(hours=13)
    with session_scope() as s:
        crew_alerts(s, fixed_now)
    body = _alert_body(f"crew:{sid}:30")[0]
    assert f"Vystriedať ihneď (limit 13 h uplynie o {fmt(limit)})." in body, body
    assert "najneskôr o" not in body, body
    later = fixed_now + timedelta(hours=3, minutes=30)
    set_fake_now(later)
    with session_scope() as s:
        crew_alerts(s, later)
    body = _alert_body(f"crew:{sid}:prekroceny")[0]
    assert f"Vystriedať ihneď (limit 13 h uplynul o {fmt(limit)})." in body and "najneskôr o" not in body, body
    early = _shift(fixed_now - timedelta(hours=11, minutes=35), number="96502")  # do limitu 85 min
    with session_scope() as s:
        crew_alerts(s, fixed_now)
    body = _alert_body(f"crew:{early}:90")[0]
    relieve = fixed_now - timedelta(hours=11, minutes=35) + timedelta(hours=12, minutes=30)
    assert f"Vystriedať najneskôr o: {fmt(relieve)} (vrátane 30 min na odovzdanie rušňa)" in body, body


def test_r5o4_odhad_bez_nastupu_vystriedat_ihned(cfg, fixed_now):
    """[R5O4/R5U1] Idúci vlak bez záznamu o nástupe (upozornenie podľa odhadu plánovača): 24 min do odhadovaného
    limitu – „Odhad: vystriedať ihneď (limit 13 h uplynie o …)“, nie „vystriedať najneskôr o <minulý čas>“."""
    limit = fixed_now + timedelta(minutes=24)
    start = limit - timedelta(hours=13)
    with session_scope() as s:
        t = Train(number="96703", origin="Žilina", destination="Košice", status="v_jazde", traction="E")
        s.add(t)
        s.flush()
        s.add(Recommendation(train_id=t.id, decision="POKRAČOVAŤ", color="ČERVENÁ", priority="KRITICKÉ",
                             summary="Vlak 96703: POKRAČOVAŤ (FIKTÍVNE).", state="nove",
                             crew={"unknown_start": True, "driver": "neznámy (chýba záznam o nástupe)",
                                   "start": start.isoformat(), "limit": limit.isoformat(),
                                   "relieve_by": (limit - timedelta(minutes=30)).isoformat(), "status": "ČERVENÁ",
                                   "max_duty_h": 13}))
    with session_scope() as s:
        crew_alerts(s, fixed_now)
    body = _alert_body("crew:odhad:%")[0]
    assert f"Odhad: vystriedať ihneď (limit 13 h uplynie o {fmt(limit)})." in body, body
    assert "najneskôr o" not in body, body
