"""Regresné testy k 3. revízii prevádzky agenta (oblasť A) – nálezy z kontroly opráv.

Hromadná pošta nespustí lavínu falošných ORANŽOVÝCH správ a po dočítaní sa vlaky hneď prepočítajú (MS0), trvalo
chybný vlak nespôsobí dvojitý prepočet celého vozového parku (MS4), pripomienka zrušenej výluky do odvolania sa
neopakuje (MS5), check.sh bez Dockera radí príkazy systemd (OD6), create-user nájde starší účet s veľkými písmenami
a pri --update prečíta nové heslo z výzvy alebo zo vstupu (OD2).

Všetko FIKTÍVNE (adresy *.example); SMTP je atrapa v procese, nič neodchádza mimo testu.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.utils import format_datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from vlaky import config as config_mod
from vlaky.agent import beh
from vlaky.agent.zdravie import get_kv, health_status, set_kv
from vlaky.cas import local, now_utc, set_fake_now
from vlaky.db import session_scope
from vlaky.models import Closure, Driver, DriverShift, Notification, Path as TrainPath, Recommendation, Train, User
from vlaky.notifikacie import odosielanie

ROOT = Path(__file__).resolve().parent.parent
D1, D2 = "dispecer1@dopravca.example", "dispecer2@dopravca.example"
FAR = {"ts": "2100-01-01T00:00:00+00:00"}  # pravidelný prepočet sa v teste nespustí sám


# ------------------------------------------------------------------ pomôcky
class Relay:
    """Falošný SMTP server v procese: funguje, alebo odmieta spojenie (výpadok)."""

    def __init__(self) -> None:
        self.down, self.sent = False, []

    def smtp(self):
        relay = self

        class FakeSMTP:
            def __init__(self, host, port, timeout=30):
                if relay.down:
                    raise ConnectionRefusedError(111, "Connection refused")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self, context=None):
                pass

            def login(self, user, password):
                pass

            def send_message(self, msg, to_addrs=None):
                relay.sent.append((to_addrs[0], str(msg["Subject"])))
                return {}

        return FakeSMTP


def _setup(cfg, monkeypatch, relay: Relay | None = None, **kw):
    base = {**cfg.model_dump(), "teams_webhook_url": "", "daily_report_hour": 23, "shift_change_hours": "3"}
    if relay is not None:
        base.update(smtp_host="relay.example", smtp_starttls=False)
        monkeypatch.setattr(odosielanie.smtplib, "SMTP", relay.smtp())
    base.update(kw)
    return config_mod.override_settings(**base)


def _eml(mid: str, subject: str, body: str, sender: str = "vyluky.orke@zsr.example") -> bytes:
    head = (f"From: {Header('ŽSR OR Košice (FIKTÍVNE)', 'utf-8').encode()} <{sender}>\r\n"
            f"To: agent.dispecing@dopravca.example\r\nSubject: {Header(subject, 'utf-8').encode()}\r\n"
            f"Date: {format_datetime(local(now_utc()))}\r\nMessage-ID: <{mid}>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=\"utf-8\"\r\nContent-Transfer-Encoding: 8bit\r\n\r\n")
    return head.encode("ascii") + body.replace("\n", "\r\n").encode("utf-8")


def _drop(cfg, name: str, data: bytes) -> None:
    path = Path(cfg.mail_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    ts = now_utc().timestamp()
    os.utime(path, (ts, ts))  # čas doručenia = čas vloženia do schránky


def _rov(ref: str, a: str, b: str, start: datetime, end: datetime) -> tuple:
    def f(dt: datetime) -> str:
        return local(dt).strftime("%d.%m.%Y %H:%M")

    subject = f"FIKTÍVNE – ukážka: Rozkaz o výluke (ROV) č. {ref} – výluka {a} – {b}"
    body = (f"Vážení dopravcovia,\n\nFIKTÍVNE – ukážka (nejde o skutočný rozkaz ŽSR).\n\n"
            f"oznamujeme Vám, že na základe Rozkazu o výluke (ROV) č. {ref} bude vykonaná výluka:\n\n"
            f"Úsek: {a} – {b}\nDruh výluky: úplná výluka trate\nTermín: od {f(start)} do {f(end)}\n"
            f"Dôvod výluky: oprava mosta\n\nS pozdravom\nOR Košice – prevádzka (FIKTÍVNE)\n")
    return subject, body


def _backlog(cfg, closure_start: datetime, count: int = 60) -> None:
    """Nahromadená pošta: výluka Kysak – Košice (trasa vlaku 95121) v prvej dávke a informačné e-maily za ňou."""
    subject, body = _rov("2701/2026", "Kysak", "Košice", closure_start, closure_start + timedelta(hours=5))
    _drop(cfg, "a_000_rov2701.eml", _eml("rov-2701@zsr.example", subject, body))
    for i in range(1, count):
        _drop(cfg, f"a_{i:03d}.eml", _eml(f"info-{i}@zsr.example", f"FIKTÍVNE – informácia {i}",
                                          f"FIKTÍVNE – ukážka. Informácia č. {i}: bez vplyvu na prevádzku.\n",
                                          sender="info.orke@zsr.example"))


def _train(s, number: str, origin: str, dest: str, ready: datetime, path_dep: datetime,
           shift_start: datetime) -> int:
    t = Train(number=number, run_date=local(ready).date().isoformat(), origin=origin, destination=dest,
              customer="Zákazník (fiktívny)", traction="E", gauge="1435", vmax_kmh=100, length_m=500,
              weight_t=1600, status="planovany", ready_utc=ready, deadline_utc=ready + timedelta(hours=10),
              notes="FIKTÍVNY")
    s.add(t)
    s.flush()
    s.add(TrainPath(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena", departure_utc=path_dep,
                    note="FIKTÍVNA trasa"))
    driver = Driver(name=f"Rušňovodič {number} (fiktívny)", phone="")
    s.add(driver)
    s.flush()
    s.add(DriverShift(driver_id=driver.id, train_id=t.id, start_utc=shift_start, status="planovana"))
    s.flush()
    return t.id


def _three_trains(base: datetime) -> tuple:
    """A 95121 (výluka Kysak – Košice je na jeho trase), B 95122 a C 95123 (mimo výluky)."""
    with session_scope() as s:
        a = _train(s, "95121", "Žilina", "Košice", base + timedelta(minutes=30), base + timedelta(minutes=40),
                   base + timedelta(minutes=10))
        b = _train(s, "95122", "Čadca", "Žilina", base + timedelta(minutes=30), base + timedelta(minutes=40),
                   base + timedelta(minutes=10))
        c = _train(s, "95123", "Žilina", "Čadca", base + timedelta(minutes=50), base + timedelta(minutes=60),
                   base + timedelta(minutes=30))
    return a, b, c


def _latest(s, train_id: int) -> Recommendation:
    return s.scalar(select(Recommendation).where(Recommendation.train_id == train_id)
                    .order_by(Recommendation.id.desc()))


# ------------------------------------------------------------------ MS0 hromadná pošta bez lavíny ORANŽOVÝCH správ
@pytest.mark.parametrize("vypadok", [False, True], ids=["bezna_prevadzka", "po_vypadku_agenta"])
def test_hromadna_posta_zelene_vlaky_bez_falosnej_oranzovej_a_hned_po_dociatani(cfg, fixed_now, monkeypatch,
                                                                                vypadok):
    """[MS0, kontrola opráv] Hromadná pošta (viac ako jedna dávka) s výlukou pre vlak A v prvej dávke – pri bežnej
    prevádzke aj po 3 h výpadku agenta. Kým sa schránka dočítava, vlak A nedostane ZELENÚ (výluka sa mu oznámi
    hneď), ale zelené vlaky B a C nedostanú správu „ORANŽOVÁ“ len preto, že sa schránka ešte dočítava; v cykle,
    v ktorom sa schránka dočíta, sú B a C hneď znova ZELENÉ (nie až o 15 min pravidelným prepočtom). Nový vlak D
    dostane prvé odporúčanie hneď (bez ZELENEJ) – odložiť sa smie len zhoršenie pri rovnakom rozhodnutí."""
    relay = Relay()
    _setup(cfg, monkeypatch, relay)
    t0 = fixed_now
    resume = t0 + (timedelta(hours=3) if vypadok else timedelta(minutes=1))
    a, b, c = _three_trains(resume)
    beh.run_cycle(t0)  # bežná prevádzka: schránka prázdna a dočítaná, vlaky dostali odporúčanie
    with session_scope() as s:
        assert [_latest(s, tid).color for tid in (a, b, c)] == ["ZELENÁ"] * 3
        # nový vlak bez odporúčania – jeho prvé odporúčanie počas dočítavania sa pošle hneď (nesmie sa stratiť)
        d = _train(s, "95124", "Žilina", "Púchov", resume + timedelta(minutes=40), resume + timedelta(minutes=50),
                   resume + timedelta(minutes=20))
    sent_before = len(relay.sent)

    set_fake_now(resume - timedelta(minutes=1))
    _backlog(cfg, resume + timedelta(hours=2))
    set_fake_now(resume)
    first = beh.run_cycle(resume)
    assert first["mail"]["fetched"] == 50 and first["mail"]["drained"] is False
    with session_scope() as s:
        rec_a = _latest(s, a)
        assert rec_a.color != "ZELENÁ" and any("Kysak" in r for r in rec_a.reasons), (rec_a.decision, rec_a.color)
        assert _latest(s, d).color != "ZELENÁ"
        assert get_kv(s, "mail_last_ok").get("partial") is True and not get_kv(s, "mail_last_ok").get("ts")
    assert any("95121" in subject for _r, subject in relay.sent[sent_before:])  # výluka pre A sa oznámi hneď
    assert any("95124" in subject for _r, subject in relay.sent[sent_before:])  # prvé odporúčanie D tiež
    others = [subject for _r, subject in relay.sent[sent_before:] if "95122" in subject or "95123" in subject]
    assert others == [], others  # B a C: žiadna správa „ORANŽOVÁ“ len preto, že sa schránka dočítava
    assert first.get("held_back") == 2  # zhoršenie počká na dočítanie
    if vypadok:  # posledná úplná kontrola pred 3 h – zdravie aj cyklus to hlásia
        assert any(e.startswith("schránka: dočítava sa") and "viac ako 30 min" in e for e in first["errors"])
    else:        # krátke dočítavanie hneď po úplnej kontrole nie je poplach
        assert not any(e.startswith("schránka") for e in first["errors"]), first["errors"]

    t2 = resume + timedelta(seconds=10)
    set_fake_now(t2)
    second = beh.run_cycle(t2)
    assert second["mail"]["drained"] is True and second["evaluated"] == 4
    with session_scope() as s:
        assert _latest(s, b).color == "ZELENÁ" and _latest(s, c).color == "ZELENÁ"
        assert _latest(s, b).trigger == "schránka dočítaná"
        assert get_kv(s, "last_evaluate").get("neuplna_schranka") is False
    burst = relay.sent[sent_before:]
    assert not [subject for _r, subject in burst if "95122" in subject or "95123" in subject], burst


def test_dlhe_dociatavanie_zhorsenie_neodklada_donekonecna(cfg, fixed_now, monkeypatch):
    """[MS0, kontrola opráv] Zhoršenie ZELENÁ → ORANŽOVÁ počas dočítavania počká najviac VLAKY_REEVALUATE_MINUTES od
    začiatku dočítavania – keď schránka dočítaná nie je ani potom, dispečeri sa to dozvedia (údaje nie sú čerstvé)."""
    relay = Relay()
    _setup(cfg, monkeypatch, relay)
    t0 = fixed_now
    t1 = t0 + timedelta(minutes=20)
    a, b, c = _three_trains(t1)
    beh.run_cycle(t0)
    sent_before = len(relay.sent)
    with session_scope() as s:  # dočítavanie trvá už 19 min (napr. pomalý jazykový model)
        set_kv(s, "mail_last_ok", {"partial": True, "since": (t0 + timedelta(minutes=1)).isoformat(),
                                   "checked": (t1 - timedelta(minutes=1)).isoformat(), "last_full": t0.isoformat(),
                                   "fetched": 50})
    set_fake_now(t1 - timedelta(minutes=1))
    _backlog(cfg, t1 + timedelta(hours=2))
    set_fake_now(t1)
    out = beh.run_cycle(t1)
    assert out["mail"]["drained"] is False and not out.get("held_back")
    with session_scope() as s:
        assert _latest(s, b).color == "ORANŽOVÁ"
    assert [r for r, subject in relay.sent[sent_before:] if "95122" in subject] == [D1, D2]


# ------------------------------------------------------------------ MS4 trvalo chybný vlak
def test_chybny_vlak_po_emaile_sa_prepocita_sam_nie_cely_vozovy_park(cfg, fixed_now, monkeypatch):
    """[MS4, kontrola opráv] Prepočet po e-maile zlyhá len pri jednom vlaku: agent v tom istom cykle znova prepočíta
    len tento vlak (trvalo chybný vlak nezdvojnásobí prepočet celého vozového parku v každom cykle s e-mailom);
    chyba sa hlási v cykle a vlak má jeden záznam o chybe výpočtu. Prechodná chyba sa opakovaním opraví hneď."""
    from vlaky.planovanie import planovac

    _setup(cfg, monkeypatch)
    t0 = fixed_now
    with session_scope() as s:
        good = _train(s, "95101", "Žilina", "Košice", t0 + timedelta(minutes=30), t0 + timedelta(minutes=40),
                      t0 + timedelta(minutes=10))
        bad = _train(s, "95102", "Čadca", "Žilina", t0 + timedelta(minutes=30), t0 + timedelta(minutes=40),
                     t0 + timedelta(minutes=10))
    calls: list = []
    failing = {"always": True, "once": False}
    real = planovac.evaluate_train

    def flaky(session, train_id, now=None, trigger=""):
        calls.append(train_id)
        if train_id == bad and (failing["always"] or failing["once"]):
            failing["once"] = False
            raise ValueError("chybné údaje vlaku (FIKTÍVNE)")
        return real(session, train_id, now, trigger)

    monkeypatch.setattr(planovac, "evaluate_train", flaky)
    beh.run_cycle(t0)
    t1 = t0 + timedelta(minutes=3)
    set_fake_now(t1)
    subject, body = _rov("2201/2026", "Kysak", "Košice", t0 + timedelta(hours=2), t0 + timedelta(hours=7))
    _drop(cfg, "rov2201.eml", _eml("rov-2201@zsr.example", subject, body))
    with session_scope() as s:
        set_kv(s, "last_evaluate", {"ts": t1.isoformat(), "count": 2})  # pravidelný prepočet nie je na rade
    before = len(calls)
    out = beh.run_cycle(t1)
    assert out["mail"]["planning"]["ok"] is True
    assert sorted(calls[before:]) == sorted([good, bad, bad]), calls[before:]  # nie 2 × celý vozový park
    assert any(e.startswith("plánovanie: 1 vlak") for e in out["errors"]), out["errors"]
    with session_scope() as s:
        failures = s.scalar(select(func.count(Recommendation.id)).where(Recommendation.train_id == bad,
                                                                       Recommendation.decision == "ESKALOVAŤ"))
    assert failures == 1

    failing.update(always=False, once=True)  # prechodná chyba (napr. zámok databázy) – opakovanie ju opraví
    t2 = t1 + timedelta(minutes=2)
    set_fake_now(t2)
    subject, body = _rov("2202/2026", "Kraľovany", "Ružomberok", t0 + timedelta(hours=3), t0 + timedelta(hours=6))
    _drop(cfg, "rov2202.eml", _eml("rov-2202@zsr.example", subject, body))
    out = beh.run_cycle(t2)
    assert not any(e.startswith("plánovanie") for e in out["errors"]), out["errors"]
    with session_scope() as s:
        assert _latest(s, bad).decision != "ESKALOVAŤ"


# ------------------------------------------------------------------ MS5 pripomienka zrušenej výluky
def test_pripomienka_zrusenej_vyluky_do_odvolania_sa_neopakuje(cfg, fixed_now, monkeypatch):
    """[MS5, kontrola opráv] Pripomienka „… do odvolania stále platí“ sa pri výpadku servera správ neodošle a dispečer
    medzitým výluku zruší: po obnovení servera ju agent už nepošle a zdravie ju nepočíta ako nedoručenú."""
    relay = Relay()
    relay.down = True
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
        closure = Closure(kind="pomala_jazda", from_station="Žilina", to_station="Púchov",
                          start_utc=fixed_now - timedelta(days=25), end_utc=fixed_now + timedelta(days=3),
                          status="overene", source="email", speed_kmh=30, until_revoked=True,
                          external_ref="TLG 77/2026", note="Platí do odvolania.")
        s.add(closure)
        s.flush()
        cid = closure.id
    beh.run_cycle(fixed_now)
    with session_scope() as s:
        rows = [n.status for n in s.scalars(select(Notification).where(
            Notification.dedup_key.like("do_odvolania:%")))]
        assert rows and set(rows) == {"chyba"}
        s.get(Closure, cid).status = "zrusene"  # ŽSR obmedzenie odvolala, dispečer ho zrušil v registri
    relay.down = False
    for k in range(2, 12):
        t = fixed_now + timedelta(minutes=k)
        set_fake_now(t)
        beh.run_cycle(t)
    assert not [r for r, subject in relay.sent if "do odvolania" in subject], relay.sent
    _ok, detail = health_status()
    assert detail["dorucenie"] == "ok", detail


# ------------------------------------------------------------------ OD6 check.sh bez Dockera – rady pre systemd
def _stub(folder: Path, name: str, script: str) -> Path:
    path = folder / name
    path.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash nie je k dispozícii")
def test_check_sh_rady_podla_sposobu_instalacie(tmp_path):
    """[OD6, kontrola opráv] HTTPS nedostupné a certifikát čoskoro vyprší: bez Dockera (systemd) check.sh radí
    systemctl/journalctl, nie príkazy docker compose (tie tam nie sú); pri Dockeri ostávajú docker compose."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "systemctl", "exit 0\n")
    _stub(bin_dir, "docker", 'if [ "$1" = compose ]; then echo kontajner-1; else echo healthy; fi\n')
    _stub(bin_dir, "curl", "echo 000\n")
    soon = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%b %d %H:%M:%S %Y GMT")
    _stub(bin_dir, "openssl", f'if [ "$1" = x509 ]; then echo "notAfter={soon}"; else cat >/dev/null; fi\n')
    backups = tmp_path / "zalohy"
    (backups / "db").mkdir(parents=True)
    (backups / "db" / "vlaky-db-20261101T000000Z.sql.gz").write_bytes(b"x")
    py_ok = _stub(tmp_path, "py_ok", "echo \"{'stav': 'OK'}\"\nexit 0\n")
    env_file = tmp_path / "vlaky.env"
    env_file.write_text("VLAKY_PUBLIC_URL=https://agent.dopravca.example\n", encoding="utf-8")

    def run(mode: str) -> str:
        ops = tmp_path / "ops.env"
        values = {"DEPLOY_MODE": mode, "VLAKY_APP_DIR": str(ROOT), "BACKUP_DIR": str(backups),
                  "CHECK_DISK_PATHS": "/", "CHECK_DISK_MAX_PCT": "100", "CHECK_ENV_FILE": str(env_file),
                  "CHECK_PYTHON": str(py_ok), "RESTIC_REPOSITORY": "s3:https://zalohy.example/vlaky"}
        ops.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("VLAKY_")}
        env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", OPS_ENV_FILE=str(ops), NO_COLOR="1")
        r = subprocess.run(["bash", str(ROOT / "deploy" / "monitoring" / "check.sh")], env=env,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 1, r.stdout
        return r.stdout

    out = run("systemd")
    lines = [ln for ln in out.splitlines() if ln.startswith("CHYBA")]
    unreachable = next(ln for ln in lines if "je nedostupné" in ln)
    cert = next(ln for ln in lines if "certifikát pre agent.dopravca.example vyprší" in ln)
    assert "systemctl status caddy vlaky-web" in unreachable and "journalctl -u caddy" in cert
    assert "docker" not in out, out
    out = run("docker")
    assert "docker compose ps; docker compose logs --tail 100 caddy web" in out
    assert "docker compose logs caddy | grep -i acme" in out


# ------------------------------------------------------------------ OD2 create-user
def test_create_user_najde_starsi_ucet_s_velkymi_pismenami(cfg, capsys):
    """[OD2, kontrola opráv] Starší účet „Admin“ (vytvorený pred normalizáciou mien, prihlasuje sa ako „Admin“):
    create-user „admin“ ho nezduplikuje a --update zmení práve tento účet."""
    from vlaky.__main__ import main
    from vlaky.pouzivatelia import authenticate, hash_password

    with session_scope() as s:
        s.add(User(username="Admin", password_hash=hash_password("Stare-Heslo-2026"), role="admin", full_name="",
                   email="", phone="", active=True))
    assert main(["create-user", "admin", "--password", "Nove-Heslo-2026-fiktivne"]) == 2
    assert "Admin už existuje" in capsys.readouterr().err
    assert main(["create-user", "Admin", "--update", "--password", "Nove-Heslo-2026-fiktivne"]) == 0
    assert main(["create-user", "ADMIN", "--update", "--full-name", "Správca (FIKTÍVNY)"]) == 0
    with session_scope() as s:
        assert [tuple(r) for r in s.execute(select(User.username, User.role, User.full_name))] == \
            [("Admin", "admin", "Správca (FIKTÍVNY)")]
        assert authenticate(s, "Admin", "Nove-Heslo-2026-fiktivne") is not None


def test_create_user_update_nove_heslo_z_vyzvy_alebo_vstupu(cfg, capsys, monkeypatch):
    """[OD2, kontrola opráv] create-user --update --password bez hodnoty prečíta nové heslo z výzvy alebo z prvého
    riadku vstupu (nie z príkazového riadku – nebude v histórii); --update bez --password heslo nemení."""
    from vlaky.__main__ import main
    from vlaky.pouzivatelia import authenticate

    assert main(["create-user", "novy3", "--password", "Prve-Heslo-2026-fiktivne"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("Nove-Heslo-Zo-Vstupu-2026\n"))
    assert main(["create-user", "novy3", "--update", "--password"]) == 0
    assert "upravený: heslo" in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO("Ine-Heslo-Zo-Vstupu-2026\n"))
    assert main(["create-user", "novy3", "--update", "--role", "veduci"]) == 0
    with session_scope() as s:
        assert authenticate(s, "novy3", "Nove-Heslo-Zo-Vstupu-2026") is not None
        assert s.scalar(select(User.role).where(User.username == "novy3")) == "veduci"
    monkeypatch.setattr(sys, "stdin", io.StringIO("kratke\n"))
    assert main(["create-user", "novy3", "--update", "--password"]) == 2
    assert "aspoň 10 znakov" in capsys.readouterr().err
