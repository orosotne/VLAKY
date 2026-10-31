"""Príkazový riadok Agenta dispečera.

  python -m vlaky init-db                     # vytvorí tabuľky a nahrá hraničné priechody
  python -m vlaky create-user MENO --role dispecer --password HESLO [--email ...] [--phone ...]
  python -m vlaky import-osm --pbf slovakia.osm.pbf   # prebuduje sieť z OpenStreetMap
  python -m vlaky web [--host 0.0.0.0 --port 8000]    # webová aplikácia (mapa, fronta, odporúčania)
  python -m vlaky agent [--once]              # beh agenta (schránka, plánovanie, notifikácie)
  python -m vlaky demo [--reset]              # ukážkové (FIKTÍVNE) dáta + simulácia polohy
  python -m vlaky simulate                    # simulátor GPS polôh pre ukážkové vlaky
  python -m vlaky test-notify                 # skúšobná správa na interný zoznam
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
    cu = sub.add_parser("create-user")
    cu.add_argument("username")
    cu.add_argument("--role", default="dispecer",
                    choices=["dispecer", "veduci", "planovac", "obchod", "admin"])
    cu.add_argument("--password", required=True)
    cu.add_argument("--full-name", default="")
    cu.add_argument("--email", default="")
    cu.add_argument("--phone", default="")
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
    sub.add_parser("test-notify")
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
        from .db import init_db, session_scope
        from .pouzivatelia import create_user

        init_db()
        with session_scope() as s:
            create_user(s, args.username, args.password, args.role, args.full_name, args.email, args.phone)
        print(f"Používateľ {args.username} ({args.role}) vytvorený.")
        return 0
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

        print(seed_demo(reset=args.reset))
        return 0
    if args.cmd == "simulate":
        from .simulacia.demo import run_simulator

        return run_simulator()
    if args.cmd == "test-notify":
        from .db import init_db, session_scope
        from .notifikacie.odosielanie import send_internal

        init_db()
        with session_scope() as s:
            out = send_internal(s, "test", "[AGENT] Skúšobná správa",
                                "Toto je skúšobná správa Agenta dispečera.", "INFO",
                                dedup_key="")
        for n in out:
            print(n.channel, n.recipient, n.status, n.error)
        return 0
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


if __name__ == "__main__":
    sys.exit(main())
