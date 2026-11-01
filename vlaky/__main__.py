"""Príkazový riadok Agenta dispečera.

  python -m vlaky init-db                     # vytvorí tabuľky a nahrá hraničné priechody
  python -m vlaky create-user MENO --role dispecer --password HESLO [--email ...] [--phone ...]
                                              # existujúci účet sa nezmení; zmena: --update (len zadané údaje)
  python -m vlaky import-osm --pbf slovakia.osm.pbf   # prebuduje sieť z OpenStreetMap
  python -m vlaky web [--host 0.0.0.0 --port 8000]    # webová aplikácia (mapa, fronta, odporúčania)
  python -m vlaky agent [--once]              # beh agenta (schránka, plánovanie, notifikácie)
  python -m vlaky demo [--reset]              # ukážkové (FIKTÍVNE) dáta + simulácia polohy
  python -m vlaky simulate                    # simulátor GPS polôh pre ukážkové vlaky
  python -m vlaky test-notify [--kanal ...]   # skúšobná správa na každý kanál (návratový kód 0 = všetko OK)
  python -m vlaky navrh-nasadenia             # návrh nasadenia rušňovodičov (CP-SAT, 13 h, odpočinok 11 h)
  python -m vlaky kalibracia [--aplikovat]    # návrh kalibrácie rezerv a dôb preberania z histórie
  python -m vlaky health                      # stav agenta (návratový kód 0 = OK)
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vlaky", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    cu = sub.add_parser("create-user", help="nový používateľ; existujúci sa bez --update nezmení")
    cu.add_argument("username")
    cu.add_argument("--role", default=None, choices=["dispecer", "veduci", "planovac", "obchod", "admin"],
                    help="rola (nový používateľ: predvolene dispecer)")
    cu.add_argument("--password", default=None,
                    help="heslo (bez neho sa vypýta, prípadne prečíta zo vstupu – nebude v histórii príkazov)")
    cu.add_argument("--full-name", default=None)
    cu.add_argument("--email", default=None)
    cu.add_argument("--phone", default=None)
    cu.add_argument("--update", action="store_true",
                    help="zmeniť EXISTUJÚCI účet – len zadané údaje (heslo, rola, meno, e-mail, telefón)")
    cu.add_argument("--activate", action="store_true", help="s --update: znova povoliť zrušený účet")
    io = sub.add_parser("import-osm")
    io.add_argument("--pbf", required=True)
    io.add_argument("--out", default=None)
    w = sub.add_parser("web")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    a = sub.add_parser("agent")
    a.add_argument("--once", action="store_true", help="jeden cyklus a koniec (napr. pre cron/test)")
    d = sub.add_parser("demo")
    d.add_argument("--reset", action="store_true")
    sub.add_parser("simulate")
    tn = sub.add_parser("test-notify", help="skúšobná správa na každý kanál interného zoznamu (e-mail, Teams, SMS)")
    tn.add_argument("--kanal", action="append", choices=["email", "teams", "sms"],
                    help="len tento kanál (dá sa zopakovať); predvolene všetky")
    sub.add_parser("navrh-nasadenia", help="návrh nasadenia rušňovodičov (CP-SAT) na najbližších 24 h")
    kal = sub.add_parser("kalibracia", help="návrh kalibrácie rezerv z histórie (bez zmeny)")
    kal.add_argument("--aplikovat", action="store_true", help="použiť návrh (len položky s dosť vzorkami)")
    kal.add_argument("--kto", default="veduci")
    sub.add_parser("health")
    args = ap.parse_args(argv)

    if args.cmd == "init-db":
        from .db import init_db, session_scope
        from .hranice import seed_borders

        init_db()
        with session_scope() as s:
            n = seed_borders(s)
        print(f"Databáza pripravená, hraničných priechodov: {n}")
        return 0
    if args.cmd == "create-user":
        return _create_user(args)
    if args.cmd == "import-osm":
        from .config import settings
        from .siet.osm_import import build

        meta = build(args.pbf, args.out or str(settings().data_dir / "siet"))
        print(meta)
        return 0
    if args.cmd == "web":
        import uvicorn

        from .config import settings

        uvicorn.run("vlaky.web.app:create_app", factory=True, host=args.host, port=args.port,
                    proxy_headers=True, forwarded_allow_ips=settings().forwarded_allow_ips,
                    timeout_graceful_shutdown=10)
        return 0
    if args.cmd == "agent":
        from .agent.beh import run_agent

        return run_agent(once=args.once)
    if args.cmd == "demo":
        from .simulacia.demo import seed_demo

        try:
            print(seed_demo(reset=args.reset))
        except RuntimeError as exc:
            print(f"CHYBA: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "simulate":
        from .simulacia.demo import run_simulator

        try:
            return run_simulator()
        except RuntimeError as exc:
            print(f"CHYBA: {exc}", file=sys.stderr)
            return 2
    if args.cmd == "test-notify":
        return _test_notify(args.kanal)
    if args.cmd == "navrh-nasadenia":
        import json

        from .db import init_db, session_scope
        from .planovanie.nasadenie import propose_driver_assignment

        init_db()
        with session_scope() as s:
            print(json.dumps(propose_driver_assignment(s), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "kalibracia":
        import json

        from .db import init_db, session_scope
        from .planovanie.kalibracia import apply_calibration, propose_calibration

        init_db()
        with session_scope() as s:
            prop = propose_calibration(s)
            print(json.dumps(prop, ensure_ascii=False, indent=1))
            if args.aplikovat:
                print(json.dumps(apply_calibration(s, prop, args.kto), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "health":
        from .agent.zdravie import health_status

        ok, detail = health_status()
        print(detail)
        return 0 if ok else 1
    return 2


def _read_password(args) -> str:
    """Heslo z --password, inak z výzvy (terminál) alebo z prvého riadku vstupu (skript) – nie v histórii príkazov."""
    if args.password is not None:
        return args.password
    if sys.stdin.isatty():
        import getpass

        first = getpass.getpass("Heslo: ")
        if getpass.getpass("Heslo znova: ") != first:
            raise ValueError("Heslá sa nezhodujú.")
        return first
    return sys.stdin.readline().rstrip("\r\n")


def _create_user(args) -> int:
    """create-user: nový účet; existujúci sa NIKDY potichu neprepíše (heslo, rola, kontakty, zrušený účet).

    Zmena existujúceho účtu len s --update a len údaje, ktoré sú zadané (zrušený účet znova povolí --activate)."""
    from .db import init_db, session_scope
    from .pouzivatelia import create_user, find_user, normalize_username, update_user

    try:
        username = normalize_username(args.username)
    except ValueError as exc:
        print(f"CHYBA: {exc}", file=sys.stderr)
        return 2
    init_db()
    with session_scope() as s:
        existing = find_user(s, username)
        if existing is not None and not args.update:
            state = "aktívny" if existing.active else "ZRUŠENÝ"
            print(f"CHYBA: Používateľ {username} už existuje ({existing.role}, {state}) – nič sa nezmenilo. "
                  f"Zmenu hesla, roly alebo kontaktov urobíte s --update (zmenia sa len zadané údaje), "
                  f"alebo vo webe (Používatelia).", file=sys.stderr)
            return 2
        if existing is None and args.update:
            print(f"CHYBA: Používateľ {username} neexistuje – nový účet vytvoríte bez --update.", file=sys.stderr)
            return 2
        if args.activate and not args.update:
            print("CHYBA: --activate patrí k --update (zmena existujúceho účtu).", file=sys.stderr)
            return 2
        try:
            if existing is None:
                password = _read_password(args)
                user = create_user(s, username, password, args.role or "dispecer", args.full_name or "",
                                   args.email or "", args.phone or "", actor="cli")
                print(f"Používateľ {username} ({user.role}) vytvorený.")
                return 0
            password = _read_password(args) if args.password is not None else None
            _user, changes = update_user(s, username, password=password, role=args.role, full_name=args.full_name,
                                         email=args.email, phone=args.phone, activate=args.activate, actor="cli")
        except ValueError as exc:
            s.rollback()
            print(f"CHYBA: {exc}", file=sys.stderr)
            return 2
    if not changes:
        print(f"Používateľ {username}: bez zmien (zadajte, čo sa má zmeniť: --password, --role, --full-name, "
              f"--email, --phone, --activate).")
    else:
        print(f"Používateľ {username} upravený: {', '.join(changes)}.")
    return 0


def _test_notify(channels: list | None) -> int:
    """test-notify: skúšobná správa na každý kanál interného zoznamu; návratový kód 1, ak niečo neodišlo."""
    from .db import init_db, session_scope
    from .notifikacie.odosielanie import outbox_counts_as_sent, send_test, transport_problems, unknown_env_vars

    init_db()
    with session_scope() as s:
        out, notes = send_test(s, channels)
        rows = [(n.channel, n.recipient, n.status, n.error or "") for n in out]
    problems = []
    for ch, recipient, status, error in rows:
        print(ch, recipient, status, error)
        if status in ("chyba", "zablokovana") or (status == "outbox" and not outbox_counts_as_sent()):
            problems.append(f"{ch} {recipient}: {status}")
    for note in notes:
        print(note)
    for problem in transport_problems():
        print(f"POZOR: {problem}")
    unknown = unknown_env_vars()
    if unknown:
        print(f"POZOR: neznáme premenné prostredia (preklep?): {', '.join(unknown)}")
    if any(status == "outbox" for _ch, _r, status, _e in rows) and outbox_counts_as_sent():
        print("Poznámka: stav „outbox“ = správa je len v adresári outbox (server správ nie je nastavený) – "
              "stačí len vo vývoji a v ukážke.")
    if not rows:
        problems.append("žiadny príjemca – nastavte interný zoznam (VLAKY_NOTIFY_EMAILS, v tieňovej prevádzke "
                        "VLAKY_SHADOW_EMAILS)")
    if problems:
        print("VÝSLEDOK: CHYBA – " + "; ".join(problems), file=sys.stderr)
        return 1
    print("VÝSLEDOK: OK – skontrolujte, či správa prišla na každý kanál.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
