"""Dopravne (M3 – ručné opravy s auditom): možnosť čakania, odstavenia, výmeny rušňovodiča, užitočná dĺžka koľají.

Údaje (LocationInfo) používa plánovač (miesta na čakanie) a vrstva mapy „Miesta na čakanie a odstavenie“.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ..audit import audit
from ..db import session_scope
from ..models import LocationInfo
from .auth import csrf_form, require
from .common import FormReader, redirect, render
from .data import get_network

router = APIRouter()
view = require("view")
operate = require("operate")
STATION_TYPES = {"station": "stanica", "yard": "zriaďovacia stanica", "border": "hraničný bod", "junction": "odbočka",
                 "spur_junction": "odbočenie vlečky", "halt": "zastávka", "crossover": "výhybňa / priecestie",
                 "service_station": "služobná stanica"}


def _info_dict(li) -> dict:
    if li is None:
        return {"can_wait": True, "can_stable": False, "can_change_crew": False, "max_train_length_m": None,
                "note": "", "exists": False}
    return {"can_wait": li.can_wait, "can_stable": li.can_stable, "can_change_crew": li.can_change_crew,
            "max_train_length_m": li.max_train_length_m, "note": li.note or "", "exists": True}


@router.get("/dopravne", response_class=HTMLResponse)
def stations_page(request: Request, q: str = "", user=Depends(view)):
    net = get_network()
    q = q.strip()[:80]
    with session_scope() as s:
        infos = {li.station: li for li in s.scalars(select(LocationInfo).order_by(LocationInfo.station))}
        results = []
        if q and len(q) >= 2 and net is not None:
            seen = set()
            for st in net.search_stations(q, limit=60):
                if st.name in seen:
                    continue
                seen.add(st.name)
                results.append({"name": st.name, "type": STATION_TYPES.get(st.type, st.type),
                                "ref": st.railway_ref, "uic": st.uic_ref, "info": _info_dict(infos.get(st.name))})
                if len(results) >= 30:
                    break
        evidence = [{"name": name, "info": _info_dict(li)} for name, li in infos.items()]
        return render(request, "dopravne.html", {"q": q, "results": results, "evidence": evidence,
                                                 "network_ok": net is not None}, user=user)


def _station(name: str):
    net = get_network()
    st = net.find_station(name) if net is not None and name else None
    if st is None:
        raise HTTPException(404, "Dopravňa nie je v registri siete.")
    return st


@router.get("/dopravne/upravit", response_class=HTMLResponse)
def station_edit(request: Request, stanica: str, user=Depends(operate)):
    st = _station(stanica)
    with session_scope() as s:
        li = s.scalar(select(LocationInfo).where(LocationInfo.station == st.name))
        info = _info_dict(li)
        values = {"can_wait": "1" if info["can_wait"] else "", "can_stable": "1" if info["can_stable"] else "",
                  "can_change_crew": "1" if info["can_change_crew"] else "",
                  "max_train_length_m": info["max_train_length_m"] or "", "note": info["note"]}
        return render(request, "dopravna.html", {"st": st, "type": STATION_TYPES.get(st.type, st.type),
                                                 "values": values, "errors": {}, "exists": info["exists"]}, user=user)


@router.post("/dopravne/upravit")
def station_save(request: Request, user=Depends(operate), form=Depends(csrf_form)):
    st = _station(str(form.get("stanica") or ""))
    reader = FormReader(form)
    values = {"can_wait": reader.flag("can_wait"), "can_stable": reader.flag("can_stable"),
              "can_change_crew": reader.flag("can_change_crew"),
              "max_train_length_m": reader.number("max_train_length_m", "Užitočná dĺžka koľaje [m]", minimum=50,
                                                  maximum=3000),
              "note": reader.text("note", "Poznámka", maxlen=2000)}
    if not reader.ok:
        return render(request, "dopravna.html", {"st": st, "type": STATION_TYPES.get(st.type, st.type),
                                                 "values": reader.values(), "errors": reader.errors,
                                                 "exists": True}, user=user, status_code=422)
    with session_scope() as s:
        li = s.scalar(select(LocationInfo).where(LocationInfo.station == st.name))
        created = li is None
        if created:
            li = LocationInfo(station=st.name)
            s.add(li)
        changes = {k: [None if created else getattr(li, k), v] for k, v in values.items()
                   if created or getattr(li, k) != v}
        for k, v in values.items():
            setattr(li, k, v)
        s.flush()
        if changes:
            audit(s, user.username, "location_info_update", "location_info", li.id, station=st.name,
                  created=created, changes=changes)
    return redirect(f"/dopravne?q={st.name}", f"Údaje dopravne {st.name} uložené." if changes else "Bez zmien.")
