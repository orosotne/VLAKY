"""Štvrtá revízia – fronta na potvrdenie po stranách a odkaz z upozornenia na udalosť (oblasť M4, nález QC7).

Bez LLM čaká na potvrdenie každá udalosť z pravidiel – jeden súborný rozkaz (SROV) s 55 riadkami zaplní celú stranu
fronty. Novšia naliehavá udalosť (zrušenie pridelenej trasy z KIS) musí byť na stránke dosiahnuteľná: krátka položka
s kotvou #udalost-N na prvej strane, celý formulár na ďalšej strane a odkaz z upozornenia DÔLEŽITÉ, ktorý ju ukáže
s formulárom navrchu fronty. Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from test_web_spolocne import csrf, login_as
from vlaky.hranice import seed_borders
from vlaky.models import ExtractedEvent, Notification, Path as TrainPath, Train

pytest_plugins = ["test_web_spolocne"]

AR = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=zsr.example; dkim=pass\n"
      " header.d=zsr.example; dmarc=pass header.from=zsr.example\n")
PAIRS = [("Kysak", "Košice"), ("Margecany", "Kysak"), ("Štrba", "Poprad-Tatry"), ("Ružomberok", "Liptovský Mikuláš"),
         ("Kraľovany", "Ružomberok"), ("Púchov", "Považská Bystrica"), ("Bytča", "Žilina"), ("Leopoldov", "Trnava"),
         ("Galanta", "Šaľa"), ("Nové Zámky", "Šurany"), ("Trenčín", "Nové Mesto nad Váhom")]


@pytest.fixture()
def env(cfg, session, fixed_now, monkeypatch):
    """Overenie odosielateľa ako v prevádzke; plánovač podvrhnutý (test overuje frontu, nie odporúčania)."""
    from vlaky.planovanie import planovac

    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    monkeypatch.setattr(planovac, "evaluate_all", lambda session, now=None, trigger="": [])
    seed_borders(session)
    session.commit()
    return cfg


def _mail(mid: str, subject: str, body: str, sender: str, extra: str = "") -> bytes:
    hdr = (f"{AR}{extra}From: {sender}\nTo: agent.dispecing@dopravca.example\nSubject: {subject}\n"
           f"Date: Mon, 02 Nov 2026 04:50:00 +0100\nMessage-ID: <{mid}@zsr.example>\nMIME-Version: 1.0\n"
           "Content-Type: text/plain; charset=\"utf-8\"\nContent-Transfer-Encoding: 8bit\n")
    return (hdr + "\n" + body).replace("\n", "\r\n").encode("utf-8")


def _srov(number: int) -> bytes:
    """Súborný ROV s 55 riadkami (11 úsekov × 5 dní) – 55 udalostí vo fronte."""
    rows, n = [], 0
    for d in range(23, 28):
        for a, b in PAIRS:
            n += 1
            rows.append(f"{n:<4}| 1. TK | {a} – {b} | {d}.11.2026 08:00 | {d}.11.2026 14:00 | výluka koľaje")
    return _mail(f"srov{number}", f"SROV č. {number}/2026 – výluky 23.11. – 27.11.2026", f"Zasielame SROV č. "
                 f"{number}/2026:\n\nPor.č. | Koľaj | Úsek | Začiatok | Koniec | Druh výluky\n" + "\n".join(rows) + "\n",
                 "ZSR OR Kosice <vyluky.orke@zsr.example>")


def _cards(page: str) -> list[int]:
    return [int(x) for x in re.findall(r'<article class="card queue-item" id="udalost-(\d+)"', page)]


def test_qc7_fronta_po_stranach_a_odkaz_z_upozornenia(env, session, fixed_now, client):
    """QC7: SROV s 55 riadkami (55 čakajúcich udalostí) a potom zrušenie pridelenej trasy z KIS (#56). Prvá strana
    ukáže 50 celých formulárov, stránkovanie a krátku položku #56 (kotva, odkaz na formulár, zamietnutie); odkaz
    z upozornenia DÔLEŽITÉ ukáže #56 s formulárom navrchu fronty; druhá strana má celý formulár #56; po zamietnutí
    odkaz povie, ako bola udalosť spracovaná."""
    from vlaky.posta.spracovanie import process_mailbox

    dep = fixed_now + timedelta(hours=3)
    t = Train(number="12345", origin="Čadca", destination="Žilina", traction="E", gauge="1435", vmax_kmh=90,
              status="planovany", run_date="2026-11-02", ready_utc=dep - timedelta(minutes=30),
              deadline_utc=dep + timedelta(hours=5))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number="12345", kind="ad_hoc", status="pridelena", departure_utc=dep,
                          points=[]))
    session.commit()
    (Path(env.mail_dir) / "srov.eml").write_bytes(_srov(50))
    process_mailbox(session, now=fixed_now - timedelta(minutes=5))
    session.commit()
    assert len(list(session.scalars(select(ExtractedEvent)))) == 55
    (Path(env.mail_dir) / "kis.eml").write_bytes(_mail(
        "kis-q7", "KIS – zrušenie trasy vlaku Pn 12345 na deň 02.11.2026", "Typ správy: Zrušenie trasy\nVlak: Pn 12345\n"
        "Deň jazdy: 02.11.2026\n\nTrasa vlaku Pn 12345 na deň 02.11.2026 bola zrušená.\n",
        "KIS <kis-noreply@zsr.example>", extra="Auto-Submitted: auto-generated\n"))
    process_mailbox(session, now=fixed_now)
    session.commit()
    kis = session.scalars(select(ExtractedEvent).where(ExtractedEvent.kind == "trasa_zrusena")).one()
    alerts = list(session.scalars(select(Notification).where(Notification.kind == "upozornenie")))
    assert alerts and all(f"/fronta/udalost/{kis.id}" in a.body for a in alerts), [a.body for a in alerts]

    login_as(client, "dispecer")
    page = client.get("/fronta").text
    full = _cards(page)
    assert len(full) == 50 and kis.id not in full
    assert "Strana 1 z 2" in page and "/fronta?strana=2" in page
    assert page.count(f'id="udalost-{kis.id}"') == 1                       # krátka položka s kotvou
    assert f'href="/fronta?udalost={kis.id}#udalost-{kis.id}"' in page
    assert f'action="/fronta/{kis.id}/zamietnut"' in page

    r = client.get(f"/fronta/udalost/{kis.id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/fronta?udalost={kis.id}#udalost-{kis.id}"
    focus = client.get(r.headers["location"]).text
    assert _cards(focus)[0] == kis.id and focus.count(f'id="udalost-{kis.id}"') == 1
    assert f'action="/fronta/{kis.id}/potvrdit"' in focus and "z odkazu" in focus

    second = client.get("/fronta?strana=2").text
    assert kis.id in _cards(second) and len(_cards(second)) == 6 and "Strana 2 z 2" in second

    r = client.post(f"/fronta/{kis.id}/zamietnut", data={"csrf_token": csrf(client, "/fronta"), "dovod": "trasa platí"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.get(f"/fronta/udalost/{kis.id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/fronta"
    after = client.get("/fronta").text
    assert "už bola spracovaná" in after and "zamietnutá" in after
    assert client.get("/fronta/udalost/999999", follow_redirects=True).status_code == 200


def test_qc7_navrat_po_chybe_potvrdenia_ukaze_formular(env, session, fixed_now, client):
    """QC7: po chybe potvrdenia (napr. „Opravte polia“) sa dispečer vráti na celý formulár udalosti, aj keď nie je na
    prvej strane fronty (návrat na /fronta#udalost-N by ukázal len krátku položku)."""
    from vlaky.posta.spracovanie import process_mailbox

    (Path(env.mail_dir) / "srov.eml").write_bytes(_srov(51))
    process_mailbox(session, now=fixed_now)
    session.commit()
    last = max(e.id for e in session.scalars(select(ExtractedEvent)))
    login_as(client, "dispecer")
    assert last not in _cards(client.get("/fronta").text)
    r = client.post(f"/fronta/{last}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "upravit",
                                                     "event_kind": "neznamy_druh"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/fronta?udalost={last}#udalost-{last}"
    page = client.get(r.headers["location"]).text
    assert _cards(page)[0] == last and f'action="/fronta/{last}/potvrdit"' in page
