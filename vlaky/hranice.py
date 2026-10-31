"""Register hraničných priechodov a preberacích staníc (modul M6)."""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import BorderCrossing

# Počiatočné odhady doby preberania [min] – kalibrovať z vlastnej histórie (M13).
_DEFAULT_DWELL = {"hlavny": (60.0, 150.0), "pravidelny": (60.0, 150.0), "obcasny": (90.0, 240.0),
                  "ziadny": (90.0, 240.0)}
_EXTERNAL_EU_BORDER_DWELL = (180.0, 420.0)


def seed_borders(session: Session) -> int:
    """Nahrá/aktualizuje priechody z data/hranice.json (ručne zmenené doby preberania nemení)."""
    path = settings().borders_path
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    try:
        from .siet.graf import network

        net = network()
    except Exception:  # sieť nie je k dispozícii (napr. minimálna inštalácia)
        net = None
    count = 0
    for item in data["priechody"]:
        bc = session.scalar(select(BorderCrossing).where(BorderCrossing.code == item["code"]))
        new = bc is None
        if new:
            bc = BorderCrossing(code=item["code"])
            p50, p90 = _EXTERNAL_EU_BORDER_DWELL if item["country"] == "UA" else \
                _DEFAULT_DWELL.get(item["freight"], (60.0, 150.0))
            bc.dwell_p50_min, bc.dwell_p90_min = p50, p90
            session.add(bc)
        for key in ("name", "country", "sk_station", "foreign_station", "handover_station", "handover_side",
                    "gauge", "traction", "freight", "notes", "source"):
            setattr(bc, key, item.get(key, ""))
        bc.active = item.get("freight") != "ziadny" or True
        if net is not None:
            st = net.find_station(item["sk_station"])
            if st:
                bc.lat, bc.lon = st.lat, st.lon
        count += 1
    session.flush()
    return count


def find_border(session: Session, text: str) -> BorderCrossing | None:
    """Nájde priechod podľa kódu, názvu alebo stanice (napr. „Rajka“, „Čadca“)."""
    from .siet.graf import norm

    if not text:
        return None
    q = norm(text)
    for bc in session.scalars(select(BorderCrossing)):
        names = [bc.code, bc.name, bc.sk_station, bc.foreign_station, bc.handover_station]
        if any(q == norm(n) for n in names if n):
            return bc
    for bc in session.scalars(select(BorderCrossing)):
        names = [bc.name, bc.sk_station, bc.foreign_station, bc.handover_station]
        if any(q in norm(n) for n in names if n):
            return bc
    return None
