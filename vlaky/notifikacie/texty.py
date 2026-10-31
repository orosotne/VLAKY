"""Texty správ pre dispečera (slovensky, čistý text – čitateľné v e-maile, Teams aj SMS)."""
from __future__ import annotations

from datetime import datetime

from ..cas import fmt
from ..config import settings
from ..models import Recommendation


def _t(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return fmt(datetime.fromisoformat(iso))
    except ValueError:
        return iso


def recommendation_subject(rec: Recommendation) -> str:
    train = rec.train
    rel = f"{train.origin} → {train.destination}" if train else ""
    head = rec.decision
    if rec.chosen.get("depart") and rec.decision not in ("ESKALOVAŤ", "POKRAČOVAŤ"):
        head += f" {_t(rec.chosen['depart'])}"
    prefix = "[TIEŇ]" if settings().shadow_mode else ""
    return f"{prefix}[AGENT][{rec.priority}] Vlak {train.number if train else '?'} {rel}: {head} ({rec.color})"


def recommendation_body(rec: Recommendation) -> str:
    s = settings()
    lines = [rec.summary, ""]
    if rec.trigger:
        lines.append(f"Čo sa zmenilo: {rec.trigger}")
    ch = rec.chosen or {}
    if ch:
        lines += [
            f"Odporúčanie: {rec.decision} – semafor {rec.color}",
            f"Trasa: {ch.get('route', '')}",
            f"Odchod: {_t(ch.get('depart'))}   Príchod: {_t(ch.get('arrival_p50'))} (90 % do {_t(ch.get('arrival_p90'))})",
            f"P(včas): {ch.get('p_on_time', 0):.0%}   P(zadržania pred výlukou): {ch.get('p_hold', 0):.0%}",
            f"Očakávaný náklad: {ch.get('expected_cost', 0):,.0f} € (vzorové sadzby, ak nie sú nastavené vlastné)",
        ]
    pp = rec.path_proposal or {}
    if pp.get("stations"):
        lines += ["", f"NÁVRH TRASY ({pp.get('route', '')}, {pp.get('km', '')} km):"]
        if pp.get("needs_new_path"):
            win = pp.get("request_window")
            lines.append("  → Treba objednať (ad hoc) trasu u ŽSR"
                         + (f", odchod v okne {_t(win[0])} – {_t(win[1])}" if win else "") + ".")
        for st in pp["stations"]:
            if st.get("type") == "junction":
                continue
            lines.append(f"  {st['name']:<34} prích. {_t(st.get('arr')):>13}  odch. {_t(st.get('dep')):>13}"
                         f"  ({st.get('km', '')} km)")
    win = rec.windows or {}
    if win:
        lines += ["", "Okná bezpečného odchodu (ZELENÁ):"]
        for route, wins in win.items():
            txt = ", ".join(f"{_t(a)} – {_t(b)}" for a, b in wins) or "žiadne"
            lines.append(f"  {route}: {txt}")
    crew = rec.crew or {}
    if crew:
        lines += ["", f"Rušňovodič: {crew.get('driver') or '—'}; nástup {_t(crew.get('start'))}, limit "
                      f"{crew.get('max_duty_h', 13)} h do {_t(crew.get('limit'))}; koniec {_t(crew.get('end_p50'))} "
                      f"(90 % do {_t(crew.get('end_p90'))}) – {crew.get('status', '')}"]
        if crew.get("status") in ("ORANŽOVÁ", "ČERVENÁ"):
            lines.append(f"  → Vystriedať najneskôr o {_t(crew.get('relieve_by'))}.")
    if rec.variants:
        lines += ["", "Porovnanie variantov:"]
        for v in rec.variants:
            lines.append(f"  {v.get('route', '')[:30]:<30} {_t(v.get('depart')):>13} {v.get('kind', ''):<12} "
                         f"{v.get('status', ''):<9} P(včas) {v.get('p_on_time', 0):.0%}  {v.get('expected_cost', 0):,.0f} €")
    if rec.reasons:
        lines += ["", "Dôvody a upozornenia:"] + [f"  - {r}" for r in rec.reasons]
    age = rec.data_age or {}
    if age:
        parts = []
        if age.get("position_min") is not None:
            parts.append(f"poloha pred {age['position_min']:.0f} min")
        if age.get("mailbox_min") is not None:
            parts.append(f"schránka skontrolovaná pred {age['mailbox_min']:.0f} min")
        if parts:
            lines += ["", "Čerstvosť dát: " + ", ".join(parts)]
    lines += ["", f"Detail a potvrdenie: {s.public_url.rstrip('/')}/odporucania/{rec.id}",
              "", "Agent dispečer iba odporúča. Rozhoduje a koná dispečer. Správu nepreposielajte mimo firmy."]
    return "\n".join(lines)


def sms_text(rec: Recommendation) -> str:
    train = rec.train
    ch = rec.chosen or {}
    txt = f"AGENT {rec.priority}: vlak {train.number if train else '?'} {rec.decision}"
    if ch.get("depart") and rec.decision not in ("ESKALOVAŤ", "POKRAČOVAŤ"):
        txt += f" {_t(ch['depart'])}"
    if (rec.crew or {}).get("status") in ("ORANŽOVÁ", "ČERVENÁ"):
        txt += f"; vystriedať rušňovodiča do {_t(rec.crew.get('relieve_by'))}"
    return (txt + f". Detail: {settings().public_url.rstrip('/')}/odporucania/{rec.id}")[:300]
