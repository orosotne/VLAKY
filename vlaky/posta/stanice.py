"""Hľadanie názvov dopravní a hraničných priechodov v texte e-mailov.

Názvy sa porovnávajú s registrom siete (:func:`vlaky.siet.graf.network`) – bez ohľadu na diakritiku,
so skratkami („hl. st.“) a s jednoduchou podporou skloňovania („z Čadce do Žiliny“).
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from ..config import settings
from .text import fold

# slová, ktorými sa názov dopravne nezačína (zabránia falošným zhodám)
_STOP_WORDS = {
    "a", "aj", "do", "od", "v", "vo", "na", "dna", "dne", "o", "po", "pre", "pri", "medzi", "mezi",
    "usek", "useku", "usekoch", "trat", "trati", "trate", "kolaj", "kolaji", "kolej", "koleji",
    "stanica", "stanice", "stanici", "vlak", "vlaku", "km", "z", "zo", "ze", "cez", "pres", "smer",
    "smerom", "tk", "tv", "vyluka", "vyluky", "vyluke", "denne", "termin", "plati",
}
_ABBREV = ("hl.", "st.", "os.", "zast.", "zst.", "nakl.", "zel.", "n.", "odb.", "vyh.", "zr.")
_HARD_END = re.compile(r"[,;:!?()\[\]{}<>\"]$")
# skrátené slovo názvu dopravne: veľké písmeno a najviac dve malé, bodka („N.“, „L.“, „Sp.“, „Pop.“)
_SHORT_WORD = re.compile(r"[A-ZÁČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ][a-záäčďéíĺľňóôŕšťúýž]{0,2}\.")
_ABBR_WORD = re.compile(r"[a-z]{1,4}\.")
_MAIN_SUFFIX = ("osobna stanica", "hlavna stanica")


@dataclass
class StationMatch:
    """Nájdená dopravňa: pôvodný text, názov podľa registra ŽSR a spôsob zhody."""

    text: str                 # ako je napísané v e-maile
    name: str                 # názov v registri siete
    ref: str                  # skratka ŽSR (railway_ref), ak je
    method: str               # presne | skratka | sklonovanie | priblizne
    start: int = -1
    end: int = -1


@dataclass
class Tok:
    raw: str
    start: int
    end: int

    @property
    def hard_end(self) -> bool:
        r = self.raw
        if _HARD_END.search(r):
            return True
        # skrátené slovo názvu („N. Zámky“, „L. Mikuláš“, „Sp. N. Ves“) koniec názvu nie je
        return r.endswith(".") and not r.lower().endswith(_ABBREV) and not _SHORT_WORD.fullmatch(r)


def tokens(text: str, offset: int = 0) -> list[Tok]:
    return [Tok(m.group(0), m.start() + offset, m.end() + offset) for m in re.finditer(r"\S+", text)]


# označenie dopravne pred jej názvom („žst. Žilina“, „ŽST Kysak“, „žel. st. Púchov“, „z. st. Žilina“, „stanica
# Vrútky“, „železničná stanica …“, „zastávka …“) – názvom dopravne nie je
_PREFIX_WORDS = {"zst", "zst.", "z.st.", "zel.st.", "zeleznicna", "zeleznicnej", "zeleznicnou", "zeleznicnu",
                 "stanica", "stanice", "stanici", "stanicou", "stanicu", "dopravna", "dopravne", "dopravni", "dopravnu",
                 "zastavka", "zastavky", "zastavke", "zastavku", "os.", "zast."}
_PREFIX_FIRST = {"z.", "zel."}           # „z. st.“, „žel. st.“ – „st.“ samo (napr. „St. Ľubovňa“) nie


def strip_station_prefix(toks: list[Tok]) -> list[Tok]:
    """Tokeny bez označenia dopravne na začiatku („žst. Žilina“ → „Žilina“); bez neho pôvodný zoznam."""
    i = 0
    while i < len(toks) and i < 4:
        w = fold(toks[i].raw.strip(",;:(["))
        if w in _PREFIX_WORDS:
            i += 1
        elif w in _PREFIX_FIRST and i + 1 < len(toks) and fold(toks[i + 1].raw) in ("st.", "st"):
            i += 2
        else:
            break
    return toks[i:]


def clean_name(s: str) -> str:
    """Odstráni interpunkciu na okrajoch názvu (bodku ponechá pri skratke „hl. st.“)."""
    s = s.strip().lstrip(",;:([{\"'<>!?*")
    while s and s[-1] in ",;:)]}\"'>!?*. ":
        if s[-1] == "." and s.lower().endswith(_ABBREV):
            break
        s = s[:-1]
    return s.strip()


def _span_text(text: str, a: Tok, b: Tok) -> str:
    return clean_name(text[a.start:b.end])


class Stations:
    """Prístup k registru dopravní (ak sieť nie je k dispozícii, nič nenájde – nepadá)."""

    def __init__(self) -> None:
        try:
            from ..siet.graf import expand_aliases, network, norm

            self.net = network()
            self._norm = lambda s: expand_aliases(norm(s))
        except Exception:  # sieť chýba (minimálna inštalácia) – pravidlá pobežia bez nej
            self.net = None
            self._norm = lambda s: " ".join(fold(s).split())
        self._stems: Optional[list] = None
        self._flat: Optional[dict] = None
        self._name_prefixes: Optional[set] = None
        self._abbr_cache: Optional[dict] = None

    # ------------------------------------------------------------------ základné vyhľadávanie
    @staticmethod
    def _best(cands: list):
        rank = {"station": 0, "yard": 1, "border": 2, "junction": 3, "spur_junction": 4, "halt": 5}
        return sorted(cands, key=lambda s: (not s.main_component, rank.get(s.type, 9), len(s.name)))[0]

    def exact(self, text: str):
        """Presná zhoda názvu (bez diakritiky, so skratkami ako „hl. st.“)."""
        if not self.net or not text or not text.strip():
            return None
        n = self._norm(text.strip())
        if not n or n.split()[0] in _STOP_WORDS:
            return None
        cands = self.net.by_norm.get(n)
        if not cands:
            # „Poprad Tatry“ = „Poprad-Tatry“ (pomlčka a medzera v názve sú rovnocenné)
            if self._flat is None:
                self._flat = {}
                for k, lst in self.net.by_norm.items():
                    self._flat.setdefault(k.replace(" - ", " "), []).extend(lst)
            cands = self._flat.get(n.replace(" - ", " "))
        return self._best(cands) if cands else None

    def by_ref(self, text: str):
        if not self.net or not text:
            return None
        return self.net.by_ref.get(text.strip().lower())

    def _stem_index(self) -> list:
        if self._stems is None:
            self._stems = []
            if self.net:
                for st in self.net.stations:
                    if st.type in ("station", "yard", "border") and st.name:
                        toks = tuple(self._norm(st.name).replace(" - ", " ").split())
                        self._stems.append((toks, st))
        return self._stems

    def _longer_name_starts_with(self, words: list[str]) -> bool:
        """Text je presne začiatkom dlhšieho názvu dopravne („Skalica“ → „Skalica na Slovensku“)."""
        if self._name_prefixes is None:
            self._name_prefixes = set()
            if self.net:
                for key in self.net.by_norm:
                    toks = tuple(key.replace(" - ", " ").split())
                    for i in range(1, len(toks)):
                        self._name_prefixes.add(toks[:i])
        return tuple(words) in self._name_prefixes

    def stem(self, text: str):
        """Zhoda so skloňovaným tvarom („Čadce“ → Čadca, „Dunajskej Stredy“ → Dunajská Streda).

        Uznáva sa len rozdiel v pádovej koncovke (vrátane vypadávajúcej samohlásky: Ružomberok → Ružomberka).
        Iné slovo s podobným začiatkom („Skalica“ ≠ Skalité, „Kremnička“ ≠ Kremnica) zhodou nie je; ak je text
        začiatkom dlhšieho názvu („Skalica na Slovensku“), skloňovanie sa nepoužije – rozhodne približné
        hľadanie, ktoré vždy vyžaduje potvrdenie dispečerom.
        """
        words = self._norm(text).replace(" - ", " ").split()
        if not words or len(words) > 4 or words[0] in _STOP_WORDS:
            return None
        found = [st for toks, st in self._stem_index()
                 if len(toks) == len(words) and all(_stem_eq(a, b) and _inflection_eq(a, b)
                                                    for a, b in zip(toks, words))]
        if not found or len({s.name for s in found}) != 1:
            return None
        if self._longer_name_starts_with(words):
            return None
        return self._best(found)

    def prefix_unique(self, text: str):
        """„Zvolen“ → „Zvolen osobná stanica“: len ak je doplnenie jednoznačné."""
        if not self.net:
            return None
        n = self._norm(text)
        if len(n) < 4:
            return None
        cands = {s.name: s for k, lst in self.net.by_norm.items() if k.startswith(n + " ")
                 for s in lst if s.type in ("station", "yard")}
        if len(cands) == 1:
            return next(iter(cands.values()))
        main = [s for name, s in cands.items() if self._norm(name).endswith(_MAIN_SUFFIX)]
        return main[0] if len(main) == 1 else None

    def abbrev(self, text: str):
        """Skrátený názov dopravne („N. Zámky“ → Nové Zámky, „L. Mikuláš“, „Sp. N. Ves“, „B. Bystrica“, „Nové Mesto
        n. V.“, „Pop.-Tatry“): každé skrátené slovo je začiatkom slova názvu, ostatné slová sa zhodujú – len ak je
        dopravňa jednoznačná."""
        if not self.net or not text:
            return None
        words = self._norm(text).replace(" - ", " ").split()
        if len(words) >= 2 and len(words[-1]) == 1 and words[-1].isalpha():
            words[-1] += "."              # „Nové Mesto n. V.“ – bodka za posledným písmenom sa pri čistení stratí
        if not words or len(words) > 5 or not any(_ABBR_WORD.fullmatch(w) for w in words) or \
                words[0] in _STOP_WORDS or all(_ABBR_WORD.fullmatch(w) for w in words):
            return None
        key = tuple(words)
        if self._abbr_cache is None:
            self._abbr_cache = {}
        if key not in self._abbr_cache:
            found: dict = {}
            for norm_key, lst in self.net.by_norm.items():
                kw = norm_key.replace(" - ", " ").split()
                if len(kw) == len(words) and all(k.startswith(w[:-1]) if _ABBR_WORD.fullmatch(w) else k == w
                                                 for w, k in zip(words, kw)):
                    for s in lst:
                        if s.type in ("station", "yard", "border", "halt"):
                            found[s.name] = s
            self._abbr_cache[key] = next(iter(found.values())) if len(found) == 1 else None
        return self._abbr_cache[key]

    def typo(self, text: str):
        """Preklep v názve (podobnosť ≥ 0,88)."""
        if not self.net:
            return None
        n = self._norm(text)
        if len(n) < 5:
            return None
        close = difflib.get_close_matches(n, list(self.net.by_norm.keys()), n=2, cutoff=0.88)
        if not close:
            return None
        return self._best(self.net.by_norm[close[0]])

    def match(self, text: str, allow_fuzzy: bool = True) -> Optional[StationMatch]:
        """Celý text ako názov dopravne (bunka tabuľky, hodnota poľa, výstup LLM)."""
        if not text or not str(text).strip():
            return None
        t = clean_name(str(text))
        st = self.exact(t)
        if st:
            return StationMatch(t, st.name, st.railway_ref, "presne")
        if re.fullmatch(r"[A-Za-z]{3,5}", t):
            st = self.by_ref(t)
            if st:
                return StationMatch(t, st.name, st.railway_ref, "skratka")
        st = self.stem(t)
        if st:
            return StationMatch(t, st.name, st.railway_ref, "sklonovanie")
        st = self.abbrev(t)
        if st:
            return StationMatch(t, st.name, st.railway_ref, "skratka")
        if allow_fuzzy:
            st = self.prefix_unique(t) or self.typo(t)
            if st:
                return StationMatch(t, st.name, st.railway_ref, "priblizne")
        return None

    # ------------------------------------------------------------------ hľadanie v texte
    def prefix(self, toks: list[Tok], text: str, max_tokens: int = 6) -> Optional[StationMatch]:
        """Najdlhší začiatok zoznamu tokenov, ktorý je názvom dopravne (pravá strana úseku)."""
        limit = min(max_tokens, len(toks))
        for j, t in enumerate(toks[:limit]):
            if t.hard_end:
                limit = j + 1
                break
        for k in range(limit, 0, -1):
            cand = _span_text(text, toks[0], toks[k - 1])
            st = self.exact(cand)
            if st:
                return StationMatch(cand, st.name, st.railway_ref, "presne", toks[0].start, toks[k - 1].end)
        for k in range(min(limit, 3), 0, -1):
            cand = _span_text(text, toks[0], toks[k - 1])
            st = self.stem(cand)
            if st:
                return StationMatch(cand, st.name, st.railway_ref, "sklonovanie", toks[0].start, toks[k - 1].end)
        for k in range(min(limit, 4), 0, -1):
            cand = _span_text(text, toks[0], toks[k - 1])
            st = self.abbrev(cand)
            if st:
                return StationMatch(cand, st.name, st.railway_ref, "skratka", toks[0].start, toks[k - 1].end)
        return None

    def suffix(self, toks: list[Tok], text: str, max_tokens: int = 6) -> Optional[StationMatch]:
        """Najdlhší koniec zoznamu tokenov, ktorý je názvom dopravne (ľavá strana úseku)."""
        n = len(toks)
        if not n:
            return None
        first = max(0, n - max_tokens)
        for j in range(n - 2, first - 1, -1):
            if toks[j].hard_end:
                first = j + 1
                break
        for i in range(first, n):
            cand = _span_text(text, toks[i], toks[-1])
            st = self.exact(cand)
            if st:
                return StationMatch(cand, st.name, st.railway_ref, "presne", toks[i].start, toks[-1].end)
        for i in range(max(first, n - 3), n):
            cand = _span_text(text, toks[i], toks[-1])
            st = self.stem(cand)
            if st:
                return StationMatch(cand, st.name, st.railway_ref, "sklonovanie", toks[i].start, toks[-1].end)
        for i in range(max(first, n - 4), n):
            cand = _span_text(text, toks[i], toks[-1])
            st = self.abbrev(cand)
            if st:
                return StationMatch(cand, st.name, st.railway_ref, "skratka", toks[i].start, toks[-1].end)
        return None


def _stem_eq(station_tok: str, text_tok: str) -> bool:
    if station_tok == text_tok:
        return True
    if len(station_tok) < 4 or len(text_tok) < 3:
        return False
    if station_tok.endswith("any") and text_tok in (station_tok[:-3] + "ian", station_tok[:-3] + "ianoch"):
        return True                       # „Kraľovany“ → „z Kraľovian“
    k = 0
    for a, b in zip(station_tok, text_tok):
        if a != b:
            break
        k += 1
    need = len(station_tok) - 1 if len(station_tok) <= 6 else len(station_tok) - 2
    return k >= max(3, need) and len(text_tok) - k <= 4


# pádové koncovky (bez diakritiky): „Čadc-e“, „Žilin-ou“, „Dunajsk-ej“, „Nov-ých Zámk-ov“, „Košic-iach“
_ENDINGS = ("", "a", "e", "i", "o", "u", "y", "ou", "om", "ov", "ej", "ho", "mu", "am", "ach", "och", "ech", "iam",
            "iach", "ami", "mi", "ym", "ych", "ymi", "eho", "emu", "ie", "ia", "iu", "ich", "imi", "ovi", "em")
_FLEETING = re.compile(r"^(.*[^aeiouy])(?:ie|e|o)([kcnlr])$")      # Ružomberok → Ružomberk-a, Michaloviec → Michalovc-e


def _stems(tok: str) -> set:
    out: set = set()
    if tok.endswith("ian") and len(tok) > 6:
        out.add(tok[:-3] + "an")          # 2. pád množného čísla „Kraľovian“ (Kraľovany), „Šurian“ (Šurany)
    for e in _ENDINGS:
        if not tok.endswith(e) or len(tok) - len(e) < 3:
            continue
        base = tok[:len(tok) - len(e)] if e else tok
        out.add(base)
        m = _FLEETING.match(base)
        if m:
            out.add(m.group(1) + m.group(2))
    return out


def _inflection_eq(station_tok: str, text_tok: str) -> bool:
    """Slová sa líšia len pádovou koncovkou (rovnaký kmeň)."""
    return station_tok == text_tok or bool(_stems(station_tok) & _stems(text_tok))


@lru_cache
def stations() -> Stations:
    return Stations()


# ---------------------------------------------------------------------- hraničné priechody
@dataclass(frozen=True)
class BorderName:
    code: str
    name: str                 # názov priechodu
    variant: str              # názov stanice (ako sa môže objaviť v texte)
    pattern: re.Pattern


_SKIP = ("obe stanice", "neoverene", "bez urcenej stanice")


def _variants(item: dict) -> list[str]:
    out: list[str] = []
    for key in ("sk_station", "foreign_station", "handover_station"):
        val = item.get(key) or ""
        if not val or any(s in fold(val) for s in _SKIP):
            continue
        for part in re.split(r"[/;()]", val):
            part = part.strip()
            if part and not fold(part).startswith(("pps", "v praxi")) and len(part) <= 40:
                out.append(part)
    for part in re.split(r"\s[–-]\s|[()]", item.get("name", "")):
        part = part.strip()
        if part and "peazna" not in fold(part) and "trat" not in fold(part):
            out.append(part)
    seen, uniq = set(), []
    for v in out:
        k = fold(v)
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return uniq


def _variant_pattern(variant: str) -> re.Pattern:
    words = fold(variant).replace("-", " ").split()
    if not words:
        return re.compile(r"(?!x)x")
    parts = []
    for w in words:
        if len(w) >= 5:
            parts.append(re.escape(w[: max(4, len(w) - 2)]) + r"\w{0,5}")
        elif len(words) == 1 and w[-1] in "aeiouy":
            # krátky názov so skloňovaním: Kúty → do Kút, v Kútoch, Kútami
            parts.append(re.escape(w[:-1]) + r"(?:a|e|i|o|u|y|om|ou|och|ov|ach|ami|iach)?")
        elif len(words) == 1:
            parts.append(re.escape(w) + r"(?:a|e|u|y|om|ou|och|ov|ach|ami)?")
        else:
            parts.append(re.escape(w))
    return re.compile(r"\b" + r"[\s-]+".join(parts) + r"\b")


@lru_cache
def border_names() -> tuple:
    """Názvy hraničných priechodov a staníc z data/hranice.json (na hľadanie v texte)."""
    try:
        with open(settings().borders_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return ()
    out = []
    for item in data.get("priechody", []):
        for v in _variants(item):
            out.append(BorderName(item["code"], item.get("name", v), v, _variant_pattern(v)))
    return tuple(out)


def find_borders_in_text(folded: str) -> list[tuple[BorderName, int, int]]:
    """Všetky výskyty názvov hraničných staníc v texte (pozície platia aj v origináli)."""
    hits = []
    for b in border_names():
        for m in b.pattern.finditer(folded):
            hits.append((b, m.start(), m.end()))
    hits.sort(key=lambda h: (h[1], -(h[2] - h[1])))
    return hits
