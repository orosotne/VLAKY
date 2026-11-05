"""Stránky správy: nastavenia parametrov (M14), používatelia a roly, audit, zdravie agenta."""
from __future__ import annotations

import importlib
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from ..audit import audit
from ..cas import ensure_utc, now_utc
from ..config import settings
from ..db import session_scope
from ..models import AgentHealth, AuditLog, Email, ParameterSet, PositionReport, User
from .auth import csrf_form, require
from .common import (INT_PARAMS, PARAM_META, PROB_PARAMS, FormReader, ago, current_param_set, current_params,
                     fmt_full, fmt_num, iso, log, param_defaults, redirect, render)
from .data import get_network

router = APIRouter()
view = require("view")
params_perm = require("params")
users_perm = require("users")
audit_perm = require("audit")


# ================================================================ NASTAVENIA (M14)
# rezerva pred výlukou a po nej: najviac 24 h. Plánovač hľadá okná výluk podľa nej (pohľad späť, okná pred
# pripravenosťou vlaku) – preklep (napr. hodiny namiesto minút, 4200 namiesto 42) by vlak držal pred úsekom dni po
# výluke a prepočet každého vlaku by prechádzal mesiace okien výluk
BUFFER_PARAMS = ("buffer_before_closure_min", "buffer_after_closure_min")
BUFFER_MAX_MIN = 24 * 60


def _param_groups(values: dict, defaults: dict) -> list:
    groups: dict = {}
    for key in defaults:
        label, unit, group = PARAM_META.get(key, (key, "", "Ostatné"))
        groups.setdefault(group, []).append({"key": key, "label": label, "unit": unit, "value": values.get(key),
                                             "default": defaults[key], "integer": key in INT_PARAMS,
                                             "prob": key in PROB_PARAMS,
                                             "max": BUFFER_MAX_MIN if key in BUFFER_PARAMS else None})
    return [{"name": name, "items": items} for name, items in groups.items()]


def _settings_overview() -> list:
    cfg = settings()
    yes_no = lambda b: "áno" if b else "nie"  # noqa: E731
    return [
        ("Tieňová prevádzka (odporúčania len na tieňový zoznam)", yes_no(cfg.shadow_mode)),
        ("Tieňový zoznam (e-maily)", ", ".join(cfg.csv(cfg.shadow_emails)) or "—"),
        ("Dispečeri – e-maily (povolení príjemcovia)", ", ".join(cfg.csv(cfg.notify_emails)) or "—"),
        ("Dispečeri – telefóny na SMS (povolení príjemcovia)", ", ".join(cfg.csv(cfg.notify_phones)) or "—"),
        ("Kanál MS Teams nastavený", yes_no(bool(cfg.teams_webhook_url))),
        ("SMS brána nastavená", yes_no(bool(cfg.sms_gateway_url))),
        ("Povolení odosielatelia e-mailov (domény/adresy)", ", ".join(cfg.csv(cfg.allowed_senders)) or "—"),
        ("Vyžadovať SPF/DKIM/DMARC", yes_no(cfg.require_dmarc_pass)),
        ("Prah istoty extrakcie (pod ním ide udalosť na potvrdenie)", fmt_num(cfg.confirm_threshold, 2)),
        ("Horizont potvrdzovania [h]", str(cfg.confirm_horizon_h)),
        ("Poloha je stará po [min]", str(cfg.position_stale_minutes)),
        ("Prepočet idúcich vlakov každých [min]", str(cfg.reevaluate_minutes)),
        ("Token pre príjem polôh nastavený", yes_no(bool(cfg.positions_token))),
        ("Zdroj schránky agenta", cfg.mail_source),
        ("Jazykový model (extrakcia)", cfg.llm_provider + (f" / {cfg.llm_model}" if cfg.llm_model else (
            " – chýba VLAKY_LLM_MODEL, agent používa pravidlá" if cfg.llm_provider not in ("", "none") else ""))),
    ]


@router.get("/nastavenia", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(params_perm)):
    now = now_utc()
    with session_scope() as s:
        defaults = param_defaults()
        values = current_params(s, now)
        current = current_param_set(s, now)
        history = list(s.scalars(select(ParameterSet).order_by(ParameterSet.valid_from.desc(), ParameterSet.id.desc())
                                 .limit(20)))
        extra = {k: v for k, v in ((current.values or {}) if current else {}).items() if k not in defaults}
        return render(request, "nastavenia.html", {
            "groups": _param_groups(values, defaults), "current": current, "history": history, "now": now,
            "overview": _settings_overview(), "errors": {}, "form_values": {}, "note": "", "extra": extra}, user=user)


@router.post("/nastavenia")
def settings_save(request: Request, user=Depends(params_perm), form=Depends(csrf_form)):
    reader = FormReader(form)
    defaults = param_defaults()
    new_values: dict = {}
    for key in defaults:
        label = PARAM_META.get(key, (key,))[0]
        if key in PROB_PARAMS:
            v = reader.number(key, label, required=True, minimum=0, maximum=1)
        elif key == "samples":
            v = reader.number(key, label, required=True, minimum=200, maximum=50000, integer=True)
        elif key == "seed":
            v = reader.number(key, label, required=True, minimum=0, maximum=2 ** 31 - 1, integer=True)
        elif key == "crew_max_duty_min":
            v = reader.number(key, label, required=True, minimum=60, maximum=24 * 60)
        elif key in BUFFER_PARAMS:
            v = reader.number(key, label, required=True, minimum=0, maximum=BUFFER_MAX_MIN)
            if v is not None and v > BUFFER_MAX_MIN:
                reader.errors[key] = (f"{label}: najviac {fmt_num(BUFFER_MAX_MIN)} min (24 h) – rezerva sa zadáva "
                                      f"v minútach; dlhšia rezerva by vlak držala pred úsekom viac ako deň pred výlukou "
                                      f"či po nej (skontrolujte, či nejde o preklep).")
        else:
            v = reader.number(key, label, required=True, minimum=0, maximum=1_000_000)
        if v is not None:
            new_values[key] = int(v) if key in INT_PARAMS else float(v)
    if "p_on_time_green" in new_values and "p_on_time_amber" in new_values \
            and new_values["p_on_time_amber"] > new_values["p_on_time_green"]:
        reader.errors["p_on_time_amber"] = "Prah ČERVENEJ pre P(včas) nesmie byť vyšší ako prah ZELENEJ."
    if "p_hold_green" in new_values and "p_hold_red" in new_values \
            and new_values["p_hold_green"] > new_values["p_hold_red"]:
        reader.errors["p_hold_green"] = "Prah ZELENEJ pre P(zadržania) nesmie byť vyšší ako prah ČERVENEJ."
    now = now_utc()
    valid_from = reader.local_dt("valid_from", "Platí od") or now
    if valid_from <= now:
        valid_from = now  # minulosť (aj predvyplnený čas bez sekúnd) = platí okamžite, po predchádzajúcich sadách
    note = reader.text("note", "Dôvod zmeny", required=True, maxlen=2000)
    with session_scope() as s:
        if not reader.ok:
            values = {**current_params(s, now), **new_values}
            history = list(s.scalars(select(ParameterSet).order_by(ParameterSet.valid_from.desc()).limit(20)))
            return render(request, "nastavenia.html", {
                "groups": _param_groups(values, defaults), "current": current_param_set(s, now),
                "history": history, "now": now, "overview": _settings_overview(), "errors": reader.errors,
                "form_values": reader.values(), "note": note}, user=user, status_code=422)
        before = current_params(s, now)
        changes = {k: [before.get(k), v] for k, v in new_values.items() if before.get(k) != v}
        if not changes and valid_from <= now:
            return redirect("/nastavenia", "Hodnoty sa nezmenili – nová sada parametrov sa neuložila.", "info")
        # ďalšie kľúče platnej sady (napr. kalibrované zdržania z histórie) sa zachovajú
        current = current_param_set(s, now)
        extra = {k: v for k, v in ((current.values or {}) if current else {}).items() if k not in defaults}
        ps = ParameterSet(valid_from=valid_from, created_by=user.username, values={**extra, **new_values}, note=note)
        s.add(ps)
        s.flush()
        audit(s, user.username, "parameters_update", "parameter_set", ps.id, valid_from=valid_from, note=note,
              changes=changes)
    when = "okamžite" if valid_from <= now + timedelta(minutes=1) else "od " + (iso(valid_from) or "")
    return redirect("/nastavenia", f"Nová sada parametrov uložená (platí {when}). Zmenené: "
                                   f"{', '.join(changes) or 'žiadne hodnoty'}.")


# ================================================================ POUŽÍVATELIA
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")


@router.get("/pouzivatelia", response_class=HTMLResponse)
def users_page(request: Request, user=Depends(users_perm)):
    from ..pouzivatelia import ROLES

    with session_scope() as s:
        users = list(s.scalars(select(User).order_by(User.active.desc(), User.username)))
        return render(request, "pouzivatelia.html", {"users": users, "roles": ROLES}, user=user)


@router.post("/pouzivatelia/novy")
def user_create(request: Request, user=Depends(users_perm), form=Depends(csrf_form)):
    from ..pouzivatelia import ROLES, create_user

    reader = FormReader(form)
    username = reader.text("username", "Prihlasovacie meno", required=True, maxlen=80).lower()
    full_name = reader.text("full_name", "Meno a priezvisko", maxlen=160)
    role = reader.choice("role", "Rola", ROLES, "dispecer")
    email = reader.text("email", "E-mail", maxlen=200)
    phone = reader.text("phone", "Telefón", maxlen=40)
    password = str(form.get("password") or "")
    if username and not _USERNAME.match(username):
        reader.errors["username"] = "Prihlasovacie meno: 3–80 znakov (malé písmená, číslice, . _ -)."
    if len(password) < 10:
        reader.errors["password"] = "Heslo musí mať aspoň 10 znakov."
    if not reader.ok:
        return redirect("/pouzivatelia", " ".join(reader.errors.values()), "chyba")
    with session_scope() as s:
        if s.scalar(select(User).where(User.username == username)) is not None:
            return redirect("/pouzivatelia", f"Používateľ {username} už existuje.", "chyba")
        try:
            u = create_user(s, username, password, role, full_name, email, phone)
        except ValueError as exc:
            return redirect("/pouzivatelia", str(exc), "chyba")
        audit(s, user.username, "user_create", "user", u.id, username=username, role=role)
    return redirect("/pouzivatelia", f"Používateľ {username} vytvorený.")


@router.post("/pouzivatelia/{user_id}/upravit")
def user_update(user_id: int, request: Request, user=Depends(users_perm), form=Depends(csrf_form)):
    from ..pouzivatelia import ROLES, hash_password

    reader = FormReader(form)
    role = reader.choice("role", "Rola", ROLES)
    active = reader.flag("active")
    password = str(form.get("password") or "")
    if not reader.ok:
        return redirect("/pouzivatelia", " ".join(reader.errors.values()), "chyba")
    with session_scope() as s:
        u = s.get(User, user_id)
        if u is None:
            raise HTTPException(404, "Používateľ neexistuje.")
        if u.id == user.id and (role != u.role or not active):
            return redirect("/pouzivatelia", "Vlastnú rolu ani prístup si nemôžete zmeniť.", "chyba")
        changes = {}
        if u.role != role:
            changes["role"] = [u.role, role]
            u.role = role
        if u.active != active:
            changes["active"] = [u.active, active]
            u.active = active
        if password:
            try:
                u.password_hash = hash_password(password)
            except ValueError as exc:
                return redirect("/pouzivatelia", str(exc), "chyba")
            changes["password"] = "zmenené"
        if changes:
            audit(s, user.username, "user_update", "user", u.id, username=u.username, changes=changes)
        name = u.username
    return redirect("/pouzivatelia", f"Používateľ {name} uložený." if changes else "Bez zmien.")


# ================================================================ AUDIT
@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, strana: int = 1, kto: str = "", akcia: str = "", entita: str = "", id: str = "",  # noqa: A002
               user=Depends(audit_perm)):
    per_page = 50
    strana = max(1, strana)
    with session_scope() as s:
        q = select(AuditLog)
        if kto.strip():
            q = q.where(AuditLog.actor.contains(kto.strip()[:80]))
        if akcia.strip():
            q = q.where(AuditLog.action.contains(akcia.strip()[:80]))
        if entita.strip():
            q = q.where(AuditLog.entity == entita.strip()[:60])
        if id.strip():
            q = q.where(AuditLog.entity_id == id.strip()[:60])
        total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = list(s.scalars(q.order_by(AuditLog.ts.desc(), AuditLog.id.desc())
                              .offset((strana - 1) * per_page).limit(per_page)))
        return render(request, "audit.html", {"rows": rows, "strana": strana, "total": total,
                                              "pages": max(1, -(-total // per_page)),
                                              "filters": {"kto": kto, "akcia": akcia, "entita": entita, "id": id}},
                      user=user)


# ================================================================ ZDRAVIE
def _components(s) -> list:
    try:
        mod = importlib.import_module("vlaky.agent.zdravie")
        return [c for c in mod.components_overview(s) if c is not None]
    except (ImportError, AttributeError, NotImplementedError):
        out = []
        for (c,) in s.execute(select(AgentHealth.component).distinct()):
            out.append(s.scalar(select(AgentHealth).where(AgentHealth.component == c)
                                .order_by(AgentHealth.ts.desc(), AgentHealth.id.desc())))
        return [c for c in out if c is not None]


@router.get("/zdravie", response_class=HTMLResponse)
def health_page(request: Request, user=Depends(view)):
    now = now_utc()
    cfg = settings()
    checks: list = []
    agent_status = None
    try:
        mod = importlib.import_module("vlaky.agent.zdravie")
        ok, detail = mod.health_status()
        agent_status = {"ok": ok, "detail": detail}
    except Exception:  # noqa: BLE001
        log.exception("Stav agenta sa nepodarilo zistiť")
    with session_scope() as s:
        comps = _components(s)
        rows = []
        for c in comps:
            age = (now - ensure_utc(c.ts)).total_seconds() / 60.0
            rows.append({"c": c, "age": age, "stale": age > max(3 * cfg.heartbeat_minutes, 15)})
        rows.sort(key=lambda r: ({"error": 0, "warn": 1}.get(r["c"].status, 2), r["c"].component))
        last_pos = s.scalar(select(func.max(PositionReport.received_utc)))
        last_mail = s.scalar(select(func.max(Email.received_at)))
        from ..agent.zdravie import queue_counts  # rovnaké počítanie ako /fronta, hlavička a súhrn

        qc = queue_counts(s)
        queue = qc["total"]
        quarantined = s.scalar(select(func.count(Email.id)).where(Email.verdict == "karantena",
                                                                  Email.received_at >= now - timedelta(hours=24))) or 0
        checks.append(("Databáza", "ok", "dostupná"))
    net = get_network()
    if net is not None:
        checks.append(("Model siete", "ok", f"{len(net.nodes)} uzlov, {len(net.edges)} úsekov, "
                                            f"{len(net.stations)} dopravní"))
    else:
        checks.append(("Model siete", "error", "nie je k dispozícii"))
    checks.append(("Príjem polôh", "ok" if cfg.positions_token else "warn",
                   "token nastavený" if cfg.positions_token else "VLAKY_POSITIONS_TOKEN nie je nastavený"))
    checks.append(("Posledná prijatá poloha", "ok" if last_pos else "warn",
                   f"{fmt_full(last_pos)} ({ago(last_pos)})" if last_pos else "zatiaľ žiadna"))
    checks.append(("Posledný e-mail v schránke agenta", "ok" if last_mail else "warn",
                   f"{fmt_full(last_mail)} ({ago(last_mail)})" if last_mail else "zatiaľ žiadny"))
    queue_text = f"{queue} {'udalosť' if queue == 1 else 'udalosti' if 1 < queue < 5 else 'udalostí'}"
    if queue and qc.get("auto"):
        queue_text += f" (z toho zapísané automaticky – overte: {qc['auto']})"
    checks.append(("Fronta na potvrdenie", "warn" if queue else "ok", queue_text))
    checks.append(("E-maily v karanténe (24 h)", "warn" if quarantined else "ok", str(quarantined)))
    return render(request, "zdravie.html", {"rows": rows, "checks": checks, "agent": agent_status}, user=user)
