"""Odovzdávka služby dispečerov (modul M12)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..cas import fmt, now_utc
from ..models import (Closure, Email, ExtractedEvent, Handover, Recommendation, ShiftHandoverReport, Train)
from ..planovanie.planovac import DONE_STATES


def _latest_rec(session: Session, train_id: int):
    return session.scalar(select(Recommendation).where(Recommendation.train_id == train_id)
                          .order_by(Recommendation.id.desc()))


def build_report(session: Session, now: datetime | None = None, hours: int = 12) -> tuple:
    now = now or now_utc()
    since = now - timedelta(hours=hours)
    trains = list(session.scalars(select(Train).where(Train.status.notin_(DONE_STATES)).order_by(Train.number)))
    lines = [f"ODOVZDÁVKA SLUŽBY – {fmt(now)}", ""]
    data: dict = {"trains": [], "waiting": 0, "quarantined": 0, "closures_new": 0}
    red, orange, crew_risk = [], [], []
    running = [t for t in trains if t.status == "v_jazde"]
    to_border = [t for t in trains if t.status == "na_ceste_k_hranici"]
    lines.append(f"Vlaky v jazde: {len(running)}; na ceste k hranici: {len(to_border)}; "
                 f"ostatné aktívne: {len(trains) - len(running) - len(to_border)}")
    for t in trains:
        rec = _latest_rec(session, t.id)
        entry = {"number": t.number, "status": t.status, "decision": rec.decision if rec else "",
                 "color": rec.color if rec else ""}
        data["trains"].append(entry)
        if rec and rec.color == "ČERVENÁ":
            red.append((t, rec))
        elif rec and rec.color == "ORANŽOVÁ":
            orange.append((t, rec))
        if rec and (rec.crew or {}).get("status") in ("ORANŽOVÁ", "ČERVENÁ"):
            crew_risk.append((t, rec))
    for title, items in (("ČERVENÉ vlaky (vyžadujú rozhodnutie)", red), ("ORANŽOVÉ vlaky", orange)):
        lines += ["", title + ":"]
        lines += [f"  - {r.summary}" for _t, r in items] or ["  (žiadne)"]
    lines += ["", "Rušňovodiči blízko limitu 13 h:"]
    lines += [f"  - vlak {t.number}: vystriedať najneskôr o "
              f"{fmt(datetime.fromisoformat(r.crew['relieve_by']))}" for t, r in crew_risk] or ["  (žiadni)"]
    handovers = list(session.scalars(select(Handover).where(Handover.status.in_(("ocakavany", "prisiel")))))
    lines += ["", "Očakávané preberania na hraniciach:"]
    lines += [f"  - vlak {h.train_id}: {h.border.handover_station if h.border else '?'} avízo "
              f"{fmt(h.avizo_eta_utc)} ({h.status})" for h in handovers] or ["  (žiadne)"]
    new_closures = list(session.scalars(select(Closure).where((Closure.created_at >= since) |
                                                              (Closure.updated_at >= since))
                                        .order_by(Closure.start_utc)))
    data["closures_new"] = len(new_closures)

    def _state(c) -> str:
        if c.status == "zrusene":
            return "ZRUŠENÁ"
        created = c.created_at.replace(tzinfo=since.tzinfo) if c.created_at.tzinfo is None else c.created_at
        kind = "nová" if created >= since else f"zmenená (verzia {c.version})"
        return f"{kind}, {'overená' if c.status == 'overene' else 'NEPOTVRDENÁ'}"

    lines += ["", f"Nové alebo zmenené výluky za {hours} h:"]
    lines += [f"  - {c.from_station} – {c.to_station} {fmt(c.start_utc)} – {fmt(c.end_utc)} ({_state(c)})"
              for c in new_closures] or ["  (žiadne)"]
    waiting = session.scalars(select(ExtractedEvent).where(ExtractedEvent.status == "navrh",
                                                           ExtractedEvent.needs_confirmation.is_(True))).all()
    quarantined = session.scalars(select(Email).where(Email.verdict == "karantena",
                                                      Email.received_at >= since)).all()
    data["waiting"], data["quarantined"] = len(waiting), len(quarantined)
    lines += ["", f"Nepotvrdené udalosti z e-mailov: {len(waiting)}",
              f"E-maily v karanténe za {hours} h: {len(quarantined)}",
              "", "Otvorené úlohy: doplní odovzdávajúci dispečer."]
    return "\n".join(lines), data


def create_report(session: Session, now: datetime | None = None, notify: bool = True) -> ShiftHandoverReport:
    content, data = build_report(session, now)
    rep = ShiftHandoverReport(content=content, data=data, created_at=now or now_utc())
    session.add(rep)
    session.flush()
    if notify:
        from ..notifikacie.odosielanie import send_internal

        send_internal(session, "odovzdavka", "[AGENT] Odovzdávka služby", content, "INFO",
                      dedup_key=f"odovzdavka:{rep.id}", channels=["email"])
    return rep
