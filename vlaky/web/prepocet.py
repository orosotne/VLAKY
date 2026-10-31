"""Prepočet vlakov vyvolaný z webu (po úprave vlaku, výluky, preberania, hlásení rušňovodiča, potvrdení e-mailu).

Rovnako ako cyklus agenta (vlaky/agent/beh.py) pošle upozornenie len k NOVÝM odporúčaniam (id väčšie ako pred
prepočtom); odosielacia služba duplicity potlačí podľa fingerprintu. Bez toho by odporúčanie vytvorené z webu
agent pri ďalšom cykle už neoznámil (fingerprint sa nezmenil).
"""
from __future__ import annotations

import importlib
from typing import Optional

from sqlalchemy import func, select

from ..db import session_scope
from ..models import Recommendation
from .common import log


def _planner():
    return importlib.import_module("vlaky.planovanie.planovac")


def _max_rec_id(session) -> int:
    return session.scalar(select(func.max(Recommendation.id))) or 0


def _notify_new(session, recs: list, before: int) -> int:
    fresh = [r for r in recs if r is not None and r.id and r.id > before]
    if not fresh:
        return 0
    try:
        notifier = importlib.import_module("vlaky.notifikacie.odosielanie")
        sent = 0
        for rec in fresh:
            sent += len(notifier.notify_recommendation(session, rec) or [])
        return sent
    except (ImportError, AttributeError, NotImplementedError):
        log.info("Odosielanie upozornení zatiaľ nie je k dispozícii")
    except Exception:  # noqa: BLE001 – odporúčanie zostáva uložené, upozornenie pošle agent/súhrn
        log.exception("Upozornenie k novému odporúčaniu sa nepodarilo odoslať")
    return 0


def evaluate_train_bg(train_id: int, trigger: str) -> None:
    """Prepočet jedného vlaku na pozadí. Chyby sa len zalogujú (web nesmie spadnúť)."""
    try:
        planovac = _planner()
        with session_scope() as s:
            before = _max_rec_id(s)
            rec = planovac.evaluate_train(s, train_id, trigger=trigger)
            _notify_new(s, [rec], before)
    except NotImplementedError:
        log.info("Plánovač zatiaľ nie je k dispozícii")
    except Exception:  # noqa: BLE001
        log.exception("Prepočet vlaku %s zlyhal", train_id)


def evaluate_all_bg(trigger: str) -> None:
    try:
        planovac = _planner()
        with session_scope() as s:
            before = _max_rec_id(s)
            recs = planovac.evaluate_all(s, trigger=trigger) or []
            _notify_new(s, list(recs), before)
    except NotImplementedError:
        log.info("Plánovač zatiaľ nie je k dispozícii")
    except Exception:  # noqa: BLE001
        log.exception("Prepočet všetkých vlakov zlyhal")


def evaluate_train_now(train_id: int, actor: str) -> tuple:
    """Synchrónny prepočet (tlačidlo „Prepočítať“). Vracia (dict odporúčania | None, chybová správa)."""
    try:
        planovac = _planner()
        with session_scope() as s:
            before = _max_rec_id(s)
            rec: Optional[Recommendation] = planovac.evaluate_train(s, train_id, trigger=f"ručný prepočet ({actor})")
            if rec is None:
                return None, ""
            _notify_new(s, [rec], before)
            return {"id": rec.id, "summary": rec.summary, "color": rec.color, "decision": rec.decision}, ""
    except NotImplementedError:
        return None, "Plánovač zatiaľ nie je k dispozícii (rozhranie sa dopĺňa)."
    except Exception as exc:  # noqa: BLE001
        log.exception("Prepočet vlaku %s zlyhal", train_id)
        return None, f"Prepočet zlyhal: {type(exc).__name__}: {str(exc)[:200]}"
