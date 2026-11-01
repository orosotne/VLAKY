"""Regresné testy k nálezom tretej revízie webu (oblasť W3).

UI0/WR5 – obmedzený (plánovač) alebo neaktuálny (agent stojí) semafor sa na webe nikde neukáže ako ZELENÁ: varianta
„odporúčaná“, okná bezpečného odchodu, história odporúčaní ani upozornenia na nástenke.
UI1 – detail odporúčania pri výpadku agenta: poznámka o neaktuálnych dátach pri každej farbe, zostávajúci pracovný
čas rušňovodiča a vek dát k teraz (nie zmrazené čísla z prepočtu).
UI7 – slovenské texty webu: kódy stavov a akcií, desatinná čiarka, limit 13 h, kotva vlaku bez polohy, parameter 18 h.
WR6 – prihlásenie s prázdnym heslom: limit pokusov mimo slučky udalostí, spoločný rozpočet, bez prechodu všetkých
záznamov limitu.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import timedelta

import httpx
from sqlalchemy import select

from test_web_spolocne import HESLO, login_as, make_recommendation, make_train, make_user

pytest_plugins = ["test_web_spolocne"]


# ------------------------------------------------------------------ pomôcky
def _section(html: str, heading: str) -> str:
    """Karta (section) s nadpisom h2 – od nadpisu po koniec karty."""
    i = html.find(f">{heading}</h2>")
    assert i >= 0, f"Na stránke chýba časť {heading!r}"
    return html[i:html.find("</section>", i)]


def _header(html: str) -> str:
    i = html.find('<section class="decision')
    assert i >= 0
    return html[i:html.find("</section>", i)]


def _semafors(fragment: str) -> list:
    return re.findall(r"semafor-(\w+)", fragment)


def _windows_informative(html: str) -> bool:
    """Okná odchodu obmedzeného semaforu nie sú „bezpečné“: iný nadpis, poznámka „len informatívne“, bez zvýraznenia."""
    windows = _section(html, "Okná odchodu podľa výluk – len informatívne")
    return ">Okná bezpečného odchodu</h2>" not in html and "Semafor je obmedzený" in windows \
        and "<strong>" not in windows


def _set_agent(at, mailbox_at=None) -> None:
    """Posledný cyklus agenta a posledná úspešná kontrola schránky (agent potom stojí)."""
    from vlaky.agent.zdravie import set_kv
    from vlaky.db import session_scope

    with session_scope() as s:
        set_kv(s, "agent_last_cycle", {"ts": at.isoformat()})
        set_kv(s, "mail_last_ok", {"ts": (mailbox_at or at).isoformat()})


def _green_after_cancelled_closure(t0):
    """Ako v náleze WR5: s výlukou prvé odporúčanie, po jej zrušení aktuálne odporúčanie ZELENÁ (varianty, okná)
    a jeho e-mail dispečerom (upozornenie na nástenke)."""
    from vlaky.db import session_scope
    from vlaky.hranice import seed_borders
    from vlaky.models import Closure
    from vlaky.notifikacie.odosielanie import notify_recommendation
    from vlaky.planovanie.planovac import evaluate_train

    with session_scope() as s:
        seed_borders(s)
        s.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry",
                      start_utc=t0 + timedelta(hours=1), end_utc=t0 + timedelta(hours=8), status="overene"))
    _set_agent(t0)
    tid = make_train(number="64001", status="planovany", origin="Žilina", destination="Košice",
                     ready_utc=t0 + timedelta(hours=1), deadline_utc=t0 + timedelta(hours=30))
    with session_scope() as s:
        evaluate_train(s, tid, t0, trigger="test: s výlukou")
        c = s.scalar(select(Closure))
        c.status, c.version = "zrusene", c.version + 1
    with session_scope() as s:
        rec = evaluate_train(s, tid, t0 + timedelta(minutes=5), trigger="test: výluka zrušená")
        sent = notify_recommendation(s, rec)
        assert rec.color == "ZELENÁ" and (rec.chosen or {}).get("status") == "ZELENÁ"
        assert any(rec.windows.values()) and sent, "predpoklad testu: zelené odporúčanie s oknami a e-mailom"
        return tid, rec.id


# ------------------------------------------------------------------ UI0/WR5: agent stojí – nikde ZELENÁ
def test_detail_bez_cerstvych_dat_agenta_nikde_zelena(client, fixed_now):
    from vlaky.cas import set_fake_now

    t0 = fixed_now
    _tid, rid = _green_after_cancelled_closure(t0)
    login_as(client, "d301")
    set_fake_now(t0 + timedelta(minutes=6))  # dáta čerstvé: ZELENÁ všade (bez falošného poplachu)
    page = client.get(f"/odporucania/{rid}").text
    assert _semafors(_header(page))[0] == "zelena"
    assert "zelena" in _semafors(_section(page, "Porovnanie variantov"))
    assert "len informatívne" not in _section(page, "Okná bezpečného odchodu").lower()
    assert "Údaje agenta nie sú čerstvé" not in page

    set_fake_now(t0 + timedelta(hours=2))  # agent 2 h neprebehol a nečítal schránku
    page = client.get(f"/odporucania/{rid}").text
    head = _header(page)
    assert "zelena" not in _semafors(head) and "Údaje agenta nie sú čerstvé" in head
    variants = _section(page, "Porovnanie variantov")
    chosen_row = re.search(r'<tr class="chosen".*?</tr>', variants, re.S)
    assert chosen_row and "zelena" not in _semafors(chosen_row.group(0))
    assert "zelena" not in _semafors(variants)
    assert _windows_informative(page)
    history = _section(page, "História odporúčaní pre vlak")
    current = re.search(r"<li><strong>.*?</strong></li>", history, re.S)
    assert current and "zelena" not in _semafors(current.group(0))
    # vek dát k teraz (nie pri prepočte): schránka pred 120 min, nie „pred 5 min“
    card = _section(page, "Istota a čerstvosť dát")
    mailbox = re.search(r"Posledná kontrola schránky</dt>\s*<dd>(.*?)</dd>", card, re.S).group(1)
    assert mailbox.startswith("pred 2 h 00 min") and "pri prepočte pred 5 min" in mailbox

    # upozornenia na nástenke: e-mail „… (ZELENÁ)“ aktuálneho odporúčania sa neukáže ako zelený
    items = [a for a in client.get("/api/upozornenia").json()["upozornenia"] if a.get("href") == f"/odporucania/{rid}"]
    assert items and all("(ZELENÁ)" not in a["text"] and "bez čerstvých údajov agenta" in a["text"] for a in items)
    dash = client.get("/").text
    alerts = dash[dash.find('id="upozornenia"'):]
    alerts = alerts[:alerts.find("</ul>")]
    assert f"/odporucania/{rid}" in alerts and "(ZELENÁ)" not in alerts


# ------------------------------------------------------------------ UI0: semafor obmedzený plánovačom
def test_idúci_vlak_bez_polohy_a_nastupu_varianty_ani_okna_nie_su_zelene(client, fixed_now):
    from vlaky.db import session_scope
    from vlaky.planovanie.planovac import evaluate_train

    t0 = fixed_now
    tid = make_train(number="95779", status="v_jazde", origin="Žilina", destination="Košice",
                     planned_departure_utc=t0 - timedelta(minutes=90), ready_utc=t0 - timedelta(minutes=90),
                     deadline_utc=t0 + timedelta(hours=8))
    with session_scope() as s:
        rec = evaluate_train(s, tid, t0, trigger="test: idúci vlak bez polohy a bez nástupu rušňovodiča")
        rid = rec.id
        assert rec.color == "ORANŽOVÁ" and rec.chosen.get("status") == "ZELENÁ", (rec.color, rec.chosen)
        windows = any(rec.windows.values())
    login_as(client, "d302")
    page = client.get(f"/odporucania/{rid}").text
    assert "Údaje agenta nie sú čerstvé" not in page  # agent beží – obmedzil plánovač, nie vek dát
    variants = _section(page, "Porovnanie variantov")
    assert _semafors(variants) and "zelena" not in _semafors(variants)
    assert "obmedzený" in variants
    if windows:
        assert _windows_informative(page)


def test_schranka_stara_pri_prepocte_varianty_nie_su_zelene_ani_po_kontrole(client, fixed_now):
    """Plánovač obmedzil ZELENÚ (schránka 120 min) – aj keď sa schránka medzitým skontrolovala, okná a varianty
    toho odporúčania sa neukážu ako zelené (sú z výpočtu bez aktuálnych výluk)."""
    from vlaky.db import session_scope
    from vlaky.models import Path, Train
    from vlaky.planovanie.planovac import evaluate_train

    t0 = fixed_now
    _set_agent(t0, mailbox_at=t0 - timedelta(minutes=120))
    with session_scope() as s:
        t = Train(number="77010", origin="Plaveč", destination="Košice", traction="M", gauge="1435", vmax_kmh=80,
                  status="planovany", ready_utc=t0 + timedelta(hours=3), deadline_utc=t0 + timedelta(hours=12))
        s.add(t)
        s.flush()
        s.add(Path(train_id=t.id, train_number="77010", kind="ad_hoc", status="pridelena",
                   departure_utc=t0 + timedelta(hours=3)))
        s.flush()
        rec = evaluate_train(s, t.id, t0, trigger="test: schránka 120 min")
        rid = rec.id
        assert rec.color == "ORANŽOVÁ" and rec.chosen.get("status") == "ZELENÁ", (rec.color, rec.chosen)
        assert any(rec.windows.values())
    _set_agent(t0)  # agent schránku medzitým prečítal, vlak ešte neprepočítal
    login_as(client, "d303")
    page = client.get(f"/odporucania/{rid}").text
    assert "zelena" not in _semafors(_header(page))
    assert "zelena" not in _semafors(_section(page, "Porovnanie variantov"))
    assert _windows_informative(page)


def test_neobmedzena_oranzova_ukaze_drahsiu_zelenu_variantu_obmedzena_nie(client, fixed_now):
    """Bez falošného poplachu: ORANŽOVÁ zvolená pre nižší náklad (plánovač semafor neobmedzil) – drahšia ZELENÁ
    varianta a jej okná sú skutočne zelené. S obmedzením (schránka 95 min pri prepočte) už nie."""
    now = fixed_now
    chosen = {"route": "hlavná", "depart": (now + timedelta(hours=1)).isoformat(), "kind": "POČKAŤ",
              "status": "ORANŽOVÁ", "p_on_time": 0.85, "p_hold": 0.0, "expected_cost": 900,
              "arrival_p50": (now + timedelta(hours=4)).isoformat(),
              "arrival_p90": (now + timedelta(hours=5)).isoformat(), "reasons": ["P(včas) 85 % < 90 %"]}
    green = dict(chosen, depart=(now + timedelta(hours=6)).isoformat(), kind="ODSTAVIŤ", status="ZELENÁ",
                 p_on_time=0.97, expected_cost=1400, reasons=[])
    crew = {"driver": "Ján FIKTÍVNY", "start": now.isoformat(), "limit": (now + timedelta(hours=13)).isoformat(),
            "left_min": 780, "status": "ZELENÁ"}
    windows = {"hlavná": [[(now + timedelta(hours=6)).isoformat(), (now + timedelta(hours=7)).isoformat()]]}
    common = dict(color="ORANŽOVÁ", decision="POČKAŤ", chosen=chosen, variants=[chosen, green], windows=windows,
                  crew=crew)
    plain = make_recommendation(make_train(number="61001"), data_age={"position_min": 1.0, "mailbox_min": 3.0},
                                **common)
    capped = make_recommendation(make_train(number="61002"), data_age={"position_min": 1.0, "mailbox_min": 95.0},
                                 **common)
    login_as(client, "d304")
    page = client.get(f"/odporucania/{plain}").text
    assert "zelena" in _semafors(_section(page, "Porovnanie variantov"))
    assert "len informatívne" not in _section(page, "Okná bezpečného odchodu").lower()
    page = client.get(f"/odporucania/{capped}").text
    assert "zelena" not in _semafors(_section(page, "Porovnanie variantov"))
    assert _windows_informative(page)


# ------------------------------------------------------------------ UI1: výpadok agenta – časy k teraz
def test_detail_pri_vypadku_agenta_pocita_pracovny_cas_k_teraz(client, fixed_now):
    from vlaky.cas import set_fake_now
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift, PositionReport
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.siet.graf import network

    t0 = fixed_now
    _set_agent(t0)
    st = network().find_station("Poprad-Tatry")
    tid = make_train(number="95004", status="v_jazde", origin="Žilina", destination="Košice",
                     planned_departure_utc=t0 - timedelta(hours=9), deadline_utc=t0 + timedelta(hours=8))
    with session_scope() as s:
        d = Driver(name="Rušňovodič FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=tid, start_utc=t0 - timedelta(hours=10, minutes=30),
                          status="prebieha"))
        s.add(PositionReport(train_id=tid, train_number="95004", ts_utc=t0 - timedelta(minutes=1), lat=st.lat,
                             lon=st.lon, source="gps"))
        s.flush()
        rec = evaluate_train(s, tid, t0, trigger="test: rušňovodič 10,5 h v službe")
        rid, color = rec.id, rec.color
        assert rec.crew.get("left_min") == 150  # pri prepočte 2 h 30 min do limitu 13 h
    login_as(client, "d305")
    set_fake_now(t0 + timedelta(minutes=160))  # agent stojí 160 min, limit rušňovodiča už uplynul
    page = client.get(f"/odporucania/{rid}").text
    head = _header(page)
    assert "Údaje agenta nie sú čerstvé" in head, color  # pri každej farbe, nielen pri ZELENEJ
    crew = page[page.find('id="h-crew"'):]
    crew = crew[:crew.find("</section>")]
    left = re.search(r"Zostáva do limitu</dt><dd>(.*?)</dd>", crew, re.S).group(1)
    assert left.startswith("−10 min"), left  # 150 − 160 min, nie zmrazené „2 h 30 min“
    state = re.search(r"Stav</dt><dd>(.*?)</dd>", crew, re.S).group(1)
    assert _semafors(state) == ["cervena"], state
    card = _section(page, "Istota a čerstvosť dát")
    mailbox = re.search(r"Posledná kontrola schránky</dt>\s*<dd>(.*?)</dd>", card, re.S).group(1)
    assert mailbox.startswith("pred 2 h 40 min"), mailbox
    position = re.search(r"Poloha vlaku</dt>\s*<dd>(.*?)</dd>", card, re.S).group(1)
    assert position.startswith("stará 2 h 41 min") and "neistá" in position, position
    # detail vlaku: poznámka o neaktuálnych dátach agenta tiež pri každej farbe
    assert "Údaje agenta nie sú čerstvé" in client.get(f"/vlaky/{tid}").text


# ------------------------------------------------------------------ UI7: slovenské texty
def test_slovenske_texty_webu(client, fixed_now):
    from vlaky.audit import audit
    from vlaky.db import session_scope
    from vlaky.models import Email, Path

    now = fixed_now
    tid = make_train(number="62001", status="planovany", origin="Žilina", destination="Košice")
    rid = make_recommendation(tid, color="ORANŽOVÁ", crew={"driver": "Ján FIKTÍVNY", "status": "ORANŽOVÁ",
                                                            "start": now.isoformat(), "max_duty_h": 13.0,
                                                            "limit": (now + timedelta(hours=13)).isoformat()},
                              path_proposal={"route": "Žilina – Košice", "km": 110.1, "needs_new_path": False,
                                             "stations": [{"name": "Žilina", "type": "station", "km": 0.6,
                                                           "arr": None, "dep": now.isoformat()},
                                                          {"name": "Košice", "type": "station", "km": 110.1,
                                                           "arr": (now + timedelta(hours=2)).isoformat(),
                                                           "dep": None}]})
    with session_scope() as s:
        s.add(Path(train_id=tid, train_number="62001", kind="ad_hoc", status="pridelena",
                   departure_utc=now + timedelta(hours=1)))
        audit(s, "agent", "recommendation", "train", tid)
        audit(s, "rusnovodic:62001", "hlasenie_problem", "train", tid, typ="problem")
        s.add(Email(message_id="<t-62001@zsr.example>", sender="dispecing@zsr.example", subject="Výluka",
                    body_text="FIKTÍVNE", status="spracovany", received_at=now))
    login_as(client, "veduci62", "veduci")
    detail = client.get(f"/odporucania/{rid}").text
    assert "(110,1 km)" in detail and "<td>0,6</td>" in detail and "110.1" not in detail
    assert "limit 13 h)" in detail and "13.0 h" not in detail
    train = client.get(f"/vlaky/{tid}").text
    assert "ad hoc trasa – pridelená" in train and "ad_hoc – pridelena" not in train
    assert "nové odporúčanie agenta" in train and "rušňovodič vlaku 62001" in train
    assert "hlásenie: problém" in train and "rusnovodic:62001</strong>" not in train
    reports = client.get("/reporty", params={"od": now.date().isoformat(), "do": now.date().isoformat()}).text
    assert "spracovaný: 1" in reports and "spracovany: 1" not in reports
    settings_page = client.get("/nastavenia").text
    label = re.search(r'<label for="par-max_hold_h">(.*?)</label>', settings_page, re.S).group(1)
    assert "PPŽS" in label and "max_hold_h" not in label
    health = client.get("/zdravie").text
    summary = health[health.find("Celkový stav"):]
    summary = summary[:summary.find("</section>")]
    assert "Posledná kontrola schránky" in summary and "Doručovanie správ" in summary
    assert "<dt>schranka</dt>" not in summary and "<dt>dorucenie</dt>" not in summary
    # vlak bez polohy na mape: „zobrazené pri východiskovej stanici Žilina“ (nie „východisková stanica“)
    row = next(r for r in client.get("/api/vlaky").json()["vlaky"] if r["id"] == tid)
    assert row["anchor"]["label"] == "pri východiskovej stanici Žilina"



# ------------------------------------------------------------------ WR6: prázdne heslo
async def _post_login(app, data: dict, client_addr):
    transport = httpx.ASGITransport(app=app, client=client_addr)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies={"vlaky_login": "t" * 32}) as ac:
        return await ac.post("/login", data={"csrf_token": "t" * 32, "next": "/", **data})


def test_prazdne_heslo_zapise_neuspech_mimo_slucky_udalosti(web_app):
    lim = web_app.state.login_limiter
    on_loop: list = []
    real = lim.fail

    def fail(key):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(key)

    lim.fail = fail

    async def burst():
        return await asyncio.gather(*[_post_login(web_app, {"username": "nikto", "password": ""},
                                                  (f"2001:db8:{i + 1:x}::1", 1)) for i in range(5)])

    assert [r.status_code for r in asyncio.run(burst())] == [401] * 5
    assert on_loop == [False] * 5  # zápis do limitu v threadpoole, nie v slučke udalostí


def test_prazdne_hesla_maju_spolocny_rozpocet(web_app):
    from vlaky.web.auth import LoginLimiter

    make_user("dispecer63")
    lim = web_app.state.login_limiter = LoginLimiter(empty_burst=10, empty_per_s=0.0)
    counted: list = []
    real = lim.fail
    lim.fail = lambda key: counted.append(key) or real(key)

    async def flood():  # z meniacich sa sietí /64 – limit adresy nezasiahne, zasiahne spoločný rozpočet
        return await asyncio.gather(*[_post_login(web_app, {"username": f"x{i}" if i % 2 else "", "password": ""},
                                                  (f"2001:db8:{i + 1:x}::1", 1)) for i in range(30)])

    codes = sorted(r.status_code for r in asyncio.run(flood()))
    assert codes == [401] * 10 + [429] * 20, codes
    assert len(counted) == 10  # nad rozpočet bez zápisu do limitu
    ok = asyncio.run(_post_login(web_app, {"username": "dispecer63", "password": HESLO}, ("198.51.100.9", 1)))
    assert ok.status_code == 303  # riadne prihlásenie rozpočet neobmedzuje


def test_limiter_pri_mnohych_blokovanych_klucoch_bez_prechodu_vsetkych():
    from vlaky.web.auth import LoginLimiter

    lim = LoginLimiter(max_failures=5, window_s=900, max_keys=200)
    for i in range(600):  # 600 sietí /64, každá zablokovaná (5 neúspechov)
        for _ in range(5):
            lim.fail(f"ip:2001:db8:{i:x}::/64")
    assert lim.retry_after("ip:2001:db8:257::/64") > 0  # aj nový útočník pri plnej pamäti je zablokovaný
    assert len(lim._fails) <= 2 * 200  # pamäť ohraničená aj pri samých blokovaných záznamoch
    big = LoginLimiter(max_failures=5, window_s=900, max_keys=10000)
    t0 = time.perf_counter()
    for i in range(24000):
        for _ in range(5):
            big.fail(f"ip:k{i}")
    elapsed = time.perf_counter() - t0
    assert len(big._fails) <= 20000 and big.retry_after("ip:k23999") > 0
    assert elapsed < 10, elapsed  # 120 000 neúspechov bez prechodu všetkých záznamov pri každom (inak minúty)
