"""Pomocné funkcie na prácu s textom e-mailov.

Najdôležitejšia je funkcia :func:`fold` – vráti text bez diakritiky, malými písmenami a s jednotnými
pomlčkami a medzerami, ale **rovnako dlhý** ako vstup. Regulárne výrazy pravidiel tak stačí písať
bez diakritiky a nájdené pozície platia aj v pôvodnom texte (odkiaľ berieme názvy s diakritikou).
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# znaky, ktoré zjednotíme ešte pred odstránením diakritiky (1 znak → 1 znak)
_CHAR_MAP = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", "→": "-", "⇒": "-", "➡": "-",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", "　": " ", "\x0b": " ", "\x0c": " ",
    "„": '"', "“": '"', "”": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "…": ".",
}
# znaky, ktoré z textu úplne odstránime (neviditeľné; často sa nimi maskuje prompt injection)
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍‎‏⁠⁡⁢⁣﻿­\x00"))
# pomlčky, ktoré v pôvodnom texte oddeľujú úseky aj bez medzier (Kraľovany–Ružomberok)
SECTION_DASHES = "–—―−→⇒➡"


@lru_cache(maxsize=4096)
def fold_char(c: str) -> str:
    """Jeden znak → jeden znak bez diakritiky (malým písmom)."""
    if c in _CHAR_MAP:
        return _CHAR_MAP[c]
    low = c.lower()
    base = "".join(ch for ch in unicodedata.normalize("NFKD", low) if not unicodedata.combining(ch))
    if len(base) == 1:
        return base
    if len(low) == 1:
        return low
    return "?"


def clean_text(text: str | None) -> str:
    """Odstráni neviditeľné znaky a znaky NUL, zjednotí konce riadkov."""
    if not text:
        return ""
    t = text.translate(_INVISIBLE)
    return t.replace("\r\n", "\n").replace("\r", "\n")


def fold(text: str) -> str:
    """Text bez diakritiky, malými písmenami, s jednotnými pomlčkami – rovnako dlhý ako vstup."""
    return "".join(fold_char(c) for c in text)


def squash(text: str) -> str:
    """Bez diakritiky a so zlúčenými medzerami (na porovnávanie krátkych reťazcov)."""
    return " ".join(fold(clean_text(text)).split())


def one_line(text: str, limit: int = 300) -> str:
    """Skráti text na jeden riadok (pre poznámky a citácie)."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def truncate(text: str, limit: int, marker: str = "\n[… text skrátený …]") -> tuple[str, bool]:
    """Skráti text na limit znakov (na hranici riadka, ak sa dá)."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    if cut < limit * 0.8:
        cut = limit
    return text[:cut] + marker, True


_PHONE_RE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[ /.-]?\d{2,4}(?:[ /.-]?\d{2,4}){2,3}|0\d{3}[ /.-]?\d{3}[ /.-]?\d{3})(?!\w)")
_MAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def redact_personal(text: str) -> str:
    """Nahradí telefónne čísla a e-mailové adresy zástupným textom (minimalizácia údajov pre LLM)."""
    text = _MAIL_RE.sub("[e-mail]", text)
    return _PHONE_RE.sub("[telefón]", text)


def strip_punct(token: str) -> str:
    return token.strip(" \t,;:()[]{}\"'<>!?*")
