"""Auditný záznam (kto, kedy, čo, prečo) – sekcia 0 pravidlo 5 v PROMPT.md."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog


def audit(session: Session, actor: str, action: str, entity: str = "", entity_id: Any = "",
          **detail: Any) -> AuditLog:
    rec = AuditLog(actor=actor or "system", action=action, entity=entity,
                   entity_id=str(entity_id) if entity_id is not None else "", detail=_jsonable(detail))
    session.add(rec)
    return rec


def _jsonable(value: Any) -> Any:
    from datetime import datetime

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
