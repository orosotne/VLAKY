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
from .parser import is_forward_subject, is_reply_subject, reply_quote_spans
from .stanice import StationMatch, find_borders_in_text, stations, tokens
from .text import SECTION_DASHES, clean_text, fold, one_line
from .udalosti import ExtractedItem, MailDocument, normalize_data

RULES_VERSION = "pravidla-2026.11"
MAX_RULE_CONFIDENCE = 0.85


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in parts))


# ------------------------------------------------------------------ klasifikácia
# predmet zrušenia je pomalá jazda / POTR / obmedzenie rýchlosti (nie výluka): „Po skončení výluky sa ruší POTR“
_SPEED_OBJ = (r"(?:pomal\w*\s+j[ai]zd\w*|potr\b|(?:docasn|prechodn)\w*\s+(?:obmedzen|omezen)\w*\s+(?:tratov\w*\s+)?"
              r"rychlost\w*|(?:obmedzen|omezen)\w*\s+(?:tratov\w*\s+)?rychlost\w*)")
_NOT_SPEED_OBJ = r"(?!\w*\s+(?:\w+\s+){0,2}?" + _SPEED_OBJ + ")"
_KIND_PATTERNS: dict[str, re.Pattern] = {
    "vyluka_zrusenie": _rx(
        r"zrusen\w*\s+(?:\w+\s+){0,2}(?:vyluk|rov\b|rozkaz\w* o vyluke)",
        r"(?:vyluk\w*|\brov\b)(?:\s+c\.)?(?:\s+[\w/.-]+)?\s+(?:sa\s+)?(?:rusi|zrusuje|nekona|nebude konat|odvolava|je zrusen|bola zrusen|sa zrusil)"
        + _NOT_SPEED_OBJ,
        r"odvolan\w*\s+(?:\w+\s+)?vyluk", r"rusime\s+(?:\w+\s+)?vyluk", r"storn\w*\s+(?:\w+\s+)?vyluk",
        r"vyluk\w*\s+(?:\w+\s+){0,3}sa\s+(?:nekona|rusi)" + _NOT_SPEED_OBJ),
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
# zrušenie pomalej jazdy / POTR: „ruší sa pomalá jazda … zavedená TLG č. 37/2026“, „Odvolávame POTR č. 12/2026“,
# „pomalá jazda … sa ruší“, „zrušenie pomalej jazdy“ (podstatné meno – nie „do odvolania pomalá jazda“, nie
# „zrušenie pomalej jazdy oznámime“); budúce „ktorá sa zruší po oprave“ nie je zrušenie
_SPEED_CANCEL = re.compile(
    r"\b(?:rusi|zrusuje|rusime|zrusujeme|rusia|odvolava|odvolavame|odvolavaju)\s+(?:sa\s+|se\s+)?(?:\w+\s+){0,2}?"
    + _SPEED_OBJ +
    r"|\b" + _SPEED_OBJ + r"(?:(?!vyluk|\brov\b)[^;]){0,160}?\s(?:sa\s+|se\s+)?(?:rusi\b|zrusuje\b|odvolava\b"
    r"|je\s+zrusen|bola\s+zrusen|bol\s+zrusen|je\s+odvolan|bola\s+odvolan|bol\s+odvolan|zrusil)"
    r"|(?P<noun>\b(?:zrusen|odvolan|storn)\w*\s+(?:\w+\s+){0,2}?" + _SPEED_OBJ + ")")
# oznámenie budúceho zrušenia („zrušenie pomalej jazdy oznámime telegramom“) – nie je to zrušenie
_FUTURE_NOTICE = re.compile(r"\b(?:oznamime|oznami|oznamia|oznamene\s+bude|bude\s+oznamen\w*|budu\s+oznamen\w*|budeme"
                            r"|zverejnime|informujeme|bude\s+nasledovat)\b")
# možné, nie oznámené zrušenie („o prípadnom zrušení výluky Vás budeme informovať“, „v prípade zrušenia výluky …“)
_HYPOTHETICAL = re.compile(r"\bpripadn\w*|\bv\s+pripade\b|\beventualn\w*|\bak\s+(?:by|sa)\b|\bmozn\w*\s+zrusen")
# veta, ktorá pomalú jazdu zavádza („zavedenie pomalej jazdy …, potom sa ruší“) – nie je zrušením
_INTRODUCES = re.compile(r"\bzavadz\w*|\bzavedeni[eu]?\b")
# slová novej výluky či pomalej jazdy – e-mail, ktorý ich má aj mimo vety o zrušení pomalej jazdy, je nová výluka
_NEW_RESTRICTION = re.compile(_KIND_PATTERNS["vyluka_nova"].pattern + "|" + _KIND_PATTERNS["pomala_jazda"].pattern)

# ------------------------------------------------------------------ druh obmedzenia
_SPEED_WORDS = re.compile(r"pomal\w*\s+j[ai]zd|\bpotr\b|(?:obmedzen|omezen|znizen|snizen)\w*\s+(?:tratov\w*\s+)?rychlost"
                          r"|rychlost\w*\s+(?:je\s+|bude\s+)?(?:obmedzen|omezen|znizen|snizen)")
_TV_WORDS = re.compile(r"vyluk\w*\s+(?:\w+\s+)?\btv\b|vypnut\w*\s+(?:\w+\s+)?(?:\btv\b|trakcn)|vypnuti\w*\s+(?:\w+\s+)?"
                       r"(?:\btv\b|trakcn)|napatov\w*\s+vyluk|napetov\w*\s+vyluk|beznapatov|vyluk\w*\s+trakcn"
                       r"|\btv\s+(?:\d\.\s?)?(?:tk|ob)|\btv\b")
# výluka koľaje (s číslom koľaje aj bez neho); „jednokoľajná prevádzka“ výlukou koľaje nie je – je to jej dôsledok
_TRACK_CLOSURE = re.compile(r"vyluk\w*\s+(?:\d\.\s?)?(?:tratov\w*\s+|stanicn\w*\s+|hlavn\w*\s+)?kol[ae]j"
                            r"|vyluk\w*\s+\d\.\s?tk\b|vyluceni\w*\s+(?:\d\.\s?)?(?:tratov\w*\s+|stanicn\w*\s+)?kol[ae]j"
                            r"|vyluk\w*\s+tk\b")
# číslo koľaje („1. TK“, „2. traťová koľaj“, „koľaj č. 3“) – vylúčená je len táto koľaj, ostatné sa dajú použiť
_TRACK_NUMBER = re.compile(r"\b\d\.\s?tk\b|\b\d\.\s?(?:tratov\w*|stanicn\w*|hlavn\w*)\s+kol[ae]j|kol[ae]j\w*\s+c\.\s?\d"
                           r"|\btk\s+c\.\s?\d|\b\d\.\s?kol[ae]j")
# doprava ide ďalej po inej koľaji (dvojkoľajná trať, vylúčená je jedna koľaj)
_SINGLE_OP = re.compile(r"jednokol[ae]jn\w*\s+(?:prevadzk|premavk|doprav|jazd)|po\s+jednej\s+kol[ae]j"
                        r"|veden\w*\s+po\s+(?:\d\.\s?|druhej\s+|susednej\s+|vedlajsej\s+|opacnej\s+)(?:tratov\w*\s+)?"
                        r"(?:kol[ae]j|tk\b)|po\s+(?:druhej|susednej|vedlajsej|opacnej)\s+(?:tratov\w*\s+)?kol[ae]j")
# jednokoľajná trať: výluka (jedinej) traťovej koľaje je výlukou celej trate
_SINGLE_LINE = re.compile(r"jednokol[ae]jn\w*\s+(?:trat|usek|zeleznicn\w*\s+trat)")
# úplná výluka: výslovne úplná, neprejazdná, doprava prerušená, vlaky nepôjdu, výluka oboch (všetkých) koľají –
# posudzuje sa PRED trakčným vedením a koľajou („úplná výluka oboch TK vrátane vypnutia TV“ je úplná výluka, nie len
# trakčné vedenie: neprejde ani motorový vlak); „napäťová výluka TV, obe koľaje“ úplnou výlukou nie je
_FULL_CLOSURE = re.compile(
    r"\bupln(?:a|ou|ej|e|u|y|ym|eho)\b"
    r"|neprejazdn|nesjizdn|nezjazdn"
    r"|(?P<flow>\b(?:doprav|prevadzk|premavk|provoz)\w*(?:\s+[^\s,;.()]+){0,4}?\s+(?:je\s+|bude\s+|su\s+|budu\s+|bola\s+"
    r"|bolo\s+|bol\s+)?(?:uplne\s+)?(?:prerusen|zastaven|vylucen))"
    r"|\bprerusen\w*\s+(?:prevadzk|doprav|premavk|provoz)|\bzastaven\w*\s+(?:prevadzk|doprav|premavk)"
    r"|(?P<trains>\b(?:vsetk\w*\s+|ziadn\w*\s+)?vlak\w*(?:\s+[^\s,;.()]+){0,4}?\s+(?:nepojdu|nepremavaj\w*"
    r"|nebudu\s+premavat|neprejdu|nebudu\s+jazdit|nebudu\s+prechadzat))"
    r"|vsetk\w*\s+(?:tratov\w*\s+|stanicn\w*\s+|hlavn\w*\s+|dopravn\w*\s+)?kol[ae]j"
    r"|vyluk\w*\s+(?:\w+\s+){0,2}?(?:celeho|celej|celu)\s+(?:\w+\s+)?(?:usek|trat|stanic|dopravn)"
    r"|vyluk\w*\s+(?:celeho\s+)?useku\b"
    r"|vyluk\w*\s+(?:(?!tv\b|trakcn|napat|napet)\w+\s+)?ob(?:e|i|och|ou|oma)\s+(?:tratov\w*\s+|stanicn\w*\s+|hlavn\w*\s+)?"
    r"(?:kol[ae]j|tk\b)"
    r"|vyluceni\w*\s+ob(?:e|i|och|ou|oma)\s+(?:tratov\w*\s+)?kol[ae]j"
    r"|vyluk\w*\s+(?:\d\.?\s?a\s?\d\.)\s?(?:tk\b|tratov\w*\s+kol[ae]j|kol[ae]j)")
_FULL_NEGATED = re.compile(r"\bne(?:bude|budu|bola|bol|bolo|je)\b|\bnie\s+(?:je|su|bude)\b|\bbez\b")
# „úplná“, „oboch koľají“ spresnené trakčným vedením („úplná výluka trakčného vedenia“, „napäťová výluka oboch TK“) –
# výluka trakčného vedenia, nie výslovne celého úseku
_TV_QUALIFIER = re.compile(r"\s+(?:(?:napatov|napetov)\w*\s+|vyluk\w*\s+|vypnut\w*\s+|vypnuti\w*\s+){0,2}"
                           r"(?:trakcn|\btv\b|napatov|napetov|beznapat)")
_TV_BEFORE = re.compile(r"(?:napatov|napetov|beznapatov|trakcn\w*|\btv)\w*\s+(?:\w+\s+)?$")
# text výslovne púšťa vlaky motorovej trakcie („Vlaky s motorovou trakciou môžu úsekom prejsť“)
_DIESEL_OK = re.compile(r"motorov\w*\s+(?:trakci\w*|vlak\w*|rusn\w*|hnacie\w*)(?:\s+(?!ne)[^\s.;]+){0,4}?\s+(?:mozu|moze|budu"
                        r"|premav\w*|prejd\w*|prechadz\w*|jazd\w*|ide|idu|pojdu)\b"
                        r"|vlak\w*\s+(?:s\s+)?(?:motorov|nezavisl)\w*\s+trakci\w*\s+(?!ne)\w+")
_DIESEL_NO = re.compile(r"motorov\w*\s+(?:trakci\w*|vlak\w*|rusn\w*)(?:\s+[^\s.;]+){0,4}?\s+(?:nemoz\w*|nesm\w*|nepojd\w*"
                        r"|neprejd\w*|nepremav\w*|nebud\w*)|aj\s+(?:pre\s+)?(?:vlak\w*\s+)?(?:s\s+)?motorov\w*")
_LIMIT_WORDS = re.compile(r"(?:obmedzen|omezen)\w*\s+(?:dlzky|delky|hmotnosti|naprav|loziskov)|zakaz\w*\s+(?:jazdy|vjazdu)")
# zmienka o výluke, ktorá ju neohlasuje („po skončení výluky platí pomalá jazda“, „počas výluky“, „z dôvodu výluky“) –
# podľa nej sa druh obmedzenia neurčuje
_CLOSURE_MENTION = re.compile(r"\b(?:po|pred|pocas|pri|pre|z\s+dovodu|kvoli|vzhladom\s+na|v\s+suvislosti\s+s"
                              r"|v\s+nadvaznosti\s+na|v\s+case|v\s+mieste|po\s+dobu|na\s+dobu|do\s+skoncenia"
                              r"|po\s+(?:skonceni|ukonceni)|pocas\s+trvania)\s+(?:\w+\s+){0,1}?vyluk\w*"
                              r"|oddeleni\w*\s+vyluk\w*|\b(?:plan|planu|plane|planom|zoznam\w*|harmonogram\w*)\s+vyluk\b")


def _full_closure(folded: str) -> bool:
    """Text výslovne hovorí o úplnej výluke (nie zápor „doprava nebude prerušená“, nie len osobné vlaky, nie „úplná
    výluka trakčného vedenia“ či „napäťová výluka oboch TK“ – tie posúdi :func:`_tv_full`)."""
    for m in _FULL_CLOSURE.finditer(folded):
        text = m.group(0)
        if (m.group("flow") or m.group("trains")) and _FULL_NEGATED.search(text):
            continue
        if text.startswith("upln") and _TV_QUALIFIER.match(folded, m.end()):
            continue                      # „úplná výluka trakčného vedenia“, „úplná napäťová výluka“
        if re.match(r"vyluk", text) and re.search(r"\bob(?:e|i|och|ou|oma)\b", text) and \
                _TV_BEFORE.search(folded, max(0, m.start() - 30), m.start()):
            continue                      # „napäťová výluka oboch TK“ – trakčné vedenie oboch koľají
        if m.group("trains") and (re.search(r"(?:osobn|regionaln|rychlik|dialkov)\w*\s*$",
                                            folded[max(0, m.start() - 25):m.start()])
                                  or re.search(r"osobn|regionaln|rychlik", text)):
            continue                      # nepremávajú len osobné vlaky – nákladné môžu ísť po druhej koľaji
        return True
    return False


def _tv_full(folded: str) -> bool:
    """„Úplná výluka trakčného vedenia“, „úplná napäťová výluka“ – úplná výluka spresnená trakčným vedením."""
    return any(_TV_QUALIFIER.match(folded, m.end()) for m in re.finditer(r"\bupln(?:a|ou|ej|e|u|y|ym|eho)\b", folded))


def restriction_kind(folded: str, track: Optional[str] = None, known_closure: bool = False) -> tuple[str, bool]:
    """Druh obmedzenia podľa slov v texte → (druh, bol_predpokladany).

    Keď si text nie je istý, platí prísnejší druh: výluka (výluka, ROV) sa nikdy nezmení na pomalú jazdu len preto,
    že text spomína aj pomalú jazdu; úplná výluka sa posudzuje pred trakčným vedením a koľajou; výluka koľaje bez
    čísla koľaje (na jednokoľajnej trati je to celá trať) je predpokladaná úplná výluka. ``track`` – číslo koľaje
    zistené inde (napr. stĺpec tabuľky); ``known_closure`` – text je riadok tabuľky výluk (slovo „výluka“ v ňom
    chýba, číslo koľaje znamená výluku koľaje)."""
    speed = bool(_SPEED_WORDS.search(folded))
    tv = bool(_TV_WORDS.search(folded))
    closure_words = bool(re.search(r"vyluk|vyluceni", folded))
    if speed and not (closure_words or tv):
        return "pomala_jazda", False
    if _full_closure(folded):
        return "uplna", False
    if tv and _tv_full(folded):
        # „úplná výluka trakčného vedenia“: neprejde ani motorový vlak? Bez výslovného „motorové vlaky môžu prejsť“
        # prísnejší prípad – predpokladaná úplná výluka (poznámka „overte druh“)
        if _DIESEL_OK.search(folded):
            return "trakcne_vedenie", False
        return "uplna", not _DIESEL_NO.search(folded)
    track_closure = bool(_TRACK_CLOSURE.search(folded))
    numbered = bool(_TRACK_NUMBER.search(folded)) or (bool(track) and str(track).strip().lower() not in ("obe", ""))
    single_op = bool(_SINGLE_OP.search(folded))
    if track_closure:
        if numbered or single_op:
            return "kolaj", False
        if _SINGLE_LINE.search(folded):
            return "uplna", False
        return "uplna", True
    if tv:
        return "trakcne_vedenie", False
    if (numbered or single_op) and (closure_words or (known_closure and not speed)):
        return "kolaj", False
    if _LIMIT_WORDS.search(folded):
        return "obmedzenie", False
    if speed and not closure_words:
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
# dokument, ktorý rušenú pomalú jazdu zaviedol: „zavedená TLG č. 37/2026“, „uvedená v POTR č. 12/2026“; dokument,
# na základe ktorého sa niečo ruší („na základe TLG č. 50/2026“, „v zmysle telegramu …“), ním nie je
_REF_TARGET_BEFORE = re.compile(r"(?:zaveden|uveden|vydan|oznamen)\w*(?:\s+(?:v|vo))?\s*\(?\s*$")
_REF_AUTHORITY_BEFORE = re.compile(r"(?:na\s+zaklade|v\s+zmysle|v\s+sulade\s+s|podla)\s*\(?\s*$")
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
# riadok s údajmi o dokumente či kontakte (vybavuje, telefón, podpis) – dátum v ňom nie je termínom výluky
_DOC_LINE = re.compile(r"\b(?:vybavuj\w*|vyhotovil\w*|vypracoval\w*|spracoval\w*|schvalil\w*|podpis\w*|telefon\w*|tel\."
                       r"|mobil\w*|e-mail|fax|kontakt\w*)")
# dátum v novom texte odpovede, ktorý nie je termínom: zmienka o oznámení, prijatí či odoslaní („ako bolo oznámené
# 30.10.2026“, „prijaté 2.11.2026“), dátum za pozdravom (podpis); termín má pri sebe slová termínu výluky
_MENTION_BEFORE = re.compile(r"\b(?:oznamen|oznamil|prijat|prijal|dorucen|odoslan|zaslan|poslan|napisal|zverejnen"
                             r"|uverejnen|informoval|vybaven|potvrden)\w*\s*(?:dna\s+|dne\s+|zo\s+dna\s+)?$")
_CLOSING = re.compile(r"\bs\s+(?:pozdravom|pozdravem|uctou|pranim)\b|\bdakujem\b|\bdekuji\b|\b(?:best|kind)\s+regards\b"
                      r"|\bregards\b|\bmit\s+freundlichen\b")
_TERM_CONTEXT = re.compile(r"vyluk|termin|presuv|presun|posuv|posun|meni|zmen|plati|nekona|kona|\bod\b|\bdo\b|\bdna\b"
                           r"|\bdne\b|v\s+case|\bnoc|\bhod\b|obmedzen|pomal\w*\s+jazd|potr\b|\brov\b")


def _reply_term(f: str, t: datumy.Temporal, pers: list[Period], floor: int) -> bool:
    """Dátum ``t`` v novom texte odpovede je termínom (patrí k obdobiu alebo je pri ňom slovo termínu) – nie dátum
    vydania, oznámenia či prijatia ani dátum v podpise."""
    if any(p.pos <= t.start < p.end_pos for p in pers):
        return True
    ls = f.rfind("\n", 0, t.start) + 1
    le = f.find("\n", t.end)
    le = len(f) if le == -1 else le
    if datumy.not_term_date(f, t, floor) or _DOC_LINE.search(f, ls, le) or \
            _MENTION_BEFORE.search(f, max(floor, t.start - 30), t.start) or _CLOSING.search(f, floor, t.start):
        return False
    return bool(_TERM_CONTEXT.search(f, ls, le))


# holé čísla dní pred dátumom, ktoré sa nepodarilo prečítať („v dňoch 3, 4 a 5.11.“, „5. a 3.11.“): aspoň dve
# čísla, alebo jedno so spojkou „a“ (nie „Rozkaz č. 5, 12.11.“)
_LOOSE_DAYS = re.compile(r"(?<![\d.,/:])(?<!c\.\s)(?<!c\.)\d{1,2}\.?(?:(?:\s*(?:,|;|\+|&|a|aj)\s*\d{1,2}\.?)+"
                         r"\s*(?:,|;|\+|&|a|aj)|\s*(?:\+|&|a|aj))\s*$")
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


def _from_iso(value: Optional[str]) -> Optional[datetime]:
    """Čas ISO z :func:`_iso` späť na datetime (UTC); neplatný → None."""
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


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


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """Text s medzerami namiesto znakov v ``spans`` (dĺžka a konce riadkov ostávajú)."""
    chars = list(text)
    for s, e in spans:
        for i in range(max(0, s), min(len(chars), e)):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


# koniec vety: „.“, „!“, „?“ pred veľkým písmenom – nie za skratkou („v žst. Kraľovany“, „č. 5“); nový riadok len
# vtedy, keď riadok končí interpunkciou, ďalší riadok začína veľkým písmenom či odrážkou alebo je prázdny (zalomený
# riadok pokračuje tou istou vetou)
_SENTENCE_END = re.compile(r"\n|[.!?](?=[ \t]+\S)")
_ABBREV_END = re.compile(r"(?:^|[^a-z])(?:zst|st|c|tel|napr|resp|hod|tzv|cca|ing|mgr|odb|vyh|zast|zel|ul|min|max|km)$")
_ORDINAL_END = re.compile(r"(?<![\d.])\d{1,2}\.$")
_ORDINAL_NEXT = re.compile(r"[ \t]+(?:tk|tv|sk|kolaj\w*|tratov\w*|stanicn\w*|hlavn\w*)\b")


def _sentence_spans(text: str, f: str, start: int, end: int) -> list[tuple[int, int]]:
    """Vety v ``text[start:end]`` (``f`` je zložený text rovnakej dĺžky) ako (začiatok, koniec)."""
    out, s = [], start
    for m in _SENTENCE_END.finditer(f, start, end):
        nxt = text[m.end():end].lstrip(" \t")[:1]
        if m.group(0) != "\n":
            if not nxt.isupper() or _ABBREV_END.search(f[max(start, m.start() - 6):m.start()]):
                continue
            if _ORDINAL_END.search(f, max(start, m.start() - 3), m.start() + 1) and \
                    _ORDINAL_NEXT.match(f, m.end()):
                continue                  # „výluka 1. TK …“, „2. TV“ – radová číslovka, nie koniec vety
        elif nxt and nxt != "\n" and not nxt.isupper() and nxt not in "-•*>" and \
                not f[s:m.start()].rstrip().endswith((".", "!", "?", ":", ";")) and f[s:m.start()].strip():
            continue
        if f[s:m.start()].strip():
            out.append((s, m.start() + (0 if m.group(0) == "\n" else 1)))
        s = m.end()
    if f[s:end].strip():
        out.append((s, end))
    return out


def _clause_span(f: str, sentence: tuple[int, int], pos: int) -> tuple[int, int]:
    """Časť vety ``sentence`` medzi čiarkami či bodkočiarkami, v ktorej je pozícia ``pos``."""
    s, e = sentence
    a = max(f.rfind(",", s, pos), f.rfind(";", s, pos))
    b = min([x for x in (f.find(",", pos, e), f.find(";", pos, e)) if x != -1] or [e])
    return (a + 1 if a != -1 else s), b


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
    if "zle_poradie" in p.flags:
        notes.append("Dni v zozname nie sú v chronologickom poradí – rok je doplnený podľa ďalšieho dátumu, "
                     "overte termín.")
    return notes


def _confidence(found: int, required: int, penalties: float = 0.0) -> float:
    frac = found / required if required else 0.0
    return round(max(0.3, min(MAX_RULE_CONFIDENCE, 0.5 + 0.35 * frac - penalties)), 2)


# ------------------------------------------------------------------ kontext jedného e-mailu
# blok hlavičiek vloženej (preposlanej či citovanej) správy: „From:/Od: …“, „Subject:/Predmet: …“
_MAIL_HEAD = re.compile(r"^[ \t>*]*(from|od|von|to|komu|pre|an|date|datum|sent|odoslane|odeslano|gesendet|subject"
                        r"|predmet|betreff|cc|kopia|kopie|message-id)[ \t]*:[ \t]*([^\n]*)$", re.M)
_ATTACHMENT_HEAD = re.compile(r"^\[Príloha: [^\n]*\]$", re.M)
# podpis za pozdravom („S pozdravom\nOR Trnava, oddelenie výluk“) – do druhu e-mailu ani obmedzenia sa nepočíta
_CLOSING_LINE = re.compile(r"^[ \t]*(?:s\s+(?:pozdravom|pozdravem|uctou|pranim\b[^\n]*|priatelskym\s+pozdravom)"
                           r"|best\s+regards|kind\s+regards|regards|mit\s+freundlichen\s+grussen)[ \t,.!]*$", re.M)
_SIG_STOP = re.compile(r"^[ \t]*(?:-{3,}|_{5,}|>|(?:from|od|sent|odoslane|subject|predmet)[ \t]*:|\[priloha:)", re.M)


def _signature_spans(f: str, start: int, end: int) -> list[tuple[int, int]]:
    """Podpis za pozdravom: od riadku „S pozdravom“ po prázdny riadok (najviac 8 riadkov), citáciu či prílohu."""
    out = []
    for m in _CLOSING_LINE.finditer(f, start, end):
        stop = f.find("\n\n", m.end(), end)
        stop = end if stop == -1 else stop
        s2 = _SIG_STOP.search(f, m.end() + 1, stop)
        if s2:
            stop = s2.start()
        lines = f.count("\n", m.start(), stop)
        if lines > 8:
            stop = m.start() + len("\n".join(f[m.start():stop].split("\n")[:9]))
        out.append((m.start(), stop))
    return out


class _Ctx:
    def __init__(self, doc: MailDocument) -> None:
        self.doc = doc
        self.text, self.subject_end = doc.full_text()
        self.f = fold(self.text)
        self.subject_f = self.f[: self.subject_end]
        self.ref: date = doc.ref_date()
        self.st = stations()
        self._sec_cache: dict = {}
        self.body_start = self.subject_end + 2
        self.body_end = min(len(self.f), self.body_start + len(clean_text(doc.body).strip()))
        self.attachments = self._attachment_parts()
        # odpoveď s citovanou pôvodnou správou: termíny (dátumy, časy) sa berú len z nového textu – citácia ostáva
        # len súvislosťou (úsek, číslo výluky); ``fq`` je text s vymazanou citáciou
        self.quotes: list[tuple[int, int]] = []
        self.quoted_terms = False
        self.quoted_attachment = False
        self.fq = self._without_quotes(doc)
        self.toks = datumy.scan(self.fq, self.ref)
        self.periods = datumy.periods(self.fq, self.toks)
        self.blocks = self._blocks()
        self.tables = self._tables()
        # text na určenie druhu e-mailu a obmedzenia – bez podpisu („OR Trnava, oddelenie výluk“)
        self.sig = _signature_spans(self.f, self.body_start, self.body_end)
        self.fc = _blank(self.f, self.sig) if self.sig else self.f
        # vety, ktoré rušia pomalú jazdu či POTR (určí klasifikácia); ``speed_cancel`` – celý e-mail je takéto zrušenie
        self.cancels: list[tuple[int, int]] = []
        self.speed_cancel = False
        # vety, ktoré rušia výluku (zrušenie napísané len v texte: „ROV č. … sa ruší“, „výluka … sa nekoná“)
        self.closure_cancels: list[tuple[int, int]] = []

    def _attachment_parts(self) -> list[tuple[int, int]]:
        """Texty príloh v celom texte (za riadkom „[Príloha: …]“) ako (začiatok, koniec)."""
        heads = list(_ATTACHMENT_HEAD.finditer(self.text, self.body_end))
        out = []
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(self.text)
            out.append((m.end() + 1, max(m.end() + 1, end)))
        return out

    def _embedded(self) -> list[tuple[str, int, int]]:
        """Vložené správy (preposlaná správa v texte, priložený e-mail): (predmet, začiatok tela, koniec časti)."""
        parts = [(self.body_start, self.body_end)] + self.attachments
        out = []
        for ps, pe in parts:
            heads = list(_MAIL_HEAD.finditer(self.f, ps, pe))
            i = 0
            while i < len(heads):
                run = [heads[i]]
                while i + 1 < len(heads) and heads[i + 1].start() == run[-1].end() + 1:
                    i += 1
                    run.append(heads[i])
                i += 1
                names = {h.group(1) for h in run}
                subj = next((h for h in run if h.group(1) in ("subject", "predmet", "betreff")), None)
                if len(run) < 2 or subj is None or not names & {"from", "od", "von"}:
                    continue
                subject = self.text[subj.start(2):subj.end(2)].strip()
                out.append((subject, run[-1].end() + 1, pe))
        return out

    def _identity(self, start: int, end: int) -> tuple[set, set]:
        """Čísla výluk a úseky v časti textu – podľa nich sa pozná, že príloha opakuje tú istú výluku."""
        refs = set()
        for pat in _CLOSURE_REF_RES:
            for m in pat.finditer(self.f, start, end):
                refs.add(re.sub(r"[^0-9a-z/]", "", m.group(1)).strip("/"))
        secs = {frozenset((s.a.name, s.b.name)) for s in self.sections(start, end)}
        return refs - {""}, secs

    def _term_positions(self, blanked: str, ranges: list[tuple[int, int]]) -> list[int]:
        """Pozície dátumov vlastného termínu v nových (necitovaných) častiach ``ranges`` textu ``blanked``."""
        toks = datumy.scan(blanked, self.ref)
        pers = datumy.periods(blanked, toks)
        return [t.start for t in toks if t.kind in ("date", "datetime") and any(s <= t.start < e for s, e in ranges)
                and not any(p.pos <= t.start < p.end_pos and _OLD_PERIOD.search(blanked, max(0, p.pos - 60), p.pos)
                            for p in pers)
                and _reply_term(blanked, t, pers, next(s for s, e in ranges if s <= t.start < e))]

    def _has_term(self, blanked: str, ranges: list[tuple[int, int]]) -> bool:
        """V nových (necitovaných) častiach ``ranges`` textu ``blanked`` je vlastný termín."""
        return bool(self._term_positions(blanked, ranges))

    def _without_quotes(self, doc: MailDocument) -> str:
        """Text bez citovanej pôvodnej správy (pozície ostávajú), ak má nový text odpovede vlastný termín; inak
        celý text (citácia je jediný zdroj termínu, napr. „Potvrdzujeme.“ nad citovaným ROV). Dátum, ktorý termínom
        nie je („ako bolo oznámené 30.10.2026“, „zo dňa 2.11.2026“, „prijaté 2.11.2026“, dátum v podpise), citáciu
        nevymaže.

        Citácia sa hľadá aj vo vloženej správe – preposlaná odpoveď („FW: RE: …“ v texte alebo ako priložený e-mail)
        má citovaný pôvodný ROV so starým termínom. Odpoveď či preposlanie, ktoré majú vlastný termín, a príloha, ktorá
        opakuje tú istú výluku (rovnaké číslo či úsek – napr. „V prílohe pôvodný ROV“): príloha je len súvislosťou,
        platí novší termín z textu správy."""
        groups: list[tuple[list, list]] = []      # (citované úseky, nový text tej istej správy)
        own = reply_quote_spans(doc.subject, self.f, self.body_start, self.body_end)
        if own:
            groups.append((own, [(self.body_start, self.body_end)]))
        for subject, start, end in self._embedded():
            if is_reply_subject(subject):
                spans = reply_quote_spans(subject, self.f, start, end)
                if spans:
                    groups.append((spans, [(start, end)]))
        accepted: list[tuple[int, int]] = []
        for spans, ranges in groups:
            blanked = _blank(self.f, spans)
            if self._has_term(blanked, ranges):
                accepted += spans
        blanked = _blank(self.f, accepted) if accepted else self.f
        term_pos = self._term_positions(blanked, [(self.body_start, self.body_end)]) if self.attachments and (
            is_reply_subject(doc.subject) or is_forward_subject(doc.subject)) else []
        if term_pos:
            # o ktorej výluke správa hovorí: predmet a vety s jej vlastným termínom (nie „V prílohe aj nový ROV č. …“)
            refs, secs = self._identity(0, self.subject_end)
            for s, e in _sentence_spans(self.text, blanked, self.body_start, self.body_end):
                if any(s <= p < e for p in term_pos):
                    r, sc = self._identity(s, e)
                    refs, secs = refs | r, secs | sc
            if not (refs or secs):
                refs, secs = self._identity(0, self.body_end)
            for s, e in self.attachments:
                if any(hs <= s < he for hs, he in accepted):
                    continue
                a_refs, a_secs = self._identity(s, e)
                # čísla výluk rozhodujú, keď ich majú obe strany (iný ROV na tom istom úseku nie je tá istá výluka)
                if (refs & a_refs) if (refs and a_refs) else (secs & a_secs):
                    accepted.append((s, e))
                    self.quoted_attachment = True
            blanked = _blank(self.f, accepted) if accepted else self.f
        if not accepted:
            return self.f
        self.quotes = accepted
        self.quoted_terms = any(any(s <= t.start < e for s, e in accepted) for t in datumy.scan(self.f, self.ref))
        if is_reply_subject(doc.subject):
            blanked = _blank(blanked, [(0, self.subject_end)])    # predmet „RE:“ je predmet pôvodnej správy
        return blanked

    def in_quote(self, pos: int) -> bool:
        return any(s <= pos < e for s, e in self.quotes)

    def sentences(self) -> list[tuple[int, int]]:
        """Vety tela a príloh (bez predmetu) – raz pre e-mail."""
        if getattr(self, "_sentences", None) is None:
            self._sentences = _sentence_spans(self.text, self.fc, self.subject_end, len(self.text))
        return self._sentences

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

        blank = 0
        for s, e in _lines(self.text, self.subject_end + 1):
            line = self.text[s:e]
            if self.in_quote(s):          # tabuľka v citovanej pôvodnej správe nie je nový termín
                flush()
                continue
            if not line.strip() and run and blank == 0:
                blank = 1                 # tabuľka z HTML: medzi riadkami tabuľky je prázdny riadok
                continue
            after_blank, blank = blank, 0
            if line.count("\t") >= 2:
                cells = [c.strip() for c in line.split("\t")]
            elif line.count(";") >= 2 and len(line) < 400:
                cells = [c.strip() for c in line.split(";")]
            elif line.count("|") >= 2:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
            else:
                flush()
                continue
            if after_blank and self._header_like(cells):
                flush()                   # za prázdnym riadkom ďalšia tabuľka s vlastnou hlavičkou
            run.append((s, e, cells))
        flush()
        return tables

    _HEADER_WORDS = re.compile(r"usek|stanic|dopravn|zaciatok|koniec|^od\b|^do\b|trat|kol[ae]j|druh|typ|prichod|"
                               r"odchod|prijezd|odjezd|prich|odch|vlak|\beta\b|poznam|cislo|rychlost|datum|^cas|"
                               r"platnost|termin|dovod|hranic|priechod|pps")

    def _header_like(self, cells: list) -> bool:
        """Riadok vyzerá ako hlavička tabuľky (aspoň dva názvy stĺpcov, žiadny dátum ani čas)."""
        folded = [" ".join(fold(c).split()) for c in cells]
        hits = sum(1 for c in folded if c and self._HEADER_WORDS.search(c))
        return hits >= 2 and not any(datumy.scan(c, self.ref) for c in folded if c)

    def _make_table(self, run: list) -> Optional[Table]:
        for hi, (s, e, cells) in enumerate(run):
            folded = [" ".join(fold(c).split()) for c in cells]
            if self._header_like(cells):
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
    def first_group(self, patterns: list[re.Pattern], start: int, end: int,
                    skip: list[tuple[int, int]] = ()) -> Optional[str]:
        """Prvá hodnota skupiny 1 niektorého vzoru v ``[start, end)`` (mimo úsekov ``skip``)."""
        best = None
        for pat in patterns:
            for m in pat.finditer(self.f, start, end):
                if any(s <= m.start() < e for s, e in skip):
                    continue
                if best is None or m.start(1) < best[0]:
                    best = (m.start(1), m.end(1))
                break
        if not best:
            return None
        return self.text[best[0]:best[1]].rstrip(".,;)")

    def cancel_ref(self, spans: list[tuple[int, int]], skip: list[tuple[int, int]] = ()) -> Optional[str]:
        """Číslo rušenej výluky: ROV (v rušiacej vete, potom kdekoľvek) pred inými číslami – telegram, ktorým sa
        výluka ruší („TLG č. 61/2026 – výluka … podľa ROV č. 1330/2026 … sa nekoná“), rušenou výlukou nie je."""
        for pats in (_CLOSURE_REF_RES[:1], _CLOSURE_REF_RES):
            for s, e in spans:
                ref = self.first_group(pats, s, e, skip)
                if ref:
                    return ref
        return None

    def speed_ref(self, start: int, end: int) -> Optional[str]:
        """Číslo rušenej pomalej jazdy: dokument, ktorý ju zaviedol („zavedená TLG č. 37/2026“), alebo jej vlastné
        číslo („POTR č. 12/2026“, „č. pomalej jazdy 5/2026“) – nie číslo telegramu, ktorý ju ruší, ani dokumentu,
        na základe ktorého sa ruší („na základe TLG č. 50/2026“)."""
        best = None
        for i, pat in enumerate(_CLOSURE_REF_RES):
            for m in pat.finditer(self.f, start, end):
                before = self.f[max(start, m.start() - 30):m.start()]
                if _REF_AUTHORITY_BEFORE.search(before):
                    continue
                attached = bool(_REF_TARGET_BEFORE.search(before))
                own = i == 4 or (i == 2 and bool(re.search(r"potr|pomal|obmedzen", m.group(0))))
                if attached or own:
                    key = (0 if attached else 1, m.start(1))
                    if best is None or key < best[0]:
                        best = (key, m.start(1), m.end(1))
        return self.text[best[1]:best[2]].rstrip(".,;)") if best else None

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

    def speed(self, start: int, end: int, f: Optional[str] = None) -> Optional[int]:
        for m in _SPEED_RE.finditer(f or self.f, start, end):
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

    def block_of(self, pos: int) -> Optional[tuple[int, int]]:
        for bs, be in self.blocks:
            if bs <= pos <= be:
                return bs, be
        return None

    def unused_times(self, start: int, end: int) -> list[str]:
        """Časy v časti textu, ktoré nepatria k žiadnemu nájdenému obdobiu (napr. ďalšie okno, ktoré sa nespracovalo)."""
        spans = [(p.pos, p.end_pos) for p in self.periods]
        out = []
        for t in self.toks:
            if t.kind not in ("time", "datetime") or not (start <= t.start < end):
                continue
            if not any(a <= t.start and t.end <= b for a, b in spans):
                out.append(one_line(self.text[t.start:t.end], 40))
        return out

    def unused_dates(self, start: int, end: int) -> list[str]:
        """Dátumy v časti textu, ktoré nepatria k žiadnemu obdobiu (napr. ďalší deň zoznamu, ktorý sa nespracoval),
        aj holé čísla dní pred dátumom, ktoré sa nepodarilo prečítať („v dňoch 3, 4 a 5.11.“).

        Nepočíta pôvodný deň presunu („z 12.11. na 13.11.“, „namiesto 12.11.“), dátum vydania, vyhotovenia či
        rozhodnutia („zo dňa 2.11.“) ani dátum v riadku s údajmi o dokumente („Vybavuje: …, tel. …, 30.10.2026“)."""
        spans = [(p.pos, p.end_pos) for p in self.periods]
        out = []
        for t in self.toks:
            if t.kind not in ("date", "datetime") or not (start <= t.start < end):
                continue
            loose = _LOOSE_DAYS.search(self.f, max(start, t.start - 40), t.start)
            if loose and not any(u.start < t.start and u.end > loose.start() for u in self.toks if u is not t):
                out.append(one_line(self.text[loose.start():t.start] + self.text[t.start:t.end].split()[0], 40))
                continue
            if t.kind != "date" or any(a <= t.start and t.end <= b for a, b in spans):
                continue
            ls = self.f.rfind("\n", 0, t.start) + 1
            le = self.f.find("\n", t.end)
            if datumy.not_term_date(self.f, t, start) or _DOC_LINE.search(self.f, ls, le if le != -1 else end):
                continue
            out.append(one_line(self.text[t.start:t.end], 40))
        return out

    def is_old_period(self, p: Period) -> bool:
        """Obdobie označené ako pôvodné („Pôvodný termín: …“) – pri zmene neplatí."""
        ls = self.f.rfind("\n", 0, p.pos) + 1
        return bool(_OLD_PERIOD.search(self.f, max(ls, p.pos - 60), p.pos))


# ------------------------------------------------------------------ e-maily, ktoré nie sú prevádzkovým oznámením
# automatická odpoveď (neprítomnosť, doručenie, prečítanie) a pozvánka na poradu či stretnutie: spomínajú výluky,
# úseky aj dátumy, ale nič neohlasujú – nesmú z nich vzniknúť výluky (predpokladaná úplná výluka by zastavila vlaky)
_AUTO_SUBJECT = re.compile(r"\s*(?:\[[^\]]{0,40}\]\s*)?(?:automaticka\s+odpoved|automatic\s+reply|automatische\s+antwort"
                           r"|auto[ -]?reply|autoreply|auto:|out\s+of\s+office|mimo\s+kancelar|nepritomnost"
                           r"|abwesenheit|precitane:|precteno:|read:|gelesen:|nedorucitel|undeliverable"
                           r"|delivery\s+status\s+notification|mail\s+delivery\s+failed|returned\s+mail)")
_INVITE_SUBJECT = re.compile(r"\s*(?:\[[^\]]{0,40}\]\s*)?(?:pozvank\w*|pozvanie\b|invitation\b|einladung\b"
                             r"|updated\s+invitation|aktualizovan\w*\s+pozvank\w*|(?:prijate|odmietnute|predbezne\s+prijate"
                             r"|accepted|declined|tentative|zrusene|canceled|cancelled)\s*:\s*(?:pozvank|stretnut|porad"
                             r"|meeting|invitation))")


# hromadný informačný e-mail (newsletter, spravodaj, tlačová správa) – spomína výluky, ale dopravcovi nič neohlasuje
_BULK_SUBJECT = re.compile(r"\b(?:newsletter\w*|spravodaj\w*|zpravodaj\w*|bulletin\w*)\b"
                           r"|^\s*(?:\[[^\]]{0,40}\]\s*)?tlacov\w*\s+(?:sprav|zprav|informaci)")


def non_operational(doc: MailDocument) -> str:
    """Prečo e-mail nie je prevádzkovým oznámením (automatická odpoveď, pozvánka, newsletter) – „“ = je (alebo môže
    byť). Posudzuje sa len podľa hlavičiek, príloh a predmetu – nie podľa tela (prevádzkové oznámenie môže mať v päte
    napr. odkaz na odhlásenie z rozosielania)."""
    if doc.auto_reason:
        return doc.auto_reason
    subject = fold(doc.subject or "")
    if _AUTO_SUBJECT.match(subject):
        return "automatická odpoveď (predmet)"
    if _INVITE_SUBJECT.match(subject):
        return "pozvánka na poradu či stretnutie (predmet)"
    if _BULK_SUBJECT.search(subject) and not is_reply_subject(doc.subject) and not is_forward_subject(doc.subject):
        return "hromadný informačný e-mail – newsletter (predmet)"
    for name, text in doc.attachments:
        if str(name or "").lower().endswith(".ics") or str(text or "").lstrip().upper().startswith("BEGIN:VCALENDAR"):
            return "pozvánka do kalendára (príloha .ics)"
    return ""


def non_operational_item(reason: str) -> ExtractedItem:
    return ExtractedItem("ine", {}, 0.3, [f"Nie je to prevádzkové oznámenie ({reason}) – udalosti (výluky, trasy) sa "
                                          "z neho neextrahujú. Ak predsa obsahuje zmenu, zapíšte ju ručne."],
                         evidence={"neprevadzkovy_email": reason})


# ------------------------------------------------------------------ extraktor
class RuleExtractor:
    """Extrakcia pravidlami (bez LLM)."""

    name = "pravidla"
    version = RULES_VERSION

    def extract(self, doc: MailDocument) -> list[ExtractedItem]:
        reason = non_operational(doc)
        if reason:
            return [non_operational_item(reason)]
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
            if c.quoted_attachment:
                it.notes.append("Príloha opakuje tú istú výluku (pôvodný dokument) – platí novší termín z textu "
                                "správy; príloha slúži len na úsek a číslo výluky.")
            elif c.quoted_terms:
                it.notes.append("Odpoveď s citovanou pôvodnou správou – termín je len z nového textu; citácia slúži "
                                "len na úsek a číslo výluky.")
            it.data = normalize_data(it.kind, it.data)
        return items

    # -------------------------------------------------------------- klasifikácia
    def classify(self, c: _Ctx) -> tuple[Optional[str], Optional[str], dict]:
        scores: dict[str, int] = {}
        body = c.fc[c.subject_end:]              # bez podpisu („OR Trnava, oddelenie výluk“)
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
            # vety, ktoré rušia pomalú jazdu (POTR) či výluku – aj pri zmene či zrušení podľa predmetu: do druhu,
            # rýchlosti ani čísla zmenenej výluky sa nerátajú („Zmena ROV …, zároveň sa ruší pomalá jazda TLG …“)
            c.cancels = [] if c.tables else _speed_cancels(c)
            c.closure_cancels = [] if c.tables else _closure_cancels(c)
            subj = {k for k in kinds if _KIND_PATTERNS[k].search(c.subject_f)}
            for k in ("vyluka_zrusenie", "vyluka_zmena"):
                if k in subj:
                    return best_fam, k, scores
            # zrušenie pomalej jazdy / POTR je zrušením celého e-mailu, len ak e-mail inde neohlasuje novú výluku
            # či pomalú jazdu s termínom – inak by sa nová výluka stratila (zrušenie je potom ďalšia udalosť)
            sole = bool(c.cancels) and not _new_restriction_outside(c, c.cancels)
            if sole and any(s < c.subject_end for s, _ in c.cancels):
                return best_fam, self._speed_cancel_kind(c, scores), scores
            if not c.tables:   # súborný prehľad (tabuľka) môže spomínať zmeny aj zrušenia – berie sa ako nové
                body_kind = self._body_change_kind(c, scores)
                if body_kind:
                    return best_fam, body_kind, scores
            # zrušenie výluky napísané len vo vete („ROV č. … (úsek, dátum) sa ruší“, „výluka … sa nekoná“, „…
            # odpadá“) – zrušením celého e-mailu je, ak e-mail inde neohlasuje novú výluku s termínom (inak je to
            # ďalšia udalosť); vety o dňoch, ktoré ostávajú v platnosti, novou výlukou nie sú
            if c.closure_cancels:
                keep = _remaining_sentences(c)
                if not _new_restriction_outside(c, c.closure_cancels + c.cancels + keep):
                    scores["vyluka_zrusenie"] = max(scores.get("vyluka_zrusenie", 0), 1)
                    return best_fam, "vyluka_zrusenie", scores
            if sole:
                return best_fam, self._speed_cancel_kind(c, scores), scores
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

    @staticmethod
    def _body_change_kind(c: _Ctx, scores: dict) -> Optional[str]:
        """Zrušenie či zmena výluky podľa tela e-mailu (predmet o nich nehovorí) – None = nová výluka či pomalá jazda.

        Zmienka o možnom či budúcom zrušení („o prípadnom zrušení výluky Vás budeme informovať“) zrušením nie je. Veta,
        ktorá výluku ruší a zároveň nahrádza či presúva („ROV … sa ruší a nahrádza ho ROV …“), je zmena. Keď e-mail
        mimo viet o zrušení ohlasuje novú výluku s termínom („Výluka podľa ROV … sa nekoná. Nová výluka …“), je to
        nová výluka a zrušenie je ďalšia udalosť – inak by sa nová výluka stratila (prísnejší prípad)."""
        sentences = c.sentences()

        def sentence(pos: int) -> tuple[int, int]:
            return next(((s, e) for s, e in sentences if s <= pos < e), (pos, pos + 1))

        def hits(kind: str) -> list[tuple[int, int]]:
            out = []
            for m in _KIND_PATTERNS[kind].finditer(c.fc, c.subject_end):
                s, e = sentence(m.start())
                if not (_FUTURE_NOTICE.search(c.fc, s, e) or _HYPOTHETICAL.search(c.fc, s, e)):
                    out.append((s, e))
            return out

        cancel, change = hits("vyluka_zrusenie"), hits("vyluka_zmena")
        if cancel:
            if any(_change_words(c.fc[s:e]) for s, e in cancel) or any(x not in cancel for x in change):
                return "vyluka_zmena"
            if all(x in c.closure_cancels for x in cancel) and _new_restriction_outside(
                    c, c.closure_cancels + c.cancels + _remaining_sentences(c)):
                return "vyluka_nova"
            return "vyluka_zrusenie"
        return "vyluka_zmena" if change else None

    @staticmethod
    def _speed_cancel_kind(c: _Ctx, scores: dict) -> str:
        """E-mail je zrušením pomalej jazdy či POTR (číslo rušeného dokumentu hľadá :meth:`_Ctx.speed_ref`)."""
        c.speed_cancel = True
        in_subject = sum(1 for s, _ in c.cancels if s < c.subject_end)
        scores["vyluka_zrusenie"] = max(scores.get("vyluka_zrusenie", 0),
                                        3 * in_subject + min(3, len(c.cancels) - in_subject))
        return "vyluka_zrusenie"

    # -------------------------------------------------------------- výluky a pomalé jazdy
    def _closures(self, c: _Ctx, kind: str) -> list[ExtractedItem]:
        items: list[ExtractedItem] = []
        if kind == "vyluka_zrusenie" and not c.speed_cancel and not c.tables and len(c.closure_cancels) > 1:
            # zrušenie viacerých výluk v jednom e-maile („Výluka ROV … sa ruší. Výluka ROV … sa nekoná.“) – každá veta
            # je samostatné zrušenie, nie len prvá
            multi = self._cancel_items(c)
            if len(multi) > 1:
                return multi + (self._secondary_speed_cancels(c, multi) if c.cancels else [])
        for t in c.tables:
            items += self._closures_from_table(c, t, kind)
        events, leftover = self._pair_sections(c, kind, fallback=not items)
        if items:   # tabuľka (SROV) – z textu len udalosti s vlastným úsekom aj obdobím
            events = [e for e in events if e[0] is not None and e[1] is not None]
        single = len(events) == 1 and not items
        for sec, per, span in events:
            items.append(self._closure_item(c, kind, sec, per, span, single))
        if not items:
            items.append(self._closure_item(c, kind, None, c.periods[0] if c.periods else None,
                                            (0, len(c.text)), True))
        items = _dedupe(items)
        if kind != "vyluka_zrusenie" and not c.tables:     # súborný prehľad (tabuľka) sa číta po riadkoch
            self._unassigned_notes(c, items, leftover)
        for it in items:
            it.evidence.pop("_pos", None)
            it.evidence.pop("_ppos", None)
        extra: list[ExtractedItem] = []
        if c.cancels and not c.speed_cancel:
            extra += self._secondary_speed_cancels(c, items)
        if kind != "vyluka_zrusenie" and c.closure_cancels:
            extra += self._secondary_closure_cancels(c, items, notes_only=kind == "vyluka_zmena")
        return items + extra

    def _cancel_items(self, c: _Ctx) -> list[ExtractedItem]:
        """Zrušenie po vetách: každá veta s číslom či úsekom rušenej výluky je samostatné zrušenie."""
        out: list[ExtractedItem] = []
        for s, e in c.closure_cancels:
            ref = c.cancel_ref([(s, e)], c.cancels)
            secs = [x for x in c.sections(s, e) if not x.line_desc]
            if not ref and not secs:
                continue
            data, notes = self._cancel_term(c, s, e, {"external_ref": ref,
                                                      "from_station": secs[0].a.text if secs else None,
                                                      "to_station": secs[0].b.text if secs else None})
            found = (2 if ref else 1) + (1 if data.get("start") else 0)
            out.append(ExtractedItem("vyluka_zrusenie", data, _confidence(min(found, 2), 2), notes,
                                     {"external_ref": 0.8 if ref else 0.0}, {"zrusenie": one_line(c.text[s:e], 160)}))
        return _merge_cancels(_dedupe(out))

    def _unassigned_notes(self, c: _Ctx, items: list[ExtractedItem], leftover: list[str]) -> None:
        """Nič sa nestratí potichu: termín či úsek, ktorý sa nepodarilo priradiť, a pomalá jazda, ktorá nie je
        samostatnou udalosťou („po výluke platí do odvolania pomalá jazda 50 km/h“), sú v poznámke a znížia istotu."""
        common: list[str] = []
        if leftover:
            common.append("V e-maile je aj " + ", ".join(leftover[:4]) + ", čo sa nepodarilo priradiť k výluke – overte "
                          "termín a úsek (môže ísť o ďalšiu výluku).")

        def is_speed(it: ExtractedItem) -> bool:
            return it.kind == "pomala_jazda" or it.data.get("kind") == "pomala_jazda"

        # pozície úseku aj termínu pomalých jázd, ktoré sú samostatnou udalosťou (pri zozname je úsek v úvode)
        speed_pos = [it.evidence.get(k) for it in items if is_speed(it) for k in ("_pos", "_ppos")
                     if it.evidence.get(k) is not None]
        speed_notes: list[str] = []
        if any(not is_speed(it) for it in items):
            for s, e in _speed_clauses(c):
                if any(s <= pos < e for pos in speed_pos):
                    continue
                speed_notes.append(f"V e-maile je aj pomalá jazda („{one_line(c.text[s:e], 140)}“) – ako samostatná "
                                   "udalosť sa nezapísala; ak platí, zapíšte ju v registri výluk.")
        for it in items:
            if it.kind == "vyluka_zrusenie":
                continue
            add = common + ([] if is_speed(it) else speed_notes)
            add = [n for n in add if n not in it.notes]
            if add:
                it.notes += add
                it.confidence = round(max(0.3, it.confidence - 0.05 * len(add)), 2)

    def _secondary_speed_cancels(self, c: _Ctx, items: list[ExtractedItem]) -> list[ExtractedItem]:
        """Zrušenie pomalej jazdy (POTR) v e-maile, ktorý ohlasuje novú výluku či pomalú jazdu: s číslom rušeného
        dokumentu je to ďalšia udalosť (zrušenie vždy potvrdzuje dispečer), bez čísla len upozornenie pri výluke.
        Zrušenie je označené ako zrušenie pomalej jazdy (``kind`` = pomala_jazda) – nesmie zrušiť výluku iného druhu
        s rovnakým číslom (holé číslo „12/2026“ môže mať aj ROV inej OR)."""
        out: list[ExtractedItem] = []
        for s, e in c.cancels:
            sentence = one_line(c.text[s:e], 160)
            ref = c.speed_ref(s, e)
            if not ref:
                for it in items:
                    it.notes.append(f"E-mail ruší aj pomalú jazdu („{sentence}“), jej číslo sa nenašlo – overte ju a "
                                    "zrušte v registri výluk ručne.")
                continue
            secs = [x for x in c.sections(s, e) if not x.line_desc]
            pers = [p for p in c.periods if s <= p.pos < e]
            data = {"external_ref": ref, "from_station": secs[0].a.text if secs else None,
                    "to_station": secs[0].b.text if secs else None, "start": _iso(pers[0].start) if pers else None,
                    "kind": "pomala_jazda"}
            notes = ["Zrušenie pomalej jazdy uvedené v e-maile spolu s novou výlukou či pomalou jazdou – overte, ktorú "
                     "pomalú jazdu ruší a odkedy."]
            out.append(ExtractedItem("vyluka_zrusenie", data, _confidence(2, 2, 0.05), notes, {"external_ref": 0.8},
                                     {"zrusenie": sentence}))
        return out

    def _secondary_closure_cancels(self, c: _Ctx, items: list[ExtractedItem],
                                   notes_only: bool = False) -> list[ExtractedItem]:
        """Zrušenie inej výluky vo vete e-mailu, ktorý ohlasuje novú výluku: s číslom či úsekom ďalšia udalosť
        (potvrdzuje dispečer). Upozornenie namiesto udalosti, keď sa číslo ani úsek nenašli, keď ide o tú istú výluku
        ako nová výluka e-mailu (presun termínu napísaný ako „… sa nekoná, … bude …“) a pri zmene výluky – potvrdenie
        zrušenia by inak mohlo zrušiť aj výluku s novým termínom."""
        out: list[ExtractedItem] = []
        main_refs = {re.sub(r"[^0-9a-z/]", "", fold(str(it.data.get("external_ref") or ""))) for it in items} - {""}
        main_secs = {frozenset((fold(str(it.data.get("from_station") or "")), fold(str(it.data.get("to_station") or ""))))
                     for it in items}
        for s, e in c.closure_cancels:
            sentence = one_line(c.text[s:e], 160)
            ref = c.cancel_ref([(s, e)])
            secs = [x for x in c.sections(s, e) if not x.line_desc]
            same = (ref and re.sub(r"[^0-9a-z/]", "", fold(ref)) in main_refs) or any(
                frozenset((fold(x.a.text), fold(x.b.text))) in main_secs for x in secs)
            if notes_only or same or (not ref and not secs):
                for it in items:
                    it.notes.append(f"E-mail ruší aj výluku („{sentence}“) – overte, ktorej výluky sa zrušenie týka, a ak "
                                    "platí, zrušte ju v registri výluk ručne.")
                    it.confidence = round(max(0.3, it.confidence - 0.05), 2)
                continue
            data, notes = self._cancel_term(c, s, e, {"external_ref": ref,
                                                      "from_station": secs[0].a.text if secs else None,
                                                      "to_station": secs[0].b.text if secs else None})
            notes.insert(0, "Zrušenie výluky uvedené v e-maile spolu s novou výlukou – overte, ktorú výluku ruší.")
            out.append(ExtractedItem("vyluka_zrusenie", data, _confidence(2, 2, 0.05), notes,
                                     {"external_ref": 0.8 if ref else 0.0}, {"zrusenie": sentence}))
        return out

    def _cancel_term(self, c: _Ctx, s: int, e: int, data: dict) -> tuple[dict, list[str]]:
        """Termín, ktorého sa zrušenie týka („výluka … dňa 25.11.2026 sa ruší“): začiatok a koniec rušeného obdobia.
        Zrušenie len časti viacdennej výluky („v ostatných dňoch platí bez zmeny“) celú výluku nezruší – zápis ho
        odmietne a dispečer upraví termín výluky v registri."""
        notes: list[str] = []
        data = dict(data)
        pers = [p for p in c.periods if s <= p.pos < e]
        if pers:
            data["start"] = _iso(min(p.start for p in pers))
            ends = [p.end for p in pers if p.end is not None]
            if ends and len(ends) == len(pers):
                data["end"] = _iso(max(ends))
        else:
            def term_day(t: datumy.Temporal) -> bool:      # nie dátum vydania, podpisu či kontaktu
                ls = c.fq.rfind("\n", 0, t.start) + 1
                le = c.fq.find("\n", t.end)
                le = len(c.fq) if le == -1 else le
                return not (datumy.not_term_date(c.fq, t) or _DOC_LINE.search(c.fq, ls, le)
                            or _MENTION_BEFORE.search(c.fq, max(0, t.start - 30), t.start)
                            or any(a <= t.start < b for a, b in c.sig))

            days = sorted({t.day for t in c.toks if s <= t.start < e and t.kind in ("date", "datetime") and t.day
                           and term_day(t)})
            if days:
                data["start"] = _iso(datetime.combine(days[0], time(0, 0)))
                data["end"] = _iso(datetime.combine(days[-1] + timedelta(days=1), time(0, 0)))
                listed = ", ".join(f"{d:%d.%m.%Y}" for d in days)
                if _PARTIAL_CANCEL.search(c.fq, s, e) or _remaining_sentences(c):
                    notes.append(f"Ruší sa len časť výluky (deň {listed}) – ostatné dni výluky platia. Celá výluka sa "
                                 "automaticky nezruší: upravte jej termín v registri výluk.")
                else:
                    notes.append(f"Zrušenie uvádza deň {listed} – ak výluka trvá dlhšie, zruší sa len tento deň (celá "
                                 "výluka sa automaticky nezruší); overte termín.")
        return data, notes

    def _block_sections(self, c: _Ctx, bs: int, be: int) -> list[Section]:
        """Úseky odseku; bez úseku výluka v jednej dopravni („v žst. Ružomberok“, „v obvode stanice …“) – od = do.
        Dopravňa vo vete o výluke vedľa úseku („úplná výluka v žst. Kraľovany a v úseku Kraľovany – Ružomberok“) je
        ďalšia udalosť – výluka stanice zasahuje aj vlaky, ktoré úsekom nejdú (nie „vlaky čakajú v žst. …“)."""
        secs = [s for s in c.sections(bs, be) if not s.line_desc and not c.in_table(s.start)]
        if not secs:
            st = _station_place(c, bs, be)
            return [Section(st, st, [], st.start, st.end)] if st and not c.in_table(st.start) else []
        for pos, st in _station_places(c, bs, be):
            if c.in_table(st.start) or c.in_quote(st.start) or any(x.start <= st.start < x.end for x in secs) or \
                    any(x.a.name == st.name and x.a is x.b for x in secs):
                continue
            if _closure_station(c, bs, pos, st):
                secs.append(Section(st, st, [], st.start, st.end))
        return secs

    def _pair_sections(self, c: _Ctx, kind: str, fallback: bool = True
                       ) -> tuple[list[tuple[Optional[Section], Optional[Period], tuple[int, int]]], list[str]]:
        """Úseky a obdobia výluk podľa odsekov (aj zoznamov „1)“, „a)“, „-“): úsek uvedený raz platí pre všetky
        nasledujúce odseky len s termínom (a naopak termín pre všetky nasledujúce odseky len s úsekom), každý termín
        a každý úsek odseku dá udalosť. Čo sa nepodarilo priradiť, vráti ako zoznam textov (poznámka, nič sa
        nestratí potichu)."""
        events: list = []
        unused_secs: list[Section] = []
        unused_pers: list[Period] = []
        cur_sec: Optional[list] = None        # [úseky, (začiatok, koniec), použité]
        cur_per: Optional[list] = None        # [obdobia, (začiatok, koniec), použité]

        def drop_sec(x: Optional[list]) -> None:
            if x is not None and not x[2]:
                unused_secs.extend(x[0])

        def drop_per(x: Optional[list]) -> None:
            if x is not None and not x[2]:
                unused_pers.extend(x[0])

        for bs, be in c.blocks[1:]:
            if c.in_table(bs):
                continue
            secs = self._block_sections(c, bs, be)
            pers = [p for p in c.periods if bs <= p.pos < be and not c.in_table(p.pos)
                    and (kind == "vyluka_zrusenie" or not c.is_old_period(p))]
            if secs and pers:
                if len(secs) == 1:
                    events += [(secs[0], p, (bs, be)) for p in pers]
                elif len(pers) == 1:
                    events += [(s, pers[0], (bs, be)) for s in secs]
                else:
                    got, lost_secs, lost_pers = self._pair_block(c, secs, pers, bs, be)
                    events += got
                    unused_secs.extend(lost_secs)
                    unused_pers.extend(lost_pers)
                drop_sec(cur_sec)
                drop_per(cur_per)
                cur_sec = cur_per = None
            elif secs:
                if cur_per is not None:
                    events += [(s, p, (cur_per[1][0], be)) for s in secs for p in cur_per[0]]
                    cur_per[2] = True
                else:
                    drop_sec(cur_sec)
                    cur_sec = [secs, (bs, be), False]
            elif pers:
                if cur_sec is not None:
                    events += [(s, p, (cur_sec[1][0], be)) for s in cur_sec[0] for p in pers]
                    cur_sec[2] = True
                else:
                    drop_per(cur_per)
                    cur_per = [pers, (bs, be), False]
        subj = [s for s in c.sections(0, c.subject_end) if not s.line_desc] if fallback else []
        if fallback and not subj:
            st = _station_place(c, 0, c.subject_end)
            subj = [Section(st, st, [], st.start, st.end)] if st else []
        if not events and fallback:
            if cur_sec is not None and not cur_sec[2]:
                events += [(s, None, cur_sec[1]) for s in cur_sec[0]]
                cur_sec[2] = True
            if cur_per is not None and not cur_per[2] and not events:
                events += [(subj[0] if subj else None, p, cur_per[1]) for p in cur_per[0]]
                cur_per[2] = True
        if not events and subj:
            cands = [p for p in c.periods if kind == "vyluka_zrusenie" or not c.is_old_period(p)]
            per = cands[0] if cands else None
            events.append((subj[0], per, (0, len(c.text))))
        drop_sec(cur_sec)
        drop_per(cur_per)
        # rovnaký úsek a obdobie len raz; úsek bez obdobia vynecháme, ak je aj s obdobím
        out, seen = [], set()
        with_period = {(s.a.name, s.b.name) for s, p, _ in events if s and p}
        for s, p, span in events:
            key = (s.a.name if s else None, s.b.name if s else None, p.start if p else None, p.end if p else None)
            if key in seen or (s and not p and (s.a.name, s.b.name) in with_period):
                continue
            seen.add(key)
            out.append((s, p, span))
        # čo sa nepriradilo: úsek z citácie či úsek, ktorý už má udalosť, je len súvislosť; obdobie, ktoré už má
        # udalosť (rovnaký termín), tiež
        used_secs = {(s.a.name, s.b.name) for s, _p, _sp in out if s}
        used_pers = {(p.start, p.end) for _s, p, _sp in out if p}
        leftover = [f"úsek „{s.a.text} – {s.b.text}“" if s.a.name != s.b.name else f"dopravňa „{s.a.text}“"
                    for s in unused_secs if not c.in_quote(s.start) and (s.a.name, s.b.name) not in used_secs]
        leftover += [f"termín „{one_line(c.text[p.pos:p.end_pos], 60)}“" for p in unused_pers
                     if (p.start, p.end) not in used_pers]
        return out, list(dict.fromkeys(leftover))

    @staticmethod
    def _pair_block(c: _Ctx, secs: list[Section], pers: list[Period], bs: int, be: int) -> tuple[list, list, list]:
        """Odsek s viacerými úsekmi aj termínmi sa páruje po vetách: úseky a termíny vety, ktoré nie sú striedané
        („A – B a C – D dňa X a dňa Y“), dajú každý s každým; striedané („A – B dňa X, C – D dňa Y“) idú v poradí;
        termín vety bez úseku patrí k úsekom predchádzajúcej vety (úseky vety bez termínu k termínom nasledujúcej).
        Opakovaný úsek („… Kysak – Košice dňa 1.12. … Kysak – Košice dňa 2.12.“) sa hľadá v každej vete zvlášť.
        Vráti (udalosti, úseky a termíny, ktoré sa nepriradili)."""
        events: list = []
        used_s: set = set()
        used_p: set = set()
        stations_only = [s for s in secs if s.a is s.b]   # výluka stanice vedľa úseku (_block_sections)
        last_secs: list = []
        pending: list = []                                 # termíny pred prvou vetou s úsekom

        def pair(ss: list, ps: list) -> None:
            for s in ss:
                for p in ps:
                    events.append((s, p, (bs, be)))
                    used_s.add((s.a.name, s.b.name))
                    used_p.add(id(p))

        for s0, e0 in [x for x in c.sentences() if x[1] > bs and x[0] < be]:
            ss, se = max(s0, bs), min(e0, be)
            # všetky zmienky úsekov vo vete (aj opakované) v poradí textu, úseky bez opakovania
            occ = [x for ls, le in _lines(c.text, ss, se) for x in c._sections_in_line(ls, le)
                   if not x.line_desc and not c.in_table(x.start) and x.a.name != x.b.name]
            occ = sorted(occ + [x for x in stations_only if ss <= x.start < se], key=lambda x: x.start)
            s_secs = list({(x.a.name, x.b.name): x for x in reversed(occ)}.values())[::-1]
            s_pers = [p for p in pers if ss <= p.pos < se]
            if s_secs and pending:
                pair(s_secs, pending)
                pending = []
            if s_secs and s_pers:
                first_p, last_p = min(p.pos for p in s_pers), max(p.pos for p in s_pers)
                if len(s_secs) == 1 or len(s_pers) == 1 or occ[-1].start < first_p or last_p < occ[0].start:
                    pair(s_secs, s_pers)
                else:
                    # striedané zmienky a termíny: termíny za zmienkou úseku až po ďalšiu zmienku patria k nemu
                    for i, x in enumerate(occ):
                        nxt = occ[i + 1].start if i + 1 < len(occ) else se
                        pair([x], [p for p in s_pers if x.start <= p.pos < nxt])
                    pair(occ[:1], [p for p in s_pers if p.pos < occ[0].start])
                last_secs = s_secs
            elif s_secs:
                last_secs = s_secs
            elif s_pers:
                if last_secs:
                    pair(last_secs, s_pers)
                else:
                    pending += s_pers
        lost_secs = [s for s in secs if (s.a.name, s.b.name) not in used_s]
        lost_pers = [p for p in pers if id(p) not in used_p]
        return events, lost_secs, lost_pers

    def _restriction(self, c: _Ctx, f: str, sec: Optional[Section], per: Optional[Period], bs: int, be: int,
                     single: bool) -> tuple[str, bool]:
        """Druh obmedzenia udalosti z vety, ktorá ju ohlasuje (veta s jej úsekom, potom s jej termínom, odsek, celý
        text). Zmienka o výluke („po výluke“, „počas výluky“) výluku neohlasuje; časť vety o pomalej jazde sa pri
        výluke do druhu nepočíta – výluka sa tak nikdy nezmení na pomalú jazdu len preto, že text spomína aj pomalú
        jazdu („…, po výluke platí do odvolania pomalá jazda 50 km/h“)."""
        cands: list[str] = []
        sentences = c.sentences()
        positions = ([sec.start] if sec else []) + ([per.pos] if per else [])
        if sec and per and c.block_of(sec.start) != c.block_of(per.pos):
            positions.reverse()           # zoznam: úsek v úvode, druh pri termíne („1) Úplná výluka … dňa …“)
        for pos in positions:
            sent = next(((s, e) for s, e in sentences if s <= pos < e), None)
            if sent is not None and f[sent[0]:sent[1]] not in cands:
                cands.append(f[sent[0]:sent[1]])
        cands.append(f[bs:be] if not single else f[c.subject_end:] + " " + f[:c.subject_end])
        first: Optional[tuple[str, bool]] = None
        for text in cands:
            plain = _CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), text)
            closure = bool(re.search(r"vyluk|vyluceni", plain) or _TV_WORDS.search(plain)
                           or _TRACK_CLOSURE.search(plain) or _full_closure(plain))
            if closure:
                res = restriction_kind(_blank_speed_clauses(plain) if _SPEED_WORDS.search(plain) else plain)
            elif _SPEED_WORDS.search(plain):
                res = ("pomala_jazda", False)
            else:
                continue
            if not res[1]:
                return res                # druh výslovne uvedený v najbližšej vete
            first = first or res
        return first or restriction_kind(cands[-1])

    def _closure_item(self, c: _Ctx, kind: str, sec: Optional[Section], per: Optional[Period],
                      span: tuple[int, int], single: bool) -> ExtractedItem:
        bs, be = (0, len(c.text)) if single else span
        # výluka v e-maile, ktorý ruší aj pomalú jazdu či inú výluku: vety o zrušení sa do druhu, rýchlosti ani
        # čísla nerátajú (pri zrušení pomalej jazdy samotnom sú to práve ony)
        if kind == "vyluka_zrusenie":
            skip = [] if c.speed_cancel else list(c.cancels)
        else:
            skip = list(c.cancels) + list(c.closure_cancels)
        f = _blank(c.fc, skip) if skip else c.fc
        ctx_f = f[bs:be] if not single else f
        rkind, assumed = self._restriction(c, f, sec, per, bs, be, single)
        item_kind = kind
        if kind == "vyluka_nova" and rkind == "pomala_jazda":
            item_kind = "pomala_jazda"
        if kind == "pomala_jazda" and rkind != "pomala_jazda" and re.search(
                r"vyluk", _CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), ctx_f)):
            item_kind = "vyluka_nova"
        if item_kind == "pomala_jazda":
            rkind, assumed = "pomala_jazda", False
        notes: list[str] = []
        fc: dict = {}
        ev: dict = {}
        penalties = 0.0
        data: dict = {"kind": rkind}
        if assumed and item_kind != "vyluka_zrusenie":
            if _TRACK_CLOSURE.search(ctx_f):
                notes.append("Výluka koľaje bez čísla koľaje – predpokladá sa úplná výluka úseku (na jednokoľajnej "
                             "trati je to jediná koľaj); overte druh výluky.")
            elif _tv_full(ctx_f):
                notes.append("Úplná výluka trakčného vedenia – z textu nie je isté, či úsekom prejdú vlaky motorovej "
                             "trakcie; predpokladá sa úplná výluka úseku (najprísnejší prípad), overte druh výluky.")
            else:
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
                if m.method != "presne" and (key == "from_station" or sec.a is not sec.b):
                    penalties += 0.05
                    notes.append(f"Názov „{m.text}“ určený podľa skloňovaného tvaru → {m.name}.")
            if sec.a is sec.b:
                notes.append(f"Výluka v dopravni {sec.a.name} – úsek je len táto dopravňa (všetky jej koľaje).")
            ev["usek"] = c.line_of(sec.start)
            ev["_pos"] = sec.start
        if per:
            ev["_ppos"] = per.pos
            data["start"], data["end"] = _iso(per.start), _iso(per.end)
            if per.daily_from:
                data["daily_from"], data["daily_to"] = per.daily_from, per.daily_to
            if "do_odvolania" in per.flags:
                data["until_revoked"] = True
            notes += _flag_notes(per)
            if "rok_doplneny" in per.flags:
                penalties += 0.03
            if "zle_poradie" in per.flags:
                penalties += 0.1
            fc["start"] = 0.85
            fc["end"] = 0.85 if per.end else 0.0
            ev["obdobie"] = c.line_of(per.pos)
            blk = c.block_of(per.pos)
            loose = c.unused_times(*blk) if blk else []
            if loose:          # čas v tom istom odseku, ktorý sa nikam nepriradil – termín nemusí byť úplný
                penalties += 0.1
                notes.append("V texte je čas " + ", ".join(f"„{x}“" for x in loose[:3]) +
                             ", ktorý sa nepodarilo priradiť k obdobiu – overte termín.")
            loose_days = c.unused_dates(*blk) if blk else []
            if loose_days:     # deň v tom istom odseku bez obdobia (napr. ďalšia noc zoznamu) – termín nemusí byť úplný
                penalties += 0.1
                notes.append("V texte je dátum " + ", ".join(f"„{x}“" for x in loose_days[:3]) +
                             ", ktorý sa nepodarilo priradiť k obdobiu – overte termín.")
        a, b = (bs, be) if not single else (0, len(c.text))
        # zrušenie pomalej jazdy: číslo rušeného dokumentu („ruší sa pomalá jazda zavedená TLG č. 37/2026“,
        # „Odvolávame POTR č. 12/2026“), nie číslo telegramu, ktorý ju ruší
        ref = (c.speed_ref(a, b) or c.speed_ref(0, len(c.text))) if item_kind == "vyluka_zrusenie" and \
            c.speed_cancel else None
        if item_kind == "vyluka_zrusenie" and not c.speed_cancel:
            ref = c.cancel_ref(list(c.closure_cancels) + [(a, b), (0, len(c.text))], skip)
        ref = ref or c.first_group(_CLOSURE_REF_RES, a, b, skip) or (
            c.first_group(_CLOSURE_REF_RES, 0, len(c.text), skip) if item_kind in ("vyluka_zmena", "vyluka_zrusenie")
            else None)
        if ref:
            data["external_ref"] = ref
        data["line_ref"] = c.line_ref(a, b)
        own = c.block_of(per.pos) if per is not None and sec is not None else None
        if own is not None and own == c.block_of(sec.start):
            own = None                    # úsek aj termín v tom istom odseku – platí celý odsek
        data["track"] = (c.track(*own) if own else None) or c.track(a, b)
        if item_kind == "pomala_jazda" or rkind == "pomala_jazda":
            data["speed_kmh"] = c.speed(a, b, f)
        reason = c.reason(a, b)
        if reason:
            data["note"] = reason
        if item_kind == "vyluka_zrusenie":
            data = {"external_ref": data.get("external_ref"), "from_station": data.get("from_station"),
                    "to_station": data.get("to_station"), "start": data.get("start"), "end": data.get("end")}
            if c.speed_cancel:
                data["kind"] = "pomala_jazda"     # zrušenie pomalej jazdy – nie výluky s rovnakým číslom
                data.pop("end", None)
            else:
                # rušený termín z vety o zrušení (nie dni, ktoré ostávajú v platnosti); inak termín odseku
                spans = c.closure_cancels or ([(bs, be)] if not single else [(c.subject_end, len(c.text))])
                term, extra = self._cancel_term(c, spans[0][0], spans[-1][1], {})
                if not term.get("start") and not data.get("start"):
                    # rušený deň len v predmete („ROV č. … – zrušenie výluky dňa 25.11.2026“) – nestratí sa: bez neho
                    # by potvrdenie zrušilo celú (aj viacdennú) výluku
                    term, extra = self._cancel_term(c, 0, c.subject_end, {})
                if term.get("start"):
                    data["start"], data["end"] = term["start"], term.get("end")
                    notes += extra
                if not data.get("end"):
                    data.pop("end", None)
            found = 2 if data["external_ref"] else (1 if data["from_station"] and data["to_station"] else 0)
            found += 1 if data.get("start") else 0
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
            # riadok tabuľky výluk: koľaj zo stĺpca („2. TK“) – výluka tejto koľaje, nie predpokladaná úplná výluka;
            # rýchlosť v stĺpci „Rýchlosť“ bez slova výluky v druhu – pomalá jazda
            speed_head = t.header[cols["speed_kmh"]] if "speed_kmh" in cols else ""
            speed_col = bool(re.search(r"\d", cell("speed_kmh"))) and not cell("track") and not re.search(
                r"vyluk|vyluceni|uplna", fold(cell("kind")) + " " + speed_head + " " + row_f)
            rkind, assumed = ("pomala_jazda", False) if speed_col else restriction_kind(
                fold(cell("kind")) + " " + row_f, track=cell("track"), known_closure=True)
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
            elif len(t.rows) == 1 and len(c.tables) == 1:
                # jediná výluka v jedinej tabuľke: číslo ROV z textu e-mailu („oznamujeme výluku podľa ROV č.
                # 2260/2026:“) – nie číslo súborného rozkazu (SROV), ten má výluk viac
                head = next((c.text[m.start(1):m.end(1)].rstrip(".,;)") for m in _CLOSURE_REF_RES[0].finditer(c.f)
                             if not re.match(r"srov|suborn", m.group(0))), None)
                if head:
                    data["external_ref"] = head
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
        # pôvodný čas aktualizovaného avíza („(pôvodne 13:35)“, „namiesto 13:35“) nie je novým ETA
        old = [t for t in c.toks if _OLD_TIME_BEFORE.search(c.f, max(0, t.start - 25), t.start)]
        toks = [t for t in c.toks if t not in old]
        chosen: list[datumy.Temporal] = []
        for pos in labels:
            got = datumy.first_datetime_after(c.f, toks, pos, 120, fallback_day=None)
            if got:
                etas.append((got[0], got[1].start))
                chosen.append(got[1])
        if not etas:     # bez návestia: prvý dátum s časom v tele
            for t in toks:
                if t.kind == "datetime" and t.start >= c.subject_end:
                    etas.append((t.dt, t.start))
                    chosen.append(t)
                    break
        avizo_notes: list[str] = []
        if old and etas:
            avizo_notes.append("Pôvodný čas avíza („" + "“, „".join(one_line(c.text[t.start:t.end], 30) for t in old[:3])
                               + "“) sa nepoužil – platí nový predpokladaný príchod.")
        # ďalší čas za ETA v tom istom odseku („Rušňovodiča prosíme pristaviť o 14:30“) – ETA treba overiť (čas pred
        # návestím, napr. odchod z východiskovej stanice, za ETA omylom nevezme)
        for t in chosen[:1]:
            blk = c.block_of(t.start)
            others = [u for u in toks if blk and t.end <= u.start <= blk[1] and u.kind in ("time", "datetime")]
            if others:
                avizo_notes.append("V avíze je aj čas " + ", ".join(f"„{one_line(c.text[u.start:u.end], 30)}“"
                                                                    for u in others[:3]) +
                                   " – za predpokladaný príchod sa použil čas za návestím; overte ETA.")
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
            notes: list[str] = list(avizo_notes) if len(pairs) == 1 else []
            fc: dict = {}
            ev: dict = {}
            penalties = 0.05 * sum(1 for n in notes if n.startswith("V avíze je aj čas"))
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
        place, other = _incident_place(c)
        if place is not None:
            a, b = place.a, place.b
            data["from_station"], data["to_station"] = a.text, b.text
            fc["from_station"] = fc["to_station"] = 0.8 if a is not b else 0.75
            ev["usek"] = c.line_of(place.start)
            if a is b:
                notes.append("Mimoriadnosť v stanici – úsek je len táto dopravňa.")
            if other:
                # dopravňa aj iný úsek vo vete o mimoriadnosti – zapíše sa dopravňa (celá stanica, prísnejší prípad)
                notes.append(f"V texte o mimoriadnosti je aj {other} – zapísaná je dopravňa {a.name} (celá stanica, "
                             "prísnejší prípad); overte miesto mimoriadnosti.")
                penalties += 0.1
        end_notice = _incident_end_match(c)
        freight = _freight_ban(c) if end_notice is not None else None
        if freight is not None:
            # obnovenie len pre niektoré vlaky: pre nákladné vlaky (RID) obmedzenie trvá – nie je to koniec mimoriadnosti
            return [self._freight_restriction(c, data, fc, ev, notes, penalties, end_notice, freight)]
        if end_notice is not None:
            # oznámenie o skončení do registra nič nezapisuje – miesto je len informácia (úsek, kde je prevádzka
            # obnovená, inak dopravňa)
            secs = [s for s in c.sections(c.subject_end, len(c.text)) if not s.line_desc and not c.in_quote(s.start)]
            if secs:
                data = {**data, "from_station": secs[0].a.text, "to_station": secs[0].b.text}
            return self._incident_end(c, data, fc, ev, end_notice)
        end_m = _END_LABEL.search(c.fq, c.subject_end)

        def estimate(t: datumy.Temporal) -> bool:     # čas predpokladaného konca („predpokladané obnovenie … o 9:00“)
            return bool(end_m) and end_m.start() <= t.start <= end_m.end() + 60

        # obdobie „od … do …“ (mimoriadnosť nie je do odvolania) – len z vety o mimoriadnosti, nie okno náhradnej
        # dopravy či kontaktu na dispečera („Náhradná autobusová doprava od 6:00 do 22:00“)
        sentences = _sentence_spans(c.text, c.fq, c.subject_end, len(c.text))
        pers = [p for p in c.periods if p.pos >= c.subject_end and not c.in_table(p.pos) and not c.is_old_period(p)
                and _incident_clause(c, sentences, p.pos)] or [p for p in c.periods if p.pos < c.subject_end]
        start_tok = None
        if pers:
            start_dt = min(p.start for p in pers)
            spans = [(p.pos, p.end_pos) for p in pers]
            # uvedený skorší začiatok mimo obdobia („od 02.11.2026 04:30 …, neprejazdná v čase 6:00 – 13:00“) –
            # začiatok nie je neskorší (prísnejší prípad)
            early = [t.dt for t in c.toks if t.kind == "datetime" and t.start >= c.subject_end and not estimate(t)
                     and not any(a <= t.start < b for a, b in spans) and _incident_clause(c, sentences, t.start)
                     and t.dt < start_dt]
            start_dt = min(early + [start_dt])
            data["start"] = _iso(start_dt)
            fc["start"] = 0.85
            ev["zaciatok"] = c.line_of(pers[0].pos)
            if all(p.end is not None for p in pers):
                data["end"] = _iso(max(p.end for p in pers))
                fc["end"] = 0.85
                ev["obdobie"] = c.line_of(pers[0].pos)
            for p in pers:
                notes += [n for n in _flag_notes(p) if n not in notes]
            if any("rok_doplneny" in p.flags for p in pers):
                penalties += 0.03
            if any("zle_poradie" in p.flags for p in pers):
                penalties += 0.1
        else:
            cands = [t for t in c.toks if t.start >= c.subject_end and t.kind == "datetime" and not estimate(t)]
            start_tok = next((t for t in cands if _incident_clause(c, sentences, t.start)), None) or \
                next(iter(cands), None)
            if start_tok is not None:
                data["start"] = _iso(start_tok.dt)
                fc["start"] = 0.8
                ev["zaciatok"] = c.line_of(start_tok.start)
        if end_m and not (pers and not data.get("end")):
            got = datumy.first_datetime_after(c.fq, c.toks, end_m.end(), 80,
                                              fallback_day=start_tok.day if start_tok else (
                                                  pers[0].start.date() if pers else None))
            if got:
                end_dt = got[0]
                start_dt = start_tok.dt if start_tok is not None else (min(p.start for p in pers) if pers else None)
                if start_dt is not None and end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                known = _from_iso(data.get("end"))
                if known is None or local_naive_to_utc(end_dt) > known:   # predpokladaný neskorší koniec – prísnejší
                    data["end"] = _iso(end_dt)
                    fc["end"] = 0.7
                    notes.append("Koniec je predpokladaný (odhad ŽSR), nie záväzný.")
        if not data.get("end") and not re.search(r"do\s+odvolani", c.fq):
            # bez konca platí mimoriadnosť do odvolania – čas, ktorý sa nepriradil (aj okno, ktoré nepatrí
            # k mimoriadnosti, napr. náhradná doprava), môže byť neprečítaný koniec
            spans = [(p.pos, p.end_pos) for p in pers]
            loose = [one_line(c.text[t.start:t.end], 40) for t in c.toks
                     if t.kind in ("time", "datetime") and t.start >= c.subject_end and t is not start_tok
                     and not any(a <= t.start and t.end <= b for a, b in spans)]
            if loose:
                penalties += 0.1
                notes.append("V texte je čas " + ", ".join(f"„{x}“" for x in loose[:3]) + ", ktorý sa nepodarilo "
                             "priradiť – koniec mimoriadnosti nie je určený (platí do odvolania), overte koniec.")
        desc = _incident_sentence(c)
        if desc:
            data["description"] = desc
            fc["description"] = 0.8
        found = sum(1 for k in ("start", "description") if data.get(k)) + (1 if data.get("from_station") else 0)
        if not data.get("from_station"):
            notes.append("Miesto mimoriadnosti (úsek alebo stanica) sa nenašlo.")
        return [ExtractedItem("mimoriadnost", data, _confidence(found, 3, penalties), notes, fc, ev)]

    def _incident_end(self, c: _Ctx, place: dict, fc: dict, ev: dict, m: re.Match) -> list[ExtractedItem]:
        """Oznámenie o skončení mimoriadnosti („prevádzka je obnovená“) nie je nová mimoriadnosť – do registra výluk
        sa nič nezapíše (ani záznam „navrh“ do odvolania) a čaká vo fronte ako „iné“ s vysvetlením. Mimoriadnosť
        v registri sa automaticky neruší: oznámenie nemá číslo a podľa úseku by sa mohla zrušiť iná výluka (napr.
        plánovaná výluka na tom istom úseku) – ukončí ju dispečer. Obmedzenie rýchlosti po obnovení prevádzky
        („rýchlosť obmedzená na 30 km/h“) je samostatná pomalá jazda."""
        body_dates = [t for t in c.toks if t.start >= c.subject_end and t.day]
        # čas obnovenia – nie koniec obmedzenia za ním („… s obmedzením rýchlosti na 40 km/h do 18:00“)
        toks = [t for t in c.toks if not re.search(r"\bdo\s*$", c.fq[max(0, t.start - 4):t.start])]
        got = datumy.first_datetime_after(c.fq, toks, m.start(), 400,
                                          fallback_day=body_dates[0].day if body_dates else c.ref)
        if got is None:
            first = next((t for t in c.toks if t.start >= c.subject_end and t.kind == "datetime"), None)
            got = (first.dt, first) if first is not None else None
        reopen = got[0] if got else None
        sec = {k: place.get(k) for k in ("from_station", "to_station")}
        a, b = sec.get("from_station"), sec.get("to_station")
        where = (f" v dopravni {a}" if a == b else f" v úseku {a} – {b}") if a else ""
        notes = [f"Oznámenie o skončení mimoriadnosti{where} (prevádzka obnovená" +
                 (f" od {reopen:%d.%m.%Y %H:%M}" if reopen else "") + ") – nie je to nová mimoriadnosť a do registra "
                 "výluk sa nezapisuje. Ak ŽSR mimoriadnosť ukončila, ukončite ju v registri výluk ručne: automaticky "
                 "sa nezruší, lebo oznámenie nemá číslo a podľa úseku by sa mohla zrušiť iná výluka."]
        evidence = {**ev, "skoncenie_mimoriadnosti": c.line_of(m.start())}
        if got:
            evidence["obnovenie"] = c.line_of(got[1].start)
        items = [ExtractedItem("ine", {}, 0.5, notes, {}, evidence)]
        if _SPEED_AFTER.search(c.fq, c.subject_end) and c.speed(c.subject_end, len(c.text)):
            items.append(self._speed_after_incident(c, sec, reopen, fc, ev))
        return items

    def _freight_restriction(self, c: _Ctx, place: dict, fc: dict, ev: dict, notes: list[str], penalties: float,
                             end_notice: Optional[re.Match], freight: tuple) -> ExtractedItem:
        """Prevádzka obnovená len čiastočne – pre nákladné vlaky (vlaky s RID) obmedzenie trvá („Pre nákladné vlaky
        platí zákaz jazdy cez stanicu do 14:00“, „nákladné vlaky do 14:00 nepremávajú“). Dopravca vozí nákladné
        vlaky: pre neho mimoriadnosť trvá do uvedeného času – zapíše sa ako mimoriadnosť (nie oznámenie o skončení,
        po ktorom by dispečer mimoriadnosť ukončil a zrušil jediné obmedzenie)."""
        s, e, until, label = freight
        notes = list(notes)
        data: dict = {k: place.get(k) for k in ("from_station", "to_station")}
        start = None
        if end_notice is not None:
            toks = [t for t in c.toks if not re.search(r"\bdo\s*$", c.fq[max(0, t.start - 4):t.start])]
            got = datumy.first_datetime_after(c.fq, toks, end_notice.start(), 400, fallback_day=c.ref)
            start = got[0] if got else None
        if start is None:
            first = next((t for t in c.toks if t.start >= c.subject_end and t.kind == "datetime"
                          and not (s <= t.start < e)), None)
            start = first.dt if first is not None else None
        if start is None and c.doc.received_at is not None:
            from ..cas import local as _local

            start = _local(c.doc.received_at).replace(tzinfo=None, second=0, microsecond=0)
        if until is not None and start is not None and until <= start:
            until += timedelta(days=1)
        data["start"] = _iso(start)
        sentence = one_line(c.text[s:e], 200)
        if until is not None:
            data["end"] = _iso(until)
            notes.append(f"Prevádzka je obnovená len čiastočne – pre {label} platí obmedzenie do {until:%d.%m.%Y %H:%M} "
                         f"(„{sentence}“). Dopravca vozí nákladné vlaky, preto sa zapisuje ako mimoriadnosť do tohto "
                         "času. Ak je v registri mimoriadnosť v tejto dopravni do odvolania, nastavte jej koniec na "
                         "tento čas (nie skôr).")
        else:
            notes.append(f"Prevádzka je obnovená len čiastočne – pre {label} obmedzenie trvá („{sentence}“), koniec nie "
                         "je uvedený (platí do odvolania); overte koniec.")
            penalties += 0.1
        if "RID" in label:
            notes.append("Obmedzenie platí pre vlaky s nebezpečným tovarom (RID) – overte, či ho vlak vezie.")
        desc = _incident_sentence(c) or ""
        data["description"] = one_line(desc if sentence in desc else f"{desc} {sentence}", 400)
        evidence = {**ev, "obmedzenie_nakladnych_vlakov": c.line_of(s)}
        fcx = {k: v for k, v in fc.items() if k in ("from_station", "to_station")}
        fcx.update({"start": 0.7, "end": 0.8 if until is not None else 0.0, "description": 0.8})
        found = sum(1 for k in ("start", "description") if data.get(k)) + (1 if data.get("from_station") else 0)
        if not data.get("from_station"):
            notes.append("Miesto mimoriadnosti (úsek alebo stanica) sa nenašlo.")
        return ExtractedItem("mimoriadnost", data, _confidence(found, 3, penalties + 0.05), notes, fcx, evidence)

    def _speed_after_incident(self, c: _Ctx, sec: dict, reopen: Optional[datetime], fc: dict,
                              ev: dict) -> ExtractedItem:
        """Pomalá jazda po obnovení prevádzky (od obnovenia; do odvolania, ak nie je uvedený koniec)."""
        notes = ["Obmedzenie rýchlosti po skončení mimoriadnosti – zapisuje sa ako pomalá jazda."]
        pers = [p for p in c.periods if p.pos >= c.subject_end and p.end is not None]
        data: dict = {"kind": "pomala_jazda", **sec, "start": _iso(reopen) if reopen else None,
                      "speed_kmh": c.speed(c.subject_end, len(c.text))}
        if pers:
            data["start"] = data["start"] or _iso(pers[0].start)
            data["end"] = _iso(max(p.end for p in pers))
        elif re.search(r"do\s+odvolani", c.fq):
            data["until_revoked"] = True
            notes.append("Platí do odvolania – koniec nie je určený.")
        required = ["from_station", "to_station", "start", "end", "speed_kmh"]
        found = sum(1 for k in required if data.get(k)) + (0.5 if data.get("until_revoked") else 0)
        missing = [k for k in required if not data.get(k) and not (k == "end" and data.get("until_revoked"))]
        if missing:
            notes.append("Chýbajú polia: " + ", ".join(missing) + ".")
        return ExtractedItem("pomala_jazda", data, _confidence(found, len(required)), notes,
                             {k: v for k, v in fc.items() if k in sec}, dict(ev))

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
    # názov stanice za „do“ (najviac dve slová) nezačína číslicou – dátum za ním („do Čadce 03.11.2026 o 15:10“)
    # patrí k ETA, nie k návestiu
    r"(?:\s+(?:vlaku\s+)?(?:na\s+(?:hranicu|hranici|statnu\s+hranicu)|do\s+(?:pps\s+|stanice\s+)?[^\s:\d][^\s:]*"
    r"(?:\s+(?!o\s|v\s|vo\s)[^\s:\d][^\s:]*)?))?"
    r"|\beta\b|cas\s+prichodu|prichod\s+(?:vlaku\s+)?na\s+hranic\w*|prijezd\s+(?:vlaku\s+)?na\s+hranic\w*")
# pôvodný čas v aktualizácii avíza („(pôvodne 13:35)“, „namiesto 13:35“) – nie je novým ETA
_OLD_TIME_BEFORE = re.compile(r"(?:povodn\w*|puvodn\w*|namiesto|misto|predtym|doteraz\w*|predchadzajuc\w*|stary|stare"
                              r"|stareho)\s*:?\s*(?:o\s+|v\s+|cas\s+|eta\s+)?\(?\s*$")
_END_LABEL = re.compile(
    r"predpoklad\w*\s+(?:cas\s+)?(?:obnoveni\w*|ukonceni\w*|odstraneni\w*|sprejazdneni\w*|uvolneni\w*|zprovozneni\w*"
    r"|obnova|koniec|konec)|obnoveni\w*\s+(?:prevadzky|dopravy|provozu)\s+(?:sa\s+)?(?:predpoklad\w*\s+)?(?:o|na|do)?"
    r"|odstranenie\s+(?:poruchy|nasledkov)\s+(?:sa\s+)?predpoklad\w*")
_INCIDENT_WORDS = re.compile(r"mimoriadn|mimoradn|porucha|poruchy|poruchu|nehod|neprejazd|nesjizd|vykolaj|vykolej|"
                             r"zrazk|prerusen|preruseni|pretrhnut|lom\s+kolajnice|poskoden")
# mimoriadnosť v jednej dopravni: „v (železničnej) stanici X“, „v žst. X“, „v ŽST X“, „v obvode stanice X“,
# „v dopravni X“, „v zastávke X“ (česky „ve stanici X“, „v železniční stanici X“)
_STATION_PLACE = re.compile(r"\bv[eo]?\s+(?:obvode\s+|obvodu\s+)?(?:(?:zeleznicn\w*\s+)?(?:stanici|stanice|dopravni"
                            r"|dopravne|zastavke|zastavky)|zst\b\.?|z\.\s?st\.|zel\.\s?st\.|zel\.\s+stanici)\s*")
# bez predložky: „stanica X je neprejazdná“, „Žst. X je …“, „ŽST X – neprejazdná“ (nie „zo žst. X do …“, „do stanice“)
_STATION_NOUN = re.compile(r"(?<![\w.])(?<!z\s)(?<!zo\s)(?<!do\s)(?<!od\s)(?<!ku\s)(?<!k\s)(?<!cez\s)(?<!smer\s)"
                           r"(?:zeleznicn\w*\s+)?(?:stanica|stanicu|dopravna|dopravnu|zst\b\.?|z\.\s?st\.|zel\.\s?st\.)\s*")
# miestny pád názvu: „v Kraľovanoch“, „V Ružomberku“, „vo Vrútkach“ (len vo vete o mimoriadnosti)
_LOCATIVE = re.compile(r"(?<!\w)[vV][eo]?\s+(?=[A-ZÁČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ])")
# oznámenie o skončení mimoriadnosti (nie nová mimoriadnosť): „ukončenie mimoriadnej udalosti“, „mimoriadna udalosť
# bola ukončená“, „prevádzka je obnovená“, „obe koľaje sú zjazdné“, „porucha bola odstránená“, „trakčné vedenie je
# opäť pod napätím“; záporná veta („prevádzka nebola obnovená“, „zatiaľ nie je obnovená“) skončením nie je
_INCIDENT_END = re.compile(
    r"\bukoncen\w*\s+(?:\w+\s+){0,2}?(?:mimoriadn|mimoradn|prerusen\w*\s+(?:prevadzk|doprav|provoz))"
    r"|\b(?:mimoriadn|mimoradn)\w*(?:\s+udalost\w*)?(?:\s+\S+){0,8}?\s+(?:je|bola|byla|bol|byl)\s+ukoncen"
    r"|\b(?:je|su|bola|boli|bol|byla|byly|byl|bylo|jsou)\s+(?:opat\s+|znovu\s+|uz\s+)?"
    r"(?:obnoven(?:a|y|e|o)?|zjazdn\w*|prejazdn\w*|sjizdn\w*|sprejazdnen\w*|uvolnen\w*|pod\s+napatim)\b"
    r"|\b(?:prevadzk|doprav|provoz|premavk)\w*(?:\s+\S+){0,10}?\s+obnoven(?:a|y|e|o)\b"
    r"|\b(?:porucha|poruchu|poruchy|nehoda|nehodu|prekazka|prekazku|nasledky|zavada|zavadu)\b(?:\s+\S+){0,4}?\s+"
    r"(?:je|bola|boli|bol|byla|byly|byl)\s+(?:uz\s+)?odstranen"
    r"|\bprerusen\w*(?:\s+\S+){0,8}?\s+(?:je|bolo|bola|bol|bylo|byla|byl)\s+(?:uz\s+)?ukoncen")
_NEGATION = re.compile(r"\b(?:nie|nebol\w*|nebyl\w*|neni|nejsou|nebude|nebudu)\b")
# … ale mimoriadnosť ešte trvá (predpokladaný či budúci koniec, prerušená či vylúčená doprava, vypnuté trakčné
# vedenie, odklon, náhradná doprava, len jedna koľaj či len niektoré vlaky, obnovenie s obmedzením) – vtedy je to
# mimoriadnosť (prísnejší prípad); „ukončenie prerušenia dopravy“ sa za trvanie nepočíta a meškanie vlakov
# („predpokladané meškanie 20 minút“, „odhad 15 min“) tiež nie
_INCIDENT_ONGOING = re.compile(
    r"(?:predpoklad|ocakav|ocekav|odhad)\w*(?:\s+[^\s,;]+){0,3}?\s+(?:obnoven|ukoncen|odstranen|sprejazdnen|uvolnen"
    r"|zprovozn|koniec|konec|zjazdn|prejazdn|sjizdn)"
    r"|(?:obnoveni|ukonceni|odstraneni|sprejazdneni|uvolneni|zprovozneni)\w*(?:\s+[^\s,;]+){0,3}?\s+(?:sa\s+|se\s+)?"
    r"(?:predpoklad|ocakav|ocekav|odhad)"
    r"|\b(?:bude|budu)\s+(?:\w+\s+){0,2}?(?:obnoven|sprejazdnen|zjazdn|prejazdn|uvolnen|ukoncen|odstranen)"
    r"|(?:prerusen|neprejazdn|nezjazdn|nesjizdn|uzavret|uzavren|uzatvoren|zatvoren|vylucen|zablokovan|zastaven)"
    r"|vypnut|bez\s+napati|beznapat|nefunkcn|mimo\s+prevadzk|obmedzen\w*\s+(?:prevadzk|doprav)|odklon|odklan"
    r"|nahradn\w*\s+(?:autobusov\w*\s+)?doprav|\bnad\b|motorov"
    r"|\b(?:len|iba|jen|pouze)\s+(?:po|pre|pro)\b|jednokol[ae]j|po\s+jednej\s+kol[ae]j"
    r"|po\s+\d\.\s*(?:tratov\w*\s+|stanicn\w*\s+|hlavn\w*\s+)?kol[ae]j|\b\d\.\s*(?:tratov\w*\s+|stanicn\w*\s+"
    r"|hlavn\w*\s+)?kol[ae]j\w*\s+(?:je|bola|bol)\b|kol[ae]j\w*\s+c\.\s*\d+\w?\s+(?:je|bola|bol)\b"
    r"|kol[ae]j\w*\s+(?:je\s+|su\s+)?obsaden|s\s+obmedzen\w*\b(?!\s+(?:tratov\w*\s+)?rychlost)|privolavac")
# mimoriadnosť ešte trvá: „Mimoriadnosť v ŽST Kraľovany trvá.“ (nie „pomalá jazda trvá do odvolania“)
_INCIDENT_CONTINUES = re.compile(r"\b(?:trva|trvaju|pretrvava|pretrvavaju)\b")
# skončenie prerušenia („ukončenie prerušenia dopravy“, „prerušenie dopravy … je ukončené“) nie je trvanie
_INCIDENT_END_PHRASE = re.compile(r"\bukoncen\w*\s+(?:\w+\s+){0,2}?(?:mimoriadn|mimoradn|prerusen)\w*"
                                  r"|\bprerusen\w*(?:\s+\S+){0,8}?\s+(?:je|bolo|bola|bol|bylo|byla|byl)\s+(?:uz\s+)?"
                                  r"ukoncen\w*")
# obmedzenie rýchlosti po obnovení prevádzky („rýchlosť cez stanicu obmedzená na 30 km/h“)
_SPEED_AFTER = re.compile(_SPEED_WORDS.pattern + r"|rychlost\w*(?:\s+\S+){0,4}?\s+(?:obmedzen|omezen|znizen|snizen)"
                          r"|\d{1,3}\s?km\s?/\s?h")


def _incident_end_match(c: _Ctx) -> Optional[re.Match]:
    """Oznámenie o skončení mimoriadnosti (prvá nezáporná zmienka) – None, ak ho niet alebo text ukazuje, že
    mimoriadnosť ešte trvá. Posudzuje sa po vetách: zápor v tej istej časti vety („prevádzka zatiaľ nie je
    obnovená“) zmienku ruší a sám znamená trvanie."""
    found = None
    ongoing = False
    for s, e in [(0, c.subject_end)] + _sentence_spans(c.text, c.fq, c.subject_end, len(c.text)):
        for m in _INCIDENT_END.finditer(c.fq, s, e):
            a, _b = _clause_span(c.fq, (s, e), m.start())
            if _NEGATION.search(c.fq, a, m.end()):
                ongoing = True
            elif found is None:
                found = m
        rest = _INCIDENT_END_PHRASE.sub(lambda x: " " * len(x.group(0)), c.fq[s:e])
        if _INCIDENT_ONGOING.search(rest) or (_INCIDENT_CONTINUES.search(rest) and _INCIDENT_WORDS.search(rest)
                                              and not _SPEED_AFTER.search(rest)):
            ongoing = True
    if found is None or ongoing:
        return None
    rest = _INCIDENT_END_PHRASE.sub(" ", c.fq)
    if re.search(r"do\s+odvolani", rest) and not _SPEED_AFTER.search(rest):
        return None                       # „… do odvolania“ bez obmedzenia rýchlosti – mimoriadnosť trvá
    return found


# veta o mimoriadnosti (porucha, prerušená, neprejazdná, vylúčená doprava …) a časť vety s vedľajšou informáciou
# (náhradná doprava, kontakt na dispečera, informácie pre cestujúcich) – jej okno nie je obdobím mimoriadnosti
_INCIDENT_CLAUSE = re.compile(_INCIDENT_WORDS.pattern + r"|nezjazdn|uzavret|uzavren|uzatvoren|vylucen|zablokovan"
                              r"|zastaven|vypnut|obmedzen\w*\s+(?:prevadzk|doprav)")
_SIDE_INFO = re.compile(r"nahrad\w*|autobus\w*|k\s+dispozici\w*|kontakt\w*|\btel\b|telefon\w*|cestujuc\w*"
                        r"|informaci\w*\s+(?:pre|podav\w*|poskyt\w*)|infolink")


def _incident_clause(c: _Ctx, sentences: list[tuple[int, int]], pos: int) -> bool:
    """Pozícia ``pos`` je vo vete o mimoriadnosti a nie v časti vety s vedľajšou informáciou (náhradná doprava,
    kontakt na dispečera)."""
    sent = next(((s, e) for s, e in sentences if s <= pos < e), None)
    if sent is None or not _INCIDENT_CLAUSE.search(c.fq, *sent):
        return False
    a, b = _clause_span(c.fq, sent, pos)
    return not (_SIDE_INFO.search(c.fq, a, b) or re.search(r"\bNAD\b", c.text[a:b]))


def _speed_cancels(c: _Ctx) -> list[tuple[int, int]]:
    """Vety, ktoré rušia pomalú jazdu či POTR (predmet je jedna veta): nie „do odvolania pomalá jazda“, nie
    oznámenie budúceho zrušenia („zrušenie pomalej jazdy oznámime“) ani veta, ktorá pomalú jazdu zavádza."""
    out = []
    for s, e in [(0, c.subject_end)] + _sentence_spans(c.text, c.fq, c.subject_end, len(c.text)):
        if _INTRODUCES.search(c.fq, s, e):
            continue
        for m in _SPEED_CANCEL.finditer(c.fq, s, e):
            if m.group("noun") and (re.search(r"\bdo\s*$", c.fq[max(s, m.start() - 4):m.start()])
                                    or _FUTURE_NOTICE.search(c.fq, s, e)):
                continue
            out.append((s, e))
            break
    return out


# zrušenie výluky napísané vo vete: „ROV č. 2241/2026 (Leopoldov – Trnava, 25.11.2026) sa ruší“, „výluka … sa nekoná“,
# „… bola zrušená“, „… odpadá“, „… nebude vykonaná“, „Ruší sa výluka …“ (nie zmena termínu, nie budúce oznámenie)
_CANCEL_VERB = re.compile(r"\b(?:rusi|rusia|zrusuje|zrusuju|nekona|nekonaju|neuskutocni|neuskutocnia|nerealizuje"
                          r"|nerealizuju|odvolava|odvolavaju|stornuje|stornuju)\b")
_CANCEL_PRED = re.compile(
    r"\b(?:je|bola|bol|bolo|su|boli|bude|budu|byla|byl|bylo)\s+(?:zrusen|odvolan|stornovan)\w*"
    r"|\bnebud(?:e|u)\s+(?:vykonan|realizovan|uskutocnen)\w*|\bnebud(?:e|u)\s+(?:sa\s+)?kona"
    r"|\bodpada(?:ju)?\b|\bodpadne\b"
    r"|\b(?:zrusen|odvolan|storn)\w*\s+(?:\w+\s+){0,2}(?:vyluk|rov\b|rozkaz\w*\s+o\s+vyluke)")
_CLOSURE_NOUN = re.compile(r"vyluk|\brov\b|rozkaz\w*\s+o\s+vyluke")
_CHANGE_WORDS = re.compile(r"presuv|presun|posuv|posun|\bmeni\b|\bmenia\b|\bzmen|nov\w*\s+termin|nahradn\w*\s+termin"
                           r"|namiesto|nahradz|prelozen|preklad|odklad|odloz")
# dni či termíny výluky, ktoré po zrušení časti ostávajú v platnosti („v ostatných dňoch platí bez zmeny“)
_REMAINING = re.compile(r"ostatn\w*\s+(?:dn|termin|noc|cast)|\bv\s+ostatnych\b|(?:ostava|zostava)\w*\s+v\s+platnosti"
                        r"|\bbez\s+zmeny\b|nadalej\s+plat")
_PARTIAL_CANCEL = re.compile(r"\b(?:len|iba|jen|pouze|vylucne)\s+(?:v\s+den|v\s+dnoch|dna|dne|na\s+den|pre\s+den"
                             r"|pro\s+den|v\s+termine)|" + _REMAINING.pattern)


def _change_words(seg: str) -> bool:
    """Veta mení termín výluky (presun, náhrada, nový termín) – nie „ostatné dni platia bez zmeny“."""
    return bool(_CHANGE_WORDS.search(_REMAINING.sub(lambda m: " " * len(m.group(0)), seg)))


def _closure_cancels(c: _Ctx) -> list[tuple[int, int]]:
    """Vety nového textu, ktoré rušia výluku (nie pomalú jazdu, nie zmenu termínu, nie zmienku „po výluke“)."""
    out = []
    for s, e in c.sentences():
        if c.in_quote(s):
            continue
        seg = _CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), c.fc[s:e])
        if not _CLOSURE_NOUN.search(seg):
            continue
        if not (_CANCEL_PRED.search(seg) or (_CANCEL_VERB.search(seg) and re.search(r"\b(?:sa|se)\b", seg))):
            continue
        if _change_words(seg) or _INTRODUCES.search(seg) or _FUTURE_NOTICE.search(seg) or _HYPOTHETICAL.search(seg):
            continue
        sp = _SPEED_CANCEL.search(seg)
        if sp and not re.search(r"(?:vyluk\w*|\brov\b|rozkaz\w*\s+o\s+vyluke)", seg[:sp.start()] + seg[sp.end():]):
            continue                          # ruší sa pomalá jazda (POTR) – rieši _speed_cancels
        out.append((s, e))
    return out


def _remaining_sentences(c: _Ctx) -> list[tuple[int, int]]:
    """Vety o dňoch výluky, ktoré ostávajú v platnosti – nie sú novou výlukou."""
    return [(s, e) for s, e in c.sentences() if not c.in_quote(s) and _REMAINING.search(c.fc, s, e)
            and (s, e) not in c.closure_cancels]


def _speed_clauses(c: _Ctx) -> list[tuple[int, int]]:
    """Časti viet nového textu o pomalej jazde s rýchlosťou (nie zrušenie, nie podpis, nie citácia)."""
    out = []
    for s, e in c.sentences():
        if c.in_quote(s) or c.in_table(s) or any(a <= s < b for a, b in c.cancels) or any(a <= s < b for a, b in c.sig):
            continue
        seg = c.fc[s:e]
        if not _SPEED_WORDS.search(seg):
            continue
        a = 0
        for b in [m.start() for m in re.finditer(r";|,(?!\d)", seg)] + [len(seg)]:
            part = seg[a:b]
            if _SPEED_WORDS.search(part) and _SPEED_RE.search(part):      # s rýchlosťou – nie vysvetlivka „POTR = …“
                out.append((s + a, s + b))
            a = b + 1
    return out


def _blank_speed_clauses(text: str) -> str:
    """Časti vety (medzi čiarkami, bodkočiarkami, vetami) o pomalej jazde vymaže – druh výluky sa určí zo zvyšku."""
    out = []
    for part in re.split(r"(?<=[,;.\n])", text):
        out.append(" " * len(part) if _SPEED_WORDS.search(part) else part)
    return "".join(out)


def _new_restriction_outside(c: _Ctx, spans: list[tuple[int, int]]) -> bool:
    """E-mail mimo viet ``spans`` ohlasuje výluku či pomalú jazdu s termínom: v odseku sú slová výluky či pomalej
    jazdy (z predmetu sa počíta len výluka – „ROV č. …“; pomalá jazda v predmete patrí aj k jej zrušeniu) a termín
    (obdobie či dátum, ktorý nie je pôvodným termínom ani dátumom vydania)."""
    rest = _blank(c.fq, spans)
    subject_new = bool(_KIND_PATTERNS["vyluka_nova"].search(rest, 0, c.subject_end))

    def outside(pos: int) -> bool:
        return not any(s <= pos < e for s, e in spans) and not c.in_table(pos)

    for bs, be in c.blocks[1:]:
        if not (subject_new or _NEW_RESTRICTION.search(rest, bs, be)):
            continue
        if any(bs <= p.pos < be and outside(p.pos) and not c.is_old_period(p) for p in c.periods) or \
                any(bs <= t.start < be and t.kind in ("date", "datetime") and outside(t.start)
                    and not datumy.not_term_date(c.fq, t) for t in c.toks):
            return True
    return False


def _station_place(c: _Ctx, start: int = 0, end: Optional[int] = None,
                   locative: bool = False) -> Optional[StationMatch]:
    """Jedna dopravňa (prvá v texte, za ktorou je názov dopravne): „v (železničnej) stanici X“, „v žst. X“, „v obvode
    stanice X“, bez predložky „stanica X je …“, „Žst. X“, „ŽST X“; ``locative`` aj „v Kraľovanoch“, „V Ružomberku“
    (miestny pád názvu – len vo vete o mimoriadnosti)."""
    end = len(c.text) if end is None else end
    best: Optional[tuple[int, StationMatch]] = None
    pats = [(_STATION_PLACE, c.f), (_STATION_NOUN, c.f)] + ([(_LOCATIVE, c.text)] if locative else [])
    for pat, src in pats:
        for m in pat.finditer(src, start, end):
            if best is not None and m.start() >= best[0]:
                break
            st = c.st.prefix(tokens(c.text[m.end():min(end, m.end() + 80)], m.end()), c.text)
            if st:
                best = (m.start(), st)
                break
    return best[1] if best else None


def _station_places(c: _Ctx, start: int, end: int) -> list[tuple[int, StationMatch]]:
    """Všetky dopravne za „v žst. X“, „v (železničnej) stanici X“, „stanica X“ … v časti textu: (pozícia, dopravňa)."""
    out: dict[int, tuple[int, StationMatch]] = {}
    for pat in (_STATION_PLACE, _STATION_NOUN):
        for m in pat.finditer(c.f, start, end):
            st = c.st.prefix(tokens(c.text[m.end():min(end, m.end() + 80)], m.end()), c.text)
            if st and st.start not in out:
                out[st.start] = (m.start(), st)
    return sorted(out.values(), key=lambda x: x[0])


# dopravňa, kde vlaky čakajú, odchádzajú, obracajú sa, kde je náhradná doprava … – nie je miestom výluky
_STATION_SIDE = re.compile(r"\b(?:cak\w*|odchod\w*|odchadz\w*|prichod\w*|prichadz\w*|premav\w*|jazd\w*|veden\w*|odklon\w*"
                           r"|odklan\w*|stoj\w*|nastup\w*|vystup\w*|zastav\w*|odstav\w*|preprav\w*|nahradn\w*|autobus\w*"
                           r"|obrat\w*|obrac\w*|ukonc\w*|zacin\w*|koncia\w*|presun\w*|prestup\w*|gvd)\b")


def _closure_station(c: _Ctx, bs: int, pos: int, st: StationMatch) -> bool:
    """Dopravňa za „v žst. X“ na pozícii ``pos`` je miestom výluky: patrí k slovu výluky pred ňou v tej istej vete
    („úplná výluka v žst. X a v úseku …“, „výluka staničnej koľaje č. 3 v žst. X“, „… vrátane zhlavia v žst. X“) –
    nie „vlaky čakajú v žst. X“, „(v žst. X odchody podľa GVD)“, „počas výluky budú vlaky čakať v žst. X“."""
    lo = max(bs, pos - 160)
    plain = _CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), c.fc[lo:pos])
    words = list(re.finditer(r"vyluk\w*|vyluceni\w*|uzavret\w*|uzavren\w*", plain))
    if not words:
        return False
    gap_start = lo + words[-1].end()
    if re.search(r"[();!?]|\.\s+[A-ZÁČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ]", c.text[gap_start:pos]) or _STATION_SIDE.search(c.fc, gap_start, pos):
        return False
    tail = re.split(r"[,;()]|\.\s", c.fc[st.end:st.end + 60], maxsplit=1)[0]
    return not _STATION_SIDE.search(tail)


# vedľajšia informácia pri mimoriadnosti: odklon, iný (už) prejazdný úsek, obchádzka – jej úsek nie je miestom
_DETOUR = re.compile(r"odklon\w*|odklan\w*|veden\w*\s+(?:cez|po)\b|prejazdn\w*|zjazdn\w*|sprejazdnen\w*|obchadzk\w*"
                     r"|presmerovan\w*")


def _clauses(f: str, s: int, e: int) -> list[tuple[int, int]]:
    """Časti vety medzi čiarkami, bodkočiarkami a dvojbodkami (nie „14:00“, „266,100“)."""
    out, a = [], s
    for m in re.finditer(r";|,(?!\d)|:(?!\d)", f[s:e]):
        b = s + m.start()
        if f[a:b].strip():
            out.append((a, b))
        a = b + 1
    if f[a:e].strip():
        out.append((a, e))
    return out


def _incident_place(c: _Ctx) -> tuple[Optional[Section], Optional[str]]:
    """Miesto mimoriadnosti z vety, ktorá ju ohlasuje – nie úsek náhradnej dopravy, odklonu či iného (už) prejazdného
    úseku. Dopravňa („v ŽST Žilina“, „stanica Kraľovany je neprejazdná“, „v Kraľovanoch“) má v tej istej časti textu
    prednosť pred úsekom (celá stanica – prísnejší prípad); druhý výsledok opisuje úsek, ktorý sa nezapísal."""
    sentences = [x for x in _sentence_spans(c.text, c.fq, c.subject_end, len(c.text)) if not c.in_quote(x[0])]

    def side(a: int, b: int) -> bool:
        seg = c.fq[a:b]
        return bool(_SIDE_INFO.search(seg) or re.search(r"\bNAD\b", c.text[a:b])
                    or (_DETOUR.search(seg) and not _INCIDENT_CLAUSE.search(seg)))

    tier1, tier2, tier3 = [], [], []
    for s, e in sentences:
        incident = bool(_INCIDENT_CLAUSE.search(c.fq, s, e))
        sent_side = bool(re.search(r"nahrad\w*\s+(?:autobus\w*\s+)?doprav|autobus\w*|\bNAD\b", c.fq[s:e] + c.text[s:e]))
        for a, b in _clauses(c.fq, s, e):
            if side(a, b):
                continue
            if not sent_side or _INCIDENT_CLAUSE.search(c.fq, a, b):
                tier3.append((a, b))
            if incident:
                tier2.append((a, b))
                if _INCIDENT_CLAUSE.search(c.fq, a, b):
                    tier1.append((a, b))

    def first_place(spans: list, locative: bool) -> Optional[StationMatch]:
        found = [st for a, b in spans for st in [_station_place(c, a, b, locative)] if st]
        return min(found, key=lambda st: st.start) if found else None

    def first_section(spans: list) -> Optional[Section]:
        for a, b in spans:
            secs = [x for x in c.sections(a, b) if not x.line_desc]
            if secs:
                return secs[0]
        return None

    for spans, loc in ((tier1, True), (tier2, True), (tier3, False)):
        st, sec = first_place(spans, loc), first_section(spans)
        if st is not None:
            other = f"úsek „{sec.a.text} – {sec.b.text}“" if sec is not None else None
            return Section(st, st, [], st.start, st.end), other
        if sec is not None:
            return sec, None
    # predmet („Mimoriadnosť ŽST Žilina“, „Mimoriadnosť Kraľovany“)
    st = _station_place(c, 0, c.subject_end, locative=True)
    if st is not None:
        return Section(st, st, [], st.start, st.end), None
    secs = [x for x in c.sections(0, c.subject_end) if not x.line_desc]
    if secs:
        return secs[0], None
    toks = tokens(c.text[:c.subject_end])
    for i in range(len(toks)):
        if re.match(r"(?:re|fw|fwd|odp|aw|tr|wg)\s*:", fold(toks[i].raw)):
            continue
        st = c.st.prefix(toks[i:], c.text)
        if st is not None and st.method == "presne":
            return Section(st, st, [], st.start, st.end), None
    return None, None


# obnovenie prevádzky len pre niektoré vlaky: pre nákladné vlaky (vlaky s RID, ťažké vlaky) obmedzenie trvá
_FREIGHT = re.compile(r"\bnakladn\w*(?:\s+(?:vlak\w*|doprav\w*|preprav\w*))?|\bvlak\w*\s+s\s+(?:nebezpecn\w*|rid\b)"
                      r"|\bnebezpecn\w*\s+(?:tovar|vec|naklad|latk|zasielk)\w*|\brid\b|\bvlak\w*\s+nad\s+\d+\s*t\b")
_FREIGHT_BAN = re.compile(r"\bzakaz\w*|\bnepremava\w*|\bnebud\w*\s+premavat|\bnesm\w*|\bnemoz\w*"
                          r"|\bnie\s+(?:je|su)\s+(?:mozn|povolen|obnoven)\w*|\bnebude\s+(?:mozn|povolen)\w*|\bcakaj\w*"
                          r"|\bzastaven\w*|\bprerusen\w*|\bneprejd\w*|\bnepojdu\b|\baz\s+od\b|\baz\s+po\b|\bodstaven\w*"
                          r"|\bnie\b")
_BAN_LIFTED = re.compile(r"zrusen|odvolan|ukoncen|skoncen|neplati|uz\s+nie|bez\s+obmedzen"
                         r"|\bnie\s+(?:je|su|bude|budu)\s+(?:obmedzen|zastaven|prerusen)\w*")


def _freight_ban(c: _Ctx) -> Optional[tuple[int, int, Optional[datetime], str]]:
    """Časť vety o obmedzení, ktoré po obnovení prevádzky trvá pre nákladné vlaky (vlaky s RID): (začiatok, koniec,
    do kedy – miestny čas alebo None, komu platí)."""
    for s, e in _sentence_spans(c.text, c.fq, c.subject_end, len(c.text)):
        if c.in_quote(s):
            continue
        for a, b in _clauses(c.fq, s, e):
            seg = c.fq[a:b]
            if not (_FREIGHT.search(seg) and _FREIGHT_BAN.search(seg)) or _BAN_LIFTED.search(seg):
                continue
            label = ("vlaky s nebezpečným tovarom (RID)" if re.search(r"\brid\b|nebezpecn", seg)
                     and not re.search(r"nakladn", seg) else "nákladné vlaky")
            until = None
            for t in c.toks:
                if not (a <= t.start < b) or t.kind not in ("time", "datetime"):
                    continue
                before = c.fq[max(a, t.start - 7):t.start]
                if not re.search(r"(?:\bdo|\baz\s+od|\baz\s+po|\bod)\s*$", before):
                    continue
                if t.kind == "datetime":
                    until = t.dt
                else:
                    days = [d for d in c.toks if d.kind in ("date", "datetime") and d.day]
                    day = min(days, key=lambda d: abs(d.start - t.start)).day if days else c.ref
                    until = datetime.combine(day, t.tm) + timedelta(days=1 if t.plus_day else 0)
                break
            return a, b, until, label
    return None


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


def _merge_cancels(items: list[ExtractedItem]) -> list[ExtractedItem]:
    """Zrušenia tej istej výluky (rovnaké číslo, bez čísla rovnaký úsek) len raz – s rušeným termínom, ak ho niektoré
    má („Zrušenie ROV č. 1360/2026“ v predmete priloženého e-mailu a „ROV č. 1360/2026 (… dňa 03.12.2026) sa ruší“)."""
    out: list[ExtractedItem] = []
    for it in items:
        key = re.sub(r"[^0-9a-z/]", "", fold(str(it.data.get("external_ref") or "")))
        sec = (fold(str(it.data.get("from_station") or "")), fold(str(it.data.get("to_station") or "")))
        same = next((o for o in out if (key and key == re.sub(r"[^0-9a-z/]", "", fold(str(o.data.get("external_ref")
                                                                                              or ""))))
                     or (not key and not o.data.get("external_ref") and any(sec) and sec == (
                         fold(str(o.data.get("from_station") or "")), fold(str(o.data.get("to_station") or ""))))),
                    None)
        if same is None:
            out.append(it)
            continue
        if not same.data.get("start") and it.data.get("start"):
            same.data["start"], same.data["end"] = it.data.get("start"), it.data.get("end")
        for k in ("from_station", "to_station"):
            same.data[k] = same.data.get(k) or it.data.get(k)
        same.notes += [n for n in it.notes if n not in same.notes]
        same.confidence = max(same.confidence, it.confidence)
    return out


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
