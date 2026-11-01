"""Stav vlaku a začiatok státia na koľajach ŽSR (limit 18 h – PPŽS, sekcie 10.2 a 10.4 PROMPT.md).

``Train.standing_since_utc`` = od kedy vlak stojí na koľajach ŽSR. Zapisuje sa LEN cez funkcie tohto modulu
(web: úprava vlaku a preberanie na hranici, hlásenia rušňovodiča, simulácia), aby všetky cesty dodržali to isté
pravidlo:

  * príchod na vstupnú hranicu zaznamenaný → čas príchodu,
  * prevzatie na hranici, odstavenie (stav „prevzatý“ / „odstavený“) → ponechá sa doterajší začiatok státia;
    ak nie je, najskoršia platná kotva (príchod na hranicu, zmeškaná trasa), inak čas zmeny – po jazde vždy
    čas zmeny (staré časy spred jazdy neplatia),
  * odchod (stav „v jazde“, aj hlásenie rušňovodiča „odchod“) → vymaže sa a zapíše sa čas odchodu,
  * ukončenie, zrušenie, odovzdanie → vymaže sa.

Hlásenie rušňovodiča „pripravený“ začiatok státia nemení. Plánovač (``hold_anchors``) z neho počíta limit 18 h;
pre staršie záznamy bez neho používa konzervatívne náhradné kotvy (príchod na hranicu, čas pripravenosti).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..cas import ensure_utc, local, now_utc
from ..models import Handover, Path, Train

RUNNING = "v_jazde"
STANDING = ("odstaveny", "prevzaty")                       # vlak stojí na koľajach ŽSR
NOT_RUN = ("planovany", "na_ceste_k_hranici", "prevzaty")  # od príchodu na hranicu ešte nešiel
ENDED = ("ukonceny", "zruseny", "odovzdany")
ARRIVED = ("prisiel", "prevzaty")                          # vstupné preberanie: vlak už je na koľajach ŽSR


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    return ensure_utc(dt) if dt else None


# ------------------------------------------------------------------ dotazy
def entry_handover(session: Session, train_id: int) -> Optional[Handover]:
    return session.scalar(select(Handover).where(Handover.train_id == train_id, Handover.direction == "vstup")
                          .order_by(Handover.id.desc()))


def _run_day(train: Train, ref: Optional[datetime] = None) -> Optional[date]:
    """Deň jazdy vlaku: zadaný dátum jazdy, inak deň plánovaného odchodu, pripravenosti alebo príchodu na hranicu."""
    if train.run_date:
        try:
            return date.fromisoformat(train.run_date)
        except ValueError:
            pass
    for dt in (train.planned_departure_utc, train.ready_utc, ref):
        if dt:
            return local(dt).date()
    return None


def allocated_paths(session: Session, train: Train, ref: Optional[datetime] = None) -> list:
    """Pridelené trasy tejto jazdy vlaku.

    Trasy priradené vlaku platia vždy. Trasa s rovnakým číslom bez priradeného vlaku (e-mail prišiel skôr, ako
    bol vlak zadaný) patrí tejto jazde, len ak jej odchod sedí s dňom jazdy (± 1 deň); ak má vlak trasu priamo
    v deň jazdy, trasy susedných dní patria iným jazdám denného vlaku s tým istým číslom. Bez dňa jazdy sa takéto
    trasy nepoužijú. ``ref`` – čas príchodu (avíza) na hranicu, ak vlak nemá dátum jazdy ani časy.
    """
    q = select(Path).where(Path.status.in_(("pridelena", "zmenena")),
                           (Path.train_id == train.id) |
                           ((Path.train_number == train.number) & (Path.train_id.is_(None))))
    own, orphans = [], []
    for p in session.scalars(q):
        if p.departure_utc:
            (own if p.train_id == train.id else orphans).append(p)
    if not orphans:
        return own
    day = _run_day(train, ref)
    if day is None:
        return own
    near = [(abs((local(p.departure_utc).date() - day).days), p) for p in orphans]
    near = [(d, p) for d, p in near if d <= 1]
    on_day = any(d == 0 for d, _p in near) or any(local(p.departure_utc).date() == day for p in own)
    return own + [p for d, p in near if d == 0 or not on_day]


# ------------------------------------------------------------------ kotvy limitu 18 h (plánovač)
def entry_arrival(train: Train, handover: Optional[Handover]) -> Optional[datetime]:
    """Príchod na vstupnú hranicu, ak vlak odvtedy ešte nešiel (od príchodu stojí na koľajach ŽSR)."""
    if handover is None or handover.direction != "vstup" or handover.status not in ARRIVED:
        return None
    if train.status == RUNNING or train.status in ENDED:
        return None
    # pred prevzatím vlak ísť nemohol; po prevzatí len ak odvtedy nebol v jazde (odstavený mohol ísť)
    if handover.status == "prevzaty" and train.status not in NOT_RUN:
        return None
    return _utc(handover.arrived_utc)


def _waited_for_path(train: Train, handover: Optional[Handover], dep: datetime) -> bool:
    """Stál vlak pripravený na koľajach ŽSR už v čase odchodu svojej trasy (a nevyšiel)?"""
    departed = _utc(train.departed_utc)
    if departed is not None and departed >= dep:
        return False  # na trase (alebo neskôr) vyšiel – nestojí od nej
    if handover is not None and handover.direction == "vstup":
        arrival = entry_arrival(train, handover)  # vlak zo zahraničia je na koľajach ŽSR až po príchode
        return arrival is not None and arrival <= dep
    if train.status == "na_ceste_k_hranici":
        return False
    ready = _utc(train.ready_utc)
    return ready is None or ready <= dep  # neznáma pripravenosť → konzervatívne áno


def hold_anchors(train: Train, handover: Optional[Handover], paths: Iterable[Path], now: datetime) -> list:
    """Od kedy vlak stojí na koľajach ŽSR – kotvy limitu 18 h (PIS ruší neodídený vlak, odstavenie max. 18 h).

    1. ``standing_since_utc``, ak vlak stojí (odstavený, prevzatý, alebo prišiel na hranicu a ešte nešiel);
       k nemu aj príchod na hranicu, ak vlak odvtedy nešiel (nikdy nie neskoršia kotva, než ukazujú údaje).
    2. Bez neho (záznamy spred zavedenia stĺpca): príchod na hranicu, ak vlak odvtedy nešiel; čas pripravenosti
       odstaveného alebo prevzatého vlaku.
    3. Minulý odchod pridelenej trasy tejto jazdy, ak na nej vlak stál pripravený na koľajach ŽSR a nevyšiel
       (nie pri odstavenom vlaku – ten mohol na trase ísť a byť odstavený až neskôr).
    Idúci a ukončený vlak nestojí. Vracia vzostupne zoradené časy pred ``now``.
    """
    if train.status == RUNNING or train.status in ENDED:
        return []
    now = ensure_utc(now)
    out = []
    arrival = entry_arrival(train, handover)
    since = _utc(train.standing_since_utc)
    if since is not None:
        if train.status in STANDING or arrival is not None:
            out.append(since)
            if arrival is not None:
                out.append(arrival)
    else:
        if arrival is not None:
            out.append(arrival)
        if train.status in STANDING and train.ready_utc:
            out.append(ensure_utc(train.ready_utc))
    if train.status != "odstaveny":
        for p in paths:
            dep = _utc(p.departure_utc)
            if dep is not None and dep < now and _waited_for_path(train, handover, dep):
                out.append(dep)
    return sorted(x for x in out if x < now)


# ------------------------------------------------------------------ zmeny stavu (zapisovatelia)
def _earliest_standing(session: Optional[Session], train: Train, at: datetime,
                       handover: Optional[Handover] = None) -> datetime:
    """Najskoršia platná kotva státia podľa doterajšieho stavu vlaku (alebo ``at``) – zmena stavu limit 18 h
    nepredĺži (vlak už mohol stáť od príchodu na hranicu alebo od zmeškanej trasy)."""
    ho = handover
    paths: list = []
    if session is not None:
        ho = ho or entry_handover(session, train.id)
        paths = allocated_paths(session, train, _utc(ho.arrived_utc or ho.avizo_eta_utc) if ho else None)
    return min(hold_anchors(train, ho, paths, at) + [at])


def set_status(session: Optional[Session], train: Train, status: str, at: Optional[datetime] = None,
               handover: Optional[Handover] = None, actual: bool = True) -> None:
    """Zmení stav vlaku a podľa neho začiatok státia (a čas odchodu). Rovnaký stav nič nemení.

    ``at`` – čas udalosti (predvolene teraz). ``actual=False`` – čas zmeny nie je skutočný čas odchodu (napr. ručná
    úprava stavu vo webe): čas odchodu sa potom nezapíše (odhad pracovného času rušňovodiča použije plánovaný
    odchod). ``session`` slúži na dohľadanie vstupného preberania a trás; bez nej sa použije len ``handover``.
    """
    old = train.status
    if not status or status == old:
        return
    at = _utc(at) or now_utc()
    since = None
    if status in STANDING:
        if old == RUNNING or old in ENDED:
            since = at  # po jazde (alebo obnovenom vlaku) státie začína teraz – staré časy spred jazdy neplatia
        else:
            since = _earliest_standing(session, train, at, handover)
    train.status = status
    if status == RUNNING:
        train.standing_since_utc = None
        train.departed_utc = at if actual else None
    elif status in ENDED:
        train.standing_since_utc = None
    elif since is not None:
        train.standing_since_utc = since


def pin_standing(session: Optional[Session], train: Train, at: Optional[datetime] = None) -> None:
    """Stojaci vlak (odstavený/prevzatý) bez začiatku státia – nový vlak zadaný rovno ako stojaci alebo starší
    záznam: zapíše začiatok státia podľa doterajších kotiev (napr. minulý čas pripravenosti), inak teraz. Neskoršia
    úprava času pripravenosti potom limit 18 h neposunie."""
    if train.status in STANDING and train.standing_since_utc is None:
        train.standing_since_utc = _earliest_standing(session, train, _utc(at) or now_utc())


def record_arrival(train: Train, handover: Handover, at: datetime) -> None:
    """Príchod vlaku na vstupnú hranicu: od tohto času stojí na koľajach ŽSR (oprava času príchodu ho prepíše)."""
    if handover.direction == "vstup" and train.status in NOT_RUN:
        train.standing_since_utc = ensure_utc(at)


def record_takeover(session: Optional[Session], train: Train, handover: Handover, at: datetime) -> None:
    """Prevzatie na hranici. Vstup: vlak stojí ďalej – doterajší začiatok státia sa ponechá, inak čas príchodu
    (pred prevzatím vlak ísť nemohol), inak čas prevzatia. Výstup: vlak je odovzdaný (koniec)."""
    at = ensure_utc(at)
    if handover.direction != "vstup":
        set_status(session, train, "odovzdany", at, handover)
        return
    candidates = [at]
    if train.status != RUNNING and train.status not in ENDED:
        candidates.append(_earliest_standing(session, train, at, handover))
    for dt in (train.standing_since_utc, handover.arrived_utc):
        if dt is not None and ensure_utc(dt) <= at:
            candidates.append(ensure_utc(dt))
    train.status = "prevzaty"
    train.standing_since_utc = min(candidates)


def is_standing(train: Train) -> bool:
    """Vlak stojí na koľajach ŽSR (odstavený, prevzatý alebo so zaznamenaným začiatkom státia)."""
    return train.status in STANDING or (train.standing_since_utc is not None and train.status != RUNNING
                                        and train.status not in ENDED)
