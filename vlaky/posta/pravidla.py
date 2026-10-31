"""Deterministická extrakcia udalostí z e-mailov pravidlami (slovenčina a čeština, aj bez diakritiky).

Postup: celý text (predmet, telo, prílohy) sa „zloží“ (bez diakritiky, rovnaká dĺžka), rozpozná sa
druh e-mailu podľa kľúčových slov (predmet má trojnásobnú váhu), potom sa hľadajú úseky (A – B,
medzi A a B), obdobia (od – do, denné okná), čísla vlakov, tabuľky (XLSX/CSV, zoznamy staníc) a polia
objednávky. Názvy dopravní sa overujú proti registru siete, aby sa správne určili hranice názvov.

Istota pravidiel je 0,5 – 0,85 podľa počtu nájdených povinných polí; pravidlá samy nikdy nedajú
viac ako 0,9 (vyššiu istotu môže dať len zhoda s LLM, pozri ``extrakcia.combine``).

Rozpoznávané typy dokumentov ŽSR: rozkaz o výluke (ROV), súborný ROV (SROV), oznámenie o povolených
výlukách, telegram (TLG) kontrolného dispečera OR, prechodné obmedzenie traťovej rýchlosti (POTR),
správy systému KIS (ponuka, akceptácia a zrušenie trasy, ad hoc trasa – produkt P5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

from ..cas import local_naive_to_utc
from . import datumy
from .datumy import Period
from .stanice import StationMatch, find_borders_in_text, stations, tokens
from .text import SECTION_DASHES, fold, one_line
from .udalosti import ExtractedItem, MailDocument, normalize_data

RULES_VERSION = "pravidla-2026.11"
MAX_RULE_CONFIDENCE = 0.85


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in parts))


# ------------------------------------------------------------------ klasifikácia
_KIND_PATTERNS: dict[str, re.Pattern] = {
    "vyluka_zrusenie": _rx(
        r"zrusen\w*\s+(?:\w+\s+){0,2}(?:vyluk|rov\b|rozkaz\w* o vyluke)",
        r"(?:vyluk\w*|\brov\b)(?:\s+c\.)?(?:\s+[\w/.-]+)?\s+(?:sa\s+)?(?:rusi|zrusuje|nekona|nebude konat|odvolava|je zrusen|bola zrusen|sa zrusil)",
        r"odvolan\w*\s+(?:\w+\s+)?vyluk", r"rusime\s+(?:\w+\s+)?vyluk", r"storn\w*\s+(?:\w+\s+)?vyluk",
        r"vyluk\w*\s+(?:\w+\s+){0,3}sa\s+(?:nekona|rusi)"),
    "vyluka_zmena": _rx(
        r"zmen\w*\s+(?:\w+\s+){0,2}(?:vyluk|rov\b|rozkaz\w* o vyluke)",
        r"(?:vyluk\w*|\brov\b)(?:\s+c\.)?(?:\s+[\w/.-]+)?\s+(?:sa\s+)?(?:meni|posuva|predlzuje|skracuje|presuva)",
        r"predlzen\w*\s+(?:\w+\s+)?vyluk", r"posun\w*\s+(?:\w+\s+)?vyluk", r"uprav\w*\s+(?:\w+\s+)?vyluk",
        r"aktualizac\w*\s+(?:\w+\s+)?vyluk", r"skraten\w*\s+(?:\w+\s+)?vyluk", r"oprav\w*\s+(?:\w+\s+){0,2}vyluk"),
    "pomala_jazda": _rx(
        r"pomal\w*\s+jazd", r"pomal\w*\s+jizd", r"\bpotr\b",
        r"(?:docasn|prechodn)\w*\s+(?:obmedzen|omezen)\w*\s+(?:tratov\w*\s+)?rychlost",
        r"rychlost\w*\s+(?:je\s+|bude\s+)?(?:obmedzen|omezen|znizen|snizen)\w*\s+na",
        r"(?:obmedzen|omezen|znizen|snizen)\w*\s+(?:tratov\w*\s+)?rychlost", r"zavedeni\w*\s+pomal"),
    "vyluka_nova": _rx(
        r"vyluk", r"vyluceni\w*\s+kol[ae]j", r"vypnut\w*\s+(?:trakcn|\btv\b)", r"vypnuti\w*\s+(?:trakcn|\btv\b)",
        r"napatov\w*\s+vyluk", r"beznapatov", r"\bs?rov\b", r"rozkaz\w*\s+o\s+vyluke",
        r"oznamen\w*\s+o\s+povolen\w*\s+vyluk"),
    "trasa_zrusena": _rx(
        r"zrusen\w*\s+(?:\w+\s+){0,2}tras", r"\btras\w*(?:\s+\S+){0,4}\s+(?:sa\s+)?(?:rusi|zrusuje|je zrusen|bola zrusen)",
        r"rusime\s+(?:\w+\s+)?tras", r"storn\w*\s+(?:\w+\s+)?tras", r"odrieknut\w*\s+(?:\w+\s+)?tras",
        r"zruseni\s+trasy"),
    "trasa_zmena": _rx(
        r"zmen\w*\s+(?:\w+\s+){0,2}tras", r"\btras\w*(?:\s+\S+){0,4}\s+(?:sa\s+)?(?:meni|je zmenen|bola zmenen)",
        r"uprav\w*\s+(?:\w+\s+){0,2}tras", r"zmen\w*\s+casov\w*\s+polohy"),
    "trasa_pridelena": _rx(
        r"pridel\w*\s+(?:\w+\s+){0,3}tras", r"\btras\w*(?:\s+\S+){0,4}\s+(?:je\s+|bola\s+)?pridelen",
        r"pridelujeme", r"pridelen\w*\s+kapacit", r"prideleni\s+trasy", r"ponuk\w*\s+(?:na\s+)?(?:\w+\s+)?tras",
        r"akceptaci\w*\s+(?:\w+\s+){0,2}(?:ponuky|trasy)", r"\bkis\b", r"\bad[\s-]?hoc\s+tras", r"\bp5\b"),
    "avizo": _rx(
        r"\baviz", r"predpoklad\w*\s+(?:cas\s+)?(?:prichod|prijezd|prijazd)", r"\beta\b",
        r"ocakavan\w*\s+prichod", r"ocekavan\w*\s+prijezd", r"prichod\s+(?:vlaku\s+)?na\s+hranic",
        r"prijezd\s+(?:vlaku\s+)?na\s+hranic"),
    "mimoriadnost": _rx(
        r"mimoriadn\w*\s+udalost", r"mimoriadnost", r"mimoradn\w*\s+udalost", r"\bporuch\w*", r"\bnehod\w*",
        r"neprejazdn", r"nesjizdn", r"vykolajen", r"vykolejen", r"\bzrazk\w*", r"prerusen\w*\s+(?:prevadzky|dopravy)",
        r"preruseni\s+(?:provozu|dopravy)"),
    "objednavka": _rx(
        r"objednav", r"objednavam", r"poziadavk\w*\s+na\s+(?:prepravu|pristavenie)", r"pozadav\w*\s+na\s+prepravu",
        r"zadost\w*\s+o\s+prepravu", r"ziadost\w*\s+o\s+prepravu"),
}
# signály, ktoré samy druh neurčujú, ale zvyšujú váhu (telegram, kontrolný dispečer)
_HINT_UNPLANNED = re.compile(r"\btlg\b|telegram|kontroln\w*\s+dispecer|\bkd\s+or\b")
_AGENT_REPLY = re.compile(r"\[agent\]")
_FAMILIES = {
    "vyluka": ("vyluka_zrusenie", "vyluka_zmena", "pomala_jazda", "vyluka_nova"),
    "trasa": ("trasa_zrusena", "trasa_zmena", "trasa_pridelena"),
    "avizo": ("avizo",),
    "mimoriadnost": ("mimoriadnost",),
    "objednavka": ("objednavka",),
}
_FAMILY_ORDER = ("vyluka", "trasa", "avizo", "mimoriadnost", "objednavka")

# ------------------------------------------------------------------ druh obmedzenia
_SPEED_WORDS = re.compile(r"pomal\w*\s+j[ai]zd|\bpotr\b|(?:obmedzen|omezen|znizen|snizen)\w*\s+(?:tratov\w*\s+)?rychlost"
                          r"|rychlost\w*\s+(?:je\s+|bude\s+)?(?:obmedzen|omezen|znizen|snizen)")
_TV_WORDS = re.compile(r"vyluk\w*\s+(?:\w+\s+)?\btv\b|vypnut\w*\s+(?:\w+\s+)?(?:\btv\b|trakcn)|vypnuti\w*\s+(?:\w+\s+)?"
                       r"(?:\btv\b|trakcn)|napatov\w*\s+vyluk|napetov\w*\s+vyluk|beznapatov|vyluk\w*\s+trakcn"
                       r"|\btv\s+(?:\d\.\s?)?(?:tk|ob)|\btv\b")
_TRACK_CLOSURE = re.compile(r"vyluk\w*\s+(?:\d\.\s?)?(?:tratov\w*\s+|stanicn\w*\s+)?kol[ae]j|vyluk\w*\s+\d\.\s?tk\b"
                            r"|vyluceni\w*\s+(?:\d\.\s?)?(?:tratov\w*\s+)?kol[ae]j|jednokol[ae]jn|vyluk\w*\s+tk\b")
_TRACK_ANY = re.compile(r"\b\d\.\s?tk\b|\b\d\.\s?(?:tratov\w*|stanicn\w*)\s+kol[ae]j|kol[ae]j\w*\s+c\.\s?\d")
_BOTH_TRACKS = re.compile(r"\bob(?:e|i|och|ou|oma)\s+(?:tratov\w*\s+)?(?:kol[ae]j\w*|tk\b)|vsetk\w*\s+kol[ae]j|uplna|uplnou"
                          r"|uplne\s+vyluc|neprejazdn|nesjizdn|vyluk\w*\s+(?:celeho\s+)?useku|zastaven\w*\s+prevadzk")
_LIMIT_WORDS = re.compile(r"(?:obmedzen|omezen)\w*\s+(?:dlzky|delky|hmotnosti|naprav|loziskov)|zakaz\w*\s+(?:jazdy|vjazdu)")


def restriction_kind(folded: str) -> tuple[str, bool]:
    """Druh obmedzenia podľa slov v texte → (druh, bol_predpokladany)."""
    speed = bool(_SPEED_WORDS.search(folded))
    tv = bool(_TV_WORDS.search(folded))
    track_closure = bool(_TRACK_CLOSURE.search(folded))
    both = bool(_BOTH_TRACKS.search(folded))
    closure_words = bool(re.search(r"vyluk|vyluceni", folded))
    if speed and not (closure_words or tv):
        return "pomala_jazda", False
    if track_closure and not both:
        return "kolaj", False
    if tv:
        return "trakcne_vedenie", False
    if both:
        return "uplna", False
    if _TRACK_ANY.search(folded) and closure_words:
        return "kolaj", False
    if _LIMIT_WORDS.search(folded):
        return "obmedzenie", False
    if speed:
        return "pomala_jazda", False
    return "uplna", True


# ------------------------------------------------------------------ ďalšie vzory
_SPEED_RE = re.compile(r"(\d{1,3})\s?km\s?/\s?h")
_TRACK_PATTERNS = [
    re.compile(r"\b(1\.?\s?a\s?2)\.\s?(?:tk\b|tratov\w*\s+kol[ae]j)"),
    re.compile(r"\b(\d)\.\s?(?:tratov\w*\s+|stanicn\w*\s+|hlavn\w*\s+)?kol[ae]j"),
    re.compile(r"\b(\d)\.\s?tk\b"),
    re.compile(r"kol[ae]j\w*\s+c\.\s?(\d{1,3}[a-z]?)\b"),
    re.compile(r"\b(ob(?:e|i|och|ou))\s+(?:tratov\w*\s+)?(?:kol[ae]j\w*|tk)\b"),
]
_LINE_REF_RE = re.compile(r"\btrat\w*\s*(?:c\.\s*|cislo\s*)?:?\s*(\d{3}[a-z]?)\b")
_LINE_DESC_RE = re.compile(r"\btrat\w*\s*(?:c\.\s*|cislo\s*)?:?\s*\d{3}[a-z]?\b")
_REF_VALUE = r"([a-z0-9][a-z0-9/\-_.]*\d[a-z0-9/\-_]*)"
_CLOSURE_REF_RES = [
    re.compile(r"\b(?:s?rov|rozkaz\w*\s+o\s+vyluke|suborn\w*\s+(?:rov|rozkaz\w*(?:\s+o\s+vyluke)?))"
               r"(?:\s*\(s?rov\))?\s*(?:c\.|cislo)?\s*:?\s*" + _REF_VALUE),
    re.compile(r"\b(?:tlg|telegram\w*)\s*(?:c\.|cislo)?\s*:?\s*" + _REF_VALUE),
    re.compile(r"\b(?:cislo|c\.)\s*(?:vyluky|rozkazu|potr|pomalej\s+jazdy|obmedzenia)\s*:?\s*" + _REF_VALUE),
    re.compile(r"\bvyluk\w*\s+(?:c\.|cislo)\s*:?\s*" + _REF_VALUE),
    re.compile(r"\bpotr\s*(?:c\.|cislo)?\s*:?\s*" + _REF_VALUE),
    re.compile(r"\b(?:id|oznacenie|evidencne\s+cislo|ev\.\s*c\.)\s+(?:vyluky|obmedzenia)?\s*:?\s*" + _REF_VALUE),
    re.compile(r"\b(?:nase\s+)?c\.\s*j\.\s*:?\s*" + _REF_VALUE),
]
_PATH_REF_RES = [
    re.compile(r"\b(?:id\s+trasy|cislo\s+trasy|c\.\s*trasy|id\s+poziadavky|cislo\s+poziadavky|c\.\s*poziadavky"
               r"|cislo\s+ziadosti|c\.\s*ziadosti|ziadost\w*\s+c\.|id\s+ponuky|cislo\s+ponuky|ponuk\w*\s+c\."
               r"|kis\s+id|id\s+kis|path\s+id|pap\s+id)\s*:?\s*" + _REF_VALUE),
]
_TRAIN_RE = re.compile(
    r"\b(?:(?P<cat>nex|pn|rn|vn|mn|lv|sv|nx)\s?(?P<num>\d{3,6})\b"
    r"|vlak\w*\s+(?:c\.\s*|cislo\s*)?(?:(?P<cat2>nex|pn|rn|vn|mn|lv|sv|nx)\s?)?(?P<num2>\d{3,6})\b)")
_MEDZI_RE = re.compile(r"\b(?:medzi|mezi)\s+(?:(?:zeleznicn\w*\s+)?(?:stanicami|stanicemi|dopravnami|dopravnami)\s+"
                       r"|zst\.?\s+|z\.\s?st\.\s+)?")
_REASON_RE = re.compile(r"\bz\s+dovodu\s+|\bz\s+duvodu\s+|\bdovod\w*(?:\s+\w+){0,2}\s*:\s*"
                        r"|\bduvod\w*(?:\s+\w+){0,2}\s*:\s*")
_REASON_END = re.compile(r"[,;.\n(]|\s(?:sa|bude|budu|je|su|zavadza|zavadzaju|plati|v\s+useku|od\s+\d)\b")
_OLD_PERIOD = re.compile(r"povodn|puvodn|doterajs|predchadzajuc|predosl|stary\b|stareho|povodne")
_ENUM_RE = re.compile(r"^\s*(?:\d{1,2}[.)]|[a-z]\)|[-•*]|•)\s+")


# ------------------------------------------------------------------ pomocné štruktúry
@dataclass
class Section:
    a: StationMatch
    b: StationMatch
    via: list = field(default_factory=list)
    start: int = 0
    end: int = 0
    line_desc: bool = False


@dataclass
class Table:
    header: list            # zložené (folded) názvy stĺpcov
    rows: list              # [[pôvodné bunky]]
    row_pos: list           # pozície riadkov v texte
    start: int
    end: int


def _iso(local_dt: Optional[datetime]) -> Optional[str]:
    if local_dt is None:
        return None
    return local_naive_to_utc(local_dt).isoformat()


def _lines(text: str, start: int = 0, end: Optional[int] = None) -> list[tuple[int, int]]:
    end = len(text) if end is None else end
    out, pos = [], start
    while pos <= end:
        nl = text.find("\n", pos, end)
        if nl == -1:
            if pos < end:
                out.append((pos, end))
            break
        out.append((pos, nl))
        pos = nl + 1
    return out


def _flag_notes(p: Optional[Period]) -> list[str]:
    if p is None:
        return []
    notes = []
    if "rok_doplneny" in p.flags:
        notes.append("Rok v dátume bol doplnený podľa dátumu prijatia e-mailu.")
    if "cez_polnoc" in p.flags:
        notes.append("Koniec je na nasledujúci deň (cez polnoc).")
    if "cely_den" in p.flags:
        notes.append("Čas nie je uvedený – predpokladá sa celý deň (00:00 – 24:00).")
    if "do_odvolania" in p.flags:
        notes.append("Platí do odvolania – koniec nie je určený.")
    if "bez_casu_zaciatku" in p.flags:
        notes.append("Čas začiatku nie je uvedený – predpokladá sa 00:00.")
    if "denne" in p.flags:
        notes.append(f"Opakuje sa denne {p.daily_from} – {p.daily_to}.")
    return notes


def _confidence(found: int, required: int, penalties: float = 0.0) -> float:
    frac = found / required if required else 0.0
    return round(max(0.3, min(MAX_RULE_CONFIDENCE, 0.5 + 0.35 * frac - penalties)), 2)


# ------------------------------------------------------------------ kontext jedného e-mailu
class _Ctx:
    def __init__(self, doc: MailDocument) -> None:
        self.doc = doc
        self.text, self.subject_end = doc.full_text()
        self.f = fold(self.text)
        self.subject_f = self.f[: self.subject_end]
        self.ref: date = doc.ref_date()
        self.toks = datumy.scan(self.f, self.ref)
        self.periods = datumy.periods(self.f, self.toks)
        self.st = stations()
        self.blocks = self._blocks()
        self.tables = self._tables()
        self._sec_cache: dict = {}

    # -------------------------------------------------------------- členenie textu
    def _blocks(self) -> list[tuple[int, int]]:
        blocks = [(0, self.subject_end)]
        cur_s = cur_e = None
        for s, e in _lines(self.text, self.subject_end + 1):
            line = self.text[s:e]
            if not line.strip():
                if cur_s is not None:
                    blocks.append((cur_s, cur_e))
                    cur_s = None
                continue
            if cur_s is not None and (_ENUM_RE.match(line) or line.startswith("[Príloha:")):
                blocks.append((cur_s, cur_e))
                cur_s = None
            if cur_s is None:
                cur_s = s
            cur_e = e
        if cur_s is not None:
            blocks.append((cur_s, cur_e))
        return blocks

    def _tables(self) -> list[Table]:
        tables: list[Table] = []
        run: list[tuple[int, int, list]] = []

        def flush() -> None:
            if len(run) >= 2:
                t = self._make_table(run)
                if t:
                    tables.append(t)
            run.clear()

        for s, e in _lines(self.text, self.subject_end + 1):
            line = self.text[s:e]
            if line.count("\t") >= 2:
                cells = [c.strip() for c in line.split("\t")]
            elif line.count(";") >= 2 and len(line) < 400:
                cells = [c.strip() for c in line.split(";")]
            elif line.count("|") >= 2:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
            else:
                flush()
                continue
            run.append((s, e, cells))
        flush()
        return tables

    _HEADER_WORDS = re.compile(r"usek|stanic|dopravn|zaciatok|koniec|^od\b|^do\b|trat|kol[ae]j|druh|typ|prichod|"
                               r"odchod|prijezd|odjezd|prich|odch|vlak|\beta\b|poznam|cislo|rychlost|datum|^cas|"
                               r"platnost|termin|dovod|hranic|priechod|pps")

    def _make_table(self, run: list) -> Optional[Table]:
        for hi, (s, e, cells) in enumerate(run):
            folded = [" ".join(fold(c).split()) for c in cells]
            hits = sum(1 for c in folded if c and self._HEADER_WORDS.search(c))
            has_time = any(datumy.scan(c, self.ref) for c in folded if c)
            if hits >= 2 and not has_time:
                rows, pos = [], []
                for s2, e2, c2 in run[hi + 1:]:
                    if sum(1 for c in c2 if c) >= 2:
                        rows.append(c2 + [""] * (len(cells) - len(c2)))
                        pos.append(s2)
                if rows:
                    return Table(folded, rows, pos, s, run[-1][1])
        return None

    def in_table(self, pos: int) -> bool:
        return any(t.start <= pos <= t.end for t in self.tables)

    # -------------------------------------------------------------- úseky
    def sections(self, start: int, end: int) -> list[Section]:
        key = (start, end)
        if key not in self._sec_cache:
            out: list[Section] = []
            seen = set()
            for ls, le in _lines(self.text, start, end):
                for sec in self._sections_in_line(ls, le):
                    k = (sec.a.name, sec.b.name)
                    if k not in seen and sec.a.name != sec.b.name:
                        seen.add(k)
                        out.append(sec)
            self._sec_cache[key] = out
        return self._sec_cache[key]

    def _is_sep(self, i: int) -> bool:
        ch = self.text[i]
        if ch in SECTION_DASHES:
            return True
        if ch == "-" or ch == ">":
            before = self.text[i - 1] if i > 0 else " "
            after = self.text[i + 1] if i + 1 < len(self.text) else " "
            if after == ">":
                return True
            return before.isspace() or after.isspace()
        return False

    def _sections_in_line(self, ls: int, le: int) -> list[Section]:
        found: list[Section] = []
        text = self.text
        # „medzi stanicami A a B“
        for m in _MEDZI_RE.finditer(self.f, ls, le):
            rs = m.end()
            toks = tokens(text[rs:le], rs)
            for j, t in enumerate(toks):
                if fold(t.raw) == "a" and 0 < j < len(toks) - 1:
                    a = self.st.prefix(toks[:j], text)
                    b = self.st.prefix(toks[j + 1:], text)
                    if a and b and not text[a.end:toks[j].start].strip():
                        found.append(Section(a, b, [], a.start, b.end))
                        break
        # „A – B“, reťaz „A – B – C“
        seps = [i for i in range(ls, le) if self.f[i] in "->" and self._is_sep(i)]
        if seps:
            bounds = [ls - 1] + seps + [le]
            parts = [(bounds[k] + 1, bounds[k + 1]) for k in range(len(bounds) - 1)]
            k = 0
            while k < len(parts) - 1:
                ltoks = tokens(text[parts[k][0]:parts[k][1]], parts[k][0])
                rtoks = tokens(text[parts[k + 1][0]:parts[k + 1][1]], parts[k + 1][0])
                a = self.st.suffix(ltoks, text) if ltoks else None
                b = self.st.prefix(rtoks, text) if (a and rtoks) else None
                if a and b:
                    via, j = [], k + 1
                    while j + 1 < len(parts) and not text[b.end:parts[j][1]].strip(" ,"):
                        ntoks = tokens(text[parts[j + 1][0]:parts[j + 1][1]], parts[j + 1][0])
                        nb = self.st.prefix(ntoks, text) if ntoks else None
                        if not nb:
                            break
                        via.append(b)
                        b, j = nb, j + 1
                    sec = Section(a, b, via, a.start, b.end)
                    sec.line_desc = bool(_LINE_DESC_RE.search(self.f[ls:a.start]))
                    found.append(sec)
                    k = j
                else:
                    k += 1
        # „Kraľovany-Ružomberok“ (spojovník bez medzier, celok nie je názvom dopravne)
        if not found:
            for t in tokens(text[ls:le], ls):
                raw = t.raw.strip(",;:()[]")
                if "-" not in raw[1:-1] or self.st.exact(raw):
                    continue
                for h in [i for i, ch in enumerate(raw) if ch == "-"]:
                    a, b = self.st.exact(raw[:h]), self.st.exact(raw[h + 1:])
                    if a and b:
                        found.append(Section(StationMatch(raw[:h], a.name, a.railway_ref, "presne", t.start, t.start + h),
                                             StationMatch(raw[h + 1:], b.name, b.railway_ref, "presne",
                                                          t.start + h + 1, t.start + len(raw))))
                        break
        return found

    # -------------------------------------------------------------- drobné polia
    def first_group(self, patterns: list[re.Pattern], start: int, end: int) -> Optional[str]:
        best = None
        for pat in patterns:
            for m in pat.finditer(self.f, start, end):
                if best is None or m.start(1) < best[0]:
                    best = (m.start(1), m.end(1))
                break
        if not best:
            return None
        return self.text[best[0]:best[1]].rstrip(".,;)")

    def line_of(self, pos: int) -> str:
        s = self.text.rfind("\n", 0, pos) + 1
        e = self.text.find("\n", pos)
        return one_line(self.text[s:e if e != -1 else len(self.text)], 200)

    def trains(self, start: int = 0, end: Optional[int] = None) -> list[tuple[str, Optional[str], int, int]]:
        end = len(self.f) if end is None else end
        out = []
        for m in _TRAIN_RE.finditer(self.f, start, end):
            num = m.group("num") or m.group("num2")
            cat = m.group("cat") or m.group("cat2")
            out.append((num, cat.capitalize() if cat else None, m.start(), m.end()))
        return out

    def speed(self, start: int, end: int) -> Optional[int]:
        for m in _SPEED_RE.finditer(self.f, start, end):
            v = int(m.group(1))
            if 5 <= v <= 160:
                return v
        return None

    def track(self, start: int, end: int) -> Optional[str]:
        best = None
        for pat in _TRACK_PATTERNS:
            m = pat.search(self.f, start, end)
            if m and (best is None or m.start() < best.start()):
                best = m
        if not best:
            return None
        val = best.group(1)
        return "obe" if val.startswith("ob") or " a " in val or "a2" in val.replace(" ", "") else val

    def line_ref(self, start: int, end: int) -> Optional[str]:
        m = _LINE_REF_RE.search(self.f, start, end)
        return m.group(1).upper() if m else None

    def reason(self, start: int, end: int) -> Optional[str]:
        m = _REASON_RE.search(self.f, start, end)
        if not m:
            return None
        e = _REASON_END.search(self.f, m.end(), min(len(self.f), m.end() + 160))
        stop = e.start() if e else min(len(self.f), m.end() + 160)
        if stop <= m.end() + 2:
            return None
        return one_line(self.text[m.start():stop], 200)

    def is_old_period(self, p: Period) -> bool:
        """Obdobie označené ako pôvodné („Pôvodný termín: …“) – pri zmene neplatí."""
        ls = self.f.rfind("\n", 0, p.pos) + 1
        return bool(_OLD_PERIOD.search(self.f, max(ls, p.pos - 60), p.pos))


# ------------------------------------------------------------------ extraktor
class RuleExtractor:
    """Extrakcia pravidlami (bez LLM)."""

    name = "pravidla"
    version = RULES_VERSION

    def extract(self, doc: MailDocument) -> list[ExtractedItem]:
        c = _Ctx(doc)
        if _AGENT_REPLY.search(c.subject_f):
            return [ExtractedItem("ine", {}, 0.5, ["Odpoveď na správu agenta (spätná väzba dispečera) – "
                                                    "údaje sa z nej neextrahujú."])]
        family, kind, scores = self.classify(c)
        if family is None:
            return []
        handler: dict[str, Callable] = {"vyluka": self._closures, "trasa": self._paths, "avizo": self._avizo,
                                        "mimoriadnost": self._incident, "objednavka": self._order}
        items = handler[family](c, kind)
        weak = scores.get(kind, 0) < 3     # druh len z tela, nie z predmetu
        hint = bool(_HINT_UNPLANNED.search(c.f))
        for it in items:
            if weak:
                it.confidence = round(max(0.3, it.confidence - 0.03), 2)
                it.notes.append("Druh e-mailu je určený len z textu (nie z predmetu).")
            if hint and family in ("vyluka", "mimoriadnost"):
                it.notes.append("Zdroj: telegram / správa kontrolného dispečera ŽSR (neplánovaná zmena).")
            it.data = normalize_data(it.kind, it.data)
        return items

    # -------------------------------------------------------------- klasifikácia
    def classify(self, c: _Ctx) -> tuple[Optional[str], Optional[str], dict]:
        scores: dict[str, int] = {}
        body = c.f[c.subject_end:]
        for kind, pat in _KIND_PATTERNS.items():
            s = 3 * min(2, len(pat.findall(c.subject_f))) + min(3, len(pat.findall(body)))
            if s:
                scores[kind] = s
        if not scores:
            return None, None, scores
        fam_scores = {fam: max(scores.get(k, 0) for k in kinds) for fam, kinds in _FAMILIES.items()}
        # mimoriadnosť: telegram/kontrolný dispečer bez výluky v predmete
        best_fam = max(_FAMILY_ORDER, key=lambda f: (fam_scores[f], -_FAMILY_ORDER.index(f)))
        if fam_scores[best_fam] == 0:
            return None, None, scores
        kinds = _FAMILIES[best_fam]
        if best_fam == "vyluka":
            subj = {k for k in kinds if _KIND_PATTERNS[k].search(c.subject_f)}
            for k in ("vyluka_zrusenie", "vyluka_zmena"):
                if k in subj:
                    return best_fam, k, scores
            if not c.tables:   # súborný prehľad (tabuľka) môže spomínať zmeny aj zrušenia – berie sa ako nové
                for k in ("vyluka_zrusenie", "vyluka_zmena"):
                    if scores.get(k, 0) >= 1:
                        return best_fam, k, scores
            if scores.get("pomala_jazda", 0) > scores.get("vyluka_nova", 0) or \
                    ("pomala_jazda" in subj and "vyluka_nova" not in subj):
                return best_fam, "pomala_jazda", scores
            return best_fam, "vyluka_nova", scores
        if best_fam == "trasa":
            subj = {k for k in kinds if _KIND_PATTERNS[k].search(c.subject_f)}
            for k in ("trasa_zrusena", "trasa_zmena", "trasa_pridelena"):
                if k in subj:
                    return best_fam, k, scores
            return best_fam, max(kinds, key=lambda k: (scores.get(k, 0), -kinds.index(k))), scores
        return best_fam, kinds[0], scores

    # -------------------------------------------------------------- výluky a pomalé jazdy
    def _closures(self, c: _Ctx, kind: str) -> list[ExtractedItem]:
        items: list[ExtractedItem] = []
        for t in c.tables:
            items += self._closures_from_table(c, t, kind)
        events = self._pair_sections(c, kind, fallback=not items)
        if items:   # tabuľka (SROV) – z textu len udalosti s vlastným úsekom aj obdobím
            events = [e for e in events if e[0] is not None and e[1] is not None]
        single = len(events) == 1 and not items
        for sec, per, span in events:
            items.append(self._closure_item(c, kind, sec, per, span, single))
        if not items:
            items.append(self._closure_item(c, kind, None, c.periods[0] if c.periods else None,
                                            (0, len(c.text)), True))
        return _dedupe(items)

    def _pair_sections(self, c: _Ctx, kind: str, fallback: bool = True
                       ) -> list[tuple[Optional[Section], Optional[Period], tuple[int, int]]]:
        events: list = []
        pend_sec = pend_per = None
        for bs, be in c.blocks[1:]:
            if c.in_table(bs):
                continue
            secs = [s for s in c.sections(bs, be) if not s.line_desc and not c.in_table(s.start)]
            pers = [p for p in c.periods if bs <= p.pos < be and not c.in_table(p.pos)
                    and (kind == "vyluka_zrusenie" or not c.is_old_period(p))]
            if secs and pers:
                if len(secs) == 1:
                    events += [(secs[0], p, (bs, be)) for p in pers]
                elif len(pers) == 1:
                    events += [(s, pers[0], (bs, be)) for s in secs]
                else:
                    for s in secs:
                        after = [p for p in pers if p.pos >= s.start] or pers
                        events.append((s, min(after, key=lambda p: abs(p.pos - s.end)), (bs, be)))
                pend_sec = pend_per = None
            elif secs:
                if pend_per:
                    events += [(s, pend_per[0], (pend_per[1][0], be)) for s in secs]
                    pend_per = None
                else:
                    pend_sec = (secs, (bs, be))
            elif pers:
                if pend_sec:
                    events += [(s, pers[0], (pend_sec[1][0], be)) for s in pend_sec[0]]
                    pend_sec = None
                elif pend_per is None:
                    pend_per = (pers[0], (bs, be))
        subj = [s for s in c.sections(0, c.subject_end) if not s.line_desc] if fallback else []
        if pend_sec and not events and fallback:
            events += [(s, None, pend_sec[1]) for s in pend_sec[0]]
        if pend_per and not events and fallback:
            events.append((subj[0] if subj else None, pend_per[0], pend_per[1]))
        if not events and subj:
            cands = [p for p in c.periods if kind == "vyluka_zrusenie" or not c.is_old_period(p)]
            per = cands[0] if cands else None
            events.append((subj[0], per, (0, len(c.text))))
        # rovnaký úsek a obdobie len raz; úsek bez obdobia vynecháme, ak je aj s obdobím
        out, seen = [], set()
        with_period = {(s.a.name, s.b.name) for s, p, _ in events if s and p}
        for s, p, span in events:
            key = (s.a.name if s else None, s.b.name if s else None, p.start if p else None, p.end if p else None)
            if key in seen or (s and not p and (s.a.name, s.b.name) in with_period):
                continue
            seen.add(key)
            out.append((s, p, span))
        return out

    def _closure_item(self, c: _Ctx, kind: str, sec: Optional[Section], per: Optional[Period],
                      span: tuple[int, int], single: bool) -> ExtractedItem:
        bs, be = (0, len(c.text)) if single else span
        ctx_f = c.f[bs:be] if not single else c.f
        rkind, assumed = restriction_kind(ctx_f if not single else c.f[c.subject_end:] + " " + c.subject_f)
        item_kind = kind
        if kind == "vyluka_nova" and rkind == "pomala_jazda":
            item_kind = "pomala_jazda"
        if kind == "pomala_jazda" and rkind != "pomala_jazda" and re.search(r"vyluk", ctx_f):
            item_kind = "vyluka_nova"
        if item_kind == "pomala_jazda":
            rkind, assumed = "pomala_jazda", False
        notes: list[str] = []
        fc: dict = {}
        ev: dict = {}
        penalties = 0.0
        data: dict = {"kind": rkind}
        if assumed and item_kind != "vyluka_zrusenie":
            notes.append("Druh výluky nie je uvedený – predpokladá sa úplná výluka úseku (najprísnejší prípad).")
            penalties += 0.03
            fc["kind"] = 0.5
        else:
            fc["kind"] = 0.8
        if sec:
            data["from_station"], data["to_station"] = sec.a.text, sec.b.text
            if sec.via:
                data["via"] = [v.text for v in sec.via]
            for key, m in (("from_station", sec.a), ("to_station", sec.b)):
                fc[key] = 0.85 if m.method == "presne" else 0.65
                if m.method != "presne":
                    penalties += 0.05
                    notes.append(f"Názov „{m.text}“ určený podľa skloňovaného tvaru → {m.name}.")
            ev["usek"] = c.line_of(sec.start)
        if per:
            data["start"], data["end"] = _iso(per.start), _iso(per.end)
            if per.daily_from:
                data["daily_from"], data["daily_to"] = per.daily_from, per.daily_to
            if "do_odvolania" in per.flags:
                data["until_revoked"] = True
            notes += _flag_notes(per)
            if "rok_doplneny" in per.flags:
                penalties += 0.03
            fc["start"] = 0.85
            fc["end"] = 0.85 if per.end else 0.0
            ev["obdobie"] = c.line_of(per.pos)
        a, b = (bs, be) if not single else (0, len(c.text))
        ref = c.first_group(_CLOSURE_REF_RES, a, b) or (c.first_group(_CLOSURE_REF_RES, 0, len(c.text))
                                                        if item_kind in ("vyluka_zmena", "vyluka_zrusenie") else None)
        if ref:
            data["external_ref"] = ref
        data["line_ref"] = c.line_ref(a, b)
        data["track"] = c.track(a, b)
        if item_kind == "pomala_jazda" or rkind == "pomala_jazda":
            data["speed_kmh"] = c.speed(a, b)
        reason = c.reason(a, b)
        if reason:
            data["note"] = reason
        if item_kind == "vyluka_zrusenie":
            data = {"external_ref": data.get("external_ref"), "from_station": data.get("from_station"),
                    "to_station": data.get("to_station"), "start": data.get("start")}
            found = 2 if data["external_ref"] else (1 if data["from_station"] and data["to_station"] else 0)
            found += 1 if data["start"] else 0
            conf = _confidence(min(found, 2), 2, penalties)
        else:
            required = ["from_station", "to_station", "start", "end"] + (
                ["speed_kmh"] if item_kind == "pomala_jazda" else [])
            found = sum(1 for k in required if data.get(k))
            if data.get("until_revoked") and not data.get("end"):
                found += 0.5
            conf = _confidence(found, len(required), penalties)
            missing = [k for k in required if not data.get(k) and not (k == "end" and data.get("until_revoked"))]
            if missing:
                notes.append("Chýbajú polia: " + ", ".join(missing) + ".")
        return ExtractedItem(item_kind, data, conf, notes, fc, ev)

    def _closures_from_table(self, c: _Ctx, t: Table, kind: str) -> list[ExtractedItem]:
        cols = _map_columns(t, _CLOSURE_COLS)
        for amb, (sk, tk) in (("od", ("from_station", "start")), ("do", ("to_station", "end"))):
            for idx, h in enumerate(t.header):
                if h in (amb, f"{amb}:") and idx not in cols.values():
                    vals = [r[idx] for r in t.rows[:5] if idx < len(r) and r[idx]]
                    if vals and all(datumy.parse_datetime_text(v, c.ref) for v in vals):
                        cols.setdefault(tk, idx)
                    elif vals and any(c.st.match(v, allow_fuzzy=False) for v in vals):
                        cols.setdefault(sk, idx)
        has_section = "section" in cols or ("from_station" in cols and "to_station" in cols)
        if not has_section or "start" not in cols:
            return []
        items = []
        for row, pos in zip(t.rows, t.row_pos):
            def cell(key: str) -> str:
                i = cols.get(key)
                return row[i].strip() if i is not None and i < len(row) else ""

            row_f = fold(" ".join(row))
            notes: list[str] = []
            data: dict = {}
            fc: dict = {}
            penalties = 0.0
            a = b = None
            if "section" in cols and cell("section"):
                secs = _cell_sections(c, cell("section"))
                if secs:
                    a, b = secs
            if (a is None or b is None) and "from_station" in cols:
                a, b = c.st.match(cell("from_station")), c.st.match(cell("to_station"))
                if a is None and cell("from_station"):
                    a = None
                    data["from_station"] = cell("from_station")
                if b is None and cell("to_station"):
                    data["to_station"] = cell("to_station")
            if a is not None:
                data["from_station"] = a.text
                fc["from_station"] = 0.85 if a.method == "presne" else 0.65
                penalties += 0 if a.method == "presne" else 0.05
            if b is not None:
                data["to_station"] = b.text
                fc["to_station"] = 0.85 if b.method == "presne" else 0.65
                penalties += 0 if b.method == "presne" else 0.05
            if not data.get("from_station") and cell("section"):
                data["from_station"] = cell("section")
                notes.append(f"Úsek „{cell('section')}“ sa nepodarilo rozdeliť na dopravne.")
            start = datumy.parse_datetime_text(cell("start"), c.ref) if cell("start") else None
            end = datumy.parse_datetime_text(cell("end"), c.ref) if cell("end") else None
            if end is not None and not datumy.HAS_TIME.search(cell("end")):
                end += timedelta(days=1)       # dátum bez času = do konca dňa
                notes.append("Koniec bez času – platí do konca uvedeného dňa.")
            daily = cell("daily")
            if daily:
                tms = re.findall(r"(\d{1,2})\s?[:.]\s?(\d{2})", daily)
                if len(tms) >= 2:
                    data["daily_from"] = f"{int(tms[0][0]):02d}:{tms[0][1]}"
                    data["daily_to"] = f"{int(tms[1][0]):02d}:{tms[1][1]}"
                    if start and not datumy.HAS_TIME.search(cell("start")):
                        start = datetime.combine(start.date(), time(int(tms[0][0]) % 24, int(tms[0][1])))
                    if end and not datumy.HAS_TIME.search(cell("end")):
                        end = datetime.combine(end.date() - timedelta(days=1),
                                               time(int(tms[1][0]) % 24, int(tms[1][1])))
            if start and end and end <= start and not re.search(r"\d{4}", cell("end")):
                end += timedelta(days=1)
                notes.append("Koniec je na nasledujúci deň (cez polnoc).")
            data["start"], data["end"] = _iso(start), _iso(end)
            rkind, assumed = restriction_kind(fold(cell("kind")) + " " + row_f)
            item_kind = "pomala_jazda" if rkind == "pomala_jazda" and kind == "vyluka_nova" else kind
            data["kind"] = "pomala_jazda" if item_kind == "pomala_jazda" else rkind
            if assumed:
                notes.append("Druh výluky nie je uvedený – predpokladá sa úplná výluka úseku.")
                penalties += 0.03
            if cell("external_ref"):
                data["external_ref"] = cell("external_ref")
            elif cell("order"):
                head = c.first_group(_CLOSURE_REF_RES, 0, len(c.text))
                if head:
                    data["external_ref"] = f"{head}/{cell('order')}"
            if cell("line_ref"):
                m = re.search(r"\d{3}[a-zA-Z]?", cell("line_ref"))
                data["line_ref"] = m.group(0).upper() if m else cell("line_ref")
            tr = cell("track")
            if tr:
                m = re.search(r"\d", tr)
                data["track"] = "obe" if re.search(r"\bob[eiao]", fold(tr)) else (m.group(0) if m else tr)
            if item_kind == "pomala_jazda":
                sp = re.search(r"(\d{1,3})", cell("speed_kmh")) if cell("speed_kmh") else None
                data["speed_kmh"] = int(sp.group(1)) if sp else c.speed(pos, pos + len(" ".join(row)) + 5)
            if cell("note"):
                data["note"] = one_line(cell("note"), 300)
            required = ["from_station", "to_station", "start", "end"] + (
                ["speed_kmh"] if item_kind == "pomala_jazda" else [])
            found = sum(1 for k in required if data.get(k))
            item = ExtractedItem(item_kind, data, _confidence(found, len(required), penalties), notes, fc,
                                 {"riadok_tabulky": one_line("\t".join(row), 300)})
            item.notes.append("Údaje z tabuľky v prílohe alebo v texte.")
            items.append(item)
        return items

    # -------------------------------------------------------------- trasy
    def _paths(self, c: _Ctx, kind: str) -> list[ExtractedItem]:
        notes: list[str] = []
        fc: dict = {}
        ev: dict = {}
        penalties = 0.0
        trains = c.trains(0, c.subject_end) + c.trains(c.subject_end)
        data: dict = {}
        if trains:
            num, cat, pos, _ = trains[0]
            data["train_number"] = num
            fc["train_number"] = 0.85
            ev["vlak"] = c.line_of(pos)
            others = sorted({t[0] for t in trains} - {num})
            if others:
                notes.append("V e-maile sú aj ďalšie čísla vlakov: " + ", ".join(others) + " – skontrolujte.")
                penalties += 0.05
        run_day = _labelled_date(c, _PATH_DATE_LABEL)
        if run_day is None:
            subj = [t for t in c.toks if t.start < c.subject_end and t.day]
            body = [t for t in c.toks if t.start >= c.subject_end and t.day and not c.in_table(t.start)]
            pick = (subj or body or [None])[0]
            run_day = pick.day if pick else None
            if pick is not None and not pick.year_given:
                notes.append("Rok v dátume jazdy bol doplnený podľa dátumu prijatia e-mailu.")
        if run_day:
            data["date"] = run_day.isoformat()
            fc["date"] = 0.8
        if re.search(r"\bad[\s-]?hoc\b|\bp5\b|individualn\w*\s+tras|mimoriadn\w*\s+tras", c.f):
            data["kind"] = "ad_hoc"
        elif re.search(r"\bgvd\b|grafikon|rocn\w*\s+(?:cestovn\w*\s+poriad|tras)|katalogov", c.f):
            data["kind"] = "gvd"
        ref = c.first_group(_PATH_REF_RES, 0, len(c.text))
        if ref:
            data["external_ref"] = ref
        if kind == "trasa_zrusena":
            found = sum(1 for k in ("train_number", "date") if data.get(k))
            return [ExtractedItem(kind, {k: data.get(k) for k in ("train_number", "date", "external_ref")},
                                  _confidence(found, 2, penalties), notes, fc, ev)]
        if re.search(r"ponuk\w*\s+(?:na\s+)?(?:\w+\s+)?tras|\bponuka\b|nabidk\w*\s+tras", c.f) and not re.search(
                r"akceptovan\w*\s+ponuk|ponuk\w*\s+(?:bola\s+)?akceptovan|prijat\w*\s+ponuk|potvrden\w*\s+akceptaci"
                r"|akceptaci\w*\s+(?:ponuky\s+)?(?:bola\s+)?potvrden|tras\w*\s+(?:je\s+|bola\s+)?pridelen", c.f):
            data["offer"] = True
            notes.append("Ponuka trasy (KIS) – trasa ešte nie je pridelená, treba ju akceptovať v lehote.")
            m = re.search(r"(?:lehot\w*|termin\w*)\s+(?:na\s+)?(?:akceptaci\w*|prijati\w*|potvrdeni\w*|odpoved\w*)"
                          r"|akceptovat\s+(?:najneskor\w*\s+)?do|platnost\w*\s+ponuky(?:\s+do)?", c.f)
            if m:
                got = datumy.first_datetime_after(c.f, c.toks, m.end(), 60)
                if got:
                    data["accept_by"] = _iso(got[0])
        points, how = _path_points(c)
        if points:
            data["points"] = points_to_iso([(st.text, a, d) for st, a, d in points], run_day, notes)
            fc["points"] = 0.8 if how == "tabulka" else 0.6
            ev["body_trasy"] = f"{len(points)} dopravní ({how})"
            if how == "retaz":
                notes.append("Body trasy sú len zo zoznamu dopravní bez časov.")
                penalties += 0.05
        found = sum(1 for k in ("train_number", "date") if data.get(k)) + (1 if data.get("points") else 0)
        if not data.get("points"):
            notes.append("V e-maile sa nenašiel zoznam dopravní s časmi.")
        return [ExtractedItem(kind, data, _confidence(found, 3, penalties), notes, fc, ev)]

    # -------------------------------------------------------------- avízo
    def _avizo(self, c: _Ctx, kind: str) -> list[ExtractedItem]:
        trains = c.trains(0, c.subject_end) + c.trains(c.subject_end)
        labels = [m.end() for m in _ETA_LABEL.finditer(c.f)]
        etas: list[tuple[datetime, int]] = []
        for pos in labels:
            got = datumy.first_datetime_after(c.f, c.toks, pos, 120, fallback_day=None)
            if got:
                etas.append((got[0], got[1].start))
        if not etas:     # bez návestia: prvý dátum s časom v tele
            for t in c.toks:
                if t.kind == "datetime" and t.start >= c.subject_end:
                    etas.append((t.dt, t.start))
                    break
        borders = find_borders_in_text(c.f)
        items: list[ExtractedItem] = []
        uniq_trains: list = []
        for t in trains:
            if t[0] not in [u[0] for u in uniq_trains]:
                uniq_trains.append(t)
        pairs: list = []
        if len(uniq_trains) <= 1:
            pairs = [(uniq_trains[0] if uniq_trains else None, etas[0] if etas else None)]
        else:
            # viac vlakov – párovanie v rámci riadku/odseku (tabuľka avíz)
            for num, cat, pos, end in uniq_trains:
                line_s, line_e = c.text.rfind("\n", 0, pos) + 1, c.text.find("\n", end)
                line_e = len(c.text) if line_e == -1 else line_e
                cand = [e for e in etas if line_s <= e[1] <= line_e]
                if not cand:
                    got = [t for t in c.toks if t.kind == "datetime" and line_s <= t.start <= line_e]
                    cand = [(got[0].dt, got[0].start)] if got else []
                pairs.append(((num, cat, pos, end), cand[0] if cand else None))
        for train, eta in pairs:
            notes: list[str] = []
            fc: dict = {}
            ev: dict = {}
            penalties = 0.0
            data: dict = {}
            if train:
                data["train_number"] = train[0]
                fc["train_number"] = 0.85
                ev["vlak"] = c.line_of(train[2])
            if eta:
                data["eta"] = _iso(eta[0])
                data["date"] = eta[0].date().isoformat()
                fc["eta"] = 0.85
                ev["eta"] = c.line_of(eta[1])
            if borders:
                ref_pos = eta[1] if eta else (train[2] if train else 0)
                codes = {b.code for b, _, _ in borders}
                best = min(borders, key=lambda h: abs(h[1] - ref_pos))
                data["border"] = c.text[best[1]:best[2]]
                data["border_code"] = best[0].code
                fc["border"] = 0.8 if len(codes) == 1 else 0.6
                ev["hranica"] = c.line_of(best[1])
                if len(codes) > 1:
                    notes.append("V e-maile je viac hraničných priechodov – vybraný najbližší k času príchodu.")
                    penalties += 0.05
            found = sum(1 for k in ("train_number", "border", "eta") if data.get(k))
            missing = [k for k in ("train_number", "border", "eta") if not data.get(k)]
            if missing:
                notes.append("Chýbajú polia: " + ", ".join(missing) + ".")
            items.append(ExtractedItem("avizo", data, _confidence(found, 3, penalties), notes, fc, ev))
        return items

    # -------------------------------------------------------------- mimoriadnosť
    def _incident(self, c: _Ctx, kind: str) -> list[ExtractedItem]:
        notes: list[str] = []
        fc: dict = {}
        ev: dict = {}
        data: dict = {}
        penalties = 0.0
        secs = [s for s in c.sections(c.subject_end, len(c.text)) if not s.line_desc] or \
            [s for s in c.sections(0, c.subject_end) if not s.line_desc]
        if secs:
            data["from_station"], data["to_station"] = secs[0].a.text, secs[0].b.text
            fc["from_station"] = fc["to_station"] = 0.8
            ev["usek"] = c.line_of(secs[0].start)
        else:
            m = re.search(r"\bv\s+(?:zst\.?|z\.\s?st\.|zel\.\s?st\.|stanici|zelezničnej stanici|dopravni|zastavke)\s+",
                          c.f)
            if m:
                st = c.st.prefix(tokens(c.text[m.end():m.end() + 80], m.end()), c.text)
                if st:
                    data["from_station"] = data["to_station"] = st.text
                    fc["from_station"] = fc["to_station"] = 0.75
                    notes.append("Mimoriadnosť v stanici – úsek je len táto dopravňa.")
        end_m = _END_LABEL.search(c.f, c.subject_end)
        start_tok = None
        for t in c.toks:
            if t.start >= c.subject_end and t.kind == "datetime" and not (end_m and t.start >= end_m.start()
                                                                           and t.start <= end_m.end() + 60):
                start_tok = t
                break
        if start_tok is not None:
            data["start"] = _iso(start_tok.dt)
            fc["start"] = 0.8
            ev["zaciatok"] = c.line_of(start_tok.start)
        if end_m:
            got = datumy.first_datetime_after(c.f, c.toks, end_m.end(), 80,
                                              fallback_day=start_tok.day if start_tok else None)
            if got:
                end_dt = got[0]
                if start_tok is not None and end_dt <= start_tok.dt:
                    end_dt += timedelta(days=1)
                data["end"] = _iso(end_dt)
                fc["end"] = 0.7
                notes.append("Koniec je predpokladaný (odhad ŽSR), nie záväzný.")
        desc = _incident_sentence(c)
        if desc:
            data["description"] = desc
            fc["description"] = 0.8
        found = sum(1 for k in ("start", "description") if data.get(k)) + (1 if data.get("from_station") else 0)
        if not data.get("from_station"):
            notes.append("Miesto mimoriadnosti (úsek alebo stanica) sa nenašlo.")
        return [ExtractedItem("mimoriadnost", data, _confidence(found, 3, penalties), notes, fc, ev)]

    # -------------------------------------------------------------- objednávka
    def _order(self, c: _Ctx, kind: str) -> list[ExtractedItem]:
        notes: list[str] = []
        fc: dict = {}
        ev: dict = {}
        data: dict = {}
        penalties = 0.0
        for key, pat in _ORDER_LABELS.items():
            m = pat.search(c.f, c.subject_end)
            if not m:
                continue
            ve = c.text.find("\n", m.end())
            ve = len(c.text) if ve == -1 else ve
            raw = c.text[m.end():ve].strip(" :\t-–")
            raw_f = fold(raw)
            if not raw:
                continue
            ev[key] = c.line_of(m.start())
            if key in ("origin", "destination"):
                st = c.st.prefix(tokens(raw), raw)
                data[key] = st.text if st else one_line(re.split(r"[,;(]", raw)[0], 120)
                fc[key] = 0.85 if st and st.method == "presne" else 0.6
            elif key in ("earliest_departure", "deadline"):
                got = datumy.first_datetime_after(c.f, c.toks, m.end(), 60)
                if got:
                    dt, tok = got
                    if tok.kind == "date" and not datumy.HAS_TIME.search(raw):
                        if key == "deadline":
                            dt = datetime.combine(tok.day, time(23, 59))
                            notes.append("Termín dodania bez času – predpokladá sa koniec dňa (23:59).")
                        else:
                            notes.append("Najskorší odchod bez času – predpokladá sa 00:00.")
                    data[key] = _iso(dt)
                    fc[key] = 0.8
            elif key == "customer":
                data[key] = one_line(raw, 200)
                fc[key] = 0.85
            elif key == "cargo":
                data[key] = one_line(re.split(r"[;]", raw)[0], 200)
                fc[key] = 0.8
            elif key == "wagons":
                m2 = re.search(r"\d{1,3}", raw)
                if m2:
                    data[key] = int(m2.group(0))
                    fc[key] = 0.8
            elif key == "weight_t":
                v = _number(raw_f, r"(?:t\b|ton)")
                if v is not None:
                    data[key] = v
                    fc[key] = 0.8
            elif key == "length_m":
                v = _number(raw_f, r"m\b")
                if v is not None:
                    data[key] = v
                    fc[key] = 0.8
        # RID / nebezpečný tovar
        rid = _rid(c.f[c.subject_end:])
        if rid is not None:
            data["rid"] = rid
            fc["rid"] = 0.8
        else:
            notes.append("Nebezpečný tovar (RID) nie je uvedený – overte.")
        # počet vozňov / hmotnosť / dĺžka aj z voľného textu
        if "wagons" not in data:
            m = re.search(r"(\d{1,3})\s*(?:ks\s+)?(?:vozn|vozu|vozov)", c.f)
            if m:
                data["wagons"] = int(m.group(1))
        if "weight_t" not in data:
            m = re.search(r"(?:hmotnost\w*|vaha|brutto)[^\n\d]{0,25}(\d{1,3}(?:[ .]\d{3})+|\d+)(?:[,.]\d+)?\s*t\b",
                          c.f[c.subject_end:])
            if m:
                data["weight_t"] = _number(m.group(0), r"t\b")
        if "length_m" not in data:
            m = re.search(r"dlzk\w*[^\n\d]{0,20}(\d[\d\s.,]{0,6})\s*m\b|delk\w*[^\n\d]{0,20}(\d[\d\s.,]{0,6})\s*m\b", c.f)
            if m:
                data["length_m"] = _number((m.group(1) or m.group(2)) + " m", r"m")
        # odkiaľ – kam z predmetu („Dunajská Streda → Košice“, „z Čadce do Žiliny“)
        if not data.get("origin") or not data.get("destination"):
            secs = c.sections(0, c.subject_end) or c.sections(c.subject_end, len(c.text))
            if secs:
                data.setdefault("origin", secs[0].a.text)
                data.setdefault("destination", secs[0].b.text)
                fc.setdefault("origin", 0.7)
                fc.setdefault("destination", 0.7)
            else:
                m = re.search(r"\bz(?:o|e)?\s+(.{3,40}?)\s+do\s+(.{3,40}?)(?:[,.;\n]|$)", c.f)
                if m:
                    a = c.st.match(c.text[m.start(1):m.end(1)])
                    b = c.st.match(c.text[m.start(2):m.end(2)])
                    if a and b:
                        data.setdefault("origin", a.text)
                        data.setdefault("destination", b.text)
                        fc.setdefault("origin", 0.65)
                        fc.setdefault("destination", 0.65)
        if not data.get("customer"):
            who = c.doc.sender_name or (c.doc.sender.split("@")[-1] if c.doc.sender else "")
            if who:
                data["customer"] = who
                fc["customer"] = 0.5
                notes.append("Zákazník nie je v texte uvedený – určený podľa odosielateľa e-mailu.")
                penalties += 0.05
        required = ["customer", "origin", "destination", "deadline"]
        found = sum(1 for k in required if data.get(k))
        if data.get("earliest_departure") and not data.get("deadline"):
            found += 0.5
        missing = [k for k in required if not data.get(k)]
        if missing:
            notes.append("Chýbajú polia: " + ", ".join(missing) + ".")
        return [ExtractedItem("objednavka", data, _confidence(found, len(required), penalties), notes, fc, ev)]


# ------------------------------------------------------------------ pomocné funkcie extrakcie
_CLOSURE_COLS = [
    ("order", r"^(?:p\.\s?c\.|por\.\s?c\.|poradie|p\.c\.|c\.)$"),
    ("external_ref", r"^(?:cislo|c\.|id|oznacenie|ref)\s*(?:vyluky|rozkazu|rov|s?rov)\b|^cislo\s+vyluky|^id$|^s?rov$"),
    ("line_ref", r"^trat\b|^c\.?\s?trate|^trat\s+c\."),
    ("section", r"^usek\b(?!\s+od|\s+do)|^tratov\w*\s+usek|^miesto\b|^usek\s*/|^dopravna\s*/\s*usek|^usek\s*\("),
    ("from_station", r"^(?:usek\s+)?od\s+(?:stanice|dopravne)|^z(?:o)?\s+stanice|^stanica\s+od|^dopravna\s+od"
                     r"|^usek\s+od$|^zaciatok\s+useku"),
    ("to_station", r"^(?:usek\s+)?do\s+(?:stanice|dopravne)|^stanica\s+do|^dopravna\s+do|^usek\s+do$|^koniec\s+useku"),
    ("track", r"^kol[ae]j|^tk$"),
    ("kind", r"^druh|^typ"),
    ("start", r"^zaciatok(?!\s+useku)|^platnost\s+od|^termin\s+od|^datum\s+od|^od\s*\(|^cas\s+od|^vyluka\s+od"
              r"|^plati\s+od|^od\s+(?:datum|dna)"),
    ("end", r"^koniec(?!\s+useku)|^platnost\s+do|^termin\s+do|^datum\s+do|^do\s*\(|^cas\s+do|^vyluka\s+do"
            r"|^plati\s+do|^ukoncenie|^do\s+(?:datum|dna)"),
    ("daily", r"^denne|^casov\w*\s+okno|^cas\s+vyluky|^denny\s+cas|^cas$"),
    ("speed_kmh", r"rychlost|km/h|vmax"),
    ("note", r"^poznam|^dovod|^popis|^pozn"),
]


def _map_columns(t: Table, spec: list[tuple[str, str]]) -> dict[str, int]:
    cols: dict[str, int] = {}
    for key, pat in spec:
        rx = re.compile(pat)
        for idx, h in enumerate(t.header):
            if idx in cols.values():
                continue
            if h and rx.search(h):
                cols[key] = idx
                break
    return cols


def _cell_sections(c: _Ctx, cell: str) -> Optional[tuple[StationMatch, StationMatch]]:
    parts = re.split(r"\s*[–—−>]\s*|\s+-\s+|\s*/\s*", cell)
    parts = [p for p in parts if p.strip()]
    if len(parts) >= 2:
        a, b = c.st.match(parts[0]), c.st.match(parts[-1])
        if a and b:
            return a, b
    if "-" in cell:
        for h in [i for i, ch in enumerate(cell) if ch == "-"]:
            a, b = c.st.match(cell[:h], allow_fuzzy=False), c.st.match(cell[h + 1:], allow_fuzzy=False)
            if a and b:
                return a, b
    return None


_PATH_DATE_LABEL = re.compile(r"(?:datum\s+jazdy|den\s+jazdy|datum\s+odchodu|datum\s+(?:jazdy\s+)?vlaku|jazda\s+dna"
                              r"|na\s+den|pre\s+den|platnost|plati\s+(?:dna|pre|na)|datum|dna|dne)\s*:?\s*")


def _labelled_date(c: _Ctx, label: re.Pattern) -> Optional[date]:
    for m in label.finditer(c.f, c.subject_end):
        for t in c.toks:
            if m.end() <= t.start <= m.end() + 25 and t.day:
                return t.day
            if t.start > m.end() + 25:
                break
    for m in label.finditer(c.f, 0, c.subject_end):
        for t in c.toks:
            if m.end() <= t.start <= m.end() + 25 and t.day:
                return t.day
    return None


_TIME_CELL = re.compile(r"^(\d{1,2})\s?[:.]\s?(\d{2})$")
_DASH_CELL = re.compile(r"^(?:-{1,3}|x|—|–|\.|)$")


def _path_points(c: _Ctx) -> tuple[list, str]:
    """Body trasy: [(StationMatch, prichod 'HH:MM'|None, odchod 'HH:MM'|None)]."""
    # 1) tabuľka s hlavičkou (XLSX/CSV)
    for t in c.tables:
        cols = _map_columns(t, [("station", r"dopravn|stanic|^zst|zastavk|^bod"), ("arr", r"prich|prijezd|^arr"),
                                ("dep", r"odch|odjezd|^dep")])
        if "station" in cols and ("arr" in cols or "dep" in cols):
            pts = []
            for row in t.rows:
                st = c.st.match(row[cols["station"]]) if cols["station"] < len(row) else None
                if not st:
                    continue
                arr = _cell_time(row[cols["arr"]]) if "arr" in cols and cols["arr"] < len(row) else None
                dep = _cell_time(row[cols["dep"]]) if "dep" in cols and cols["dep"] < len(row) else None
                pts.append((st, arr, dep))
            if len(pts) >= 2:
                return pts, "tabulka"
    # 2) riadky „dopravňa  čas  čas“ (zarovnané medzerami), voliteľne pod hlavičkou
    order = ("arr", "dep")
    pts: list = []
    for ls, le in _lines(c.text, c.subject_end + 1):
        lf = c.f[ls:le]
        if re.search(r"prich|prijezd", lf) and re.search(r"odch|odjezd", lf):
            order = ("arr", "dep") if lf.find("prich" if "prich" in lf else "prijezd") < lf.find(
                "odch" if "odch" in lf else "odjezd") else ("dep", "arr")
            continue
        p = _point_line(c, ls, le, order)
        if p:
            pts.append(p)
        elif pts and len(pts) >= 2 and c.text[ls:le].strip():
            break
    if len(pts) >= 2:
        # jeden čas v riadku: prvý = odchod, posledný = príchod, ostatné = prejazd
        fixed = []
        for i, (st, arr, dep, single) in enumerate(pts):
            if single:
                tm = arr or dep
                if i == 0:
                    arr, dep = None, tm
                elif i == len(pts) - 1:
                    arr, dep = tm, None
                else:
                    arr = dep = tm
            fixed.append((st, arr, dep))
        return fixed, "tabulka"
    # 3) reťaz „Trasa: A – B – C“
    for ls, le in _lines(c.text):
        for sec in c.sections(ls, le):
            chain = [sec.a, *sec.via, sec.b]
            if len(chain) >= 2 and (sec.via or re.search(r"tras|relaci|smer|cez", c.f[ls:le])):
                return [(s, None, None) for s in chain], "retaz"
    return [], ""


def _cell_time(cell: str) -> Optional[str]:
    cell = (cell or "").strip()
    m = re.search(r"(\d{1,2})\s?[:.]\s?(\d{2})", cell)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    return f"{hh:02d}:{mm:02d}" if hh < 24 and mm < 60 else None


def _point_line(c: _Ctx, ls: int, le: int, order: tuple[str, str]):
    line = c.text[ls:le]
    if not line.strip() or len(line) > 160:
        return None
    toks = tokens(line, ls)
    st = c.st.prefix(toks, c.text)
    if not st:
        return None
    rest = c.text[st.end:le]
    cells = [x for x in re.split(r"[\t|;]+|\s+", rest.strip()) if x != ""]
    times: list[Optional[str]] = []
    for cell in cells:
        if _TIME_CELL.match(cell):
            times.append(_cell_time(cell))
        elif _DASH_CELL.match(cell) or cell in ("–", "—"):
            times.append(None)
        elif re.match(r"^(?:prejazd|pobyt|zastavenie|\(.*\)|\d+\s?min)", fold(cell)):
            continue
        else:
            return None
    if not times or len(times) > 3 or all(t is None for t in times):
        return None
    if len(times) == 1:
        return (st, times[0], None, True)
    vals = dict(zip(order, times[:2]))
    return (st, vals.get("arr"), vals.get("dep"), False)


def points_to_iso(points: list, run_day: Optional[date], notes: list) -> list[dict]:
    """[(stanica, príchod 'HH:MM', odchod 'HH:MM')] + deň jazdy → body s časmi ISO UTC (aj cez polnoc)."""
    out = []
    if run_day is None:
        if any(a or d for _, a, d in points):
            notes.append("Chýba dátum jazdy – časy v bodoch trasy nemožno určiť.")
        return [{"station": st, "arr": None, "dep": None} for st, _, _ in points]
    day, last = run_day, None

    def conv(hhmm: Optional[str]) -> Optional[str]:
        nonlocal day, last
        if not hhmm:
            return None
        h, m = map(int, hhmm.split(":"))
        dt = datetime.combine(day, time(h, m))
        if last is not None and dt < last - timedelta(minutes=1):
            day = day + timedelta(days=1)
            dt = datetime.combine(day, time(h, m))
            if "Trasa prechádza cez polnoc – časy po polnoci sú na nasledujúci deň." not in notes:
                notes.append("Trasa prechádza cez polnoc – časy po polnoci sú na nasledujúci deň.")
        last = dt
        return _iso(dt)

    for st, arr, dep in points:
        out.append({"station": st, "arr": conv(arr), "dep": conv(dep)})
    return out


_ETA_LABEL = re.compile(
    r"(?:predpoklad\w*|ocakavan\w*|ocekavan\w*|planovan\w*)\s+(?:cas\s+)?(?:prichod\w*|prijezd\w*|prijazd\w*)"
    r"(?:\s+(?:vlaku\s+)?(?:na\s+(?:hranicu|hranici|statnu\s+hranicu)|do\s+(?:pps\s+|stanice\s+)?[^\s:]+(?:\s+[^\s:]+)?))?"
    r"|\beta\b|cas\s+prichodu|prichod\s+(?:vlaku\s+)?na\s+hranic\w*|prijezd\s+(?:vlaku\s+)?na\s+hranic\w*")
_END_LABEL = re.compile(
    r"predpoklad\w*\s+(?:cas\s+)?(?:obnoveni\w*|ukonceni\w*|odstraneni\w*|sprejazdneni\w*|uvolneni\w*|zprovozneni\w*"
    r"|obnova|koniec|konec)|obnoveni\w*\s+(?:prevadzky|dopravy|provozu)\s+(?:sa\s+)?(?:predpoklad\w*\s+)?(?:o|na|do)?"
    r"|odstranenie\s+(?:poruchy|nasledkov)\s+(?:sa\s+)?predpoklad\w*")
_INCIDENT_WORDS = re.compile(r"mimoriadn|mimoradn|porucha|poruchy|poruchu|nehod|neprejazd|nesjizd|vykolaj|vykolej|"
                             r"zrazk|prerusen|preruseni|pretrhnut|lom\s+kolajnice|poskoden")


def _incident_sentence(c: _Ctx) -> Optional[str]:
    body = c.text[c.subject_end:]
    bf = c.f[c.subject_end:]
    for m in re.finditer(r"[^.\n]*(?:\.(?!\s)[^.\n]*)*[.\n]?", bf):
        seg = m.group(0)
        if _INCIDENT_WORDS.search(seg) and len(seg.strip()) > 15:
            return one_line(body[m.start():m.end()], 400)
    first = one_line(body, 300)
    return first or None


_ORDER_LABELS = {
    "customer": re.compile(r"\b(?:zakaznik|objednavatel|objednatel|odberatel|platca|platce)\s*:"),
    "origin": re.compile(r"\b(?:odosielac\w*\s+stanic\w*|odosielajuc\w*\s+stanic\w*|stanic\w*\s+nakladky"
                         r"|nakladkov\w*\s+stanic\w*|vychodiskov\w*\s+stanic\w*|odkial|odesilac\w*\s+stanic\w*"
                         r"|stanic\w*\s+nakladky|vychozi\s+stanic\w*|odkud|miesto\s+nakladky|misto\s+nakladky)\s*:"),
    "destination": re.compile(r"\b(?:cielov\w*\s+stanic\w*|stanic\w*\s+vykladky|vykladkov\w*\s+stanic\w*"
                              r"|urcujuc\w*\s+stanic\w*|prijimajuc\w*\s+stanic\w*|kam|cilov\w*\s+stanic\w*"
                              r"|urceni|miesto\s+vykladky|misto\s+vykladky|stanic\w*\s+urcenia|stanic\w*\s+urceni)\s*:"),
    "earliest_departure": re.compile(r"\b(?:najskor\w*\s+(?:odchod|nakladka|pristavenie|odoslanie)|pristavenie(?:\s+vozn\w*)?"
                                     r"|termin\s+nakladky|nakladka\s+od|odchod\s+najskor\w*|pripraven\w*\s+na\s+odchod"
                                     r"|nejdrive\w*\s+(?:odjezd|nakladka|pristaveni)|pristaveni(?:\s+voz\w*)?"
                                     r"|odjezd\s+nejdrive|pozadovan\w*\s+odjezd|pozadovan\w*\s+odchod)\s*:"),
    "deadline": re.compile(r"\b(?:termin\s+(?:dodania|dodani|dorucenia|doruceni|vykladky|pristavenia\s+v\s+cieli)"
                           r"|dodanie\s+do|dodani\s+do|dorucenie\s+do|doruceni\s+do|pozadovan\w*\s+(?:dodanie|termin"
                           r"|dodani|doruceni|prichod|prijezd)|dodat\s+do|najneskor\w*\s+(?:dodanie|prichod|do)"
                           r"|nejpozdeji|prichod\s+do\s+ciela|prijezd\s+do\s+cile)\s*:"),
    "cargo": re.compile(r"\b(?:tovar|naklad|komodita|zbozi|substrat|druh\s+tovaru|druh\s+zbozi|nazov\s+tovaru"
                        r"|nazev\s+zbozi|zasielka|zasilka)\s*:"),
    "wagons": re.compile(r"\b(?:pocet\s+vozn\w*|pocet\s+vozu|pocet\s+voz\.|vozne|vozy|vozov)\s*:"),
    "weight_t": re.compile(r"\b(?:hmotnost\w*(?:\s+nakladu|\s+vlaku|\s+brutto|\s+zasielky)?|vaha|brutto"
                           r"|celkov\w*\s+hmotnost\w*)\s*(?:\(t\))?\s*:"),
    "length_m": re.compile(r"\b(?:dlzka\w*(?:\s+vlaku|\s+supravy)?|delka\w*(?:\s+vlaku|\s+soupravy)?)\s*(?:\(m\))?\s*:"),
}


def _number(text: str, unit_re: str) -> Optional[float]:
    m = re.search(r"(\d{1,3}(?:[ .]\d{3})+|\d+)(?:[,.](\d+))?\s*" + unit_re, text)
    if not m:
        m = re.search(r"(\d{1,3}(?:[ .]\d{3})+|\d+)(?:[,.](\d+))?", text)
        if not m:
            return None
    whole = re.sub(r"[ .]", "", m.group(1))
    val = float(whole + ("." + m.group(2) if m.group(2) else ""))
    return int(val) if val.is_integer() else val


def _rid(f: str) -> Optional[bool]:
    if re.search(r"(?:nie\s+je|nejde\s+o|neobsahuje|bez)\s+(?:\w+\s+)?(?:rid|nebezpecn)|\brid\s*:?\s*(?:nie|ne|no)\b"
                 r"|nebezpecn\w*\s+(?:tovar|vec|naklad|zbozi|latk\w*)\s*:?\s*(?:nie|ne)\b|\bnie\s+rid\b", f):
        return False
    if re.search(r"\brid\s*:?\s*(?:ano|yes|ja)\b|\brid\b[^\n]{0,30}\bun\s?\d{4}|\bun\s?\d{4}\b"
                 r"|nebezpecn\w*\s+(?:tovar|vec|naklad|zbozi|latk\w*)\s*:?\s*(?:ano|yes)\b|trieda\s+rid", f):
        return True
    return None


def _dedupe(items: list[ExtractedItem]) -> list[ExtractedItem]:
    out: list[ExtractedItem] = []
    seen: dict = {}
    for it in items:
        d = it.data
        key = (it.kind, fold(str(d.get("from_station") or "")), fold(str(d.get("to_station") or "")),
               d.get("start"), d.get("end"), d.get("external_ref") if it.kind == "vyluka_zrusenie" else None)
        if key in seen:
            prev = out[seen[key]]
            if it.confidence > prev.confidence:
                out[seen[key]] = it
            continue
        seen[key] = len(out)
        out.append(it)
    return out
