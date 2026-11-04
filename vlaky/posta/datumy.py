"""Rozpoznávanie dátumov, časov a období v texte e-mailov (slovenčina a čeština, aj bez diakritiky).

Pracuje nad textom z :func:`vlaky.posta.text.fold` (malé písmená, bez diakritiky, rovnaká dĺžka ako
originál). Výsledkom sú **miestne časy bez zóny** (Europe/Bratislava) – prevod na UTC robí volajúci
cez :func:`vlaky.cas.local_naive_to_utc`.

Podporované zápisy (príklady):
  od 12.11.2026 08:00 do 12.11.2026 14:00 · 12.11.2026 v čase 08:00 – 14:00
  dňa 12. 11. 2026 od 8.00 hod. do 14.00 hod. · denne 08:00–14:00 v dňoch 10.11.–14.11.2026
  v dňoch 10. – 14. 11. 2026 · 12. novembra 2026 · 2026-11-12T08:00 · od 05.11.2026 do odvolania
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

MONTHS = {
    # slovensky
    "januara": 1, "februara": 2, "marca": 3, "aprila": 4, "maja": 5, "juna": 6, "jula": 7,
    "augusta": 8, "septembra": 9, "oktobra": 10, "novembra": 11, "decembra": 12,
    # česky
    "ledna": 1, "unora": 2, "brezna": 3, "dubna": 4, "kvetna": 5, "cervna": 6, "cervence": 7,
    "srpna": 8, "zari": 9, "rijna": 10, "listopadu": 11, "prosince": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{1,2}):(\d{2})(?::\d{2})?)?(?!\d)")
_DAYRANGE_RE = re.compile(
    r"(?<![\d.,/:])(\d{1,2})\.?\s?-\s?(\d{1,2})\s?\.\s?(\d{1,2})\s?\.(?:\s?(\d{4}|\d{2})(?![\d:]|\.\d))?")
_DATE_RE = re.compile(r"(?<![\d.,/:])(\d{1,2})\s?\.\s?(\d{1,2})\s?\.(?:\s?(\d{4}|\d{2})(?![\d:]|\.\d))?")
# druhý dátum hneď za lomkou („v noci 12.11.2026/13.11.2026“, „12.11./13.11.2026“) – _DATE_RE ho nevezme (lomka
# pred ním), bez neho by sa „13.11“ prečítalo ako čas 13:11; platí len za iným dátumom
_SLASH_DATE_RE = re.compile(r"/(\d{1,2})\s?\.\s?(\d{1,2})\s?\.(?:\s?(\d{4}|\d{2})(?![\d:]|\.\d))?")
# noc z jedného dňa na druhý: „z 12. na 13.11.2026“, „12./13.11.2026“, „z 12. na 13. novembra 2026“ – obdobie
# začína prvým dňom (skupiny: deň, deň, mesiac číslom, mesiac slovom, rok)
_NIGHT_RE = re.compile(r"(?<![\d.,/:])(\d{1,2})\s?\.\s?(?:na\s+|/\s?)(\d{1,2})\s?\.\s?(?:(\d{1,2})\s?\.|(" + _MONTH_ALT
                       + r")\b)(?:\s?(\d{4}|\d{2})(?![\d:]|\.\d))?")
_NIGHT_GAP = re.compile(r"^\s*(?:na|/)\s*$")
# noc je to len vtedy, keď ju text tak označuje („v noci“, „nočná“) alebo okno za ňou prechádza cez polnoc;
# „presúva sa z 12.11. na 13.11.“ je presun termínu na nový deň (nie noc)
_NIGHT_MARK = re.compile(r"\bnoc")
# označenie noci ďalej pred dňami, ak sú medzi nimi len ďalšie dátumy noci („v nociach od 30.11./1.12. do 2./3.12.“)
_NIGHT_MARK_FAR = re.compile(r"\bnoc\w*")
_NIGHT_CHAIN = re.compile(r"(?:[\s\d./,;–-]|\b(?:z|zo|od|do|az|a|aj|na)\b)*\Z")
_SHIFT_MARK = re.compile(r"\b(?:presuv|presun|posuv|posun|preklad|preloz|odklad|odloz|meni\b|menia\b|zmeni\b)\w*"
                         r"\s+(?:sa\s+)?zo?\s+(?:termin\w*\s+|dna\s+)?$")
_WINDOW_RE = re.compile(r"(?<![\d.:,])(\d{1,2})\s?[:.]\s?(\d{2})(?!\d)(?:\s?(?:hod\b\.?|h\b\.?))?\s*(?:-+|do|az)\s*"
                        r"(\d{1,2})\s?[:.]\s?(\d{2})(?!\d)")
# zoznam dní či nocí s jedným oknom: „12.11. a 13.11.2026 od 8:00“, „v nociach 12./13.11. a 13./14.11.2026“
_LIST_GAP = re.compile(r"^\s*(?:,|;|/|\+|&|a|aj|i|a\s+tiez|a\s+aj|ako\s+aj)\s*(?:z\s+|zo\s+|dna\s+|dne\s+)?$")
# holý deň (alebo dvojica dní noci) tesne pred plným dátumom – skrátený zoznam „v dňoch 03., 04. a 05.11.2026“,
# „v nociach 03./04. a 04./05.11.2026“ alebo začiatok obdobia „od 3. do 5.11.2026“; mesiac a rok z plného dátumu
_BARE_BEFORE = re.compile(r"(?<![\d.,/:])(?<!\d\.\s)(\d{1,2})\s?\.(?:\s?(na\s+|/\s?)(\d{1,2})\s?\.)?"
                          r"(?:(?P<lst>\s*(?:,|;|\+|&|a|aj|i|a\s+tiez|a\s+aj|ako\s+aj)\s*(?:z\s+|zo\s+|dna\s+|dne\s+)?)"
                          r"|(?P<rng>\s*(?:-+|do|az)\s*(?:dna\s*|dne\s*)?))\Z")
# začiatok rozsahu nocí tesne pred nocou s mesiacom: „v nociach od 12./13. do 14./15.11.“, „z 12. na 13. až
# z 14. na 15.11.“ – mesiac a rok z druhej noci
_NIGHT_RANGE_BEFORE = re.compile(r"(?<![\d.,/:])(\d{1,2})\s?\.\s?(?:na\s+|/\s?)(\d{1,2})\s?\.(?P<to>\s*(?:-+|do|az)\s*)"
                                 r"(?:z\s+|zo\s+)?\Z")
# dátum, ktorý podľa textu pred ním nie je termínom: pôvodný deň presunu („z 12.11. na 13.11.“, „namiesto 12.11.“),
# dátum vydania, vyhotovenia či rozhodnutia („ROV z 30.10.“, „zo dňa 30.10.“), platnosť samotného rozkazu
NOT_TERM_BEFORE = re.compile(r"(?<![^\s(])(?:z|zo|ze)\s+(?:termin\w*\s+)?$|(?:zo|ze)\s+dn[ae]\s*$|v\s+\w+\s+dn[ae]\s*$"
                             r"|\b(?:namiesto|namisto|misto)\s*$|povodn|puvodn|doterajs|vydan|vydavan|rozhodnut"
                             r"|vyhotoven|vypracovan|\b(?:platnost|ucinnost)\w*\s+(?:rov|srov|rozkaz|telegram|tlg|potr"
                             r"|oznamen)\w*\s+od\s*$")
# ďalšie časové okno v tej istej vete: „8:00 – 12:00 a 13:00 – 16:00“, „8–12, 13–16 hod.“
_AND_GAP = re.compile(r"^\s*(?:hod\.?\s*)?(?:,|;|\+|&|a|aj|a\s+tiez|a\s+aj)?\s*(?:od\s+|v\s+case\s+|v\s+)?$")
# „obsahuje čas“ – 08:00, 8.00 hod.; nie dátum 23.11.2026
HAS_TIME = re.compile(r"(?<![\d.])\d{1,2}\s?:\s?\d{2}(?!\d)|(?<![\d.])\d{1,2}\.\d{2}(?![\d.])")
_DATE_NAME_RE = re.compile(rf"(?<![\d.])(\d{{1,2}})\s?\.\s?({_MONTH_ALT})(?:\s+(\d{{4}}))?\b")
_TIME_RE = re.compile(r"(?<![\d.:,])(\d{1,2})\s?[:.]\s?(\d{2})(?![\d])(?:\s?(?:hod\b\.?|h\b\.?))?")
_HOUR_RE = re.compile(r"(?<![\d.:,])(\d{1,2})\s?(?:hod\b\.?|h\b\.?)")
_HOUR_CTX_RE = re.compile(r"(?:\bo|\bod|\bdo|-|\baz|\bv|\bvo)\s*$")

# medzery medzi tokenmi
_AT_GAP = re.compile(r"^[\s,]*(?:(?:o|v|vo|od|o\s+case|v\s+case|case|cas|cca|asi|at|um|zaciatok|s\s+casom)\b[\s,:]*)*$")
_TIME_DATE_GAP = re.compile(r"^[\s,]*(?:(?:hod\.?|h\.?|dna|dne|dnom|v\s+den|zo\s+dna|ze\s+dne)\b[\s,.:]*)*$")
_TO_GAP = re.compile(r"^\s*(?:hod\.?\s*)?(?:-+|do|az|until|till|bis|>)\s*(?:dna\s*|dne\s*)?$")
_UNTIL_REVOKED = re.compile(r"^\s*(?:hod\.?\s*)?(?:do\s+(?:odvolania|odvolani|odvolani|odvolania)|az\s+do\s+odvolani\w*)")
_DAILY_RE = re.compile(r"\b(?:denne|kazdy\s+den|kazdodenne|vzdy|v\s+jednotlivych\s+dnoch|v\s+dnoch|ve\s+dnech|v\s+case|dennne)\b")


@dataclass
class Temporal:
    """Dátum, čas alebo dátum s časom nájdený v texte."""

    kind: str                       # date | time | datetime
    start: int
    end: int
    day: Optional[date] = None
    tm: Optional[time] = None
    plus_day: bool = False          # 24:00 → nasledujúci deň 00:00
    year_given: bool = True
    month_day: Optional[tuple] = None  # (deň, mesiac) pri dátume bez roka
    # noc D/D+1 („v noci z 12. na 13.11.“): ``day`` je jej prvý deň; okno po polnoci (začiatok pred 12:00) patrí
    # k druhému dňu – pri dátume s časom je ``day`` už tento deň
    night: bool = False
    unordered: bool = False         # rok doplnený, lebo zoznam dní nie je chronologický („3.12. a 27.11.2026“)

    @property
    def dt(self) -> Optional[datetime]:
        if self.day is None or self.tm is None:
            return None
        d = datetime.combine(self.day, self.tm)
        return d + timedelta(days=1) if self.plus_day else d


@dataclass
class Period:
    """Obdobie platnosti (miestny čas bez zóny)."""

    start: Optional[datetime]
    end: Optional[datetime]
    pos: int
    end_pos: int
    daily_from: Optional[str] = None
    daily_to: Optional[str] = None
    flags: set = field(default_factory=set)
    # flagy: cely_den, cez_polnoc, rok_doplneny, do_odvolania, bez_konca, bez_casu_zaciatku, bez_casu_konca, denne,
    # zle_poradie


def _year(y: Optional[str]) -> Optional[int]:
    if not y:
        return None
    v = int(y)
    return 2000 + v if v < 100 else v


def _mkdate(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _infer_year(month: int, day_: int, ref: date) -> Optional[date]:
    """Rok pre dátum bez roka: najbližší výskyt okolo referenčného dátumu (nie starší ako ~6 mes.)."""
    d = _mkdate(ref.year, month, day_)
    if d is None:
        return None
    if d < ref - timedelta(days=180):
        d = _mkdate(ref.year + 1, month, day_) or d
    elif d > ref + timedelta(days=185):
        d = _mkdate(ref.year - 1, month, day_) or d
    return d


def _overlaps(spans: list, a: int, b: int) -> bool:
    return any(a < e and s < b for s, e in spans)


def _crosses_midnight(folded: str, pos: int) -> bool:
    """Prvé časové okno za pozíciou (do ~80 znakov) končí na druhý deň („od 22:00 do 5:00“)."""
    m = _WINDOW_RE.search(folded, pos, min(len(folded), pos + 80))
    if m is None:
        return False
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    return b <= a and b < 24 * 60


def _is_night(folded: str, start: int, end: int, slash: bool) -> bool:
    """Dva po sebe idúce dni („12./13.11.“, „z 12. na 13.11.“) sú noc, len ak to text naznačuje.

    Noc: pred dňami je „noc…“ („v noci“, „nočná“, „v nociach“) alebo časové okno za nimi prechádza cez polnoc.
    Presun termínu („presúva sa / mení sa / preložená z 12.11. na 13.11.“) nocou nie je – platí nový deň.
    """
    marked = bool(_NIGHT_MARK.search(folded, max(0, start - 25), start))
    if not marked:
        far = None
        for far in _NIGHT_MARK_FAR.finditer(folded, max(0, start - 70), start):
            pass
        marked = far is not None and bool(_NIGHT_CHAIN.match(folded, far.end(), start))
    if not slash and not marked and _SHIFT_MARK.search(folded[max(0, start - 50):start]):
        return False
    return marked or _crosses_midnight(folded, end)


def _bare_items(folded: str, pos: int, taken: list, allow_range: bool) -> list[tuple]:
    """Holé dni zoznamu tesne pred plným dátumom na pozícii ``pos`` (zľava doprava).

    Položka: (začiatok, koniec, deň, druhý deň dvojice alebo None, pozícia „/“ či „na“ v dvojici, „na“-tvar).
    Rozsah („od 3. do 5.11.“, „3. – 5. a 10.11.“) je len medzi dvoma dňami (nie pri nociach); pred plným
    dátumom len pri ``allow_range`` (plný dátum je jeden deň)."""
    out: list[tuple] = []
    while True:
        m = _BARE_BEFORE.search(folded, max(0, pos - 30), pos)
        if m is None:
            break
        rng = m.group("rng") is not None
        item_end = m.start("rng" if rng else "lst")
        if _overlaps(taken, m.start(), item_end):
            break
        if rng and (m.group(3) or (out[-1][3] is not None if out else not allow_range)):
            break
        pair = m.group(2)
        out.append((m.start(), item_end, int(m.group(1)), int(m.group(3)) if m.group(3) else None,
                    m.start(2) if pair else None, bool(pair) and "/" not in pair))
        pos = m.start()
    out.reverse()
    return out


def _prev_month(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


def _range_night(d1: int, d2: int, last: date) -> Optional[date]:
    """Prvý deň prvej noci rozsahu „od d1./d2. do …“ pred nocou s prvým dňom ``last`` (mesiac z nej, aj cez prelom
    mesiaca); None, ak dni nie sú dva po sebe idúce alebo rozsah nedáva zmysel (nevymýšľa sa)."""
    first = _mkdate(last.year, last.month, d1)
    if first is None or first >= last:
        prev = _prev_month(last)
        first = _mkdate(prev.year, prev.month, d1)
    if first is None or not (0 < (last - first).days <= 31) or (first + timedelta(days=1)).day != d2:
        return None
    return first


def _expand_bare(items: list[tuple], first: date, year_given: bool, night: bool) -> list[Temporal]:
    """Tokeny holých dní zoznamu (``items`` z :func:`_bare_items`) pred plným dátumom s prvým dňom ``first``.

    Zoznam ide chronologicky: deň položky je v mesiaci nasledujúcej položky, ak je pred ňou, inak v mesiaci
    predchádzajúcom („30. a 1.12.“ = 30.11. a 1.12.). Noc (``night``) = dvojica po sebe idúcich dní, token je jej
    prvý deň; mimo noci dvojica „12./13.“ = oba dni a „z 12. na 13.“ zoznam ukončí. Nezmyselná položka (zlý deň,
    zlé poradie) zoznam ukončí – ďalšie dni sa nevymýšľajú (nepriradené dni ohlási kontrola pravidiel)."""
    out: list[Temporal] = []
    nxt = first

    def tok(a: int, b: int, d: date, is_night: bool = False) -> Temporal:
        return Temporal("date", a, b, d, year_given=year_given, month_day=None if year_given else (d.day, d.month),
                        night=is_night)

    for s, e, d1, d2, sep, na in reversed(items):
        if (na and not night) or (night and d2 is None):
            break
        last = d2 if d2 is not None else d1
        shared = night and d2 is not None          # susedné noci majú spoločný deň („03./04. a 04./05.“)
        cand = _mkdate(nxt.year, nxt.month, last)
        if cand is None or cand > nxt or (cand == nxt and not shared):
            prev = _prev_month(nxt)
            cand = _mkdate(prev.year, prev.month, last)
            if cand is not None and (nxt - cand).days > 10:
                cand = None                         # „5. a 3.11.“ – nie je to prelom mesiacov, ale chyba poradia
        if cand is None or cand > nxt or (cand == nxt and not shared):
            break
        day1 = cand - timedelta(days=1) if d2 is not None else cand
        if day1.day != d1:
            break                                   # dvojica nie sú dva po sebe idúce dni
        if d2 is None or night:
            out.append(tok(s, e, day1, night))
        else:                                       # „12./13.“ v zozname dní – oba dni
            out.append(tok(sep + 1, e, cand))
            out.append(tok(s, sep, day1))
        nxt = day1
    out.reverse()
    return out


def _add_bare(folded: str, toks: list[Temporal], taken: list, pos: int, first: date, year_given: bool,
              allow_range: bool) -> None:
    """Holé dni zoznamu (alebo začiatok obdobia) pred plným dátumom na pozícii ``pos`` pridá ako dátumy."""
    for t in _expand_bare(_bare_items(folded, pos, taken, allow_range), first, year_given, night=False):
        toks.append(t)
        taken.append((t.start, t.end))


# číslo položky zoznamu na začiatku riadku pred dátumom („1. 01.12.2026 od 08:00“, „2. 03.12.2026“) – nie je to deň
_ENUM_BEFORE_DATE = re.compile(r"(?m)^([ \t>*•-]*)(\d{1,2}\.)(?=[ \t]+\d{1,2}\s?\.\s?\d{1,2}\s?\.)")


def scan(folded: str, ref: date) -> list[Temporal]:
    """Nájde dátumy a časy, spojí dátum + čas do dátumu s časom. Výsledok je zoradený podľa pozície.

    Číslo položky zoznamu pred dátumom („1. 01.12.2026“) sa za deň nepovažuje."""
    toks: list[Temporal] = []
    taken: list[tuple[int, int]] = []
    folded = _ENUM_BEFORE_DATE.sub(lambda m: m.group(1) + " " * len(m.group(2)), folded)

    for m in _ISO_RE.finditer(folded):
        d = _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if not d:
            continue
        if m.group(4) is not None:
            hh, mm = int(m.group(4)), int(m.group(5))
            if hh > 24 or mm > 59:
                continue
            t = Temporal("datetime", m.start(), m.end(), d, time(hh % 24, mm), plus_day=hh == 24)
        else:
            t = Temporal("date", m.start(), m.end(), d)
        toks.append(t)
        taken.append((m.start(), m.end()))

    chain_end, chain_pos = -1, 0          # predchádzajúca noc (zoznam či rozsah nocí s plnými dátumami)
    for m in _NIGHT_RE.finditer(folded):
        if _overlaps(taken, m.start(), m.end()):
            continue
        d1, d2 = int(m.group(1)), int(m.group(2))
        mon = int(m.group(3)) if m.group(3) else MONTHS[m.group(4)]
        y = _year(m.group(5))
        if not (1 <= mon <= 12):
            continue
        b = _mkdate(y, mon, d2) if y else _infer_year(mon, d2, ref)
        if b is None or (b - timedelta(days=1)).day != d1:
            continue                      # nie sú to dva po sebe idúce dni – nie je to noc
        slash = "/" in folded[m.start():m.end()]
        a = b - timedelta(days=1)
        md_a = None if y else (a.day, a.month)          # rok doplní _fix_years z ďalšieho dátumu v zozname
        # skrátený zoznam nocí pred ňou („v nociach 03./04. a 04./05.11.“) alebo začiatok rozsahu nocí („od 12./13.
        # do 14./15.11.“) – označenie noci sa hľadá pred celým zoznamom či rozsahom
        items = _bare_items(folded, m.start(), taken, allow_range=False)
        rng = None if items else _NIGHT_RANGE_BEFORE.search(folded, max(0, m.start() - 30), m.start())
        if rng is not None and _overlaps(taken, rng.start(), rng.end()):
            rng = None
        pos = items[0][0] if items else (rng.start() if rng else m.start())
        if chain_end >= 0 and (_LIST_GAP.match(folded[chain_end:pos]) or _TO_GAP.match(folded[chain_end:pos])):
            pos = chain_pos               # ďalšia noc zoznamu („12./13.11., 13./14.11. a 14./15.11.“)
        night = _is_night(folded, pos, m.end(), slash)
        start = m.start()
        if night and rng is not None:
            first = _range_night(int(rng.group(1)), int(rng.group(2)), a)
            if first is not None:
                toks.append(Temporal("date", rng.start(), rng.start("to"), first, year_given=bool(y),
                                     month_day=None if y else (first.day, first.month), night=True))
                taken.append((rng.start(), rng.start("to")))
                start = rng.end("to")     # „až z 14. na 15.11.“ – medzera k začiatku rozsahu je „až“
        if night:
            toks.append(Temporal("date", start, m.end(), a, year_given=bool(y), month_day=md_a, night=True))
        elif slash:                       # „12./13.11. 8:00 – 14:00“ – dva dni (obidva, prísnejší prípad)
            sep = folded.index("/", m.start(), m.end())
            toks.append(Temporal("date", m.start(), sep, a, year_given=bool(y), month_day=md_a))
            toks.append(Temporal("date", sep + 1, m.end(), b, year_given=bool(y),
                                 month_day=None if y else (b.day, b.month)))
        else:                             # „z 12. na 13.11.“ – presun na nový deň, dátum 13.11. nájde _DATE_RE
            continue
        taken.append((start, m.end()))
        chain_end, chain_pos = m.end(), pos
        for t in _expand_bare(items, a, bool(y), night):
            toks.append(t)
            taken.append((t.start, t.end))

    for m in _DAYRANGE_RE.finditer(folded):
        if _overlaps(taken, m.start(), m.end()):
            continue
        d1, d2, mon = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = _year(m.group(4))
        if not (1 <= mon <= 12) or not (1 <= d1 <= 31) or not (1 <= d2 <= 31) or d1 >= d2:
            continue
        if y:
            a, b = _mkdate(y, mon, d1), _mkdate(y, mon, d2)
        else:
            b = _infer_year(mon, d2, ref)
            a = _mkdate(b.year, mon, d1) if b else None
        if not a or not b:
            continue
        sep = folded.index("-", m.start(), m.end())
        toks.append(Temporal("date", m.start(), sep, a, year_given=bool(y)))
        toks.append(Temporal("date", sep + 1, m.end(), b, year_given=bool(y)))
        taken.append((m.start(), m.end()))
        _add_bare(folded, toks, taken, m.start(), a, bool(y), allow_range=False)

    for m in _DATE_RE.finditer(folded):
        if _overlaps(taken, m.start(), m.end()):
            continue
        dd, mon = int(m.group(1)), int(m.group(2))
        if not (1 <= dd <= 31 and 1 <= mon <= 12):
            continue
        y = _year(m.group(3))
        if y:
            d = _mkdate(y, mon, dd)
            if not d:
                continue
            toks.append(Temporal("date", m.start(), m.end(), d))
        else:
            d = _infer_year(mon, dd, ref)
            if not d:
                continue
            toks.append(Temporal("date", m.start(), m.end(), d, year_given=False, month_day=(dd, mon)))
        taken.append((m.start(), m.end()))
        _add_bare(folded, toks, taken, m.start(), d, bool(y), allow_range=True)

    for m in _SLASH_DATE_RE.finditer(folded):
        a, b = m.start(1), m.end()
        if _overlaps(taken, a, b) or not any(t.kind == "date" and _NIGHT_GAP.match(folded[t.end:a]) for t in toks):
            continue
        dd, mon = int(m.group(1)), int(m.group(2))
        y = _year(m.group(3))
        d = (_mkdate(y, mon, dd) if y else _infer_year(mon, dd, ref)) if 1 <= mon <= 12 else None
        if d is None:
            continue
        toks.append(Temporal("date", a, b, d, year_given=bool(y), month_day=None if y else (dd, mon)))
        taken.append((a, b))

    for m in _DATE_NAME_RE.finditer(folded):
        if _overlaps(taken, m.start(), m.end()):
            continue
        dd, mon = int(m.group(1)), MONTHS[m.group(2)]
        y = _year(m.group(3))
        d = _mkdate(y, mon, dd) if y else _infer_year(mon, dd, ref)
        if not d:
            continue
        toks.append(Temporal("date", m.start(), m.end(), d, year_given=bool(y),
                             month_day=None if y else (dd, mon)))
        taken.append((m.start(), m.end()))
        _add_bare(folded, toks, taken, m.start(), d, bool(y), allow_range=True)

    for m in _TIME_RE.finditer(folded):
        if _overlaps(taken, m.start(), m.end()):
            continue
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 24 or mm > 59 or (hh == 24 and mm):
            continue
        toks.append(Temporal("time", m.start(), m.end(), tm=time(hh % 24, mm), plus_day=hh == 24))
        taken.append((m.start(), m.end()))

    for m in _HOUR_RE.finditer(folded):
        if _overlaps(taken, m.start(), m.end()):
            continue
        hh = int(m.group(1))
        if hh > 24 or not _HOUR_CTX_RE.search(folded[max(0, m.start() - 6):m.start()]):
            continue
        toks.append(Temporal("time", m.start(), m.end(), tm=time(hh % 24, 0), plus_day=hh == 24))
        taken.append((m.start(), m.end()))

    toks.sort(key=lambda t: t.start)
    _fix_years(folded, toks, ref)
    return _merge(folded, _night_pairs(folded, toks))


def _night_pairs(folded: str, toks: list[Temporal]) -> list[Temporal]:
    """„v noci z 12.11. na 13.11.2026“, „12.11./13.11.2026 od 22:00 do 6:00“ – noc: jeden dátum (prvý deň),
    čas „od 22:00 do 6:00“ k nemu. Bez označenia noci (a bez okna cez polnoc) ostanú dva dátumy – napr. presun
    „z 12.11. na 13.11.2026“ platí pre 13.11."""
    out: list[Temporal] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        u = toks[i + 1] if i + 1 < len(toks) else None
        if t.kind == "date" and u is not None and u.kind == "date" and _NIGHT_GAP.match(folded[t.end:u.start]):
            first = t.day
            if not t.year_given and u.year_given and t.month_day:
                dd, mon = t.month_day
                first = _mkdate(u.day.year - (1 if mon > u.day.month else 0), mon, dd) or first
            if first is not None and (u.day - first).days == 1 and \
                    _is_night(folded, t.start, u.end, "/" in folded[t.end:u.start]):
                out.append(Temporal("date", t.start, u.end, first, year_given=t.year_given or u.year_given,
                                    night=True))
                i += 2
                continue
        out.append(t)
        i += 1
    return out


# zoznam dní cez prelom roka je krátky („28.12. a 2.1.2027“); dlhší rozstup nie je prelom roka, ale zlé poradie
# („3.12. a 27.11.2026“ nie je 3.12.2025 a 27.11.2026)
_LIST_ROLLOVER_DAYS = 62


def _fix_years(folded: str, toks: list[Temporal], ref: date) -> None:
    """Dátum bez roka pred „do <dátum s rokom>“ prevezme rok z druhého dátumu (10.11.–14.11.2026), rovnako
    v zozname („28.12. a 2.1.2027“).

    Zoznam sa berie chronologicky (neskorší mesiac pred skorším = prelom roka), sprava doľava, takže rok prejde
    celým zoznamom („12./13.11., 13./14.11. a 14./15.11.2026“). Ak by tak medzi dňami zoznamu vyšlo viac ako
    ~2 mesiace, zoznam nie je chronologický: deň, ktorý by bol dávno pred prijatím e-mailu (``ref``), dostane
    nasledujúci rok, ak tak leží tesne za dátumom za ním („3.12. a 27.11.2026“ → 3.12.2026, nie 3.12.2025; „2.1.
    a 28.12.2026“ → 2.1.2027), inak ostane chronologický; rok sa považuje za doplnený a obdobie dostane príznak
    ``zle_poradie`` („overte termín“). Dlhý chronologický zoznam v tom istom roku (ročný plán „10.1. a 28.12.2026“
    prijatý v decembri 2025) sa nemení."""
    for i in range(len(toks) - 1, -1, -1):
        t = toks[i]
        if t.kind != "date" or t.year_given or not t.month_day:
            continue
        for u in toks[i + 1:i + 3]:
            if u.kind == "date" and (u.year_given or u.unordered) and u.start - t.end <= 12 \
                    and (_TO_GAP.match(folded[t.end:u.start]) or _LIST_GAP.match(folded[t.end:u.start])):
                dd, mon = t.month_day
                y = u.day.year - (1 if mon > u.day.month else 0)
                d = _mkdate(y, mon, dd)
                nxt = _mkdate(y + 1, mon, dd)
                late = d is not None and nxt is not None and d < ref - timedelta(days=30) \
                    and 0 <= (nxt - u.day).days <= _LIST_ROLLOVER_DAYS
                if u.unordered:                   # zoznam so zlým poradím – rok ostáva doplnený
                    t.day = d or t.day
                    t.unordered = True
                elif d is not None and (u.day - d).days > _LIST_ROLLOVER_DAYS and (y < u.day.year or late) \
                        and _LIST_GAP.match(folded[t.end:u.start]):
                    t.day = nxt if late else d
                    t.unordered = True
                else:
                    t.day = d or t.day
                    t.year_given = True
                break


def _after_midnight(tm: Optional[time], plus_day: bool = False) -> bool:
    """Začiatok okna noci po polnoci (pred 12:00) – okno patrí k druhému dňu noci („v noci z 12. na 13.11.
    od 0:30 do 4:30“ = 13.11. 0:30 – 4:30); okno cez polnoc či večerné okno začína prvým dňom."""
    return tm is not None and not plus_day and tm < time(12, 0)


def _night_day(day: date, night: bool, wa: Temporal) -> date:
    """Deň, ku ktorému patrí okno ``wa`` dátumu ``day`` (pri noci po polnoci druhý deň noci)."""
    return day + timedelta(days=1) if night and _after_midnight(wa.tm, wa.plus_day) else day


def _merge(folded: str, toks: list[Temporal]) -> list[Temporal]:
    out: list[Temporal] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if i + 1 < len(toks):
            u = toks[i + 1]
            gap = folded[t.end:u.start]
            if t.kind == "date" and u.kind == "time" and len(gap) <= 16 and _AT_GAP.match(gap):
                # „v dňoch 10.–14.11.2026 v čase 8:00–14:00“ = obdobie dátumov + denné okno, nespájať
                prev = out[-1] if out else None
                nxt = toks[i + 2] if i + 2 < len(toks) else None
                range_end = prev is not None and prev.kind == "date" and \
                    bool(_TO_GAP.match(folded[prev.end:t.start]))
                window = nxt is not None and nxt.kind == "time" and \
                    bool(_TO_GAP.match(folded[u.end:nxt.start]))
                if not (range_end and window):
                    out.append(Temporal("datetime", t.start, u.end, _night_day(t.day, t.night, u), u.tm, u.plus_day,
                                        t.year_given, night=t.night, unordered=t.unordered))
                    i += 2
                    continue
            if t.kind == "time" and u.kind == "date" and len(gap) <= 14 and _TIME_DATE_GAP.match(gap) \
                    and gap.strip() and not _list_item_after_window(folded, out, toks, i, gap):
                out.append(Temporal("datetime", t.start, u.end, u.day, t.tm, t.plus_day, u.year_given))
                i += 2
                continue
        out.append(t)
        i += 1
    return out


def _list_item_after_window(folded: str, out: list[Temporal], toks: list[Temporal], i: int, gap: str) -> bool:
    """„12.11.2026 od 8:00 do 14:00, 13.11.2026 od 9:00 do 15:00“: čas ``toks[i]`` uzatvára okno a dátum za
    čiarkou (bodkočiarkou, bodkou – „… do 14:00 h. Dňa 13.11.2026 od 9:00 …“) má vlastný čas – je to ďalší deň
    zoznamu, nie koniec obdobia „do 14:00 13.11.2026“."""
    if not (re.search(r"[,;.]", gap) or folded[toks[i].start:toks[i].end].endswith(".")):
        return False
    prev = out[-1] if out else None
    nxt = toks[i + 2] if i + 2 < len(toks) else None
    if prev is None or prev.kind not in ("time", "datetime") or not _TO_GAP.match(folded[prev.end:toks[i].start]):
        return False
    if nxt is None or nxt.kind != "time":
        return False
    after = folded[toks[i + 1].end:nxt.start]
    return len(after) <= 16 and bool(_AT_GAP.match(after))


def _hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _window_period(day: date, wa: Temporal, wb: Temporal, pos: int, end_pos: int, year_added: bool,
                   night: bool = False, unordered: bool = False) -> Period:
    """Obdobie dňa ``day`` s oknom ``wa`` – ``wb`` (koniec skôr ako začiatok = cez polnoc).

    ``night`` – ``day`` je prvý deň noci: okno po polnoci („od 0:30 do 4:30“) patrí k druhému dňu."""
    day = _night_day(day, night, wa)
    start = datetime.combine(day, wa.tm) + timedelta(days=1 if wa.plus_day else 0)
    end = datetime.combine(day, wb.tm) + timedelta(days=1 if wb.plus_day else 0)
    p = Period(start, end, pos, end_pos)
    if end <= start:
        p.end = end + timedelta(days=1)
        p.flags.add("cez_polnoc")
    if year_added:
        p.flags.add("rok_doplneny")
    if unordered:
        p.flags.add("zle_poradie")
    return p


def _token_period(d: Temporal, wa: Temporal, wb: Temporal, pos: int, end_pos: int) -> Period:
    """Obdobie dátumu ``d`` (deň, noc, rok doplnený, zlé poradie) s oknom ``wa`` – ``wb``."""
    return _window_period(d.day, wa, wb, pos, end_pos, not d.year_given, d.night, d.unordered)


def not_term_date(folded: str, t: Temporal, floor: int = 0) -> bool:
    """Dátum, ktorý podľa textu pred ním nie je termínom výluky (pôvodný deň presunu, dátum vydania či vyhotovenia).

    Noc „z 12. na 13.11.“ termínom je – „z“ patrí k noci."""
    if "/" in folded[t.start:t.end] or re.search(r"\bna\b", folded[t.start:t.end]):
        return False
    return bool(NOT_TERM_BEFORE.search(folded, max(floor, t.start - 30), t.start))


def _listed_dates(folded: str, toks: list[Temporal], i: int, used: set[int], step: int) -> list[int]:
    """Indexy dátumov spojených s tokenom ``i`` do zoznamu („12.11. a 13.11.“, „12./13.11. a 13./14.11.“).

    ``step`` = -1 hľadá pred tokenom, +1 za ním. Zoznam s jedným oknom znamená obdobie pre každý deň (noc).
    Dátum vydania či pôvodný deň presunu („Rozkaz zo dňa 30.10.2026, 12.11.2026 od 8:00“) do zoznamu nepatrí."""
    out: list[int] = []
    j = i + step
    while 0 <= j < len(toks) and toks[j].kind == "date" and j not in used:
        left, right = (toks[j], toks[j - step]) if step < 0 else (toks[j - step], toks[j])
        if not _LIST_GAP.match(folded[left.end:right.start]) or not_term_date(folded, toks[j]):
            break
        out.append(j)
        j += step
    return out


def periods(folded: str, toks: list[Temporal]) -> list[Period]:
    """Z tokenov poskladá obdobia (od – do, denné okná, do odvolania).

    Zoznam dní či nocí s jedným oknom („12.11. a 13.11.2026 od 8:00 do 14:00“) dá obdobie pre každý deň."""
    out: list[Period] = []
    used: set[int] = set()
    date_ranges: list[tuple[Temporal, Temporal]] = []
    windows: list[tuple[Temporal, Temporal]] = []
    # [dni ako dátumové tokeny (deň, noc), koniec posledného okna] – ďalšie okno „a 13:00 – 16:00“ pre tie isté dni
    chains: list[list] = []
    i = 0
    while i < len(toks):
        a = toks[i]
        b = toks[i + 1] if i + 1 < len(toks) else None
        gap = folded[a.end:b.start] if b else ""
        if b is not None and len(gap) <= 14 and _TO_GAP.match(gap):
            if a.kind == "datetime" and b.kind == "datetime":
                p = Period(a.dt, b.dt, a.start, b.end)
                if not (a.year_given and b.year_given):
                    p.flags.add("rok_doplneny")
                if a.unordered or b.unordered:
                    p.flags.add("zle_poradie")
                out.append(p)
                used.update((i, i + 1))
                i += 2
                continue
            if a.kind == "datetime" and b.kind == "time":
                end = datetime.combine(a.day, b.tm) + timedelta(days=1 if b.plus_day else 0)
                p = Period(a.dt, end, a.start, b.end)
                if end <= a.dt:
                    p.end = end + timedelta(days=1)
                    p.flags.add("cez_polnoc")
                if not a.year_given:
                    p.flags.add("rok_doplneny")
                if a.unordered:
                    p.flags.add("zle_poradie")
                out.append(p)
                # deň (noc) dátumu s časom: pri okne noci po polnoci je a.day už druhý deň noci
                first = a.day - timedelta(days=1) if a.night and _after_midnight(a.tm, a.plus_day) else a.day
                days = [Temporal("date", a.start, a.end, first, year_given=a.year_given, night=a.night,
                                 unordered=a.unordered)]
                wa = Temporal("time", a.start, a.end, tm=a.tm, plus_day=a.plus_day)
                for j in _listed_dates(folded, toks, i, used, -1):       # ďalšie dni zoznamu pred ním
                    d = toks[j]
                    out.append(_token_period(d, wa, b, d.start, b.end))
                    used.add(j)
                    days.append(d)
                chains.append([days, b.end])
                used.update((i, i + 1))
                i += 2
                continue
            if a.kind == "date" and b.kind == "datetime":
                p = Period(datetime.combine(a.day, time(0, 0)), b.dt, a.start, b.end, flags={"bez_casu_zaciatku"})
                out.append(p)
                used.update((i, i + 1))
                i += 2
                continue
            if a.kind == "datetime" and b.kind == "date" and b.day >= a.day:
                # „od 20.11.2026 14:00 do 30.11.2026“ – koniec bez času platí do konca uvedeného dňa (prísnejší prípad)
                p = Period(a.dt, datetime.combine(b.day, time(0, 0)) + timedelta(days=1), a.start, b.end,
                           flags={"bez_casu_konca"})
                if not (a.year_given and b.year_given):
                    p.flags.add("rok_doplneny")
                out.append(p)
                used.update((i, i + 1))
                i += 2
                continue
            if a.kind == "date" and b.kind == "date":
                date_ranges.append((a, b))
                used.update((i, i + 1))
                i += 2
                continue
            if a.kind == "time" and b.kind == "time":
                windows.append((a, b))
                used.update((i, i + 1))
                i += 2
                continue
        if a.kind in ("datetime", "date") and _UNTIL_REVOKED.match(folded[a.end:a.end + 30]):
            start = a.dt if a.kind == "datetime" else datetime.combine(a.day, time(0, 0))
            out.append(Period(start, None, a.start, a.end + len(_UNTIL_REVOKED.match(folded[a.end:]).group(0)),
                              flags={"do_odvolania"}))
            used.add(i)
        i += 1

    # denné okná v rámci obdobia dátumov (v ľubovoľnom poradí, blízko seba)
    used_windows: set[int] = set()
    for a, b in date_ranges:
        best, best_dist = None, 10 ** 9
        for k, (wa, wb) in enumerate(windows):
            if k in used_windows:
                continue
            dist = min(abs(wa.start - b.end), abs(a.start - wb.end))
            if dist <= 160 and dist < best_dist:
                best, best_dist = k, dist
        # dni zoznamu pred obdobím („v dňoch 3., 4. a 10. – 12.11.2026 v čase 8:00 – 14:00“) majú to isté okno
        idx = _listed_dates(folded, toks, next(n for n, t in enumerate(toks) if t is a), used, -1)
        listed = [toks[j] for j in idx]
        used.update(idx)
        if best is not None:
            picked = [best]
            # ďalšie denné okná hneď za ním („v čase 8:00 – 12:00 a 13:00 – 16:00“)
            for k in range(best + 1, len(windows)):
                if k in used_windows or not _AND_GAP.match(folded[windows[picked[-1]][1].end:windows[k][0].start]):
                    break
                picked.append(k)
            for k in picked:
                wa, wb = windows[k]
                used_windows.add(k)
                # noci „od 12./13. do 14./15.11.“ s oknom po polnoci – okná sú v druhé dni nocí
                start = datetime.combine(_night_day(a.day, a.night, wa), wa.tm)
                end = datetime.combine(_night_day(b.day, b.night, wa), wb.tm)
                flags = {"denne"}
                if wb.tm <= wa.tm:
                    end += timedelta(days=1)
                    flags.add("cez_polnoc")
                if a.unordered or b.unordered:
                    flags.add("zle_poradie")
                out.append(Period(start, end, min(a.start, wa.start), max(b.end, wb.end),
                                  _hhmm(wa.tm), _hhmm(wb.tm), flags))
                for e in listed:
                    out.append(_token_period(e, wa, wb, e.start, e.end))
        else:
            # celé dni; posledná noc končí ráno nasledujúceho dňa
            out.append(Period(datetime.combine(a.day, time(0, 0)),
                              datetime.combine(b.day, time(0, 0)) + timedelta(days=2 if b.night else 1),
                              a.start, b.end, flags={"cely_den"}))
            for e in listed:
                out.append(Period(datetime.combine(e.day, time(0, 0)), datetime.combine(e.day, time(0, 0))
                                  + timedelta(days=2 if e.night else 1), e.start, e.end, flags={"cely_den"}))
    # okno s jedným dátumom (napr. „12.11.2026, denne 08:00 – 14:00“ alebo dátum za oknom)
    for k, (wa, wb) in enumerate(windows):
        if k in used_windows:
            continue
        # ďalšie okno tých istých dní: „12.11.2026 v čase 8:00 – 12:00 a 13:00 – 16:00“ – len okno ZA reťazou
        # (okno pred ňou, napr. „Kontakt: 7:00 – 15:30“, k nej nepatrí) a v tom istom odseku
        chain = next((c for c in chains if wa.start > c[1] and "\n\n" not in folded[c[1]:wa.start]
                      and _AND_GAP.match(folded[c[1]:wa.start])), None)
        if chain is not None:
            for d in chain[0]:
                out.append(_token_period(d, wa, wb, wa.start, wb.end))
            chain[1] = wb.end
            continue
        ranged = [x for r in date_ranges for x in r]
        near = [t for t in toks if t.kind == "date" and t not in ranged
                and (0 <= wa.start - t.end <= 40 or 0 <= t.start - wb.end <= 40)]
        if not near:
            continue
        # pôvodný deň presunu či dátum vydania („dňa 24.11.2026 (namiesto 23.11.2026) od 08:00 do 14:00“) okno
        # nedostane, ak je pri okne aj deň termínu
        near = [t for t in near if not not_term_date(folded, t)] or near
        d = min(near, key=lambda t: min(abs(wa.start - t.end), abs(t.start - wb.end)))
        out.append(_token_period(d, wa, wb, min(d.start, wa.start), max(d.end, wb.end)))
        # zoznam dní pri okne („8:00 – 14:00 v dňoch 12.11. a 13.11.2026“, „12.11., 13.11. v čase 8:00 – 14:00“)
        di = next(n for n, t in enumerate(toks) if t is d)
        used.add(di)
        for j in _listed_dates(folded, toks, di, used, -1) + _listed_dates(folded, toks, di, used, +1):
            e = toks[j]
            if e in ranged:
                continue
            out.append(_token_period(e, wa, wb, min(e.start, wa.start), max(e.end, wb.end)))
            used.add(j)
    out += _labelled_periods(folded, toks, used)
    out.sort(key=lambda p: p.pos)
    return out


# označené riadky začiatku a konca („Začiatok výluky: 08.12.2026 07:00“ / „Koniec výluky: 08.12.2026 16:00“, „Od:“ /
# „Do:“) – obdobie aj bez „do“ medzi časmi (formulár ROV)
_START_LINE = re.compile(r"^[\s>*•-]*(?:zaciatok|zacatek|zahajeni\w*|platnost\w*\s+od|platne\s+od|od|start\w*|beginn\w*)"
                         r"(?:\s+[^\s:]+){0,2}\s*:?\s*$")
_END_LINE = re.compile(r"^[\s>*•-]*(?:koniec|konec|ukoncen\w*|platnost\w*\s+do|platne\s+do|do|ende\w*)"
                       r"(?:\s+[^\s:]+){0,2}\s*:?\s*$")


def _labelled_periods(folded: str, toks: list[Temporal], used: set[int]) -> list[Period]:
    out: list[Period] = []
    for i, a in enumerate(toks):
        if i in used or a.kind != "datetime" or not _START_LINE.match(folded[folded.rfind("\n", 0, a.start) + 1:a.start]):
            continue
        for j in range(i + 1, min(i + 3, len(toks))):
            b = toks[j]
            if folded.count("\n", a.end, b.start) > 2:
                break
            ls = folded.rfind("\n", 0, b.start) + 1
            if j in used or b.kind not in ("datetime", "time") or ls <= a.end or \
                    not _END_LINE.match(folded[ls:b.start]):
                continue
            end = b.dt if b.kind == "datetime" else datetime.combine(a.day, b.tm)
            p = Period(a.dt, end, a.start, b.end)
            if end <= a.dt and b.kind == "time":
                p.end = end + timedelta(days=1)
                p.flags.add("cez_polnoc")
            if not a.year_given or (b.kind == "datetime" and not b.year_given):
                p.flags.add("rok_doplneny")
            if a.unordered or b.unordered:
                p.flags.add("zle_poradie")
            out.append(p)
            used.update((i, j))
            break
    return out


def time_windows(folded: str, toks: list[Temporal], start: int, end: int) -> list[tuple[Temporal, Temporal]]:
    """Časové okná bez dňa v časti textu ``start:end`` („v čase od 10:00 do 14:00“, „10:00 – 14:00 hod.“): dvojice
    časov za sebou s „do“, „–“ medzi nimi."""
    out: list[tuple[Temporal, Temporal]] = []
    inside = [t for t in toks if start <= t.start < end]
    i = 0
    while i + 1 < len(inside):
        a, b = inside[i], inside[i + 1]
        gap = folded[a.end:b.start]
        if a.kind == "time" and b.kind == "time" and len(gap) <= 14 and _TO_GAP.match(gap):
            out.append((a, b))
            i += 2
            continue
        i += 1
    return out


def night_window_day(day: date, night: bool, tm: time) -> date:
    """Deň okna ``tm`` dňa ``day``: pri noci („v noci z 12. na 13.11.“) okno po polnoci patrí k druhému dňu noci."""
    return day + timedelta(days=1) if night and tm.hour < 12 else day


def first_datetime_after(folded: str, toks: list[Temporal], pos: int, max_chars: int = 120,
                         fallback_day: Optional[date] = None) -> Optional[tuple[datetime, Temporal]]:
    """Prvý dátum s časom za pozíciou (napr. za návestím „predpokladaný príchod“).

    Ak nájde len čas, doplní deň z najbližšieho dátumu v okolí alebo z ``fallback_day``.
    """
    cands = [t for t in toks if pos <= t.start <= pos + max_chars]
    for t in cands:
        if t.kind == "datetime":
            return t.dt, t
        if t.kind == "time":
            near = [d for d in toks if d.kind in ("date", "datetime") and abs(d.start - t.start) <= 160]
            if near:
                d = min(near, key=lambda d: abs(d.start - t.start))
                return datetime.combine(d.day, t.tm) + timedelta(days=1 if t.plus_day else 0), t
            if fallback_day:
                return datetime.combine(fallback_day, t.tm) + timedelta(days=1 if t.plus_day else 0), t
            return None
        if t.kind == "date":
            # dátum bez času – ešte skúsime čas hneď za ním
            nxt = [u for u in toks if u.kind == "time" and 0 <= u.start - t.end <= 25]
            if nxt:
                u = nxt[0]
                return datetime.combine(t.day, u.tm) + timedelta(days=1 if u.plus_day else 0), t
            return datetime.combine(t.day, time(0, 0)), t
    return None


def parse_datetime_text(text: str, ref: date) -> Optional[datetime]:
    """Dátum a čas z krátkeho textu (bunka tabuľky, výstup LLM). Bez času → 00:00."""
    from .text import clean_text, fold

    if not text:
        return None
    s = str(text).strip()
    try:
        d = datetime.fromisoformat(s)
        return d.replace(tzinfo=None) if d.tzinfo is None else d
    except ValueError:
        pass
    toks = scan(fold(clean_text(s)), ref)
    for t in toks:
        if t.kind == "datetime":
            return t.dt
    for t in toks:
        if t.kind == "date":
            return datetime.combine(t.day, time(0, 0))
    return None


def parse_hhmm(text: str) -> Optional[str]:
    """„8.00“, „08:00 hod.“ → „08:00“."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s?[:.]\s?(\d{2})", str(text))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 24 or mm > 59:
        return None
    return f"{hh % 24:02d}:{mm:02d}" if hh < 24 else "24:00"


def is_daily_context(folded_block: str) -> bool:
    return bool(_DAILY_RE.search(folded_block))
