"""Spoločné typy spracovania pošty: druhy udalostí, schémy ich dát a výsledok extrakcie.

Dáta udalosti (``ExtractedEvent.data``) sú JSON slovník. Časy sú reťazce ISO 8601 v UTC
(napr. ``2026-11-12T07:00:00+00:00``) – prevedené z miestneho času v e-maile (Europe/Bratislava).
Výnimkou sú denné okná ``daily_from``/``daily_to`` („HH:MM“, miestny čas) a dátumy ``date``
(„YYYY-MM-DD“, miestny deň).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterator, Optional

KINDS = ("vyluka_nova", "vyluka_zmena", "vyluka_zrusenie", "pomala_jazda", "trasa_pridelena",
         "trasa_zmena", "trasa_zrusena", "avizo", "mimoriadnost", "objednavka", "ine")
CLOSURE_KINDS = ("vyluka_nova", "vyluka_zmena", "pomala_jazda")
PATH_KINDS = ("trasa_pridelena", "trasa_zmena")
RESTRICTION_KINDS = ("uplna", "kolaj", "trakcne_vedenie", "pomala_jazda", "obmedzenie")
# druhy, ktoré vždy potvrdzuje dispečer (sekcia M5 a pokyny vedúceho)
ALWAYS_CONFIRM = ("vyluka_zrusenie", "trasa_zrusena", "mimoriadnost")

KIND_LABELS = {
    "vyluka_nova": "nová výluka", "vyluka_zmena": "zmena výluky", "vyluka_zrusenie": "zrušenie výluky",
    "pomala_jazda": "pomalá jazda", "trasa_pridelena": "pridelenie trasy", "trasa_zmena": "zmena trasy",
    "trasa_zrusena": "zrušenie trasy", "avizo": "avízo príchodu na hranicu", "mimoriadnost": "mimoriadnosť",
    "objednavka": "objednávka", "ine": "iné (nerozpoznané)",
}

_CLOSURE_KEYS = ["from_station", "to_station", "line_ref", "track", "start", "end", "daily_from", "daily_to",
                 "kind", "speed_kmh", "external_ref", "note", "via", "until_revoked"]
# zámer zmeny výluky (``vyluka_zmena.zamer``), ako ho uvádza e-mail – podľa neho register zmenu zapíše:
#   presun      – termín sa presúva (pôvodný termín zaniká, platí nový),
#   doplnenie   – pribúda ďalší termín (pôvodný ostáva celý, „bude aj v dňoch …“),
#   predlzenie  – výluka sa predlžuje (pôvodný začiatok ostáva, nový koniec; bez začiatku = začiatok z registra),
#   skratenie   – výluka sa skracuje či končí skôr (nový koniec či začiatok vo vnútri pôvodného termínu),
#   zmena_casu  – mení sa čas (okno) výluky v uvedené dni („z 08:00 – 14:00 na 06:00 – 16:00“),
#   None        – zámer nie je známy (register postupuje konzervatívne)
CHANGE_INTENTS = ("presun", "doplnenie", "predlzenie", "skratenie", "zmena_casu")
SCHEMAS: dict[str, list[str]] = {
    "vyluka_nova": _CLOSURE_KEYS,
    "vyluka_zmena": _CLOSURE_KEYS + ["zamer"],
    "pomala_jazda": _CLOSURE_KEYS,
    # start/end – rušený termín (zrušenie len časti viacdennej výluky celú výluku nezruší); kind = pomala_jazda –
    # ruší sa pomalá jazda (POTR), nie výluka iného druhu s rovnakým číslom; partial = True – e-mail ruší len časť
    # výluky (dni v týždni, čas bez dňa, „s výnimkou …“), ale rušený termín sa nepodarilo určiť: zápis zrušenie
    # nesmie vykonať ako zrušenie celej výluky
    "vyluka_zrusenie": ["external_ref", "from_station", "to_station", "start", "end", "kind", "partial"],
    # offer/accept_by: ponuka trasy zo systému KIS (ešte nie je pridelená, treba ju akceptovať)
    "trasa_pridelena": ["train_number", "date", "kind", "points", "external_ref", "offer", "accept_by"],
    "trasa_zmena": ["train_number", "date", "kind", "points", "external_ref", "offer", "accept_by"],
    "trasa_zrusena": ["train_number", "date", "external_ref"],
    # border_code dopĺňa validácia (kód priechodu z registra M6)
    "avizo": ["train_number", "border", "eta", "date", "border_code"],
    "mimoriadnost": ["from_station", "to_station", "start", "end", "description"],
    "objednavka": ["customer", "origin", "destination", "earliest_departure", "deadline", "cargo", "wagons",
                   "weight_t", "length_m", "rid"],
    "ine": [],
}
# kľúčové polia (porovnanie pravidiel s LLM a výpočet istoty)
KEY_FIELDS: dict[str, list[str]] = {
    "vyluka_nova": ["from_station", "to_station", "start", "end"],
    "vyluka_zmena": ["from_station", "to_station", "start", "end"],
    "pomala_jazda": ["from_station", "to_station", "start", "end", "speed_kmh"],
    "vyluka_zrusenie": ["external_ref", "from_station", "to_station"],
    "trasa_pridelena": ["train_number", "date", "points"],
    "trasa_zmena": ["train_number", "date", "points"],
    "trasa_zrusena": ["train_number", "date"],
    "avizo": ["train_number", "border", "eta"],
    "mimoriadnost": ["from_station", "to_station", "start", "description"],
    "objednavka": ["customer", "origin", "destination", "earliest_departure", "deadline"],
    "ine": [],
}


def normalize_data(kind: str, data: dict | None) -> dict:
    """Doplní chýbajúce kľúče schémy (None) a vynechá neznáme kľúče."""
    data = dict(data or {})
    keys = SCHEMAS.get(kind, [])
    return {k: data.get(k) for k in keys}


@dataclass
class ExtractedItem:
    """Výsledok extrakcie jednej udalosti.

    Rozbaľuje sa ako štvorica ``kind, data, confidence, notes = item``.
    """

    kind: str
    data: dict
    confidence: float
    notes: list = field(default_factory=list)
    field_confidence: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)     # pole → citácia z e-mailu
    extractor: str = "pravidla"
    field_source: dict = field(default_factory=dict)  # pole → pravidla | llm | pravidla+llm (kto hodnotu našiel)
    unverified: list = field(default_factory=list)    # kľúčové polia, ktoré pravidlá neoverili (len z LLM)

    def __iter__(self) -> Iterator[Any]:
        return iter((self.kind, self.data, self.confidence, self.notes))


@dataclass
class MailDocument:
    """Text e-mailu pripravený na extrakciu (predmet, telo, texty príloh)."""

    subject: str = ""
    body: str = ""
    attachments: list = field(default_factory=list)   # [(názov súboru, text)]
    sender: str = ""
    sender_name: str = ""
    received_at: Optional[datetime] = None            # UTC
    # e-mail nie je prevádzkovým oznámením podľa hlavičiek (automatická odpoveď, pozvánka text/calendar) – dôvod
    auto_reason: str = ""

    def full_text(self) -> tuple[str, int]:
        """Celý text na extrakciu a pozícia konca predmetu."""
        from .text import clean_text

        subject = " ".join(clean_text(self.subject).split())
        parts = [subject, "", clean_text(self.body).strip()]
        for name, text in self.attachments:
            if text and text.strip():
                parts += ["", f"[Príloha: {name}]", clean_text(text).strip()]
        return "\n".join(parts) + "\n", len(subject)

    def ref_date(self) -> date:
        """Miestny dátum prijatia – na doplnenie roka pri dátumoch bez roka."""
        from ..cas import local, now_utc

        return local(self.received_at or now_utc()).date()
