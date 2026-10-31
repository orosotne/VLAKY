"""Kalibrácia rezerv a dôb preberania z histórie (fáza F4, kapitola 9 v docs/03).

Agent hodnoty NEMENÍ sám – pripraví návrh, ktorý schváli vedúci dispečingu (apply_calibration).

    propose_calibration(session, now=None, min_samples=30) -> dict
    apply_calibration(session, proposal, actor) -> dict
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import ensure_utc, now_utc
from ..config import settings
from ..models import BorderCrossing, Handover, ParameterSet, PositionReport, Recommendation, Train
from ..siet.graf import TrainSpec, network, norm


def _q(values: list, q: float) -> float:
    s = sorted(values)
    if not s:
        return math.nan
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def _actual_arrival(session: Session, train: Train) -> Optional[datetime]:
    """Skutočný príchod: prvá poloha v cieľovej dopravni (alebo do 1,5 km od nej)."""
    net = network()
    st = net.find_station(train.destination)
    rows = session.scalars(select(PositionReport).where(PositionReport.train_id == train.id)
                           .order_by(PositionReport.ts_utc))
    for r in rows:
        if r.nearest_station and st and norm(r.nearest_station) == norm(st.name):
            return ensure_utc(r.ts_utc)
        if st:
            from ..siet.graf import haversine

            if haversine(r.lat, r.lon, st.lat, st.lon) <= 1500:
                return ensure_utc(r.ts_utc)
    return None


def propose_calibration(session: Session, now: Optional[datetime] = None, min_samples: int = 30,
                        days: int = 120) -> dict:
    now = now or now_utc()
    since = now - timedelta(days=days)
    out: dict = {"created": now.isoformat(), "period_days": days, "min_samples": min_samples,
                 "borders": [], "delay": {}, "notes": []}
    # 1) doby preberania na hraniciach
    by_border: dict = {}
    for h in session.scalars(select(Handover).where(Handover.arrived_utc.is_not(None),
                                                    Handover.taken_over_utc.is_not(None),
                                                    Handover.arrived_utc >= since)):
        mins = (ensure_utc(h.taken_over_utc) - ensure_utc(h.arrived_utc)).total_seconds() / 60.0
        if 0 <= mins <= 48 * 60:
            by_border.setdefault(h.border_id, []).append(mins)
    for bid, vals in sorted(by_border.items()):
        bc = session.get(BorderCrossing, bid)
        if bc is None:
            continue
        item = {"border_id": bid, "code": bc.code, "name": bc.name, "n": len(vals),
                "p50": round(_q(vals, 0.5), 1), "p90": round(_q(vals, 0.9), 1),
                "current_p50": bc.dwell_p50_min, "current_p90": bc.dwell_p90_min,
                "enough": len(vals) >= min_samples}
        out["borders"].append(item)
    # 2) zdržanie na 100 km: skutočný príchod oproti prvému odporúčaniu (medián plánu)
    per100: list = []
    for t in session.scalars(select(Train).where(Train.status.in_(("ukonceny", "odovzdany")),
                                                 Train.updated_at >= since)):
        first = session.scalar(select(Recommendation).where(Recommendation.train_id == t.id,
                                                            Recommendation.decision.in_(
                                                                ("VYPRAVIŤ", "POČKAŤ", "ODSTAVIŤ", "PRESMEROVAŤ")))
                               .order_by(Recommendation.id))
        actual = _actual_arrival(session, t)
        if not first or not actual or not (first.chosen or {}).get("arrival_p50"):
            continue
        planned = datetime.fromisoformat(first.chosen["arrival_p50"])
        km = (first.path_proposal or {}).get("km")
        if not km:
            r = network().route(t.origin, t.destination, TrainSpec(traction=t.traction, gauge=t.gauge))
            km = r.length_m / 1000 if r else None
        if not km or km < 10:
            continue
        delay = (actual - planned).total_seconds() / 60.0
        per100.append(delay / km * 100.0)
    if per100:
        # medián plánu už obsahuje predvolený medián zdržania → pripočítať ho späť
        cfg = settings()
        p50 = max(0.5, cfg.default_delay_p50_min_per_100km + _q(per100, 0.5))
        p90 = max(p50 + 1.0, cfg.default_delay_p50_min_per_100km + _q(per100, 0.9))
        out["delay"] = {"n": len(per100), "p50_min_per_100km": round(p50, 1),
                        "p90_min_per_100km": round(p90, 1),
                        "current_p50": cfg.default_delay_p50_min_per_100km,
                        "current_p90": cfg.default_delay_p90_min_per_100km,
                        "enough": len(per100) >= min_samples}
    if not out["borders"] and not per100:
        out["notes"].append("Zatiaľ nie je dosť histórie (preberania s časom príchodu a prevzatia, ukončené "
                            "vlaky s polohou v cieli).")
    return out


def apply_calibration(session: Session, proposal: dict, actor: str, include_small: bool = False) -> dict:
    """Použije návrh (len položky s dostatkom vzoriek, ak include_small=False). Všetko je v audite."""
    applied = {"borders": [], "delay": None}
    for b in proposal.get("borders", []):
        if not (b.get("enough") or include_small):
            continue
        bc = session.get(BorderCrossing, b["border_id"])
        if bc is None:
            continue
        old = (bc.dwell_p50_min, bc.dwell_p90_min)
        bc.dwell_p50_min, bc.dwell_p90_min = b["p50"], max(b["p90"], b["p50"])
        applied["borders"].append({"code": bc.code, "old": old, "new": (bc.dwell_p50_min, bc.dwell_p90_min)})
    d = proposal.get("delay") or {}
    if d and (d.get("enough") or include_small):
        # základ = aktuálne platná sada (nie budúca naplánovaná), aby kalibrácia neaktivovala plánované hodnoty
        last = session.scalar(select(ParameterSet).where(ParameterSet.valid_from <= now_utc())
                              .order_by(ParameterSet.valid_from.desc(), ParameterSet.id.desc()))
        values = dict(last.values) if last and last.values else {}
        values["delay_p50_min_per_100km"] = d["p50_min_per_100km"]
        values["delay_p90_min_per_100km"] = d["p90_min_per_100km"]
        session.add(ParameterSet(values=values, created_by=actor, valid_from=now_utc(),
                                 note=f"Kalibrácia zdržaní z histórie (n={d['n']})"))
        applied["delay"] = {k: d[k] for k in ("p50_min_per_100km", "p90_min_per_100km", "n")}
    audit(session, actor, "calibration_applied", "", "", **applied)
    return applied


def delay_defaults(session: Session) -> tuple:
    """(p50, p90) zdržania na 100 km – kalibrované z ParameterSet, inak z nastavení."""
    cfg = settings()
    last = session.scalar(select(ParameterSet).where(ParameterSet.valid_from <= now_utc())
                          .order_by(ParameterSet.valid_from.desc(), ParameterSet.id.desc()))
    vals = (last.values or {}) if last else {}
    try:
        return (float(vals.get("delay_p50_min_per_100km", cfg.default_delay_p50_min_per_100km)),
                float(vals.get("delay_p90_min_per_100km", cfg.default_delay_p90_min_per_100km)))
    except (TypeError, ValueError):
        return cfg.default_delay_p50_min_per_100km, cfg.default_delay_p90_min_per_100km
