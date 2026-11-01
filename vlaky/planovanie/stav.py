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

Hlásenie rušňovodiča „pripravený“ začiatok státia nemení a minulý (ani chýbajúci) čas pripravenosti neposunie, ak by
tým zmizla kotva limitu. Úprava vlaku vo webe pred zápisom zmien zapíše začiatok státia z doterajších kotiev
(``pin_standing``) – ani zmena času pripravenosti, z ktorého plánovač kotvu počíta (zmeškaná trasa), limit neposunie.
Plánovač (``hold_anchors``) z neho počíta limit 18 h pre každý vlak, ktorý nejde a neskončil (aj „plánovaný“, ktorý
čaká od zmeškanej trasy); vlak na ceste k hranici (v zahraničí) až po príchode na hranicu. Pre staršie záznamy bez
neho používa konzervatívne náhradné kotvy (príchod na hranicu, čas pripravenosti).
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
_CURRENT = object()  # hold_anchors: čas pripravenosti zo záznamu vlaku (nie zvažovaná zmena)


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    return ensure_utc(dt) if dt else None


def border_time(handover: Optional[Handover]) -> Optional[datetime]:
    """Čas príchodu (inak avíza) na vstupnú hranicu – deň jazdy vlaku bez dátumu a časov (``allocated_paths``)."""
    return _utc(handover.arrived_utc or handover.avizo_eta_utc) if handover is not None else None


# ------------------------------------------------------------------ dotazy
def entry_handover(session: Session, train_id: int) -> Optional[Handover]:
    return session.scalar(select(Handover).where(Handover.train_id == train_id, Handover.direction == "vstup")
                          .order_by(Handover.id.desc()))


def _run_day(train: Train, ref: Optional[datetime] = None, now: Optional[datetime] = None) -> Optional[date]:
    """Deň jazdy vlaku: zadaný dátum jazdy, inak deň plánovaného odchodu, pripravenosti, príchodu na hranicu,
    inak (vlak zadaný bez dátumu a časov) deň posúdenia ``now`` – ide o najbližšiu jazdu."""
    if train.run_date:
        try:
            return date.fromisoformat(train.run_date)
        except ValueError:
            pass
    for dt in (train.planned_departure_utc, train.ready_utc, ref, now):
        if dt:
            return local(dt).date()
    return None


def allocated_paths(session: Session, train: Train, ref: Optional[datetime] = None,
                    now: Optional[datetime] = None) -> list:
    """Pridelené trasy tejto jazdy vlaku.

    Trasy priradené vlaku platia vždy. Trasa s rovnakým číslom bez priradeného vlaku (e-mail prišiel skôr, ako
    bol vlak zadaný) patrí tejto jazde, len ak jej odchod sedí s dňom jazdy (± 1 deň); ak má vlak trasu priamo
    v deň jazdy, trasy susedných dní patria iným jazdám denného vlaku s tým istým číslom. ``ref`` – čas príchodu
    (avíza) na hranicu, ak vlak nemá dátum jazdy ani časy; ``now`` – čas posúdenia pre vlak bez akéhokoľvek času
    (bez neho sa takéto trasy nepoužijú).
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
    day = _run_day(train, ref, now)
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
    arrival = _utc(handover.arrived_utc)
    departed = _utc(train.departed_utc)
    if arrival is not None and departed is not None and departed >= arrival:
        return None  # od príchodu už vyšiel (skutočný odchod) – príchod nie je začiatok státia
    return arrival


def _waited_for_path(train: Train, handover: Optional[Handover], dep: datetime, ready: Optional[datetime]) -> bool:
    """Stál vlak pripravený na koľajach ŽSR už v čase odchodu svojej trasy (a nevyšiel)?"""
    departed = _utc(train.departed_utc)
    if departed is not None and departed >= dep:
        return False  # na trase (alebo neskôr) vyšiel – nestojí od nej
    if handover is not None and handover.direction == "vstup":
        arrival = entry_arrival(train, handover)  # vlak zo zahraničia je na koľajach ŽSR až po príchode
        return arrival is not None and arrival <= dep
    if train.status == "na_ceste_k_hranici":
        return False
    return ready is None or ready <= dep  # neznáma pripravenosť → konzervatívne áno


def _since_applies(train: Train, handover: Optional[Handover], arrival: Optional[datetime]) -> bool:
    """Platí zapísaný začiatok státia? Pre každý vlak, ktorý nejde a neskončil (aj plánovaný, ktorý čaká od
    zmeškanej trasy) – okrem vlaku ešte v zahraničí (na ceste k hranici, očakávaný na vstupnej hranici), kým
    nepríde na hranicu."""
    if train.status in STANDING or arrival is not None:
        return True
    if train.status == "na_ceste_k_hranici":
        return False
    return handover is None or handover.direction != "vstup" or handover.status in ARRIVED


def hold_anchors(train: Train, handover: Optional[Handover], paths: Iterable[Path], now: datetime,
                 ready=_CURRENT) -> list:
    """Od kedy vlak stojí na koľajach ŽSR – kotvy limitu 18 h (PIS ruší neodídený vlak, odstavenie max. 18 h).

    1. ``standing_since_utc``, ak vlak stojí (odstavený, prevzatý, plánovaný; vlak zo zahraničia až po príchode na
       hranicu); k nemu aj príchod na hranicu, ak vlak odvtedy nešiel (nikdy nie neskoršia kotva, než ukazujú údaje).
    2. Bez neho (záznamy spred zavedenia stĺpca): príchod na hranicu, ak vlak odvtedy nešiel; čas pripravenosti
       odstaveného alebo prevzatého vlaku.
    3. Minulý odchod pridelenej trasy tejto jazdy, ak na nej vlak stál pripravený na koľajach ŽSR a nevyšiel
       (nie pri odstavenom vlaku – ten mohol na trase ísť a byť odstavený až neskôr).
    Idúci a ukončený vlak nestojí. Vracia vzostupne zoradené časy pred ``now``. ``ready`` – zvažovaný čas
    pripravenosti namiesto zapísaného (či by jeho zmena kotvu posunula).
    """
    if train.status == RUNNING or train.status in ENDED:
        return []
    now = ensure_utc(now)
    ready = _utc(train.ready_utc) if ready is _CURRENT else _utc(ready)
    out = []
    arrival = entry_arrival(train, handover)
    since = _utc(train.standing_since_utc)
    if since is not None:
        if _since_applies(train, handover, arrival):
            out.append(since)
        if arrival is not None:
            out.append(arrival)
    else:
        if arrival is not None:
            out.append(arrival)
        if train.status in STANDING and ready:
            out.append(ready)
    if train.status != "odstaveny":
        for p in paths:
            dep = _utc(p.departure_utc)
            if dep is not None and dep < now and _waited_for_path(train, handover, dep, ready):
                out.append(dep)
    return sorted(x for x in out if x < now)


def standing_anchor(session: Optional[Session], train: Train, at: Optional[datetime] = None,
                    handover: Optional[Handover] = None, ready=_CURRENT) -> Optional[datetime]:
    """Od kedy vlak podľa doterajších údajov stojí na koľajach ŽSR (najskoršia kotva limitu 18 h), inak None.
    ``ready`` – zvažovaný čas pripravenosti namiesto zapísaného."""
    at = _utc(at) or now_utc()
    ho = handover
    paths: list = []
    if session is not None:
        ho = ho or (entry_handover(session, train.id) if train.id is not None else None)
        paths = allocated_paths(session, train, border_time(ho), at)
    anchors = hold_anchors(train, ho, paths, at, ready)
    return anchors[0] if anchors else None


# ------------------------------------------------------------------ zmeny stavu (zapisovatelia)
def _earliest_standing(session: Optional[Session], train: Train, at: datetime,
                       handover: Optional[Handover] = None) -> datetime:
    """Najskoršia platná kotva státia podľa doterajšieho stavu vlaku (alebo ``at``) – zmena stavu limit 18 h
    nepredĺži (vlak už mohol stáť od príchodu na hranicu alebo od zmeškanej trasy)."""
    anchor = standing_anchor(session, train, at, handover)
    return min(anchor, at) if anchor is not None else at


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
    """Pred úpravou údajov vlaku (čas pripravenosti, stav, dátum jazdy): zapíše začiatok státia podľa doterajších
    kotiev – zmeškaná trasa, na ktorej vlak stál pripravený, minulý čas pripravenosti odstaveného/prevzatého
    vlaku, príchod na hranicu. Úprava potom kotvu neposunie ani nezruší (plánovač začiatok státia použije, kým vlak
    nejde). Stojaci vlak (odstavený/prevzatý, napr. zadaný rovno ako stojaci) bez kotvy: teraz. Neskoršiu kotvu
    nikdy nezapíše; idúci a ukončený vlak nestojí."""
    if train.status == RUNNING or train.status in ENDED:
        return
    at = _utc(at) or now_utc()
    anchor = standing_anchor(session, train, at)
    if anchor is None and train.status in STANDING and train.standing_since_utc is None:
        anchor = at
    since = _utc(train.standing_since_utc)
    if anchor is not None and (since is None or anchor < since):
        train.standing_since_utc = anchor


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


def keep_ready_on_report(session: Session, train: Train, now: datetime) -> bool:
    """Hlásenie rušňovodiča „pripravený“: ponechať doterajší čas pripravenosti (namiesto teraz)?

    Áno pre minulý čas stojaceho vlaku (odstavený, prevzatý, so zapísaným začiatkom státia) a vždy, keď by čas
    hlásenia kotvu limitu 18 h posunul alebo zrušil – vlak čaká pripravený od zmeškanej trasy (aj pri neznámom čase
    pripravenosti), starší záznam odstaveného vlaku. Budúci čas pripravenosti sa vždy nahradí (kotvu nemá)."""
    now = ensure_utc(now)
    ready = _utc(train.ready_utc)
    if ready is not None and ready > now:
        return False
    if ready is not None and is_standing(train):
        return True
    before = standing_anchor(session, train, now)
    if before is None:
        return False
    after = standing_anchor(session, train, now, ready=now)
    return after is None or after > before
