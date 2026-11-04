"""Prepočet vlakov vyvolaný z webu (po úprave vlaku, výluky, preberania, hlásení rušňovodiča, potvrdení e-mailu).

Rovnako ako cyklus agenta (vlaky/agent/beh.py) pošle upozornenie len k NOVÝM odporúčaniam – tým, ktoré uložil tento
prepočet (id väčšie ako pred prepočtom a vložené touto reláciou); odosielacia služba duplicity potlačí podľa
fingerprintu. Odporúčanie, ktoré prepočet vrátil, lebo ho medzitým uložil súbežný prepočet agenta (rovnaký odtlačok
alebo novší začiatok prepočtu), oznámi agent vo svojom cykle (aj so zdržaním zhoršenia počas dočítavania pošty).

Prepočet sa najprv potvrdí (commit) – zámky prepočtu vlakov (planovac._serialize_train) sa uvoľnia hneď po výpočte
a upozornenia idú až potom, každé odporúčanie vo vlastnej transakcii pod zámkom doručovania vlaku
(odosielanie.serialize_train_delivery): nedostupný server správ tak nezdrží prepočet agenta ani tlačidlo „Prepočítať“
a súbežný cyklus agenta to isté odporúčanie druhý raz nepošle. Celá dávka má jeden istič (delivery_scope) – mŕtvy
server správ stojí jeden časový limit na kanál, nie jeden za každé odporúčanie.
"""
from __future__ import annotations

import importlib
from typing import Optional

from sqlalchemy import event, func, select

from ..db import session_scope
from ..models import Recommendation
from .common import log

_CREATED = "vlaky_web_created_recs"  # session.info: id odporúčaní, ktoré táto relácia vložila


def _planner():
    return importlib.import_module("vlaky.planovanie.planovac")


def _max_rec_id(session) -> int:
    """Najvyššie id odporúčania pred prepočtom; zároveň začne sledovať odporúčania, ktoré relácia vloží."""
    created = session.info.setdefault(_CREATED, set())

    def after_flush(sess, _ctx) -> None:
        created.update(o.id for o in sess.new if isinstance(o, Recommendation) and o.id)

    if not session.info.get(_CREATED + "_listen"):
        event.listen(session, "after_flush", after_flush)
        session.info[_CREATED + "_listen"] = True
    return session.scalar(select(func.max(Recommendation.id))) or 0


def _fresh_ids(session, recs: list, before: int) -> list:
    """Čísla odporúčaní, ktoré uložil tento prepočet (id väčšie ako ``before`` a vložené touto reláciou), bez
    opakovania. Odporúčanie, ktoré prepočet vrátil, lebo ho medzitým uložil súbežný prepočet (agent), oznámi ten, kto
    ho uložil – agent vo svojom cykle."""
    created = session.info.get(_CREATED) or set()
    return list(dict.fromkeys(r.id for r in recs if r is not None and r.id and r.id > before and r.id in created))


def _notify_new(rec_ids: list) -> int:
    """Upozornenia k novým odporúčaniam – volá sa až po potvrdení prepočtu (bez zámkov prepočtu vlakov).

    Každé odporúčanie vo vlastnej transakcii: notify_recommendation ho pod zámkom doručovania vlaku načíta znova
    (zastarané medzitým alebo rozhodnuté dispečerom sa nepošle) a pošle len tým, ktorým ešte neprišlo. Chyba jedného
    odporúčania nezastaví ostatné – odporúčanie zostáva uložené, upozornenie pošle agent (aktuálne nedoručené
    odporúčania) alebo súhrn."""
    if not rec_ids:
        return 0
    try:
        notifier = importlib.import_module("vlaky.notifikacie.odosielanie")
    except ImportError:
        notifier = None
    notify = getattr(notifier, "notify_recommendation", None)
    scope = getattr(notifier, "delivery_scope", None)
    if notify is None or scope is None:
        log.info("Odosielanie upozornení zatiaľ nie je k dispozícii")
        return 0
    sent = 0
    with scope():  # jeden istič pre celú dávku
        for rec_id in rec_ids:
            try:
                with session_scope() as s:
                    rec = s.get(Recommendation, rec_id)
                    if rec is not None:
                        sent += len(notify(s, rec) or [])
            except NotImplementedError:
                log.info("Odosielanie upozornení zatiaľ nie je k dispozícii")
                return sent
            except Exception:  # noqa: BLE001 – odporúčanie zostáva uložené, upozornenie pošle agent/súhrn
                log.exception("Upozornenie k novému odporúčaniu #%s sa nepodarilo odoslať", rec_id)
    return sent


def evaluate_train_bg(train_id: int, trigger: str) -> None:
    """Prepočet jedného vlaku na pozadí. Chyby sa len zalogujú (web nesmie spadnúť)."""
    try:
        planovac = _planner()
        with session_scope() as s:
            before = _max_rec_id(s)
            rec = planovac.evaluate_train(s, train_id, trigger=trigger)
            fresh = _fresh_ids(s, [rec], before)
        _notify_new(fresh)  # až po potvrdení prepočtu
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
            fresh = _fresh_ids(s, list(recs), before)
        _notify_new(fresh)  # až po potvrdení prepočtu – zámky vlakov sú uvoľnené
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
            out = {"id": rec.id, "summary": rec.summary, "color": rec.color, "decision": rec.decision}
            fresh = _fresh_ids(s, [rec], before)
        _notify_new(fresh)  # až po potvrdení prepočtu (zámok vlaku je uvoľnený)
        return out, ""
    except NotImplementedError:
        return None, "Plánovač zatiaľ nie je k dispozícii (rozhranie sa dopĺňa)."
    except Exception as exc:  # noqa: BLE001
        log.exception("Prepočet vlaku %s zlyhal", train_id)
        return None, f"Prepočet zlyhal: {type(exc).__name__}: {str(exc)[:200]}"
