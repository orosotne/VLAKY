"""Klasifikácia a extrakcia udalostí z e-mailov (modul M5).

Dva extraktory s rovnakým rozhraním ``extract(doc: MailDocument) -> list[ExtractedItem]``
(položka sa rozbalí ako ``kind, data, confidence, notes``):

  * :class:`RuleExtractor` – deterministické pravidlá (SK/CZ, s diakritikou aj bez), istota ≤ 0,85,
  * :class:`LLMExtractor` – jazykový model so štruktúrovaným výstupom (ak je nakonfigurovaný).

:func:`combine` porovná výsledky: zhoda kľúčových polí istotu zvýši, nezhoda zníži a pridá poznámku
(udalosť potom ide na potvrdenie dispečerovi). Pri chybe LLM sa použijú len pravidlá.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

from ..config import settings
from .llm import PROMPT_VERSION, LLMError, LLMExtractor
from .pravidla import RULES_VERSION, RuleExtractor
from .stanice import stations
from .text import fold
from .udalosti import KEY_FIELDS, ExtractedItem, MailDocument

__all__ = ["Extractor", "RuleExtractor", "LLMExtractor", "LLMError", "ExtractionResult", "extract", "combine",
           "RULES_VERSION", "PROMPT_VERSION"]


class Extractor(Protocol):
    name: str

    def extract(self, doc: MailDocument) -> list[ExtractedItem]: ...


@dataclass
class ExtractionResult:
    items: list
    extractor: str = "pravidla"
    llm_used: bool = False
    llm_error: str = ""
    meta: dict = field(default_factory=dict)


_FAMILY = {"vyluka_nova": "vyluka", "vyluka_zmena": "vyluka", "vyluka_zrusenie": "vyluka",
           "pomala_jazda": "vyluka", "mimoriadnost": "vyluka", "trasa_pridelena": "trasa",
           "trasa_zmena": "trasa", "trasa_zrusena": "trasa", "avizo": "avizo", "objednavka": "objednavka",
           "ine": "ine"}
_STATION_FIELDS = {"from_station", "to_station", "origin", "destination", "border"}
_TIME_FIELDS = {"start", "end", "eta", "earliest_departure", "deadline", "accept_by"}
_EMPTY = (None, "", [])
# polia, podľa ktorých sa udalosť priraďuje k záznamu v registri (zmena/zrušenie) – tiež musia byť overené
_MATCH_FIELDS = {"vyluka_zmena": ["external_ref"], "trasa_zmena": ["external_ref"], "trasa_zrusena": ["external_ref"]}


def _checked_fields(kind: str) -> list[str]:
    return list(dict.fromkeys(KEY_FIELDS.get(kind, []) + _MATCH_FIELDS.get(kind, [])))


def _unverified_cap() -> float:
    """Istota udalosti, v ktorej sú kľúčové polia len z LLM – vždy pod prahom potvrdenia (sekcia 9.6)."""
    return round(max(0.3, settings().confirm_threshold - 0.05), 2)


def _station_key(value) -> str:  # noqa: ANN001
    if not value:
        return ""
    m = stations().match(str(value))
    return fold(m.name) if m else " ".join(fold(str(value)).split())


def _time(value) -> Optional[datetime]:  # noqa: ANN001
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _same(fieldname: str, a, b) -> bool:  # noqa: ANN001
    if fieldname in _STATION_FIELDS:
        return _station_key(a) == _station_key(b)
    if fieldname in _TIME_FIELDS:
        ta, tb = _time(a), _time(b)
        return ta is not None and tb is not None and abs((ta - tb).total_seconds()) <= 60
    if fieldname == "points":
        if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
            return False
        for pa, pb in zip(a, b):
            if _station_key(pa.get("station")) != _station_key(pb.get("station")):
                return False
            for k in ("arr", "dep"):
                if pa.get(k) and pb.get(k) and not _same("start", pa.get(k), pb.get(k)):
                    return False
        return True
    if fieldname == "train_number":
        return "".join(c for c in str(a) if c.isdigit()) == "".join(c for c in str(b) if c.isdigit())
    if fieldname == "external_ref":
        ka = "".join(c for c in fold(str(a)) if c.isalnum())
        kb = "".join(c for c in fold(str(b)) if c.isalnum())
        return bool(ka and kb) and (ka == kb or ka in kb or kb in ka)
    if fieldname in ("description", "customer", "cargo", "note"):
        return True          # voľný text sa neporovnáva
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    return fold(str(a)).strip() == fold(str(b)).strip()


def _match_score(r: ExtractedItem, lm: ExtractedItem) -> int:
    if r.kind == "ine" and "skoncenie_mimoriadnosti" in r.evidence:
        # oznámenie o skončení mimoriadnosti: mimoriadnosť či zrušenie od LLM je tá istá udalosť (ostáva „iné“ s
        # nezhodou druhu) – nesmie z nej vzniknúť nová mimoriadnosť so záznamom „navrh“ do odvolania
        return 1 if lm.kind in ("mimoriadnost", "vyluka_zrusenie") else 0
    if _FAMILY.get(r.kind) != _FAMILY.get(lm.kind):
        return 0
    fam = _FAMILY.get(r.kind)
    if fam == "vyluka":
        ra = {_station_key(r.data.get("from_station")), _station_key(r.data.get("to_station"))} - {""}
        la = {_station_key(lm.data.get("from_station")), _station_key(lm.data.get("to_station"))} - {""}
        score = len(ra & la)
        if r.data.get("start") and lm.data.get("start") and _same("start", r.data["start"], lm.data["start"]):
            score += 1
        if r.data.get("external_ref") and lm.data.get("external_ref") and \
                _same("external_ref", r.data["external_ref"], lm.data["external_ref"]):
            score += 1
        return score
    if fam in ("trasa", "avizo"):
        if r.data.get("train_number") and lm.data.get("train_number") and \
                _same("train_number", r.data["train_number"], lm.data["train_number"]):
            return 2
        return 0
    return 1


def combine(rule_items: list[ExtractedItem], llm_items: Optional[list[ExtractedItem]],
            llm_error: str = "") -> list[ExtractedItem]:
    """Spojí výsledok pravidiel a LLM. ``llm_items=None`` znamená, že LLM sa nepoužil alebo zlyhal."""
    rules = [copy.deepcopy(i) for i in rule_items]
    for it in rules:
        it.field_source = {k: "pravidla" for k, v in it.data.items() if v not in _EMPTY}
    if llm_items is None:
        for it in rules:
            it.extractor = "pravidla"
            if llm_error:
                it.notes.append(f"LLM extrakcia zlyhala – použité len pravidlá ({llm_error}).")
        return rules
    llm = [copy.deepcopy(i) for i in llm_items]
    used: set[int] = set()
    out: list[ExtractedItem] = []
    for r in rules:
        best, best_score = None, 0
        for j, lm in enumerate(llm):
            if j in used:
                continue
            sc = _match_score(r, lm)
            if sc > best_score:
                best, best_score = j, sc
        if best is None:
            r.confidence = round(max(0.3, r.confidence - 0.1), 2)
            r.notes.append("LLM túto udalosť nenašiel – overte.")
            r.extractor = "pravidla"
            out.append(r)
            continue
        used.add(best)
        lm = llm[best]
        diffs: list[str] = []
        compared = 0
        fields = list(KEY_FIELDS.get(r.kind, []))
        if r.kind in ("vyluka_nova", "vyluka_zmena", "pomala_jazda") and r.field_confidence.get("kind", 0) >= 0.8:
            fields.append("kind")
        for f in fields:
            a, b = r.data.get(f), lm.data.get(f)
            if a in _EMPTY or b in _EMPTY:
                continue
            compared += 1
            if not _same(f, a, b):
                diffs.append(f)
        if lm.kind != r.kind:
            diffs.insert(0, "druh udalosti")
        merged = dict(r.data)
        source: dict = {}
        for k in list(dict.fromkeys([*r.data, *lm.data])):
            has_r, has_l = r.data.get(k) not in _EMPTY, lm.data.get(k) not in _EMPTY
            if has_r or has_l:
                source[k] = "pravidla+llm" if has_r and has_l else ("pravidla" if has_r else "llm")
            if not has_r and has_l:
                merged[k] = lm.data[k]
        # kľúčové pole, ktoré našiel len LLM, pravidlá neoverili: nijaké zvýšenie istoty, vždy potvrdenie
        unverified = [f for f in _checked_fields(r.kind) if source.get(f) == "llm"]
        notes = r.notes + [n for n in lm.notes if n not in r.notes]
        evidence = {**lm.evidence, **r.evidence}
        if diffs:
            conf = max(0.2, min(r.confidence, lm.confidence) - 0.25)
            alt = {f: (lm.kind if f == "druh udalosti" else lm.data.get(f)) for f in diffs}
            evidence["llm_alternativa"] = alt
            for f in diffs:
                rv = r.kind if f == "druh udalosti" else r.data.get(f)
                notes.append(f"Nezhoda pravidiel a LLM v poli {f}: pravidlá = {rv!r}, LLM = {alt[f]!r} – overte.")
        elif compared >= 2 and not unverified:
            conf = min(0.97, max(r.confidence, lm.confidence) + 0.1)
            notes.append("Pravidlá aj LLM sa zhodujú v kľúčových poliach.")
        else:
            conf = max(r.confidence, lm.confidence)
        if unverified:
            conf = min(conf, _unverified_cap())
            notes.append("Kľúčové polia " + ", ".join(unverified) + " našiel len LLM, pravidlá ich neoverili – overte.")
        out.append(ExtractedItem(r.kind, merged, round(conf, 2), notes, {**lm.field_confidence, **r.field_confidence},
                                 evidence, extractor="pravidla+llm", field_source=source, unverified=unverified))
    for j, lm in enumerate(llm):
        if j in used:
            continue
        if lm.kind == "ine" and (out or rules):
            continue
        lm.field_source = {k: "llm" for k, v in lm.data.items() if v not in _EMPTY}
        lm.unverified = [f for f in _checked_fields(lm.kind) if lm.data.get(f) not in _EMPTY]
        lm.confidence = round(min(lm.confidence, 0.8, _unverified_cap() if lm.unverified else 0.8), 2)
        lm.notes.append("Udalosť našiel len LLM (pravidlá nie) – overte.")
        lm.extractor = "llm"
        out.append(lm)
    return out


def extract(doc: MailDocument, *, use_llm: bool = True, llm: Optional[LLMExtractor] = None) -> ExtractionResult:
    """Celá extrakcia jedného e-mailu: pravidlá, voliteľne LLM, porovnanie. Nikdy nevráti prázdny zoznam."""
    rule_items = RuleExtractor().extract(doc)
    llm_items: Optional[list[ExtractedItem]] = None
    err = ""
    model = ""
    llm_notes: list[str] = []
    if use_llm and LLMExtractor.enabled():
        ex = llm or LLMExtractor()
        try:
            llm_items = ex.extract(doc)
            llm_notes = list(ex.last_notes)
        except LLMError as exc:
            err = str(exc)
        except Exception as exc:  # pragma: no cover - posledná poistka
            err = f"neočakávaná chyba ({type(exc).__name__})"
        model = ex.last_model or LLMExtractor.model()
    items = combine(rule_items, llm_items, err)
    for it in items:
        it.notes += [n for n in llm_notes if n not in it.notes]
    if not items:
        items = [ExtractedItem("ine", {}, 0.3, ["E-mail nebol rozpoznaný ako prevádzková zmena – skontrolujte "
                                                 "originál."], extractor="pravidla")]
    extractor = "pravidla+llm" if llm_items is not None else "pravidla"
    meta = {"rules_version": RULES_VERSION, "prompt_version": PROMPT_VERSION if llm_items is not None else "",
            "model": model, "llm_error": err, "extractor": extractor}
    return ExtractionResult(items, extractor, llm_items is not None, err, meta)
