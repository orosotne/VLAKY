"""Odosielanie upozornení a odporúčaní (ROZHRANIE – implementácia sa dopĺňa).

ZÁVÄZNÉ (sekcia 2.2 PROMPT.md): správy smú ísť LEN na interný zoznam z konfigurácie
(VLAKY_NOTIFY_EMAILS / VLAKY_NOTIFY_PHONES / VLAKY_TEAMS_WEBHOOK_URL; v tieňovej prevádzke len
VLAKY_SHADOW_EMAILS). Iného príjemcu odosielacia služba odmietne (stav "zablokovana").

    notify_recommendation(session, rec) -> list[Notification]
    send_internal(session, kind, subject, body, priority="INFO", dedup_key="") -> list[Notification]
    allowed_recipients() -> dict[str, set[str]]
"""
from __future__ import annotations


def notify_recommendation(session, rec):  # pragma: no cover - doplní sa
    raise NotImplementedError


def send_internal(session, kind, subject, body, priority="INFO", dedup_key=""):  # pragma: no cover
    raise NotImplementedError


def allowed_recipients():  # pragma: no cover - doplní sa
    raise NotImplementedError
