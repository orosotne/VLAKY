"""Objednávky zákazníkov (M2): zoznam, návrh prepravy agenta (plan_order), vytvorenie vlaku, zamietnutie."""
from __future__ import annotations

import importlib

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from ..audit import audit
from ..cas import fmt, now_utc
from ..db import session_scope
from ..models import Order, Train
from .auth import csrf_form, require
from .common import ORDER_STATUS, log, redirect, render

router = APIRouter()
view = require("view")
operate = require("operate")


@router.get("/objednavky", response_class=HTMLResponse)
def orders_page(request: Request, stav: str = "", user=Depends(view)):
    with session_scope() as s:
        q = select(Order)
        if stav in ORDER_STATUS:
            q = q.where(Order.status == stav)
        elif stav != "vsetky":
            q = q.where(Order.status != "zamietnuta")
        orders = list(s.scalars(q.order_by(Order.created_at.desc(), Order.id.desc()).limit(300)))
        ids = [o.id for o in orders]
        trains = dict(s.execute(select(Train.order_id, func.count(Train.id)).where(Train.order_id.in_(ids))
                                .group_by(Train.order_id)).all()) if ids else {}
        return render(request, "objednavky.html", {"orders": orders, "trains": trains, "stav": stav}, user=user)


def _plan(s, order_id: int) -> tuple:
    """Návrh agenta (vlaky.planovanie.planovac.plan_order) – chyby sa zobrazia, stránka nespadne."""
    try:
        planovac = importlib.import_module("vlaky.planovanie.planovac")
        plan = planovac.plan_order(s, order_id) or {}
        return plan, plan.get("error", "")
    except NotImplementedError:
        return {}, "Plánovač zatiaľ nie je k dispozícii."
    except Exception as exc:  # noqa: BLE001
        log.exception("Návrh prepravy pre objednávku %s zlyhal", order_id)
        return {}, f"Návrh prepravy sa nepodarilo vypočítať: {type(exc).__name__}: {str(exc)[:200]}"


@router.get("/objednavky/{order_id}", response_class=HTMLResponse)
def order_detail(request: Request, order_id: int, user=Depends(view)):
    with session_scope() as s:
        order = s.get(Order, order_id)
        if order is None:
            raise HTTPException(404, "Objednávka neexistuje.")
        plan, error = _plan(s, order_id) if order.status != "zamietnuta" else ({}, "")
        trains = list(s.scalars(select(Train).where(Train.order_id == order_id).order_by(Train.id)))
        return render(request, "objednavka.html", {"o": order, "plan": plan, "plan_error": error,
                                                   "trains": trains}, user=user)


@router.post("/objednavky/{order_id}/zamietnut")
def order_reject(order_id: int, request: Request, user=Depends(operate), form=Depends(csrf_form)):
    reason = str(form.get("dovod") or "").strip()[:1000]
    if not reason:
        return redirect(f"/objednavky/{order_id}", "Pri zamietnutí objednávky uveďte dôvod.", "chyba")
    with session_scope() as s:
        order = s.get(Order, order_id)
        if order is None:
            raise HTTPException(404, "Objednávka neexistuje.")
        if order.status == "zamietnuta":
            return redirect(f"/objednavky/{order_id}", "Objednávka už je zamietnutá.", "info")
        previous = order.status
        order.status = "zamietnuta"
        order.notes = (order.notes + "\n" if order.notes else "") + \
            f"Zamietnutá ({user.username}, {fmt(now_utc())}): {reason}"
        audit(s, user.username, "order_reject", "order", order.id, previous_status=previous, reason=reason)
    return redirect(f"/objednavky/{order_id}", "Objednávka zamietnutá. Informovanie zákazníka zabezpečí dispečer.")
