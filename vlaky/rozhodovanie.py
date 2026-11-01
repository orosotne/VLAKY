"""Rozhodovací modul Agenta dispečera (kanonická kópia referencie `referencia/rozhodovanie.py`).

Test `tests/test_rozhodovanie_zhoda.py` overuje, že tento modul dáva rovnaké výsledky ako
referencia. Pri zmene pravidiel uprav obidva súbory.

Pôvodný popis referencie:
Referenčná implementácia rozhodovania dispečera nákladného vlaku.

Čo rieši: pre vlak, ktorý je (alebo bude) pripravený na odchod, porovná možnosti
  * VYPRAVIŤ teraz,
  * POČKAŤ (posunúť odchod, vlak stojí pripravený),
  * ODSTAVIŤ (vlak sa odstaví, uvoľní sa čata, neskôr sa ide na novú trasu),
  * PRESMEROVAŤ (iná trasa),
a vyberie najlepšiu podľa pravidiel z docs/REZERVY_A_ROZHODOVANIE.md:
  1. tvrdé pravidlá (výluky s časovými rezervami, limit smeny čaty, lehota na novú trasu),
  2. pravdepodobnosť príchodu včas (simulácia Monte Carlo z historických zdržaní),
  3. očakávané náklady (čakanie, jazda, odstavenie, pokuty, mimoriadna výmena čaty).

Toto NIE JE produkčný kód. Je to spustiteľná špecifikácia: presne ukazuje, čo má
aplikácia počítať, a overuje čísla v príkladoch v dokumentácii.
Nenahrádza pokyny ŽSR, predpisy ani rozhodnutie dispečera.

Spustenie príkladu:  python3 referencia/rozhodovanie.py
Testy:               python3 -m unittest discover -s referencia -v

Časy sú v minútach od začiatku dňa (t0 = 00:00). Nevyžaduje externé knižnice.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional, Sequence

Z90 = 1.2815515655446004  # 90 % kvantil normovaného normálneho rozdelenia

ZELENA, ORANZOVA, CERVENA = "ZELENÁ", "ORANŽOVÁ", "ČERVENÁ"
VYPRAVIT, POCKAT, ODSTAVIT, PRESMEROVAT, ESKALOVAT = (
    "VYPRAVIŤ", "POČKAŤ", "ODSTAVIŤ", "PRESMEROVAŤ", "ESKALOVAŤ")


# ---------------------------------------------------------------------------
# Vstupné dáta
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Closure:
    """Výluka alebo iné obmedzenie úseku. Časy v minútach od t0."""

    start: float
    end: float
    electric_only: bool = False  # True = napr. vypnuté trakčné vedenie: motorová trakcia môže ísť
    label: str = ""


@dataclass(frozen=True)
class Slowdown:
    """Časovo obmedzené predĺženie jazdy úsekom (pomalá jazda, výluka jednej koľaje). Časy v minútach od t0.

    Rozšírenie kanonickej kópie (referencia ho nemá). Započíta sa len vtedy, keď vlak ide dotknutou časťou
    úseku počas okna [start, end] – napr. nočná pomalá jazda sa dennému vlaku nepočíta. part_from/part_to je
    poloha dotknutej časti v úseku ako podiel jazdnej doby (po zlúčení úsekov). Viac okien tej istej výluky
    (group, napr. denné opakovanie) sa pri jednej jazde tou istou časťou úseku počíta najviac raz.
    """

    start: float
    end: float
    extra_run: float = 0.0         # predĺženie jazdnej doby [min] (pomalá jazda), nezáporné
    extra_p50: float = 0.0         # zvýšenie mediánu zdržania [min] (napr. jednokoľajná prevádzka), nezáporné
    extra_p90: float = 0.0         # zvýšenie 90. percentilu zdržania [min], nezáporné
    group: Optional[int] = None    # výluka, z ktorej okno pochádza (None = samostatné okno)
    label: str = ""
    part_from: float = 0.0         # začiatok dotknutej časti úseku (podiel jazdnej doby úseku)
    part_to: float = 1.0           # koniec dotknutej časti úseku


@dataclass(frozen=True)
class Segment:
    """Úsek trasy medzi dvoma dopravňami."""

    name: str
    run_min: float                 # plánovaná jazdná doba vrátane plánovaných pobytov [min]
    delay_p50: float = 2.0         # medián náhodného zdržania na úseku [min] (z histórie)
    delay_p90: float = 10.0        # 90. percentil zdržania na úseku [min] (z histórie)
    closures: tuple = ()           # n-tica Closure
    can_wait_before: bool = True   # je pred úsekom dopravňa s dosť dlhou koľajou na čakanie?
    slowdowns: tuple = ()          # n-tica Slowdown (rozšírenie kanonickej kópie; prázdna = ako referencia)


@dataclass(frozen=True)
class Route:
    """Trasa vlaku (poradie úsekov) a jej vlastnosti."""

    name: str
    segments: tuple
    electric: bool = True          # vlak vedie elektrický rušeň
    extra_fixed_cost: float = 0.0  # napr. vyšší poplatok za dlhšiu trasu, nová trasa pri presmerovaní
    is_primary: bool = True        # False = alternatívna trasa (presmerovanie)


@dataclass(frozen=True)
class Params:
    """Predvolené parametre. Hodnoty sú NÁVRH na začiatok, treba ich kalibrovať.

    Tie isté kľúče a hodnoty sú v YAML bloku v PROMPT.md (test to kontroluje).
    """

    # Časové rezervy [min]
    buffer_before_closure_min: float = 30.0
    buffer_after_closure_min: float = 30.0
    deadline_buffer_min: float = 60.0
    crew_buffer_min: float = 60.0
    # Trasy (objednávky trás) [min]
    path_tolerance_min: float = 15.0
    new_path_lead_min: float = 360.0      # ad hoc trasa na krátky čas (P5) min. 6 h pred odchodom (PPŽS)
    max_hold_h: float = 18.0              # PIS ruší vlak neodídený do 18 h; odstavenie max. 18 h (PPŽS)
    # Prahy rozhodovania [pravdepodobnosti 0-1]
    p_on_time_green: float = 0.90
    p_on_time_amber: float = 0.70
    p_hold_green: float = 0.02
    p_hold_red: float = 0.10
    p_crew_relief_red: float = 0.10
    robustness_premium: float = 0.10
    # Ekonomika [EUR] - VZOROVÉ hodnoty, nahradiť vlastnými
    cost_wait_ready_per_h: float = 180.0
    cost_running_per_h: float = 300.0
    cost_stabled_per_h: float = 60.0
    cost_stable_fixed: float = 600.0
    cost_new_path: float = 0.0
    cost_late_fixed: float = 1500.0
    cost_late_per_h: float = 200.0
    cost_unplanned_hold: float = 800.0
    cost_stuck: float = 3000.0
    cost_crew_relief: float = 1200.0
    # Rušňovodič / čata [min]
    crew_max_duty_min: float = 780.0      # max. 13 h pracovného času na rušni (zadanie dispečera)
    crew_prep_min: float = 60.0           # nástup: prevzatie rušňa, skúška bŕzd (počíta sa do smeny)
    crew_post_min: float = 30.0           # po príchode: odstavenie a odovzdanie rušňa (počíta sa do smeny)
    crew_relief_delay_min: float = 120.0  # zdržanie pri mimoriadnej výmene rušňovodiča
    # Výpočet
    samples: int = 4000
    seed: int = 42


@dataclass
class Evaluation:
    """Výsledok vyhodnotenia jedného odchodu po jednej trase."""

    route: str
    depart: float
    kind: str
    p_on_time: float
    p_hold: float
    p_stuck: float
    p_crew_relief: float
    arrival_p50: float
    arrival_p90: float
    duty_p90: float
    expected_cost: float
    status: str = ""
    reasons: list = field(default_factory=list)
    needs_new_path: bool = False


@dataclass
class Recommendation:
    decision: str
    chosen: Optional[Evaluation]
    explanation: list
    green_windows: dict           # názov trasy -> zoznam (od, do) zelených okien odchodu
    candidates: list


# ---------------------------------------------------------------------------
# Pomocné funkcie
# ---------------------------------------------------------------------------
def hhmm(minutes: float) -> str:
    """Minúty od t0 -> 'HH:MM' (prípadne '+1d HH:MM')."""
    m = int(round(minutes))
    day, rem = divmod(m, 24 * 60)
    text = f"{rem // 60:02d}:{rem % 60:02d}"
    return f"+{day}d {text}" if day else text


def break_even_wait_hours(p: Params) -> float:
    """Po koľkých hodinách čakania sa oplatí radšej odstaviť.

    Čakanie stojí cost_wait_ready_per_h za hodinu. Odstavenie stojí jednorazovo
    cost_stable_fixed a potom cost_stabled_per_h za hodinu. Rovnosť nastane pri
    T = cost_stable_fixed / (cost_wait_ready_per_h - cost_stabled_per_h).
    """
    diff = p.cost_wait_ready_per_h - p.cost_stabled_per_h
    return math.inf if diff <= 0 else p.cost_stable_fixed / diff


def crew_check(shift_start: float, now: float, remaining_p50: float, remaining_p90: float,
               p: Params) -> dict:
    """Kontrola pracovnej doby rušňovodiča (limit crew_max_duty_min, predvolene 13 h).

    shift_start   - začiatok zmeny (nástup, prevzatie rušňa) [min od t0]
    now           - aktuálny čas [min od t0]
    remaining_p50 - zostávajúci čas jazdy do cieľa alebo miesta výmeny, medián [min]
    remaining_p90 - to isté, 90. percentil [min]

    Do pracovného času sa ráta aj ukončenie po príchode (crew_post_min).
    ČERVENÁ: aj pri bežnom priebehu (P50) by rušňovodič prekročil limit.
    ORANŽOVÁ: pri zlom priebehu (P90) by prekročil limit, alebo by mu ostalo
    menej ako crew_buffer_min - treba pripraviť výmenu.
    """
    limit = shift_start + p.crew_max_duty_min
    end50 = now + remaining_p50 + p.crew_post_min
    end90 = now + remaining_p90 + p.crew_post_min
    if end50 > limit:
        status = CERVENA
    elif end90 > limit - p.crew_buffer_min:
        status = ORANZOVA
    else:
        status = ZELENA
    return {
        "status": status,
        "worked_min": now - shift_start,
        "left_min": limit - now,
        "end_p50": end50,
        "end_p90": end90,
        "limit": limit,
        # najneskorší čas, keď musí nový rušňovodič prevziať vlak (vrátane odovzdania)
        "relieve_by": limit - p.crew_post_min,
    }


def _lognormal(seg: Segment, z: float) -> float:
    """Zdržanie na úseku: lognormálne rozdelenie s mediánom p50 a 90. percentilom p90."""
    p50 = max(seg.delay_p50, 0.01)
    p90 = max(seg.delay_p90, p50 * 1.0001)
    sigma = math.log(p90 / p50) / Z90
    return p50 * math.exp(sigma * z)


def _slowed(seg: Segment, z: float, t: float, travel: float) -> float:
    """Jazdná doba úsekom pri vstupe v čase t s obmedzeniami (Slowdown), ktoré platia, kým vlak ide ich časťou.

    travel = jazdná doba bez obmedzení (run_min + zdržanie pre to isté z). Rozšírenie kanonickej kópie.
    """
    extra = e50 = e90 = 0.0
    active = False
    groups = None
    for s in seg.slowdowns:
        if not (t + s.part_from * travel < s.end and t + s.part_to * travel > s.start):
            continue
        if s.group is not None:
            key = (s.group, s.part_from, s.part_to)
            if groups is None:
                groups = {key}
            elif key in groups:
                continue
            else:
                groups.add(key)
        active = True
        extra += s.extra_run
        e50 += s.extra_p50
        e90 += s.extra_p90
    if not active:
        return travel
    if not e50 and not e90:  # len dlhšia jazda (pomalá jazda) – zdržanie sa nemení
        return travel + extra
    p50 = max(seg.delay_p50 + e50, 0.01)
    p90 = max(seg.delay_p90 + e90, p50 * 1.0001)
    return seg.run_min + extra + p50 * math.exp(math.log(p90 / p50) / Z90 * z)


def _normals(n: int, k: int, seed: int) -> list:
    """Spoločné náhodné čísla pre všetky porovnávané varianty (férové porovnanie)."""
    rng = random.Random(seed)
    return [[rng.gauss(0.0, 1.0) for _ in range(k)] for _ in range(n)]


def _quantile(values: Sequence[float], q: float) -> float:
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(math.ceil(q * len(s))) - 1))
    return s[idx]


# ---------------------------------------------------------------------------
# Jadro: simulácia jednej varianty
# ---------------------------------------------------------------------------
def evaluate(route: Route, depart: float, ready: float, deadline: float, p: Params,
             normals: Optional[list] = None, stabled: bool = False,
             needs_new_path: bool = False, crew_start: Optional[float] = None) -> Evaluation:
    """Vyhodnotí odchod `depart` po trase `route` simuláciou Monte Carlo.

    Pravidlo výluky: vlak nesmie byť v úseku v čase
    [začiatok výluky - buffer_before_closure_min, koniec + buffer_after_closure_min].
    Ak by tam bol, čaká pred úsekom do konca tohto okna (neplánované zadržanie).
    Ak pred úsekom nie je kde čakať (can_wait_before=False), ide o zablokovanie.
    Obmedzenie bez zastavenia (Segment.slowdowns – pomalá jazda, výluka koľaje) predĺži jazdu úsekom len pri
    jazde jeho časťou počas jeho platnosti; čas vstupu do úseku sa berie z každej vzorky (odchod, zdržania
    a čakanie pred výlukami), nie z mediánu – nočné obmedzenie sa tak dennému vlaku nepočíta.
    """
    segs = route.segments
    if normals is None:
        normals = _normals(p.samples, len(segs), p.seed)
    n = len(normals)
    arrivals, duties, costs = [], [], []
    on_time = holds = stucks = reliefs = 0
    # Ak je rušňovodič už v službe (crew_start), pracovný čas beží od jeho nástupu;
    # inak nastúpi crew_prep_min pred odchodom.
    duty_start = crew_start if crew_start is not None else depart - p.crew_prep_min
    wait_h = max(0.0, depart - ready) / 60.0
    if stabled:
        base_cost = p.cost_stable_fixed + wait_h * p.cost_stabled_per_h
    else:
        base_cost = wait_h * p.cost_wait_ready_per_h
    base_cost += route.extra_fixed_cost + (p.cost_new_path if needs_new_path else 0.0)
    # obmedzenia úsekov ako n-tice (rýchla kontrola vo vzorkách); okno, ktoré skončí skôr, než vlak môže
    # do úseku najskôr vojsť (odchod + jazdné doby predchádzajúcich úsekov), sa nikdy neuplatní
    slow, earliest = [], depart
    for seg in segs:
        slow.append(tuple((s.start, s.end, s.part_from, s.part_to) for s in seg.slowdowns if s.end > earliest))
        earliest += seg.run_min

    for i in range(n):
        t = depart
        hold = stuck = False
        for k, seg in enumerate(segs):
            base_travel = travel = seg.run_min + _lognormal(seg, normals[i][k])
            if slow[k]:
                for a, b, f0, f1 in slow[k]:
                    if t + f0 * base_travel < b and t + f1 * base_travel > a:
                        travel = _slowed(seg, normals[i][k], t, base_travel)
                        break
            moved = True
            while moved:  # po čakaní znova skontrolovať všetky výluky na úseku
                moved = False
                for c in seg.closures:
                    if c.electric_only and not route.electric:
                        continue
                    w_start = c.start - p.buffer_before_closure_min
                    w_end = c.end + p.buffer_after_closure_min
                    if t < w_end and t + travel > w_start:
                        hold = True
                        stuck = stuck or not seg.can_wait_before
                        t = w_end
                        moved = True
                if moved and slow[k]:  # po čakaní ide vlak úsekom v inom čase
                    travel = _slowed(seg, normals[i][k], t, base_travel)
            t += travel
        # Pracovný čas rušňovodiča: od nástupu (príprava pred odchodom) po odovzdanie rušňa.
        duty = t + p.crew_post_min - duty_start
        relief = duty > p.crew_max_duty_min
        if relief:
            t += p.crew_relief_delay_min
        late = max(0.0, t - deadline)
        cost = base_cost + (t - depart) / 60.0 * p.cost_running_per_h
        if hold:
            cost += p.cost_unplanned_hold
        if stuck:
            cost += p.cost_stuck
        if relief:
            cost += p.cost_crew_relief
        if late > 0:
            cost += p.cost_late_fixed + late / 60.0 * p.cost_late_per_h
        arrivals.append(t)
        duties.append(duty)
        costs.append(cost)
        on_time += late <= 0
        holds += hold
        stucks += stuck
        reliefs += relief

    ev = Evaluation(
        route=route.name, depart=depart,
        kind=VYPRAVIT if depart <= ready + 1e-9 else (ODSTAVIT if stabled else POCKAT),
        p_on_time=on_time / n, p_hold=holds / n, p_stuck=stucks / n,
        p_crew_relief=reliefs / n,
        arrival_p50=_quantile(arrivals, 0.5), arrival_p90=_quantile(arrivals, 0.9),
        duty_p90=_quantile(duties, 0.9), expected_cost=sum(costs) / n,
        needs_new_path=needs_new_path)
    _classify(ev, deadline, p)
    return ev


def _classify(ev: Evaluation, deadline: float, p: Params) -> None:
    """Semafor: ČERVENÁ = nevypravovať, ORANŽOVÁ = len s potvrdením, ZELENÁ = odporúčané."""
    red, amber = [], []
    if ev.p_stuck > 0:
        red.append(f"riziko zablokovania trate pred výlukou ({ev.p_stuck:.0%})")
    if ev.p_on_time < p.p_on_time_amber:
        red.append(f"P(včas) {ev.p_on_time:.0%} < {p.p_on_time_amber:.0%}")
    elif ev.p_on_time < p.p_on_time_green:
        amber.append(f"P(včas) {ev.p_on_time:.0%} < {p.p_on_time_green:.0%}")
    if ev.p_hold > p.p_hold_red:
        red.append(f"P(zadržania pred výlukou) {ev.p_hold:.0%} > {p.p_hold_red:.0%}")
    elif ev.p_hold > p.p_hold_green:
        amber.append(f"P(zadržania pred výlukou) {ev.p_hold:.0%}")
    if ev.p_crew_relief > p.p_crew_relief_red:
        red.append(f"P(prekročenia smeny čaty) {ev.p_crew_relief:.0%}")
    elif ev.duty_p90 > p.crew_max_duty_min - p.crew_buffer_min:
        amber.append("malá rezerva v smene čaty")
    if deadline - ev.arrival_p50 < p.deadline_buffer_min:
        amber.append(f"rezerva voči termínu (medián) {deadline - ev.arrival_p50:.0f} min "
                     f"< {p.deadline_buffer_min:.0f} min")
    ev.status = CERVENA if red else (ORANZOVA if amber else ZELENA)
    ev.reasons = red + amber


# ---------------------------------------------------------------------------
# Odporúčanie: prehľadá odchody v horizonte a vyberie najlepšiu variantu
# ---------------------------------------------------------------------------
def _needs_new_path(depart: float, allocated: Sequence[float], p: Params) -> bool:
    if not allocated:
        return False
    return all(abs(depart - a) > p.path_tolerance_min for a in allocated)


def _windows(evals: Sequence[Evaluation], step: float) -> list:
    """Súvislé intervaly odchodov so ZELENOU (od, do)."""
    out = []
    for ev in sorted(evals, key=lambda e: e.depart):
        if ev.status != ZELENA:
            continue
        if out and ev.depart - out[-1][1] <= step + 1e-9:
            out[-1] = (out[-1][0], ev.depart)
        else:
            out.append((ev.depart, ev.depart))
    return out


def recommend(routes: Sequence[Route], now: float, ready: float, deadline: float,
              p: Params = Params(), horizon_min: float = 24 * 60, step_min: float = 10,
              allocated_departures: Sequence[float] = (),
              crew_start: Optional[float] = None,
              stable_needs_new_path: bool = False) -> Recommendation:
    """Prehľadá odchody od `ready` po `ready + horizon_min` po každej trase.

    Pravidlá výberu:
      1. ČERVENÉ varianty sa vylúčia.
      2. Odchod mimo pridelenej trasy (± path_tolerance_min) vyžaduje novú trasu:
         možný najskôr now + new_path_lead_min a stojí cost_new_path.
      3. Pri čakaní sa porovná "stáť pripravený" vs. "odstaviť" (bod zlomu
         break_even_wait_hours) a použije sa lacnejšie.
      4. Vyberie sa najnižší očakávaný náklad. Ak je víťaz ORANŽOVÝ a existuje
         ZELENÁ varianta drahšia najviac o robustness_premium, vyberie sa ZELENÁ.
      5. Ak nič neostane, rozhodnutie je ESKALOVAŤ (ŽSR + zákazník).

    Rozšírenia kanonickej kópie (referencia ich nemá, predvolené hodnoty správanie nemenia):
      crew_start – rušňovodič už v službe (POČKAŤ: jeho pracovný čas beží ďalej),
      stable_needs_new_path – odstavenie ruší trasu (idúci vlak): ODSTAVIŤ vždy znamená novú trasu,
        teda najskôr now + new_path_lead_min.
    """
    candidates, windows = [], {}
    horizon_min = min(horizon_min, p.max_hold_h * 60.0)  # dlhšie čakanie nie je podľa PPŽS prípustné
    k = int(horizon_min // step_min)
    for route in routes:
        normals = _normals(p.samples, len(route.segments), p.seed)
        per_route = []
        for j in range(k + 1):
            depart = ready + j * step_min
            # Alternatívna trasa vždy znamená novú trasu; pri hlavnej trase len odchod
            # mimo pridelenej trasy (± path_tolerance_min).
            new_path = (not route.is_primary) or _needs_new_path(depart, allocated_departures, p)
            if new_path and depart < now + p.new_path_lead_min:
                continue  # novú trasu by ŽSR nestihla prideliť
            # POČKAŤ: ten istý rušňovodič čaká (jeho pracovný čas beží od crew_start, ak už je v službe)
            options = [evaluate(route, depart, ready, deadline, p, normals, False, new_path, crew_start)]
            if depart > ready:
                # ODSTAVIŤ: rušňovodič sa uvoľní, neskôr nastúpi nový (pracovný čas od depart - prep)
                st_new = new_path or stable_needs_new_path
                if not (st_new and depart < now + p.new_path_lead_min):
                    options.append(evaluate(route, depart, ready, deadline, p, normals, True, st_new))
            best = min(options, key=lambda e: e.expected_cost)
            if not route.is_primary:
                best.kind = PRESMEROVAT
            per_route.append(best)
        windows[route.name] = _windows(per_route, step_min)
        candidates.extend(per_route)

    decision, best, explanation = select_best(candidates, p)
    return Recommendation(decision, best, explanation, windows, candidates)


def select_best(candidates: Sequence[Evaluation], p: Params) -> tuple:
    """Pravidlá výberu (4. a 5. krok v recommend) – samostatne, aby sa dali použiť aj po spresnení."""
    allowed = [e for e in candidates if e.status != CERVENA]
    if not allowed:
        least_bad = min(candidates, key=lambda e: e.expected_cost) if candidates else None
        return (ESKALOVAT, least_bad,
                ["Žiadna bezpečná varianta v horizonte. Kontaktovať ŽSR (nová trasa / iný čas) "
                 "a zákazníka (nový termín)."])

    best = min(allowed, key=lambda e: (e.expected_cost, e.depart))
    explanation = []
    if best.status == ORANZOVA:
        greens = [e for e in allowed if e.status == ZELENA
                  and e.expected_cost <= best.expected_cost * (1 + p.robustness_premium)]
        if greens:
            best = min(greens, key=lambda e: (e.expected_cost, e.depart))
            explanation.append("Uprednostnená ZELENÁ varianta (spoľahlivosť za max. "
                               f"{p.robustness_premium:.0%} vyšší náklad).")
    explanation.append(
        f"{best.kind} po trase '{best.route}' s odchodom {hhmm(best.depart)}: "
        f"P(včas) {best.p_on_time:.0%}, príchod medián {hhmm(best.arrival_p50)}, "
        f"90 % do {hhmm(best.arrival_p90)}, očakávaný náklad {best.expected_cost:,.0f} EUR.")
    if best.needs_new_path:
        explanation.append("Treba objednať novú (ad hoc) trasu u ŽSR.")
    if best.status != ZELENA:
        explanation.append("ORANŽOVÁ: vyžaduje potvrdenie dispečera. Dôvody: " + "; ".join(best.reasons))
    return best.kind, best, explanation


# ---------------------------------------------------------------------------
# Fiktívny príklad (výluka, časy aj náklady sú vymyslené - len ilustrácia výpočtu)
# ---------------------------------------------------------------------------
def example() -> tuple:
    h = 60
    closure = Closure(start=10 * h, end=14 * h, label="výluka 10:00-14:00 (fiktívna)")
    primary = Route("Košice - Žilina", (
        Segment("Košice - Spišská Nová Ves", 80, 3, 12),
        Segment("Spišská Nová Ves - Poprad-Tatry", 40, 2, 8),
        Segment("Poprad-Tatry - Liptovský Mikuláš", 60, 2, 10, (closure,)),
        Segment("Liptovský Mikuláš - Žilina", 95, 4, 15),
    ))
    detour = Route("Košice - Žilina obchádzka (fiktívna)", (
        Segment("obchádzka časť 1", 150, 5, 20),
        Segment("obchádzka časť 2", 170, 5, 25),
    ), extra_fixed_cost=400.0, is_primary=False)
    return primary, detour


def _print(title: str, rec: Recommendation, shown: set) -> None:
    print("=" * 100)
    print(title)
    print(f"{'trasa':38} {'odchod':>6} {'variant':>11} {'stav':>9} {'P(včas)':>7} "
          f"{'P(zadrž.)':>9} {'príchod50':>9} {'príchod90':>9} {'náklad €':>9}")
    for ev in rec.candidates:
        if ev.depart in shown:
            print(f"{ev.route[:38]:38} {hhmm(ev.depart):>6} {ev.kind:>11} {ev.status:>9} "
                  f"{ev.p_on_time:>7.0%} {ev.p_hold:>9.0%} {hhmm(ev.arrival_p50):>9} "
                  f"{hhmm(ev.arrival_p90):>9} {ev.expected_cost:>9,.0f}")
    print("Zelené okná odchodu:")
    for name, wins in rec.green_windows.items():
        txt = ", ".join(f"{hhmm(a)}-{hhmm(b)}" for a, b in wins) or "žiadne"
        print(f"  {name}: {txt}")
    print("ODPORÚČANIE:", rec.decision)
    for line in rec.explanation:
        print("  -", line)


def main() -> None:
    p = Params()
    primary, detour = example()
    h = 60
    deadline = 18 * h
    print("FIKTÍVNY PRÍKLAD: vlak Košice -> Žilina, termín u zákazníka 18:00,")
    print("výluka Poprad-Tatry - Liptovský Mikuláš 10:00-14:00 (rezervy 30/30 min).")
    print(f"Bod zlomu čakať/odstaviť: {break_even_wait_hours(p):.1f} h")

    # 1) Vlak je pripravený načas (05:30) na pridelenej trase 05:30.
    rec1 = recommend([primary], now=5 * h, ready=5 * h + 30, deadline=deadline, p=p,
                     horizon_min=12 * h, step_min=10, allocated_departures=[5 * h + 30])
    _print("SCENÁR 1: pripravený o 05:30 (trasa 05:30)", rec1, {5 * h + 30, 5 * h + 40})
    # Okno bezpečného odchodu bez ohľadu na trasy (koľko času je "v zálohe"):
    free = recommend([primary], now=5 * h, ready=5 * h + 30, deadline=deadline, p=p,
                     horizon_min=2 * h, step_min=5)
    win = free.green_windows[primary.name]
    if win:
        print(f"  - Najneskorší bezpečný odchod pred výlukou: {hhmm(win[0][1])}")

    # 2) Vlak je pripravený o hodinu neskôr (06:30), obchádzka nie je možná.
    shown = {9 * h, 12 * h, 12 * h + 30, 12 * h + 40, 13 * h}
    rec2 = recommend([primary], now=6 * h, ready=6 * h + 30, deadline=deadline, p=p,
                     horizon_min=12 * h, step_min=10, allocated_departures=[5 * h + 30])
    _print("SCENÁR 2: pripravený až o 06:30, bez obchádzky", rec2, shown)

    # 3) Ako 2, ale existuje obchádzka (fiktívna).
    rec3 = recommend([primary, detour], now=6 * h, ready=6 * h + 30, deadline=deadline, p=p,
                     horizon_min=12 * h, step_min=10, allocated_departures=[5 * h + 30])
    _print("SCENÁR 3: pripravený až o 06:30, obchádzka je možná", rec3, shown)

    # 4) Pracovná doba rušňovodiča (limit 13 h vrátane prípravy a odovzdania rušňa).
    print("=" * 100)
    print("SCENÁR 4: rušňovodič nastúpil o 04:00, je 14:00, do cieľa zostáva P50 2 h 10 min, "
          "P90 2 h 50 min")
    c = crew_check(shift_start=4 * h, now=14 * h, remaining_p50=130, remaining_p90=170, p=p)
    print(f"  odpracované {c['worked_min'] / 60:.1f} h, do limitu {c['left_min'] / 60:.1f} h, "
          f"limit {hhmm(c['limit'])}, koniec P50 {hhmm(c['end_p50'])}, P90 {hhmm(c['end_p90'])}")
    print(f"  STAV: {c['status']} - vystriedať najneskôr o {hhmm(c['relieve_by'])}")


if __name__ == "__main__":
    main()
