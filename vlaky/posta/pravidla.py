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

import bisect
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

from ..cas import local_naive_to_utc
from . import datumy
from .datumy import Period
from .parser import is_forward_subject, is_reply_subject, reply_quote_spans
from .stanice import StationMatch, find_borders_in_text, stations, strip_station_prefix, tokens
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
# zákaz jazdy, vjazdu či prejazdu vlakov – len vlaky (nie cestné vozidlá, osoby), nie len osobné vlaky
_NOT_PASSENGER = r"(?!\w*\s+(?:osobn|s\s+cestujuc|pre\s+cestujuc|regionaln))"
_BAN_TRAINS = (r"\bzakaz\w*\s+(?:jazd|vjazd|prejazd)\w*\s+(?:(?:vsetk|nakladn|tazk|dlh|tovarov)\w*\s+){0,2}vlak"
               + _NOT_PASSENGER +
               r"|\b(?:jazda|vjazd|prejazd)\s+(?:(?:vsetk|nakladn)\w*\s+)?vlak\w*" + _NOT_PASSENGER +
               r"(?:\s+[^\s,;.]+){0,6}?\s+(?:(?:je|bude|su|budu)\s+)?zakazan")
_KIND_PATTERNS: dict[str, re.Pattern] = {
    "vyluka_zrusenie": _rx(
        r"zrusen\w*\s+(?:\w+\s+){0,2}(?:vyluk|rov\b|rozkaz\w* o vyluke)",
        r"(?:vyluk\w*|\brov\b)(?:\s+c\.)?(?:\s+[\w/.-]+)?\s+(?:sa\s+)?(?:rusi|zrusuje|nekona|nebude konat|odvolava"
        r"|(?<!\bnie\s)je zrusen|bola zrusen|sa zrusil)" + _NOT_SPEED_OBJ,
        r"odvolan\w*\s+(?:\w+\s+)?vyluk", r"rusime\s+(?:\w+\s+)?vyluk", r"storn\w*\s+(?:\w+\s+)?vyluk",
        # „ŽSR OR Košice odvoláva výluku …“ (sloveso pred výlukou bez „sa“; „ROV … ruší ROV …“ je náhrada, nie zrušenie)
        r"\bodvolava\w*\s+(?:\w+\s+){0,2}?(?:vyluk|rov\b)",
        # „STORNO – ROV 5330/2026“, „výluku v predmete stornujeme“
        r"\bstorn\w*\s*[-:]?\s*(?:\w+\s+)?(?:s?rov\b|rozkaz)", r"vyluk\w*(?:\s+\S+){0,3}?\s+(?:stornujeme|rusime|zrusujeme)\b",
        r"vyluk\w*\s+(?:\w+\s+){0,3}sa\s+(?:nekona|nevykona|rusi)" + _NOT_SPEED_OBJ),
    "vyluka_zmena": _rx(
        r"zmen\w*\s+(?:\w+\s+){0,2}(?:vyluk|rov\b|rozkaz\w* o vyluke)",
        r"(?:vyluk\w*|\brov\b)(?:\s+c\.)?(?:\s+[\w/.-]+)?\s+(?:sa\s+)?(?:meni|posuva|predlzuje|skracuje|presuva)",
        r"predlzen\w*\s+(?:\w+\s+)?vyluk", r"posun\w*\s+(?:\w+\s+)?vyluk", r"uprav\w*\s+(?:\w+\s+)?vyluk",
        r"aktualizac\w*\s+(?:\w+\s+)?vyluk", r"skraten\w*\s+(?:\w+\s+)?vyluk", r"\boprav\w*\s+(?:\w+\s+){0,2}vyluk",
        r"(?:vyluk\w*|\brov\b)(?:\s+c\.)?(?:\s+[\w/.-]+)?(?:\s+[\w/.-]+)?\s+(?:sa\s+)?(?:odklada|odsuva)",
        r"\bodklad\w*\s+(?:\w+\s+)?vyluk"),
    "pomala_jazda": _rx(
        r"pomal\w*\s+jazd", r"pomal\w*\s+jizd", r"\bpotr\b",
        r"(?:docasn|prechodn)\w*\s+(?:obmedzen|omezen)\w*\s+(?:tratov\w*\s+)?rychlost",
        r"rychlost\w*\s+(?:je\s+|bude\s+)?(?:obmedzen|omezen|znizen|snizen)\w*\s+na",
        r"(?:obmedzen|omezen|znizen|snizen)\w*\s+(?:tratov\w*\s+)?rychlost", r"zavedeni\w*\s+pomal"),
    "vyluka_nova": _rx(
        r"vyluk", r"vyluceni\w*\s+kol[ae]j", r"vypnut\w*\s+(?:trakcn|\btv\b)", r"vypnuti\w*\s+(?:trakcn|\btv\b)",
        r"napatov\w*\s+vyluk", r"beznapatov", r"\bs?rov\b", r"rozkaz\w*\s+o\s+vyluke",
        r"oznamen\w*\s+o\s+povolen\w*\s+vyluk",
        # obmedzenie hmotnosti, dĺžky či nápravového zaťaženia vlakov (druh „obmedzenie“)
        r"(?:obmedzen|obmedzuj|omezen)\w*\s+(?:\w+\s+)?(?:dlzk\w*|delk\w*|hmotnost\w*|naprav\w*)"),
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
        r"\bexpected\s+(?:to\s+)?arriv\w*", r"\barrival\s+(?:notice|at\b)", r"\bestimated\s+(?:time\s+of\s+)?arrival",
        r"ocakavan\w*\s+prichod", r"ocekavan\w*\s+prijezd", r"prichod\s+(?:vlaku\s+)?na\s+hranic",
        r"prijezd\s+(?:vlaku\s+)?na\s+hranic"),
    "mimoriadnost": _rx(
        r"mimoriadn\w*\s+udalost", r"mimoriadnost", r"mimoradn\w*\s+udalost", r"\bporuch\w*", r"\bnehod\w*",
        r"neprejazdn", r"nesjizdn", r"vykolajen", r"vykolejen", r"\bzrazk\w*", r"prerusen\w*\s+(?:prevadzky|dopravy)",
        r"preruseni\s+(?:provozu|dopravy)",
        # zákaz jazdy, vjazdu či prejazdu vlakov (aj len nákladných či s RID; „je zakázaný vjazd vlakov“, „jazda vlakov
        # … je zakázaná“) – obmedzenie, ktoré dopravcu zastaví; nie zákaz pre cestné vozidlá, osoby či len osobné vlaky
        _BAN_TRAINS,
        # obnovenie prevádzky po mimoriadnosti (aj len pre niektoré vlaky – pre nákladné môže obmedzenie trvať)
        r"obnoveni\w*\s+(?:prevadzky|dopravy|premavky)",
        r"(?:prevadzk|doprav|premavk)\w*(?:\s+\S+){0,6}?\s+(?:je|bola|bol|su|boli)\s+(?:\S+\s+){0,3}?obnoven"),
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
# úplná výluka: výslovne úplná, neprejazdná, doprava prerušená, vlaky nepôjdu, výluka oboch (všetkých) koľají aj po
# koľajach („výluka 1. TK a 2. TK“, „koľaje č. 1 a koľaje č. 2“) – posudzuje sa PRED trakčným vedením a koľajou („úplná
# výluka oboch TK vrátane vypnutia TV“ je úplná výluka, nie len trakčné vedenie: neprejde ani motorový vlak);
# „napäťová výluka TV, obe koľaje“ úplnou výlukou nie je. „Úplná“ sa počíta len pri výluke („úplná výluka“, „výluka
# bude úplná“, „úsek bude úplne vylúčený“) – nie „z dôvodu úplnej rekonštrukcie výhybky“, „úplná uzávierka cesty“.
_FULL_CLOSURE = re.compile(
    r"(?P<full>\bupln\w*)\s+(?:[^\s,;.()]+\s+){0,2}?(?:vyluk|vyluceni|vylucen|(?:uzavret|uzavren)\w*\s+(?:trat|usek|stanic)"
    r"|(?:zastaven|prerusen)\w*\s+(?:doprav|prevadzk|premavk))"
    r"|(?P<pred>\bvyluk\w*(?:\s+[^\s,;.()]+){0,3}?\s+(?:je|bude|bola|bol|su|budu)\s+upln\w*)"
    r"|\b(?:usek|trat|stanic|dopravn)\w*(?:\s+[^\s,;.()]+){0,4}?\s+(?:je|bude|bola|bol|su|budu)\s+upln\w*\s+"
    r"(?:vylucen|uzavret|uzavren)"
    r"|(?P<pair>\b(?:vyluk|vyluceni|vylucen)\w*\s+(?:[^\s,;.()]+\s+){0,2}?(?:\d\.\s?(?:tk\s*)?(?:,|a|aj|i|\+)\s*\d\.\s?"
    r"(?:tk\b|tratov\w*\s+kol[ae]j|kol[ae]j)"
    r"|tk\s+c\.\s?\d\s*(?:,|a|aj|i)\s*(?:tk\s+)?(?:c\.\s?)?\d\b"
    r"|kol[ae]j\w*\s+c\.\s?[12]\s*(?:,|a|aj|i)\s*(?:kol[ae]j\w*\s+)?(?:c\.\s?)?[12]\b))"
    r"|neprejazdn|nesjizdn|nezjazdn"
    r"|(?P<flow>\b(?:doprav|prevadzk|premavk|provoz)\w*(?:\s+[^\s,;.()]+){0,4}?\s+(?:je\s+|bude\s+|su\s+|budu\s+|bola\s+"
    r"|bolo\s+|bol\s+)?(?:uplne\s+)?(?:prerusen|zastaven|vylucen))"
    r"|\bprerusen\w*\s+(?:prevadzk|doprav|premavk|provoz)|\bzastaven\w*\s+(?:prevadzk|doprav|premavk)"
    # vlaky úsekom neprejdú („vlaky nebudú premávať“, „Vlaky v úseku … nepremávajú“, „všetky vlaky sú odrieknuté“,
    # „vlaky budú odklonené“), aj sloveso pred vlakmi („nepôjde žiadny vlak“, „v úseku nepremávajú žiadne vlaky“) a
    # „jazda (všetkých) vlakov nebude možná“
    r"|(?P<trains>\b(?:vsetk\w*\s+|ziadn\w*\s+)?vlak\w*(?:\s+[^\s,;.()]+){0,6}?\s+(?:nepojd\w*|nepremava\w*"
    r"|neprejd\w*|nebud\w*\s+(?:premavat|jazdit|prechadzat)|nemozu\s+(?:ist|premavat|jazdit|prechadzat)"
    r"|(?:budu|su)\s+(?:\w+\s+)?(?:odklon|odriekn)\w*|su\s+(?:\w+\s+)?zrusen\w*))"
    r"|(?P<notrains>\b(?:nepremava\w*|nepojd\w*|neprejd\w*|nebud\w*\s+premavat)\s+(?:[^\s,;.()]+\s+){0,2}?"
    r"(?:ziadn\w*\s+|vsetk\w*\s+)?vlak\w*)"
    r"|(?P<nomove>\b(?:jazd|premavk|prejazd)\w*\s+(?:vsetk\w*\s+)?vlak\w*(?:\s+[^\s,;.()]+){0,6}?\s+"
    r"(?:nebude|nie\s+je|nebudu|nie\s+su)\s+mozn\w*)"
    r"|vsetk\w*\s+(?:tratov\w*\s+|stanicn\w*\s+|hlavn\w*\s+|dopravn\w*\s+)?kol[ae]j"
    r"|vyluk\w*\s+(?:\w+\s+){0,2}?(?:celeho|celej|celu)\s+(?:\w+\s+)?(?:usek|trat|stanic|dopravn)"
    r"|vyluk\w*\s+(?:celeho\s+)?useku\b"
    r"|vyluk\w*\s+(?:(?!tv\b|trakcn|napat|napet)\w+\s+)?ob(?:e|i|och|ou|oma)\s+(?:tratov\w*\s+|stanicn\w*\s+|hlavn\w*\s+)?"
    r"(?:kol[ae]j|tk\b)"
    r"|vyluceni\w*\s+ob(?:e|i|och|ou|oma)\s+(?:tratov\w*\s+)?kol[ae]j"
    r"|vyluk\w*\s+(?:\d\.?\s?a\s?\d\.)\s?(?:tk\b|tratov\w*\s+kol[ae]j|kol[ae]j)"
    # výluka trakčného vedenia aj koľají („Výluka TV aj koľají“, „výluka TV a traťových koľají“) – neprejde ani
    # motorový vlak (nie „výluka TV a koľaje č. 1“)
    r"|(?P<tvtracks>vyluk\w*\s+(?:\btv\b|trakcn\w*\s+veden\w*)\s+(?:a|aj|i|vratane|spolu\s+s)\s+"
    r"(?:vsetkych\s+|oboch\s+|obidvoch\s+)?(?:tratov\w*\s+|stanicn\w*\s+)?kol[ae]j\w*(?!\s+c\.\s?\d))")
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
                        r"|neprejd\w*|nepremav\w*|nebud\w*)|aj\s+(?:pre\s+)?(?:vlak\w*\s+)?(?:s\s+)?motorov\w*"
                        r"|(?:jazd|prejazd)\w*\s+(?:\w+\s+)?motorov\w*\s+(?:vlak|rusn|hnac)\w*(?:\s+\S+){0,3}?\s+"
                        r"(?:nie\s+je|nebude)\s+mozn|ani\s+(?:pre\s+)?(?:vlak\w*\s+)?(?:s\s+)?motorov\w*")
# výslovne ani motorové vlaky neprejdú („Jazda motorových vlakov nie je možná“, „neprejazdný ani pre motorové vlaky“,
# „motorové vlaky nepremávajú“) – výluka TV je potom úplnou výlukou
_DIESEL_BLOCKED = re.compile(r"motorov\w*\s+(?:trakci\w*|vlak\w*|rusn\w*)(?:\s+[^\s.;]+){0,4}?\s+(?:nemoz\w*|nesm\w*"
                             r"|nepojd\w*|neprejd\w*|nepremav\w*|nebud\w*\s+(?:mozn|premavat|jazdit|prechadzat))"
                             r"|(?:jazd|prejazd)\w*\s+(?:\w+\s+)?motorov\w*\s+(?:vlak|rusn|hnac)\w*(?:\s+\S+){0,3}?\s+"
                             r"(?:nie\s+je|nebude)\s+mozn"
                             r"|(?:neprejazdn|nezjazdn)\w*\s+(?:\w+\s+)?(?:ani\s+|aj\s+)?pre\s+(?:vlak\w*\s+)?(?:s\s+)?motorov"
                             r"|ani\s+(?:pre\s+)?(?:vlak\w*\s+)?(?:s\s+)?motorov\w*")
_LIMIT_WORDS = re.compile(r"(?:obmedzen|omezen)\w*\s+(?:dlzky|delky|hmotnosti|naprav|loziskov)|zakaz\w*\s+(?:jazdy|vjazdu)"
                          r"|obmedzuj\w*\s+(?:\w+\s+)?(?:dlzk|hmotnost|naprav)")
# zákaz jazdy či vjazdu vlakov – bez obmedzenia hmotnosti, dĺžky či nápravového zaťaženia a nie len pre osobné vlaky je
# to úplná výluka (dopravca vozí nákladné vlaky); „zákaz jazdy vlakov s nápravovým zaťažením nad 22,5 t“ je obmedzenie
_TRAIN_BAN = re.compile(r"\bzakaz\w*\s+(?:jazdy|vjazdu|prejazdu|vstupu)\b")
_BAN_LIMITED = re.compile(r"naprav\w*|hmotnost\w*|\b\d+(?:[,.]\d+)?\s*t\b|\bton\w*|dlhs\w*|dlzk\w*|tazs\w*|presahuj\w*"
                          r"|vyss\w*|sirs\w*|\bnad\s+\d|\bosobn\w*|cestujuc\w*|\bpo\s+(?:\d\.\s?)?(?:tk|kol[ae]j)"
                          r"|\bna\s+(?:\d\.\s?)?kol[ae]j|kol[ae]j\w*\s+c\.|elektrick\w*|\bel\.\s*trakci\w*")
# zmienka o výluke, ktorá ju neohlasuje („po skončení výluky platí pomalá jazda“, „počas výluky“, „z dôvodu výluky“) –
# podľa nej sa druh obmedzenia neurčuje
_CLOSURE_MENTION = re.compile(r"\b(?:po|pred|pocas|pri|pre|z\s+dovodu|kvoli|vzhladom\s+na|v\s+suvislosti\s+s"
                              r"|v\s+nadvaznosti\s+na|v\s+case|v\s+mieste|po\s+dobu|na\s+dobu|do\s+skoncenia"
                              r"|po\s+(?:skonceni|ukonceni)|pocas\s+trvania)\s+(?:\w+\s+){0,1}?vyluk\w*"
                              r"|oddeleni\w*\s+vyluk\w*|\b(?:plan|planu|plane|planom|zoznam\w*|harmonogram\w*)\s+vyluk\b")


def _closure_words(plain: str) -> bool:
    """Text ohlasuje výluku (nie len pomalú jazdu): výluka, vylúčenie, vylúčený úsek či koľaj, výluka trakčného
    vedenia alebo koľaje, úplná výluka („úsek … bude vylúčený“, nie „nie je vylúčené, že …“)."""
    return bool(re.search(r"vyluk|vyluceni|(?<!nie je )(?<!nie su )(?<!nebude )\bvylucen\w*", plain)
                or _TV_WORDS.search(plain) or _TRACK_CLOSURE.search(plain) or _full_closure(plain))


_CLAUSE_BOUND = re.compile(r"[,;\n!?]|(?<!\d)\.\s")
# cestná doprava, priecestie, náhradná autobusová doprava – ich prerušenie či uzávierka nie je úplnou výlukou trate
_ROAD = re.compile(r"cestn|priecest|ciest|\bcesty\b|\bcestu\b|komunikaci|autobus|nahradn")


def _clause_before(folded: str, pos: int, limit: int = 40) -> str:
    """Text časti vety pred pozíciou (najviac ``limit`` znakov, po čiarku či koniec vety – nie „2. TK“)."""
    seg = folded[max(0, pos - limit):pos]
    cut = [m.end() for m in _CLAUSE_BOUND.finditer(seg)]
    return seg[cut[-1]:] if cut else seg


def _full_closure(folded: str) -> bool:
    """Text výslovne hovorí o úplnej výluke (nie zápor „doprava nebude prerušená“, nie len osobné vlaky, nie „úplná
    výluka trakčného vedenia“ či „napäťová výluka oboch TK“ – tie posúdi :func:`_tv_full`). Prerušená doprava či
    nepremávajúce vlaky po jednej koľaji („Po 2. TK vlaky nepremávajú“) a prerušená cestná premávka na priecestí
    úplnou výlukou nie sú."""
    return _full_closure_span(folded) is not None


# vlaky „nepremávajú podľa GVD“, „nepôjdu včas“, „neprejdú bez zastavenia“ – premávajú (len inak), úsek nie je zatvorený
_MOVES_OTHERWISE = re.compile(r"\s*(?:podla|bez|vcas|nacas|pravidelne|normaln\w*|s\s+meskan\w*|v\s+obvyklom"
                              r"|ako\s+obvykle|v\s+plnom)")


def _full_closure_span(folded: str) -> Optional[tuple[int, int]]:
    """Pozícia slov úplnej výluky v texte (:func:`_full_closure`) – None, ak tam nie sú."""
    hit = _full_closure_hit(folded)
    return (hit[0], hit[1]) if hit else None


def _full_closure_hit(folded: str) -> Optional[tuple[int, int, str]]:
    """Slová úplnej výluky v texte: (začiatok, koniec, skupina vzoru – „nomove“ pre vety o vlakoch, ktoré úsekom
    neprejdú, inak „“) – None, ak tam nie sú."""
    for m in _FULL_CLOSURE.finditer(folded):
        text = m.group(0)
        nomove = bool(m.group("trains") or m.group("notrains") or m.group("nomove"))
        if m.group("flow") and _FULL_NEGATED.search(text):
            continue
        if nomove:
            nxt = _CLAUSE_BOUND.search(folded, m.end())
            tail = folded[m.end():min(nxt.start() if nxt else len(folded), m.end() + 40)]
            if _MOVES_OTHERWISE.match(tail) or _TRACK_NUMBER.search(tail) or \
                    re.search(r"\bpo\s+(?:druh|susedn|vedlajs)", tail):
                continue                  # „vlaky nepremávajú podľa GVD“, „nepôjdu vlaky po 1. TK“
            if re.search(r"(?:osobn|regionaln|rychlik|dialkov|elektrick)\w*\s*$",
                         folded[max(0, m.start() - 25):m.start()]) \
                    or re.search(r"osobn|regionaln|rychlik|elektrick|s\s+cestujuc", text) \
                    or re.match(r"\s*(?:\w+\s+)?(?:osobn|regionaln|rychlik|dialkov|elektrick|s\s+cestujuc)", tail):
                continue                  # len osobné či elektrické vlaky – nákladné (motorové) môžu ísť
            if re.search(r"\b(?:nie|nebud\w*|neboli|nebol\w*)\s+(?:\w+\s+){0,2}?(?:odklon|odriekn|zrusen)", text):
                continue                  # „vlaky nie sú zrušené“, „vlaky nebudú odklonené“
        if m.group("full") and _TV_QUALIFIER.match(folded, m.end("full")):
            continue                      # „úplná výluka trakčného vedenia“, „úplná napäťová výluka“
        if m.group("pred") and re.search(r"\btv\b|trakcn|napat|napet", text):
            continue                      # „výluka TV bude úplná“ – trakčné vedenie
        if not m.group("tvtracks") and (m.group("pair") or (re.match(r"vyluk", text) and re.search(
                r"\bob(?:e|i|och|ou|oma)\b", text))) and _TV_BEFORE.search(folded, max(0, m.start() - 30), m.start()):
            continue                      # „napäťová výluka oboch TK“ – trakčné vedenie oboch koľají
        if m.group("pair") and "stanicn" in folded[max(0, m.start() - 25):m.end()]:
            continue                      # staničné koľaje č. 1 a 2 – stanica má ďalšie koľaje
        if (m.group("flow") or nomove) and (
                _ROAD.search(text) or _ROAD.search(_clause_before(folded, m.start(), 25))
                or _TRACK_NUMBER.search(text) or _TRACK_NUMBER.search(_clause_before(folded, m.start()))):
            continue                      # „Cestná premávka na priecestí bude prerušená“, „Po 2. TK vlaky nepremávajú“
        return m.start(), m.end(), "nomove" if nomove else ""
    span = _full_clause_span(folded)
    return (span[0], span[1], "") if span else None


# úplné uzavretie trate, úseku či stanice v ľubovoľnom poradí slov v jednej časti vety („bude trať v úseku … úplne
# uzavretá“, „úsek … úplne uzavretý“, „úplná uzávera/uzávierka trate“, „trať úplne uzatvorená“) – nie traťová či
# staničná koľaj („traťová koľaj“, „staničné koľaje“), nie cesta či priecestie, nie zápor, nie trakčné vedenie
_FULL_CLAUSE_LINE = re.compile(r"\b(?:trat(?!ov)|usek|stanic(?!n)|dopravn(?!\w*\s+kol))\w*")
_FULL_CLAUSE_SHUT = re.compile(r"uzavret|uzavren|uzatvor|uzaver|uzavier|zatvoren|vylucen|neprejazdn|nezjazdn")


def _full_clause_span(folded: str) -> Optional[tuple[int, int]]:
    """Časť vety so slovom „úplný/úplne“, s traťou (úsekom, stanicou) a so slovom uzavretia – úplná výluka bez
    ohľadu na poradie slov. Zápor („trať nebude úplne uzavretá“), cesta či priecestie a trakčné vedenie ňou nie sú."""
    for m in re.finditer(r"\bupln\w*", folded):
        if _TV_QUALIFIER.match(folded, m.end()):
            continue
        cuts = [x.end() for x in _CLAUSE_BOUND.finditer(folded, 0, m.start())]
        s = cuts[-1] if cuts else 0
        nxt = _CLAUSE_BOUND.search(folded, m.end())
        clause = folded[s:nxt.start() if nxt else len(folded)]
        if not (_FULL_CLAUSE_LINE.search(clause) and _FULL_CLAUSE_SHUT.search(clause)):
            continue
        if re.search(r"\bne(?:bude|budu|bola|bol|bolo|je)\b|\bnie\s+(?:je|su|bude|budu)\b", clause) or \
                re.search(r"cestn|priecest|ciest|\bcest[auye]\b|\bcestou\b|komunikaci", clause) or \
                re.search(r"\btv\b|trakcn|napat|napet", clause):
            continue
        return s, (nxt.start() if nxt else len(folded))
    return None


def _tv_full(folded: str) -> bool:
    """„Úplná výluka trakčného vedenia“, „úplná napäťová výluka“ – úplná výluka spresnená trakčným vedením."""
    return any(_TV_QUALIFIER.match(folded, m.end()) for m in re.finditer(r"\bupln(?:a|ou|ej|e|u|y|ym|eho)\b", folded))


def _train_ban(folded: str) -> bool:
    """Zákaz jazdy či vjazdu vlakov bez obmedzenia hmotnosti, dĺžky, nápravového zaťaženia či koľaje a nie len pre
    osobné vlaky („– zákaz jazdy všetkých vlakov“, „Počas výluky zákaz jazdy vlakov.“) – úplná výluka; zápor („zákaz
    jazdy neplatí“, „bez zákazu“) ňou nie je."""
    return _train_ban_span(folded) is not None


def _train_ban_span(folded: str) -> Optional[tuple[int, int]]:
    for m in _TRAIN_BAN.finditer(folded):
        if re.search(r"\b(?:bez|nie\s+je|neplati|zrusen\w*)\s*$", folded[max(0, m.start() - 12):m.start()]):
            continue
        nxt = _CLAUSE_BOUND.search(folded, m.end())
        stop = min(nxt.start() if nxt else len(folded), m.end() + 90)
        tail = folded[m.end():stop]
        if _BAN_LIMITED.search(tail) or re.search(r"\b(?:neplati|zrusen|odvolan)", tail):
            continue
        return m.start(), stop
    return None


# koľaj, po ktorej doprava nebude možná („Výluka 2. TK … Jazda po 1. TK nebude možná“) – ak je to iná koľaj než
# vylúčená, sú zatvorené obe koľaje
_CLOSED_TRACK = re.compile(r"(?:vyluk|vyluceni|vylucen)\w*\s+(?:\w+\s+)?(\d)\.\s?(?:tk\b|tratov\w*\s+kol|kol[ae]j)"
                           r"|\b(\d)\.\s?(?:tk\b|tratov\w*\s+kol[ae]j\w*)\s+(?:\w+\s+)?(?:bude|je|budu)\s+vylucen")
_OTHER_TRACK = re.compile(r"\bpo\s+(\d)\.\s?(?:tk\b|tratov\w*\s+kol[ae]j\w*|kol[ae]j\w*)")
_NO_MOVEMENT = re.compile(r"nie\s+je\s+mozn|nebude\s+mozn|nebudu\s+mozn|nepremav\w*|nepojd\w*|neprejd\w*|vylucen\w*"
                          r"|neprejazdn\w*|nebude\s+prejazdn|zakaz\w*\s+jazdy|zastaven\w*|prerusen\w*")


def _other_track_blocked(folded: str) -> bool:
    """Text vylučuje jednu koľaj a o inej koľaji hovorí, že po nej doprava nebude možná – obe koľaje sú zatvorené."""
    return _other_track_span(folded) is not None


def _other_track_span(folded: str) -> Optional[tuple[int, int]]:
    closed = {m.group(1) or m.group(2) for m in _CLOSED_TRACK.finditer(folded)}
    if not closed or _SINGLE_OP.search(folded):
        return None
    for m in _OTHER_TRACK.finditer(folded):
        if m.group(1) in closed:
            continue
        cuts = [x.end() for x in _CLAUSE_BOUND.finditer(folded, 0, m.start())]
        s = cuts[-1] if cuts else 0
        nxt = _CLAUSE_BOUND.search(folded, m.end())
        e = nxt.start() if nxt else len(folded)
        if _NO_MOVEMENT.search(folded, s, e):
            return s, e
    return None


def _strong_full_cue(folded: str) -> Optional[tuple[int, int]]:
    """Slová úplného uzavretia v texte udalosti (úplná výluka, úplne uzavretá trať, neprejazdný úsek, prerušená
    doprava, nepremávajúce vlaky, obe koľaje, zákaz jazdy vlakov, neprejazdná aj druhá koľaj) – ich pozícia."""
    return _full_closure_span(folded) or _train_ban_span(folded) or _other_track_span(folded)


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
    if _train_ban(folded):
        # zákaz jazdy vlakov; len pre vlaky s nebezpečným tovarom (RID) – prísnejší prípad s poznámkou „overte“
        return "uplna", bool(re.search(r"\brid\b|nebezpecn", folded))
    if (closure_words or known_closure) and _other_track_blocked(folded):
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
        if _DIESEL_BLOCKED.search(folded) and not _DIESEL_OK.search(folded):
            return "uplna", False         # „Výluka TV … Jazda motorových vlakov nie je možná.“ – neprejde nik
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
# obe traťové koľaje vymenované po jednej („1. TK a 2. TK“, „TK č. 1 a TK č. 2“, „koľaje č. 1 a koľaje č. 2“)
_BOTH_TRACKS_PAIR = re.compile(r"\b(\d)\.\s?(?:tk\s*)?(?:,|a|aj|i|\+)\s*(?!\1)\d\.\s?(?:tk\b|tratov\w*\s+kol[ae]j)"
                               r"|\btk\s+c\.\s?\d\s*(?:,|a|aj|i)\s*(?:tk\s+)?"
                               r"(?:c\.\s?)?\d\b|kol[ae]j\w*\s+c\.\s?[12]\s*(?:,|a|aj|i)\s*(?:kol[ae]j\w*\s+)?(?:c\.\s?)?"
                               r"[12]\b")
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
    r"|(?:vlak\w*|train|zug)\s+(?:c\.\s*|cislo\s*|no\.\s*|nr\.\s*)?(?:(?P<cat2>nex|pn|rn|vn|mn|lv|sv|nx)\s?)?"
    r"(?P<num2>\d{3,6})\b)")
_MEDZI_RE = re.compile(r"\b(?:medzi|mezi)\s+(?:(?:zeleznicn\w*\s+)?(?:stanicami|stanicemi|dopravnami|dopravnami)\s+"
                       r"|zst\.?\s+|z\.\s?st\.\s+)?")
# úsek trate „z A do B“ – len za slovom trať či úsek („na trati z Kraľovian do Ružomberka“), nie cesta vlaku
_FROM_TO_RE = re.compile(r"\b(?:trat\w*|usek\w*)\s+(?:\w+\s+)?z[eo]?\s+")
_REASON_RE = re.compile(r"\bz\s+dovodu\s+|\bz\s+duvodu\s+|\bdovod\w*(?:\s+\w+){0,2}\s*:\s*"
                        r"|\bduvod\w*(?:\s+\w+){0,2}\s*:\s*")
_REASON_END = re.compile(r"[,;.\n(]|\s(?:sa|bude|budu|je|su|zavadza|zavadzaju|plati|v\s+useku|od\s+\d)\b")
_OLD_PERIOD = re.compile(r"povodn|puvodn|doterajs|predchadzajuc|predosl|stary\b|stareho|povodne")
# pôvodný termín za „namiesto“ či „pôvodne“ aj za novým termínom („… od 08:00 do 18:00 hod. (namiesto 08:00 – 14:00)“,
# „… 26.11.2026 namiesto 25.11.2026 od 08:00 do 14:00“): za slovom len údaje termínu (nie „namiesto nej bude …“,
# „namiesto úplnej výluky …“) – po zátvorku, čiarku, bodkočiarku či koniec vety
_OLD_TERM_HEAD = re.compile(r"\b(?:namiesto|namisto|misto|povodne|puvodne)\b")
_OLD_TERM_FILL = re.compile(r"(?:[\s.:/–—-]|\d|\b(?:povodn\w*|puvodn\w*|planovan\w*|terminu|termin|termine|dna|dne|v|vo"
                            r"|case|casu|cas|od|do|az|zo|ze|hod|h|min)\b)+")
# nový termín za zmienkou o pôvodnom („Pôvodne 25.11.2026, nový termín 26.11.2026“, „pôvodný termín … sa presúva na …“)
_NEW_TERM_MARK = re.compile(r"\b(?:nov|nahradn)\w*\s+(?:termin|datum|cas|den)\w*|\b(?:presuva|presunut|posuva|posunut"
                            r"|odklada|odlozen|preklada|prelozen|meni|zmenen|zmeni)\w*\s+(?:sa\s+)?na\b|\bteraz\b"
                            r"|\baktualn\w*")
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
# poznámka v zátvorke za názvom dopravne v úseku („Galanta (mimo) – Šaľa (mimo)“, „(vrátane)“)
_NAME_NOTE = re.compile(r"\((?:mimo|vratane|vcitane|vylucne|okrem|bez|vrat\.)\)[,;]?")


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
# („hod.“ pred veľkým písmenom je koniec vety: „… od 8:00 do 14:00 hod. Ostatné termíny platia bez zmeny.“)
_ABBREV_END = re.compile(r"(?:^|[^a-z])(?:zst|st|c|tel|napr|resp|tzv|cca|ing|mgr|odb|vyh|zast|zel|ul|min|max|km"
                         r"|[a-z]|sp|pop|nam)$")
# jednotka hneď za číslom („… do 14:00 h. Dňa …“, „30 min.“, „nad 1600 t.“) je koniec vety – nie skratka mena či
# názvu („J. Novák“, „N. Zámky“)
_UNIT_END = re.compile(r"\d\s?(?:h|hod|min|km|t)$")
# veta, ktorá začína dátumom („… do 14:00. 21.11. od 08:00 do 14:00 napäťová výluka …“): bodka za časom či slovom
# (nie bodka vnútri dátumu „20. 11. 2026“)
_DATE_START = re.compile(r"[ \t]+\d{1,2}\.\s?\d{1,2}\.")
_BEFORE_DATE_START = re.compile(r"(?:\d{1,2}:\d{2}|\d{1,2}\.\d{2}\s?(?:h|hod)|[a-z]{3,})$")
_ORDINAL_END = re.compile(r"(?<![\d.])\d{1,2}\.$")
_ORDINAL_NEXT = re.compile(r"[ \t]+(?:tk|tv|sk|kolaj\w*|tratov\w*|stanicn\w*|hlavn\w*)\b")


def _sentence_spans(text: str, f: str, start: int, end: int) -> list[tuple[int, int]]:
    """Vety v ``text[start:end]`` (``f`` je zložený text rovnakej dĺžky) ako (začiatok, koniec)."""
    out, s = [], start
    for m in _SENTENCE_END.finditer(f, start, end):
        nxt = text[m.end():end].lstrip(" \t")[:1]
        if m.group(0) != "\n":
            before = f[max(start, m.start() - 8):m.start()]
            if nxt.isupper():
                if not _UNIT_END.search(before) and _ABBREV_END.search(f[max(start, m.start() - 6):m.start()]):
                    continue
            elif not (m.group(0) == "." and nxt.isdigit() and _DATE_START.match(f, m.end(), end)
                      and _BEFORE_DATE_START.search(before)):
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


_WEEKDAY = re.compile(r"\b(?:v|vo|na)\s+(?:pondelok|utorok|stredu|streda|stvrtok|piatok|sobotu|sobota|nedelu|nedela)\b"
                      r"|\b(?:pondelok|utorok|streda|stvrtok|piatok|sobota|nedela)\s*,?(?=\s+\d)")


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
    if "bez_casu_konca" in p.flags:
        notes.append("Čas konca nie je uvedený – predpokladá sa koniec uvedeného dňa (24:00).")
    if "denne" in p.flags:
        notes.append(f"Opakuje sa denne {p.daily_from} – {p.daily_to}.")
    if "zle_poradie" in p.flags:
        notes.append("Dni v zozname nie sú v chronologickom poradí – rok je doplnený podľa ďalšieho dátumu, "
                     "overte termín.")
    return notes


def _name_note(m: StationMatch) -> str:
    """Poznámka k názvu dopravne, ktorý nie je presný: skratka („N. Zámky“), skloňovaný tvar („Vrútkach“) či podobný
    názov (preklep)."""
    how = {"skratka": "je skratka názvu dopravne", "priblizne": "určený podľa podobného názvu (overte ho)"}.get(
        m.method, "určený podľa skloňovaného tvaru")
    return f"Názov „{m.text}“ {how} → {m.name}."


def _confidence(found: int, required: int, penalties: float = 0.0) -> float:
    frac = found / required if required else 0.0
    return round(max(0.3, min(MAX_RULE_CONFIDENCE, 0.5 + 0.35 * frac - penalties)), 2)


# ------------------------------------------------------------------ kontext jedného e-mailu
# blok hlavičiek vloženej (preposlanej či citovanej) správy: „From:/Od: …“, „Subject:/Predmet: …“
_MAIL_HEAD = re.compile(r"^[ \t>*]*(from|od|von|to|komu|pre|an|date|datum|sent|odoslane|odeslano|gesendet|subject"
                        r"|predmet|betreff|cc|kopia|kopie|message-id)[ \t]*:[ \t]*([^\n]*)$", re.M)
_ATTACHMENT_HEAD = re.compile(r"^\[Príloha: [^\n]*\]$", re.M)
# text správy hovorí, že príloha je aktualizovaný či nový dokument („aktualizovaný ROV je v prílohe“, „v prílohe
# zasielame zmenený ROV“) – príloha nie je citáciou pôvodného dokumentu (nie „v prílohe pôvodný ROV“)
_UPDATED_ATTACHMENT = re.compile(
    r"\b(?:aktualizovan|zmenen|upraven|opraven|doplnen|nov)\w*\s+(?:\w+\s+){0,2}?(?:rov\b|rozkaz\w*|dokument\w*|znenie"
    r"|verzi\w*|telegram\w*|tlg\b)[^.\n]{0,60}?\bv\s+prilohe"
    r"|\bv\s+prilohe\s+(?:\w+\s+){0,3}?(?:aktualizovan|zmenen|upraven|opraven|doplnen)\w*\s+(?:\w+\s+){0,2}?"
    r"(?:rov\b|rozkaz\w*|dokument\w*|znenie|verzi\w*|telegram\w*|tlg\b)")
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
        self._place_cache: dict = {}
        self._names_cache: dict = {}
        self.body_start = self.subject_end + 2
        self.body_end = min(len(self.f), self.body_start + len(clean_text(doc.body).strip()))
        self.attachments = self._attachment_parts()
        # odpoveď s citovanou pôvodnou správou: termíny (dátumy, časy) sa berú len z nového textu – citácia ostáva
        # len súvislosťou (úsek, číslo výluky); ``fq`` je text s vymazanou citáciou
        self.quotes: list[tuple[int, int]] = []
        self.quoted_terms = False
        self.quoted_attachment = False
        self.fq = self._without_quotes(doc)
        # deň v týždni pred dátumom („v pondelok 7.12. a v utorok 8.12. vždy od 8:00 do 14:30“) zoznam dní nerozdelí
        dq = _WEEKDAY.sub(lambda m: " " * len(m.group(0)), self.fq)
        self.toks = datumy.scan(dq, self.ref)
        self.periods = datumy.periods(dq, self.toks)
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
        # rušený deň vo vete o presune („Výluka … dňa 25.11. sa ruší a presúva sa na 28.11. …“) – časť vety pred
        # slovom presunu
        self.moved_cancels: list[tuple[int, int]] = []
        # časť vety udalosti, ktorá zdieľa vetu s inou udalosťou: (začiatok úseku, začiatok obdobia) → (začiatok, koniec)
        self.own_spans: dict = {}

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
            # príloha je aktualizovaný dokument („aktualizovaný ROV je v prílohe“) alebo uvádza nový termín z textu
            # správy – nie je to citácia pôvodného dokumentu, jej termíny platia
            updated = bool(_UPDATED_ATTACHMENT.search(blanked, self.body_start, self.body_end))
            toks = datumy.scan(blanked, self.ref)
            new_days = {t.day for t in toks if t.start in term_pos and t.day}
            for s, e in self.attachments:
                if any(hs <= s < he for hs, he in accepted):
                    continue
                if updated or new_days & {t.day for t in toks if s <= t.start < e and t.day
                                          and not datumy.not_term_date(blanked, t, s)}:
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

    def in_cancel(self, pos: int) -> bool:
        """Pozícia je vo vete (časti vety), ktorá ruší výluku – jej termín je rušený termín, nie nová výluka."""
        return any(s <= pos < e for s, e in list(self.closure_cancels) + list(self.moved_cancels))

    def sentence_of(self, pos: int) -> tuple[int, int]:
        return next(((s, e) for s, e in self.sentences() if s <= pos < e), (pos, pos + 1))

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
    def place_names(self, start: int, end: int) -> set:
        """Miesta (úseky a dopravne „v žst. X“) v časti textu ako dvojice názvov – na rozlíšenie viet iných udalostí."""
        key = (start, end)
        if key not in self._names_cache:
            names = {(x.a.name, x.b.name) for x in self.sections(start, end) if not x.line_desc}
            names |= {(st.name, st.name) for _pos, st in _station_places(self, start, end)}
            self._names_cache[key] = names
        return self._names_cache[key]

    def period_positions(self) -> list[int]:
        """Pozície období zoradené vzostupne (``periods_sorted`` v rovnakom poradí) – na rýchle hľadanie období vety."""
        if getattr(self, "_period_pos", None) is None:
            self.periods_sorted = sorted(self.periods, key=lambda p: p.pos)
            self._period_pos = [p.pos for p in self.periods_sorted]
        return self._period_pos

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
                    a = self._station_at(toks[:j])
                    b = self._station_at(toks[j + 1:])
                    if a and b and text[a.end:toks[j].start].strip(" ()").lower() in ("", "mimo", "vrátane"):
                        found.append(Section(a, b, [], a.start, b.end))
                        break
        # „na trati z Kraľovian do Ružomberka“, „v úseku zo Žiliny do Vrútok“
        for m in _FROM_TO_RE.finditer(self.f, ls, le):
            toks = tokens(text[m.end():le], m.end())
            j = next((i for i, t in enumerate(toks[:7]) if fold(t.raw) == "do"), None)
            if j:
                a, b = self._station_at(toks[:j]), self._station_at(toks[j + 1:])
                if a and b and not text[a.end:toks[j].start].strip():
                    found.append(Section(a, b, [], a.start, b.end))
        # „A – B“, reťaz „A – B – C“
        seps = [i for i in range(ls, le) if self.f[i] in "->" and self._is_sep(i)]
        if seps:
            bounds = [ls - 1] + seps + [le]
            parts = [(bounds[k] + 1, bounds[k + 1]) for k in range(len(bounds) - 1)]
            k = 0
            while k < len(parts) - 1:
                # „Galanta (mimo) – Šaľa (mimo)“ – poznámka v zátvorke za názvom nie je súčasťou názvu
                ltoks = [t for t in tokens(text[parts[k][0]:parts[k][1]], parts[k][0]) if not _NAME_NOTE.fullmatch(fold(t.raw))]
                rtoks = tokens(text[parts[k + 1][0]:parts[k + 1][1]], parts[k + 1][0])
                a = self.st.suffix(ltoks, text) if ltoks else None
                b = self._station_at(rtoks) if (a and rtoks) else None
                if a and b:
                    via, j = [], k + 1
                    while j + 1 < len(parts) and not _NAME_NOTE.sub("", text[b.end:parts[j][1]].lower()).strip(" ,"):
                        ntoks = tokens(text[parts[j + 1][0]:parts[j + 1][1]], parts[j + 1][0])
                        nb = self._station_at(ntoks) if ntoks else None
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

    def _station_at(self, toks: list) -> Optional[StationMatch]:
        """Dopravňa na začiatku tokenov, aj s označením „žst.“, „ŽST“, „žel. st.“, „stanica“ pred názvom."""
        st = self.st.prefix(toks, self.text)
        if st is None:
            rest = strip_station_prefix(toks)
            if rest and len(rest) < len(toks):
                st = self.st.prefix(rest, self.text)
        return st

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
        both = _BOTH_TRACKS_PAIR.search(self.f, start, end)
        if both and "stanicn" not in self.f[max(start, both.start() - 25):both.end()]:
            return "obe"                  # „1. TK a 2. TK“, „koľaje č. 1 a koľaje č. 2“ – obe traťové koľaje
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

    def unused_times(self, start: int, end: int, skip_cancel: bool = False) -> list[str]:
        """Časy v časti textu, ktoré nepatria k žiadnemu nájdenému obdobiu (napr. ďalšie okno, ktoré sa nespracovalo);
        ``skip_cancel`` – bez časov vo vetách, ktoré výluku rušia (rušený termín nie je nepriradený termín)."""
        covered = self._in_periods()
        # časové okno vo vete o zrušení je rušený čas („…, v čase od 10:00 do 14:00 sa ruší“) alebo čas, ktorý ostáva
        # („od 08:00 do 10:00 platí bez zmeny, …“) – nie nepriradený termín
        windows = {id(t) for a, b in list(self.closure_cancels) + list(self.moved_cancels)
                   for w in datumy.time_windows(self.fq, self.toks, a, b) for t in w}
        out = []
        for t in self.toks:
            if t.kind not in ("time", "datetime") or not (start <= t.start < end) or \
                    (skip_cancel and self.in_cancel(t.start)):
                continue
            if id(t) not in covered and id(t) not in windows:
                out.append(one_line(self.text[t.start:t.end], 40))
        return out

    def _in_periods(self) -> set:
        """``id`` údajov termínu (dátum, čas), ktoré ležia v niektorom nájdenom období – raz na e-mail."""
        if getattr(self, "_in_periods_ids", None) is None:
            spans = [(p.pos, p.end_pos) for p in self.periods]
            self._in_periods_ids = {id(t) for t in self.toks if any(a <= t.start and t.end <= b for a, b in spans)}
        return self._in_periods_ids

    def unused_dates(self, start: int, end: int, skip_cancel: bool = False) -> list[str]:
        """Dátumy v časti textu, ktoré nepatria k žiadnemu obdobiu (napr. ďalší deň zoznamu, ktorý sa nespracoval),
        aj holé čísla dní pred dátumom, ktoré sa nepodarilo prečítať („v dňoch 3, 4 a 5.11.“).

        Nepočíta pôvodný deň presunu („z 12.11. na 13.11.“, „namiesto 12.11.“), dátum vydania, vyhotovenia či
        rozhodnutia („zo dňa 2.11.“) ani dátum v riadku s údajmi o dokumente („Vybavuje: …, tel. …, 30.10.2026“)."""
        covered = self._in_periods()
        out = []
        for t in self.toks:
            if t.kind not in ("date", "datetime") or not (start <= t.start < end) or \
                    (skip_cancel and self.in_cancel(t.start)):
                continue
            loose = _LOOSE_DAYS.search(self.f, max(start, t.start - 40), t.start)
            if loose and not any(u.start < t.start and u.end > loose.start() for u in self.toks if u is not t):
                out.append(one_line(self.text[loose.start():t.start] + self.text[t.start:t.end].split()[0], 40))
                continue
            if t.kind != "date" or id(t) in covered:
                continue
            ls = self.f.rfind("\n", 0, t.start) + 1
            le = self.f.find("\n", t.end)
            if datumy.not_term_date(self.f, t, start) or _DOC_LINE.search(self.f, ls, le if le != -1 else end):
                continue
            out.append(one_line(self.text[t.start:t.end], 40))
        return out

    def is_old_period(self, p: Period) -> bool:
        """Obdobie označené ako pôvodné („Pôvodný termín: …“, „… (namiesto 08:00 – 14:00)“) – pri zmene neplatí.
        Nový termín za pôvodným („Pôvodne 25.11.2026, nový termín 26.11.2026 …“, „… sa presúva na 26.11.“) pôvodným
        nie je."""
        ls = self.f.rfind("\n", 0, p.pos) + 1
        old = [m for m in _OLD_PERIOD.finditer(self.f, max(ls, p.pos - 60), p.pos)]
        if old and not _NEW_TERM_MARK.search(self.f, old[-1].end(), p.pos):
            return True
        return self.old_term_span(p) is not None

    def old_spans(self) -> list[tuple[int, int]]:
        """Časti textu s pôvodným termínom za „namiesto“ či „pôvodne“ (:data:`_OLD_TERM_HEAD`) – raz na e-mail."""
        if getattr(self, "_old_spans", None) is None:
            out = []
            for m in _OLD_TERM_HEAD.finditer(self.fq):
                if m.start() < self.subject_end:
                    limit = self.subject_end
                else:
                    limit = next((e for s, e in self.sentences() if s <= m.start() < e), len(self.fq))
                fill = _OLD_TERM_FILL.match(self.fq, m.end(), limit)
                end = fill.end() if fill else m.end()
                if re.search(r"\d", self.fq[m.end():end]):
                    out.append((m.start(), end))
            self._old_spans = out
        return self._old_spans

    def old_term_span(self, p: Period) -> Optional[tuple[int, int]]:
        """Časť textu „namiesto …“ / „pôvodne …“, v ktorej obdobie ``p`` začína alebo končí (jeho prvý či posledný
        údaj) – obdobie je pôvodným termínom, nie novým."""
        spans = self.old_spans()
        if not spans:
            return None
        edge = [t for t in self.toks if t.start == p.pos or t.end == p.end_pos]
        return next(((a, b) for a, b in spans if any(a <= t.start < b for t in edge)), None)


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


# pozvánka v kalendárovej prílohe (žiadosť o účasť, organizátor, účastníci) – nie len termín výluky na uloženie
_ICS_INVITE = re.compile(r"^(?:method:(?:request|cancel|reply|counter|declinecounter|add)\b|attendee[;:]|organizer[;:])",
                         re.M | re.I)
# číslo prevádzkového dokumentu ŽSR (ROV, SROV, rozkaz o výluke, telegram, POTR) – e-mail s ním je prevádzkové
# oznámenie, aj keď má predmet bulletinu či tlačovej správy alebo kalendárovú prílohu s termínom
_DOC_NUMBER = re.compile(r"\b(?:s?rov|rozkaz\w*\s+o\s+vyluke|tlg|telegram\w*|potr)\s*(?:\(s?rov\)\s*)?(?:c\.|cislo)?\s*:?\s*"
                         r"\d+\s*/\s*\d{2,4}\b")


def _calendar_attachment(name: object, text: object) -> bool:
    return str(name or "").lower().endswith((".ics", ".vcs", ".calendar")) or \
        str(text or "").lstrip().upper().startswith("BEGIN:VCALENDAR")


def _operational_number(doc: MailDocument) -> bool:
    """V predmete, tele alebo v prílohe (okrem kalendárovej) je číslo prevádzkového dokumentu (ROV č. 2250/2026)."""
    parts = [doc.subject or "", doc.body or ""] + [str(t or "") for n, t in doc.attachments
                                                  if not _calendar_attachment(n, t)]
    return any(_DOC_NUMBER.search(fold(p)) for p in parts)


def non_operational(doc: MailDocument) -> str:
    """Prečo e-mail nie je prevádzkovým oznámením (automatická odpoveď, pozvánka, newsletter) – „“ = je (alebo môže
    byť). Posudzuje sa podľa hlavičiek, príloh a predmetu – nie podľa obsahu tela (prevádzkové oznámenie môže mať
    v päte napr. odkaz na odhlásenie z rozosielania). Predmet bulletinu či tlačovej správy a kalendárová príloha bez
    pozvánky (termín výluky na uloženie do kalendára) sú len slabým signálom: e-mail s číslom ROV, telegramu či POTR
    je prevádzkové oznámenie (prísnejší prípad – stratená výluka by dala vlaku falošnú ZELENÚ)."""
    if doc.auto_reason:
        return doc.auto_reason
    subject = fold(doc.subject or "")
    if _AUTO_SUBJECT.match(subject):
        return "automatická odpoveď (predmet)"
    if _INVITE_SUBJECT.match(subject):
        return "pozvánka na poradu či stretnutie (predmet)"
    for name, text in doc.attachments:
        if _calendar_attachment(name, text) and _ICS_INVITE.search(str(text or "")):
            return "pozvánka na stretnutie (príloha .ics)"
    weak = weak_non_operational(doc)
    return "" if weak and _operational_number(doc) else weak


def weak_non_operational(doc: MailDocument) -> str:
    """Slabý signál, že e-mail nie je prevádzkovým oznámením: predmet newslettera, bulletinu či tlačovej správy,
    kalendárová príloha bez pozvánky – „“ = nič také."""
    subject = fold(doc.subject or "")
    if _BULK_SUBJECT.search(subject) and not is_reply_subject(doc.subject) and not is_forward_subject(doc.subject):
        return "hromadný informačný e-mail – newsletter (predmet)"
    if any(_calendar_attachment(n, t) for n, t in doc.attachments):
        return "pozvánka do kalendára (príloha .ics)"
    return ""


# informatívny prehľad výluk („Informatívny prehľad výluk OR … na 48. týždeň“, „Prehľad je informatívny, záväzné sú
# údaje uvedené v ROV“) – nie je záväzný, výluky sa z neho nezapisujú
_OVERVIEW = re.compile(r"\binformativn\w*\s+(?:\w+\s+){0,2}?prehlad\w*|\bprehlad\w*\s+(?:\w+\s+){0,3}?(?:je|ma)\s+(?:len\s+|iba\s+)?"
                       r"informativn\w*|\bprehlad\w*\s+(?:\w+\s+){0,3}?nie\s+je\s+zavazn\w*")
# zmienka o prehľade v inom dokumente (príloha, intranet, web) – e-mail sám prehľadom nie je
_OVERVIEW_MENTION = re.compile(r"\bv\s+prilohe|\bprilozen\w*|\bprikladam\w*|\bzasiela\w*\s+(?:aj|tiez|zaroven|vam)"
                               r"|\bnajdete|\bzverejnen\w*|\bdostupn\w*|\bintranet\w*|\bodkaz\w*|\bwww\.|https?:")
# vedľajšia informácia typu komunikácie (porada, konzultácie, pripomienky, žiadosti, GVD, kontakt) – jej termín ani úsek
# výluku neohlasuje (náhradná doprava nie: autobusy namiesto vlakov znamenajú výluku)
_META_COMM = re.compile(r"\bporad[auy]?\b|\bporade\b|stretnut\w*|rokovani\w*|konzultaci\w*|skoleni\w*|pripomien\w*"
                        r"|ziadost\w*|ziadame|objednav\w*|prijimame|zasielajte|zaslite|predlozte|nahlaste|\bgvd\b"
                        r"|cestovn\w*\s+poriad\w*|grafikon\w*|vybavuj\w*|\bkontakt|telefon\w*")


def _overview_or_side_info(c: "_Ctx") -> str:
    """E-mail o výlukách, ktorý žiadnu výluku neohlasuje – vysvetlenie (poznámka) alebo „“:
    informatívny prehľad („záväzné sú údaje uvedené v ROV“) a e-mail, ktorého každý termín aj úsek je len vo vedľajšej
    informácii typu porada, konzultácie či pripomienky (bez čísla ROV)."""
    body = _blank(c.fc, c.quotes) if c.quotes else c.fc
    # e-mail sám je prehľadom (predmet či veta „Prehľad je informatívny …“) – nie ROV, ktorý prehľad len spomína
    # („V prílohe zasielame aj informatívny prehľad výluk …“, „… nájdete na intranete“)
    own_number = bool(_DOC_NUMBER.search(c.subject_f)) and not _OVERVIEW.search(c.subject_f)
    hits = [m for m in _OVERVIEW.finditer(body)
            if not (m.start() >= c.subject_end and _OVERVIEW_MENTION.search(body, *c.sentence_of(m.start())))]
    if hits and not own_number:
        refs = []
        for m in _CLOSURE_REF_RES[0].finditer(c.fq, c.subject_end):
            ref = c.text[m.start(1):m.end(1)].rstrip(".,;)")
            if ref not in refs:
                refs.append(ref)
        return ("Informatívny prehľad výluk – nie je záväzný (záväzné sú údaje v ROV), výluky sa z neho nezapisujú. "
                "Overte v registri výluk, či sú v ňom" + (": ROV " + ", ".join(refs[:8]) if refs else " všetky uvedené "
                                                           "výluky") + "; chýbajúcu výluku zapíšte podľa ROV.")
    if c.first_group(_CLOSURE_REF_RES, 0, len(c.text)):
        return ""
    pers = [p for p in c.periods if p.pos >= c.subject_end and not c.in_table(p.pos) and not c.in_quote(p.pos)]
    secs = [x for x in c.sections(c.subject_end, len(c.text)) if not x.line_desc and not c.in_quote(x.start)]
    if not pers:
        return ""

    def comm(start: int, end: int) -> bool:
        if not _meta_clause(c, start, end):
            return False
        s, e = c.sentence_of(start)
        return bool(_META_COMM.search(c.fc, s, e))

    if all(comm(p.pos, p.end_pos) for p in pers) and all(comm(x.start, x.end) for x in secs):
        return ("E-mail spomína výluky len vo vedľajšej informácii (porada, konzultácie, pripomienky) – výluku "
                "neohlasuje a nič sa nezapisuje. Ak predsa ohlasuje výluku, zapíšte ju ručne.")
    return ""


# prevádzkové slová, ktoré ohlasujú obmedzenie dopravy (výluka, mimoriadnosť, zákaz jazdy, neprejazdná, uzavretá,
# nepremávajú, nesmú …) – sieť proti strate: e-mail od ŽSR s nimi a s termínom či miestom nesmie skončiť ako „iné“
_OPERATIONAL_CUES = re.compile(r"vyluk\w*|vyluceni\w*|mimoriadn\w*|mimoradn\w*|zakaz\w*\s+(?:jazdy|vjazdu|prejazdu)"
                               r"|neprejazdn\w*|nezjazdn\w*|uzavret\w*|uzavren\w*|uzatvoren\w*|nepremavaj\w*|nepojdu"
                               r"|nesmu\b|nesmie\b|prerusen\w*\s+(?:prevadzk|doprav|premavk)\w*|vykolaj\w*|\bporuch\w*"
                               r"|\bnehod\w*|obmedzen\w*\s+(?:prevadzk|doprav)\w*")
_BLOCKING_KINDS = ("vyluka_nova", "vyluka_zmena", "pomala_jazda", "mimoriadnost", "vyluka_zrusenie")
# slová, ktoré obmedzenie dopravy ohlasujú samy (výluka, mimoriadnosť, neprejazdný, vykoľajenie)
_NET_INHERENT = re.compile(r"vyluk|vyluceni|mimoriadn|mimoradn|neprejazdn|nezjazdn|vykolaj")
# predmet, ktorého uzavretie, porucha či zákaz obmedzením dopravy nie je: budova, kancelária, pokladňa, parkovisko,
# čakáreň, výťah, cesta a cestné vozidlá, osoby a cestujúci, lístky, telefóny, školenie, len osobné vlaky
_NET_NONOP = re.compile(r"budov\w*|kancelari\w*|pokladn\w*|parkovisk\w*|cakar\w*|vytah\w*|eskalator\w*|podateln\w*"
                        r"|zasadac\w*|toalet\w*|\bwc\b|bufet\w*|predajn\w*|automat\w*|cestn\w*|priecest\w*"
                        r"|\bcest[auy]\b|chodnik\w*|podchod\w*|vozidl\w*|\baut[aoy]?\b|autobus\w*|\bosob[ayu]?\b"
                        r"|\bosob\b|cestujuc\w*|preukaz\w*|listk\w*|telefon\w*|skoleni\w*|uradn\w*\s+hodin\w*"
                        r"|nastupist\w*|zamestnan\w*|navstevn\w*|chodc\w*|cyklist\w*|osobn\w*\s+vlak\w*")
# predmet uzavretia, ktorý obmedzením dopravy je: trať, úsek, koľaj, stanica, výhybka, zhlavie, trakčné vedenie, vlaky
_NET_RAIL = re.compile(r"\btrat\w*|\busek\w*|kol[ae]j\w*|\bvlak\w*|vyhyb\w*|zhlav\w*|\bzst\b|stanic\w*|dopravn\w*"
                       r"|\btv\b|trakcn\w*|zastavk\w*")


def _net_cue(body: str, m: re.Match) -> bool:
    """Prevádzkové slovo siete proti strate (:data:`_OPERATIONAL_CUES`) ohlasuje obmedzenie dopravy – nie uzavretú
    budovu, pokladňu, parkovisko či cestu, zákaz pre cestné vozidlá či osoby, „nesmú“ bez vlakov („Osoby … nesmú
    vstupovať“, „mobilné telefóny nesmú …“)."""
    lo = max(0, m.start() - 200)
    cuts = [x.end() for x in _CLAUSE_BOUND.finditer(body, lo, m.start())]
    nxt = _CLAUSE_BOUND.search(body, m.end())
    clause = body[cuts[-1] if cuts else lo:nxt.start() if nxt else len(body)]
    word = m.group(0)
    if _NET_INHERENT.match(word):
        return True
    if _NET_NONOP.search(clause):
        return False
    if re.match(r"nesm", word):
        return bool(re.search(r"\bvlak", clause))
    if re.match(r"uzavret|uzavren|uzatvoren", word):
        return bool(_NET_RAIL.search(clause))
    return True


def _dropped_content_net(c: "_Ctx", items: list) -> list:
    """Sieť proti strate obsahu: e-mail od ŽSR (prevádzkový odosielateľ) s prevádzkovými slovami (výluka,
    mimoriadnosť, zákaz jazdy, neprejazdná, uzavretá, nepremávajú …) spolu s termínom či miestom, z ktorého pravidlá
    nevytiahli žiadnu udalosť (len „iné“ či nič) a ktorý nie je oznámením o skončení mimoriadnosti, zrušením,
    neprevádzkovým e-mailom ani prehľadom – dá udalosť na potvrdenie (výluka či mimoriadnosť) s tým, čo sa našlo, a
    poznámkou. Bez miesta platí poistka pre celý vozový park (ORANŽOVÁ a DÔLEŽITÉ upozornenie), nie e-maily vlakom."""
    if any(it.kind != "ine" for it in items):
        return items
    if any(k in (it.evidence or {}) for it in items for k in ("skoncenie_mimoriadnosti", "neprevadzkovy_email")):
        return items
    try:
        from .overenie import zsr_sender

        if not zsr_sender(c.doc.sender or ""):
            return items
    except Exception:  # noqa: BLE001 – bez nastavení odosielateľov sieť nepracuje
        return items
    body = _blank(c.fq, c.sig) if c.sig else c.fq
    # zmienka o výluke („po skončení výluky“, „počas výluky“) či o inej, už zrušenej výluke nič neohlasuje
    body = _blank_cancel_mentions(_CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), body))
    cues = [m for m in _OPERATIONAL_CUES.finditer(body) if not c.in_quote(m.start()) and _net_cue(body, m)]
    if not cues:
        return items
    secs = [x for x in c.sections(0, len(c.text)) if not x.line_desc and not c.in_quote(x.start)]
    st = None if secs else (_station_place(c, c.subject_end, len(c.text)) or _station_place(c, 0, c.subject_end))
    pers = [p for p in c.periods if not c.in_quote(p.pos)]
    times = [t for t in c.toks if t.kind in ("datetime", "date", "time") and not c.in_quote(t.start)
             and not datumy.not_term_date(c.fq, t)]
    if not (secs or st or pers or times):
        return items
    words = ", ".join(dict.fromkeys(one_line(c.text[m.start():m.end()], 30) for m in cues[:4]))
    closure = bool(re.search(r"vyluk|vyluceni|uzavret|uzavren|uzatvoren", " ".join(m.group(0) for m in cues)))
    data: dict = {}
    if secs:
        data["from_station"], data["to_station"] = secs[0].a.text, secs[0].b.text
    elif st is not None:
        data["from_station"] = data["to_station"] = st.text
    if pers:
        data["start"], data["end"] = _iso(pers[0].start), _iso(pers[0].end)
        if "do_odvolania" in pers[0].flags and closure:
            data["until_revoked"] = True
    elif times:
        t = times[0]
        start = t.dt if t.kind == "datetime" else datetime.combine(t.day or c.ref, t.tm or time(0, 0)) \
            if t.kind in ("date", "time") else None
        if start is not None:
            data["start"] = _iso(start)
    if closure:
        data["kind"] = "uplna"
        if data.get("start") and not data.get("end"):
            data["until_revoked"] = True
    else:
        data["description"] = one_line(c.text[c.subject_end:], 300)
    note = (f"E-mail obsahuje „{words}“, ale pravidlá z neho nevedeli zapísať udalosť – overte vo fronte (zapísaná je "
            f"{'úplná výluka' if closure else 'mimoriadnosť'} s tým, čo sa našlo; doplňte miesto a termín).")
    notes = [note] + [n for it in items for n in it.notes]
    if not data.get("from_station"):
        notes.append("Miesto sa nepodarilo prečítať.")
    item = ExtractedItem("vyluka_nova" if closure else "mimoriadnost", data, 0.45, notes,
                         {k: 0.4 for k in data}, {"siet_proti_strate": words})
    item.data = normalize_data(item.kind, item.data)
    return [item]


def _alternate(occ: list, pers: list, end: int, pair: Callable) -> None:
    """Striedané zmienky úsekov a termíny: termíny za zmienkou úseku až po ďalšiu zmienku patria k nemu (termíny pred
    prvou zmienkou k prvému úseku)."""
    for i, x in enumerate(occ):
        nxt = occ[i + 1].start if i + 1 < len(occ) else end
        pair([x], [p for p in pers if x.start <= p.pos < nxt])
    pair(occ[:1], [p for p in pers if p.pos < occ[0].start])


# hranica časti vety pri striedaných úsekoch a termínoch: bodkočiarka, čiarka (nie desatinná) a spojka pred ďalším
# termínom („… od 08:00 do 14:00 a dňa 21.11.2026 od 08:00 … v úseku Košice – Čaňa“)
_CLAUSE_CUT = re.compile(r";|,(?!\d)|\s(?:a|aj|ako\s+aj|ale)\s+"
                         r"(?=(?:dna|dne|v\s+dnoch|v\s+den|od\s+\d|\d{1,2}\.\s?\d{1,2}\.))")


def _clause_groups(c: "_Ctx", ss: int, se: int, occ: list, pers: list) -> Optional[list]:
    """Veta so striedanými zmienkami úsekov a termínmi rozdelená na časti (:data:`_CLAUSE_CUT`): časť s úsekom aj
    termínom je skupina udalostí; časť len s termínom patrí k predchádzajúcej skupine, časť len s úsekom k nasledujúcej
    a časť bez nich (druh výluky, dôvod) k predchádzajúcej. Vráti [(zmienky úsekov, termíny, (začiatok, koniec))] –
    None, ak skupiny nie sú aspoň dve alebo niektorý úsek či termín ostane bez skupiny (rozhodne poradie zmienok)."""
    spans, a = [], ss
    for m in _CLAUSE_CUT.finditer(c.fq, ss, se):
        spans.append((a, m.start()))
        a = m.end() if m.group(0) in (";", ",") else m.start()
    spans.append((a, se))
    groups: list = []
    pend_s: list = []
    pend_p: list = []
    pend_a: Optional[int] = None
    for a, b in spans:
        cs = [x for x in occ if a <= x.start < b]
        cp = [p for p in pers if a <= p.pos < b]
        if cs and cp:
            groups.append([pend_s + cs, pend_p + cp, a if pend_a is None else pend_a, b])
            pend_s, pend_p, pend_a = [], [], None
        elif cs or (cp and (pend_s or not groups)):
            pend_s += cs
            pend_p += cp
            pend_a = a if pend_a is None else pend_a
        elif cp:
            groups[-1][1] += cp
            groups[-1][3] = b
        elif groups and pend_a is None:
            groups[-1][3] = b
    if pend_s and pend_p:
        groups.append([pend_s, pend_p, pend_a, se])
    elif pend_p and groups:
        groups[-1][1] += pend_p
        groups[-1][3] = se
    elif pend_s or pend_p:
        return None
    if len(groups) < 2 or {id(x) for g in groups for x in g[0]} != {id(x) for x in occ} or \
            {id(p) for g in groups for p in g[1]} != {id(p) for p in pers}:
        return None
    return [(g[0], g[1], (g[2], g[3])) for g in groups]


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
        if family == "vyluka" and kind in ("vyluka_nova", "vyluka_zmena", "pomala_jazda"):
            side = _overview_or_side_info(c)
            if side:
                return [ExtractedItem("ine", {}, 0.3, [side], evidence={"neprevadzkovy_email": side[:120]})]
        if family is None:
            return _dropped_content_net(c, [])
        handler: dict[str, Callable] = {"vyluka": self._closures, "trasa": self._paths, "avizo": self._avizo,
                                        "mimoriadnost": self._incident, "objednavka": self._order}
        items = handler[family](c, kind)
        items = _dropped_content_net(c, items)
        weak = scores.get(kind, 0) < 3     # druh len z tela, nie z predmetu
        hint = bool(_HINT_UNPLANNED.search(c.f))
        looks_bulk = weak_non_operational(doc)
        for it in items:
            if weak:
                it.confidence = round(max(0.3, it.confidence - 0.03), 2)
                it.notes.append("Druh e-mailu je určený len z textu (nie z predmetu).")
            if looks_bulk:
                it.notes.append(f"E-mail vyzerá ako {looks_bulk}, ale obsahuje číslo prevádzkového dokumentu ŽSR (ROV, "
                                "telegram, POTR) – spracovaný ako prevádzkové oznámenie; overte ho.")
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
        if re.match(r"\s*(?:(?:re|fw|fwd|tr|aw|odp|wg)\s*:\s*)*mu\b", c.subject_f):
            scores["mimoriadnost"] = scores.get("mimoriadnost", 0) + 3     # „MU – žst. Plešivec“ (mimoriadna udalosť)
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
            c.moved_cancels = [] if c.tables else _moved_cancels(c)
            subj = {k for k in kinds if _KIND_PATTERNS[k].search(c.subject_f)}
            # všeobecný predmet („Zmeny výluk“) bez čísla výluky, keď telo ruší výluky a ohlasuje aj novú výluku
            # s termínom – rozhodne telo (nová výluka a zrušenia ako ďalšie udalosti), inak by sa nová výluka stratila
            generic = not c.first_group(_CLOSURE_REF_RES, 0, c.subject_end) and bool(c.closure_cancels) and \
                _new_restriction_outside(c, c.closure_cancels + c.cancels + _remaining_sentences(c))
            for k in ("vyluka_zrusenie", "vyluka_zmena"):
                if k in subj and not generic:
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
        text = _blank_cancel_mentions(c.fc)     # „ako náhrada za výluku, ktorá bola zrušená“ zrušením nie je

        def sentence(pos: int) -> tuple[int, int]:
            return next(((s, e) for s, e in sentences if s <= pos < e), (pos, pos + 1))

        def hits(kind: str) -> list[tuple[int, int]]:
            out = []
            for m in _KIND_PATTERNS[kind].finditer(text, c.subject_end):
                s, e = sentence(m.start())
                if _FUTURE_NOTICE.search(c.fc, s, e) or _HYPOTHETICAL.search(c.fc, s, e):
                    continue
                if kind == "vyluka_zrusenie" and _foreign_subject(c, s, text[s:e], m.start() - s, m.end() - s):
                    continue              # „Porada k výluke sa nebude konať“ – neruší sa výluka
                out.append((s, e))
            return out

        cancel, change = hits("vyluka_zrusenie"), hits("vyluka_zmena")
        # „… sa nevykoná v plnom rozsahu, ale len …“ – zmena (skrátenie) výluky, nie zrušenie
        change += [sentence(m.start()) for m in _NOT_FULLY.finditer(c.fc, c.subject_end)
                   if not (_FUTURE_NOTICE.search(c.fc, *sentence(m.start())) or c.in_quote(m.start()))]
        if cancel:
            if any(_change_words(c.fc[s:e]) and not _replaced_here(c.fc[s:e]) for s, e in cancel) or \
                    any(x not in cancel for x in change):
                return "vyluka_zmena"
            if all(any(x[0] <= cs and ce <= x[1] for cs, ce in c.closure_cancels) for x in cancel) and \
                    _new_restriction_outside(c, c.closure_cancels + c.cancels + _remaining_sentences(c)):
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
        stay = _staying_sections(c) if kind == "vyluka_zrusenie" and not c.speed_cancel and not c.tables else {}
        if kind == "vyluka_zrusenie" and not c.speed_cancel and not c.tables and (len(c.closure_cancels) > 1 or stay):
            # zrušenie viacerých výluk v jednom e-maile („Výluka ROV … sa ruší. Výluka ROV … sa nekoná.“) – každá veta
            # je samostatné zrušenie, nie len prvá; úsek, ktorého výluka podľa e-mailu ostáva („Výluka v úseku Trnava
            # – Senica sa uskutoční podľa ROV …“), sa nikdy neruší
            multi = self._cancel_items(c, stay)
            if len(multi) > 1 or (stay and multi):
                return multi + (self._secondary_speed_cancels(c, multi) if c.cancels else [])
        for t in c.tables:
            items += self._closures_from_table(c, t, kind)
        events, leftover = self._pair_sections(c, kind, fallback=not items)
        if items:   # tabuľka (SROV) – z textu len udalosti s vlastným úsekom aj obdobím
            events = [e for e in events if e[0] is not None and e[1] is not None]
        if stay:
            # zrušenie nikdy nezruší úsek, ktorého výluka podľa e-mailu ostáva („okrem úseku …“, „… sa koná“)
            events = [e for e in events if e[0] is None or frozenset((e[0].a.name, e[0].b.name)) not in stay]
        single = len(events) == 1 and not items
        for sec, per, span in events:
            items.append(self._closure_item(c, kind, sec, per, span, single))
        if not items:
            items.append(self._closure_item(c, kind, None, c.periods[0] if c.periods else None,
                                            (0, len(c.text)), True))
        items = _dedupe(items)
        if kind != "vyluka_zrusenie" and not c.tables:     # súborný prehľad (tabuľka) sa číta po riadkoch
            self._unassigned_notes(c, items, leftover)
        if kind != "vyluka_zrusenie":
            # pôvodný termín za „namiesto“ či „pôvodne“ nie je ďalšou zmenou ani výlukou – len poznámka
            old = list(dict.fromkeys(sp for p in c.periods if p.pos >= c.subject_end and not c.in_quote(p.pos)
                                     and not c.in_table(p.pos) and (sp := c.old_term_span(p)) is not None))
            if old:
                shown = "“, „".join(one_line(c.text[a:b].strip().strip(",;(").strip(), 60) for a, b in old[:3])
                for it in items:
                    it.notes.append(f"Pôvodný termín uvedený v e-maile („{shown}“) nie je novým termínom – ako ďalšia "
                                    "udalosť sa nezapísal.")
        for it in items:
            it.evidence.pop("_pos", None)
            it.evidence.pop("_ppos", None)
        extra: list[ExtractedItem] = []
        if c.cancels and not c.speed_cancel:
            extra += self._secondary_speed_cancels(c, items)
        if kind != "vyluka_zrusenie" and (c.closure_cancels or c.moved_cancels):
            extra += self._secondary_closure_cancels(c, items, notes_only=kind == "vyluka_zmena")
        return items + extra

    def _cancel_items(self, c: _Ctx, stay: Optional[dict] = None) -> list[ExtractedItem]:
        """Zrušenie po vetách: každá veta s číslom či úsekom rušenej výluky je samostatné zrušenie, každý jej úsek
        zvlášť. Úsek, ktorého výluka podľa e-mailu ostáva (``stay``), sa nezruší – zrušenie bez úseku sa priradí len
        podľa čísla (pri výluke s viacerými úsekmi ho zápis odmietne a úsek vyberie dispečer)."""
        stay = stay or {}
        out: list[ExtractedItem] = []
        kept = sorted(stay.values())
        for s, e in c.closure_cancels:
            ref = c.cancel_ref([(s, e)], c.cancels)
            if not ref and len(c.closure_cancels) == 1:
                ref = c.cancel_ref([(0, c.subject_end), (0, len(c.text))], c.cancels)   # číslo z predmetu e-mailu
            all_secs = [x for x in c.sections(s, e) if not x.line_desc]
            secs = [x for x in all_secs if frozenset((x.a.name, x.b.name)) not in stay]
            if not ref and not secs:
                continue
            for sec in secs or [None]:
                data, notes = self._cancel_term(c, s, e, {"external_ref": ref,
                                                          "from_station": sec.a.text if sec else None,
                                                          "to_station": sec.b.text if sec else None})
                if kept:
                    notes.append("E-mail uvádza, že výluka v úseku " + ", ".join(kept) + " ostáva (koná sa) – zrušenie "
                                 "sa jej netýka; overte, ktorú výluku e-mail ruší.")
                found = (2 if ref else 1) + (1 if data.get("start") else 0)
                out.append(ExtractedItem("vyluka_zrusenie", data, _confidence(min(found, 2), 2, 0.05 if kept else 0),
                                         notes, {"external_ref": 0.8 if ref else 0.0},
                                         {"zrusenie": one_line(c.text[s:e], 160)}))
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
        (potvrdzuje dispečer). Upozornenie namiesto udalosti, keď sa číslo ani úsek nenašli, pri zmene výluky a keď
        ide o tú istú výluku ako nová výluka e-mailu bez vlastného rušeného termínu (presun napísaný ako „… sa nekoná,
        … bude …“). Zrušenie tej istej výluky s vlastným rušeným dňom („… dňa 25.11. sa ruší a presúva sa na 28.11.“,
        „… dňa 25.11. sa nekoná. Náhradný termín: 28.11.“) je ďalšia udalosť: zápis hľadá výluku v registri bez
        výluk z tohto e-mailu (nový termín nezruší) a zrušenie len časti viacdennej výluky odmietne s vysvetlením."""
        out: list[ExtractedItem] = []
        main_refs = {re.sub(r"[^0-9a-z/]", "", fold(str(it.data.get("external_ref") or ""))) for it in items} - {""}
        main_secs = {frozenset((fold(str(it.data.get("from_station") or "")), fold(str(it.data.get("to_station") or ""))))
                     for it in items}
        main_starts = {it.data.get("start") for it in items}
        for s, e in list(c.closure_cancels) + list(c.moved_cancels):
            sentence = one_line(c.text[s:e], 160)
            ref = c.cancel_ref([(s, e)])
            if not ref and (s, e) in c.moved_cancels:
                ref = c.cancel_ref([c.sentence_of(s)])
            if not ref and not _CLOSURE_NOUN.search(c.fc, s, e) and re.search(r"\btermin", c.fc[s:e]):
                ref = c.cancel_ref([(0, c.subject_end), (0, len(c.text))])   # „Termín … sa ruší“ – termín ROV e-mailu
            secs = [x for x in c.sections(s, e) if not x.line_desc]
            same_ref = bool(ref) and re.sub(r"[^0-9a-z/]", "", fold(ref)) in main_refs
            same = same_ref or any(frozenset((fold(x.a.text), fold(x.b.text))) in main_secs for x in secs)
            data, notes = self._cancel_term(c, s, e, {"external_ref": ref,
                                                      "from_station": secs[0].a.text if secs else None,
                                                      "to_station": secs[0].b.text if secs else None})
            own_term = bool(data.get("start")) and data.get("start") not in main_starts
            if notes_only or (same and not own_term) or (not ref and not secs):
                for it in items:
                    it.notes.append(f"E-mail ruší aj výluku („{sentence}“) – overte, ktorej výluky sa zrušenie týka, a ak "
                                    "platí, zrušte ju v registri výluk ručne.")
                    it.confidence = round(max(0.3, it.confidence - 0.05), 2)
                continue
            if same_ref and not secs and items:
                # rušený deň tej istej výluky (rovnaké číslo): úsek je úsekom novej výluky e-mailu
                data["from_station"] = data.get("from_station") or items[0].data.get("from_station")
                data["to_station"] = data.get("to_station") or items[0].data.get("to_station")
            notes.insert(0, "Zrušenie výluky uvedené v e-maile spolu s novou výlukou – overte, ktorú výluku ruší." if not
                         same_ref else "Rušený deň výluky uvedený v e-maile spolu s jej novým termínom – nový termín je "
                                       "samostatná udalosť; zrušenie pôvodného dňa overte (viacdennú výluku celú "
                                       "nezruší).")
            out.append(ExtractedItem("vyluka_zrusenie", data, _confidence(2, 2, 0.05), notes,
                                     {"external_ref": 0.8 if ref else 0.0}, {"zrusenie": sentence}))
        return out

    def _cancel_term(self, c: _Ctx, s: int, e: int, data: dict) -> tuple[dict, list[str]]:
        """Termín, ktorého sa zrušenie týka („výluka … dňa 25.11.2026 sa ruší“): začiatok a koniec rušeného obdobia.
        Zrušenie len časti viacdennej výluky („v ostatných dňoch platí bez zmeny“) celú výluku nezruší – zápis ho
        odmietne a dispečer upraví termín výluky v registri."""
        notes: list[str] = []
        data = dict(data)
        if _WHOLE_CANCEL.search(c.fq, s, e):
            # „Výluka podľa ROV … platná od 23.11.2026 sa v celom rozsahu ruší“ – dátum výluku len opisuje, ruší sa celá
            notes.append("Ruší sa celá výluka (v celom rozsahu) – dátum v texte výluku len označuje.")
            return data, notes
        parts = _pred_parts(c, s, e)
        cancel = [(a, b) for a, b, k, _r in parts if k == "cancel"]
        rels = [r for _a, _b, k, rs in parts if k == "cancel" for r in rs]
        stays = [(a, b) for a, b, k, _r in parts if k == "stay"]
        # termín časti vety, ktorá výluku ruší („… od 08:00 do 10:00 platí bez zmeny, od 10:00 do 14:00 sa ruší“) –
        # najprv bez vedľajšej vety, ktorá výluku len opisuje („Výluka, ktorá platí od 25.11. do 27.11., sa dňa 26.11.
        # ruší“); bez vlastného termínu termín celej vety bez častí o tom, čo ostáva (nie prvé obdobie odseku)
        own = (_span_term(c, cancel, skip=rels) or _span_term(c, cancel)) if cancel else None
        if own is None:
            own = _span_term(c, [(s, e)], skip=stays)
        own_secs = {frozenset((x.a.name, x.b.name)) for a, b in cancel for x in c.sections(a, b) if not x.line_desc}
        if not own_secs and not any(_OTHER_CLOSURES.search(c.fc, a, b) for a, b in cancel):
            # zrušenie bez vlastného úseku sa týka úseku vety („… od 08:00 do 10:00 platí, od 10:00 do 14:00 sa ruší“)
            own_secs = {frozenset((x.a.name, x.b.name)) for x in c.sections(s, e) if not x.line_desc}
        # o tom, čo ostáva v platnosti, hovorí e-mail pri tejto výluke – nie pri inej (iný úsek)
        same_stay = any(not ({frozenset((x.a.name, x.b.name)) for x in c.sections(a, b) if not x.line_desc} - own_secs)
                        for a, b in stays)
        remaining = [r for r in _remaining_sentences(c)
                     if not (own_secs and {frozenset((x.a.name, x.b.name)) for x in c.sections(*r)
                                           if not x.line_desc} - own_secs)]
        partial = bool(same_stay or remaining or any(_PARTIAL_CANCEL.search(c.fq, a, b) for a, b in cancel or [(s, e)]))
        if own is None:
            wins = [w for a, b in cancel for w in datumy.time_windows(c.fq, c.toks, a, b)]
            if wins:
                # „v čase od 10:00 do 14:00 sa ruší“ bez dňa v e-maile – bez termínu by potvrdenie zrušilo celú výluku
                shown = ", ".join(f"{a.tm:%H:%M} – {b.tm:%H:%M}" for a, b in wins[:3])
                notes.append(f"Zrušenie uvádza len čas {shown} bez dňa – deň sa nepodarilo určiť. Doplňte termín "
                             "zrušenia (začiatok a koniec), inak sa zruší celá výluka.")
            return data, notes
        start, end, how, listed = own
        data["start"] = _iso(start)
        if end is not None:
            data["end"] = _iso(end)
        if how == "den":
            if partial:
                notes.append(f"Ruší sa len časť výluky (deň {listed}) – ostatné dni výluky platia. Celá výluka sa "
                             "automaticky nezruší: upravte jej termín v registri výluk.")
            else:
                notes.append(f"Zrušenie uvádza deň {listed} – ak výluka trvá dlhšie, zruší sa len tento deň (celá "
                             "výluka sa automaticky nezruší); overte termín.")
        elif how in ("noc", "noc_okno"):
            window = "okno výluky z e-mailu" if how == "noc_okno" else \
                "čas výluky e-mail neuvádza, predpokladá sa noc 22:00 – 06:00"
            many = "," in listed
            notes.append(f"Zrušenie uvádza {'noci' if many else 'noc'} {listed} bez času – rušený termín je "
                         f"{_fmt_local(start)} – {_fmt_local(end)} ({window}); zrušenie sa týka len "
                         f"{'týchto nocí' if many else 'tejto noci'}, celá výluka sa automaticky nezruší. Overte, či "
                         f"výluka v {'tieto noci' if many else 'túto noc'} nezačína skôr alebo nekončí neskôr.")
        elif how == "od":
            notes.append(f"Zrušenie uvádza len začiatok {_fmt_local(start)} – ruší sa od tohto času do konca dňa "
                         f"{listed}; celá výluka sa automaticky nezruší. Overte termín zrušenia.")
        elif how in ("okno", "okno_den", "obdobie"):
            if partial and end is not None:
                notes.append(f"Ruší sa len časť výluky ({_fmt_local(start)} – {_fmt_local(end)}) – ostatný čas výluky "
                             "podľa e-mailu platí ďalej; celá výluka sa automaticky nezruší.")
            if how == "okno_den":
                notes.append(f"Rušený čas je z vety o zrušení, deň {listed} z inej vety e-mailu – overte termín "
                             "zrušenia.")
        return data, notes

    def _block_sections(self, c: _Ctx, bs: int, be: int) -> list[Section]:
        """Úseky odseku; bez úseku výluka v jednej dopravni („v žst. Ružomberok“, „v obvode stanice …“) – od = do.
        Dopravňa vo vete o výluke vedľa úseku („úplná výluka v žst. Kraľovany a v úseku Kraľovany – Ružomberok“) je
        ďalšia udalosť – výluka stanice zasahuje aj vlaky, ktoré úsekom nejdú (nie „vlaky čakajú v žst. …“); rovnako
        ďalšia dopravňa vo vete o výluke („Úplná výluka v žst. Štrba a v žst. Poprad-Tatry …“)."""
        secs = [s for s in c.sections(bs, be) if not s.line_desc and not c.in_table(s.start)]
        if not secs:
            st = _station_place(c, bs, be) or _locative_closure_place(c, bs, be)
            if st is None or c.in_table(st.start):
                return []
            secs = [Section(st, st, [], st.start, st.end)]
        for pos, st in _station_places(c, bs, be):
            if c.in_table(st.start) or c.in_quote(st.start) or any(x.start <= st.start < x.end for x in secs) or \
                    any(x.a.name == st.name and x.a is x.b for x in secs) or \
                    _EXCEPT_BEFORE.search(c.fc, max(bs, st.start - 40), st.start):
                continue                  # „… okrem stanice Ružomberok“ – výluka stanice nie je
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

        # obdobie či úsek vo vedľajšej informácii (náhradná doprava, porada, platnosť rozkazu, pripomienky, ďalšie
        # plánované výluky, premávka podľa GVD) – nie je termínom ani miestom výluky, ak e-mail má aj iný termín (úsek)
        all_pers = [p for p in c.periods if p.pos >= c.subject_end and not c.in_table(p.pos)
                    and (kind == "vyluka_zrusenie" or not (c.is_old_period(p) or c.in_cancel(p.pos)))]
        meta_pers = {id(p) for p in all_pers if _meta_clause(c, p.pos, p.end_pos)}
        if len(meta_pers) == len(all_pers):
            meta_pers = set()             # iný termín v e-maile nie je – prísnejší prípad, termín sa použije
        for bs, be in c.blocks[1:]:
            if c.in_table(bs):
                continue
            secs = self._block_sections(c, bs, be)
            # termín vety, ktorá ruší výluku („… dňa 25.11. od 8:00 do 12:00 sa nekoná“), je rušený termín – nie nová
            # výluka (rieši ho zrušenie)
            pers = [p for p in c.periods if bs <= p.pos < be and not c.in_table(p.pos)
                    and (kind == "vyluka_zrusenie" or not (c.is_old_period(p) or c.in_cancel(p.pos)))
                    and id(p) not in meta_pers]
            if secs and not pers and (cur_per is not None or cur_sec is not None) and \
                    all(_meta_clause(c, s.start, s.end) for s in secs):
                unused_secs.extend(secs)      # „Náhradná autobusová doprava v úseku …“ – nie je ďalší úsek výluky
                continue
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
            cands = [p for p in c.periods if kind == "vyluka_zrusenie" or not (c.is_old_period(p) or c.in_cancel(p.pos))]
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
        # termín vo vedľajšej informácii (náhradná doprava, porada, …), ktorý sa neprekrýva so žiadnym termínom výluky,
        # sa nestratí potichu – môže ísť o ďalší termín výluky („Dňa 27.11. … bude v úseku … zavedená NAD.“)
        kept = [p for _s, p, _sp in out if p is not None]
        day = timedelta(days=1)
        leftover += [f"termín „{one_line(c.text[p.pos:p.end_pos], 60)}“" for p in all_pers
                     if id(p) in meta_pers and (p.start, p.end) not in used_pers
                     and not any(p.start < (q.end or q.start + day) and q.start < (p.end or p.start + day)
                                 for q in kept)]
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
                    groups = _clause_groups(c, ss, se, occ, s_pers)
                    if groups is not None:
                        # striedané úseky a termíny v častiach vety („A – B dňa X; dňa Y bude výluka TV v úseku C –
                        # D“, „…, a dňa Y …“): páruje sa v rámci časti vety a druh či koľaj udalosti sa určí len
                        # z jej časti (nie z časti inej udalosti tej istej vety)
                        for g_occ, g_pers, g_span in groups:
                            got = len(events)
                            g_secs = list({(x.a.name, x.b.name): x for x in reversed(g_occ)}.values())[::-1]
                            if len(g_secs) == 1 or len(g_pers) == 1 or g_occ[-1].start < min(p.pos for p in g_pers) \
                                    or max(p.pos for p in g_pers) < g_occ[0].start:
                                pair(g_secs, g_pers)
                            else:
                                _alternate(g_occ, g_pers, g_span[1], pair)
                            for s_, p_, _sp in events[got:]:
                                c.own_spans[(s_.start, p_.pos)] = g_span
                    else:
                        # striedané zmienky a termíny: termíny za zmienkou úseku až po ďalšiu zmienku patria k nemu
                        _alternate(occ, s_pers, se, pair)
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

    @staticmethod
    def _own_span(c: _Ctx, sec: Optional[Section], per: Optional[Period], single: bool) -> Optional[tuple[int, int]]:
        """Časť vety udalosti, ktorá zdieľa vetu s inou udalosťou (:func:`_clause_groups`) – None, ak taká nie je."""
        if single or sec is None or per is None:
            return None
        return c.own_spans.get((sec.start, per.pos))

    @staticmethod
    def _event_sentences(c: _Ctx, sec: Optional[Section], per: Optional[Period], bs: int, be: int
                         ) -> list[tuple[int, int]]:
        """Vety časti textu ``bs:be``, ktoré môžu spresniť udalosť (druh, koľaj): nie veta o inom úseku a nie veta
        s iným termínom – tá patrí inej udalosti toho istého odseku."""
        out: list[tuple[int, int]] = []
        pos = c.period_positions()
        for s, e in c.sentences():
            s, e = max(s, bs), min(e, be)
            if e <= s:
                continue
            if sec is not None and not (s <= sec.start < e):
                names = c.place_names(s, e)
                if names and (sec.a.name, sec.b.name) not in names:
                    continue
            if per is not None and not (s <= per.pos < e) and any(
                    (p.start, p.end) != (per.start, per.end)
                    for p in c.periods_sorted[bisect.bisect_left(pos, s):bisect.bisect_left(pos, e)]):
                continue
            out.append((s, e))
        return out

    def _restriction(self, c: _Ctx, f: str, sec: Optional[Section], per: Optional[Period], bs: int, be: int,
                     single: bool, own: Optional[tuple[int, int]] = None) -> tuple[str, bool]:
        """Druh obmedzenia udalosti z viet, ktoré ju ohlasujú: najprv veta s jej termínom, potom veta s jej úsekom,
        až potom odsek či celý text. Zmienka o výluke („po výluke“, „počas výluky“) výluku neohlasuje; časť vety
        o pomalej jazde sa pri výluke do druhu nepočíta.

        Keď vlastné vety udalosti nesúhlasia, platí prísnejší prípad: výluka (aj bez uvedeného druhu) má prednosť pred
        pomalou jazdou – výluka sa nikdy nezmení na pomalú jazdu len preto, že text spomína aj pomalú jazdu, ani keď
        veta o pomalej jazde na tom istom úseku je pred vetou o výluke („Pomalá jazda … platí do 19.11. Následne dňa
        20.11. … úplná výluka v úseku …“). Veta s termínom, ktorá hovorí o inom úseku („výluka Kysak – Košice 20.11. …
        Počas výluky platí v úseku Kysak – Kostoľany pomalá jazda“), vetou udalosti nie je – druh je z vety o jej
        úseku. ``own`` – časť vety, ktorú udalosť zdieľa s inou udalosťou (druh len z tejto časti)."""
        sentences = c.sentences()
        if own is not None:
            bs, be = own
            sentences = [(max(s, bs), min(e, be)) for s, e in sentences if s < be and e > bs]

        def sent_of(pos: int) -> Optional[tuple[int, int]]:
            return next(((s, e) for s, e in sentences if s <= pos < e), None)

        ps, ss = (sent_of(per.pos) if per else None), (sent_of(sec.start) if sec else None)
        spans = [x for x in (ps, ss) if x is not None]
        later: list[tuple[int, int]] = []
        if ps is not None and ss is not None and ps != ss and sec.a is not sec.b:
            names = {(x.a.name, x.b.name) for x in c.sections(*ps) if not x.line_desc}
            if names and (sec.a.name, sec.b.name) not in names:
                spans, later = [ss], [ps]          # termín patrí vete o inom úseku
        own: list[str] = []
        for s, e in spans:
            if f[s:e] not in own:
                own.append(f[s:e])
        # odsek bez viet o inej udalosti (iný úsek či iný termín) – druh inej udalosti tohto odseku nie je druhom
        # tejto („Výluka v úseku A – B dňa 20.11. … Dňa 21.11. … bude napäťová výluka TV v úseku B – C.“)
        rest = " ".join(f[s:e] for s, e in self._event_sentences(c, sec, per, bs, be)) if not single \
            else f[c.subject_end:] + " " + f[:c.subject_end]

        def judge(text: str) -> Optional[tuple[str, bool]]:
            plain = _blank_cancel_mentions(_CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), text))
            if _closure_words(plain):
                return restriction_kind(_blank_speed_clauses(plain) if _SPEED_WORDS.search(plain) else plain)
            if _SPEED_WORDS.search(plain):
                return "pomala_jazda", False
            return None

        res = [r for r in map(judge, own) if r is not None]
        closures = [r for r in res if r[0] != "pomala_jazda"]
        if closures:
            explicit = next((r for r in closures if not r[1]), None)
            if explicit is not None:
                return explicit           # druh výluky výslovne uvedený vo vete udalosti
            whole = judge(rest)           # „výluka“ bez druhu – druh môže byť uvedený inde v odseku („1. TK“)
            return whole if whole is not None and whole[0] != "pomala_jazda" and not whole[1] else closures[0]
        if res:
            return res[0]                 # vety udalosti hovoria len o pomalej jazde
        for s, e in later:
            r = judge(f[s:e])
            if r is not None:
                return r
        return judge(rest) or restriction_kind(rest)

    def _closure_item(self, c: _Ctx, kind: str, sec: Optional[Section], per: Optional[Period],
                      span: tuple[int, int], single: bool) -> ExtractedItem:
        bs, be = (0, len(c.text)) if single else span
        # časť vety, ktorú udalosť zdieľa s inou udalosťou – druh a koľaj len z nej (ref, dôvod a trať z odseku)
        own_span = self._own_span(c, sec, per, single)
        ks, ke = own_span or (bs, be)
        # výluka v e-maile, ktorý ruší aj pomalú jazdu či inú výluku: vety o zrušení sa do druhu, rýchlosti ani
        # čísla nerátajú (pri zrušení pomalej jazdy samotnom sú to práve ony)
        if kind == "vyluka_zrusenie":
            skip = [] if c.speed_cancel else list(c.cancels)
        else:
            skip = list(c.cancels) + list(c.closure_cancels)
        f = _blank(c.fc, skip) if skip else c.fc
        ctx_f = " ".join(f[s:e] for s, e in self._event_sentences(c, sec, per, ks, ke)) if not single else f
        rkind, assumed = self._restriction(c, f, sec, per, bs, be, single, own_span)
        item_kind = kind
        if kind == "vyluka_nova" and rkind == "pomala_jazda":
            item_kind = "pomala_jazda"
        if kind == "pomala_jazda" and rkind != "pomala_jazda" and _closure_words(
                _blank_cancel_mentions(_CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), ctx_f))):
            item_kind = "vyluka_nova"
        if item_kind == "pomala_jazda":
            rkind, assumed = "pomala_jazda", False
        notes: list[str] = []
        fc: dict = {}
        ev: dict = {}
        penalties = 0.0
        # kontrola druhu: text udalosti (jej vety, nie citácia, zmienka „počas výluky“ či veta o inej udalosti) so
        # slovami úplného uzavretia a priechodný druh (koľaj, trakčné vedenie, obmedzenie) – platí prísnejší druh
        # (úplná výluka) s poznámkou a nižšou istotou, aby udalosť potvrdil dispečer
        strict_cue = None
        if item_kind in ("vyluka_nova", "vyluka_zmena") and rkind in ("kolaj", "trakcne_vedenie", "obmedzenie"):
            fq = _blank(f, c.quotes) if c.quotes else f
            spans = [(0, len(fq))] if single else self._event_sentences(c, sec, per, ks, ke)
            for s0, e0 in spans:
                plain = _blank_cancel_mentions(_CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), fq[s0:e0]))
                cue = _strong_full_cue(_blank_speed_clauses(plain) if _SPEED_WORDS.search(plain) else plain)
                if cue is None and rkind == "trakcne_vedenie" and not _DIESEL_OK.search(plain):
                    m = _DIESEL_BLOCKED.search(plain)   # výluka TV a „jazda motorových vlakov nie je možná“
                    cue = (m.start(), m.end()) if m else None
                if cue is not None:
                    strict_cue = one_line(c.text[s0 + cue[0]:s0 + cue[1]], 80)
                    break
            if strict_cue is not None:
                rkind, assumed = "uplna", False
                notes.append(f"Overte druh výluky – text obsahuje „{strict_cue}“ (úplné uzavretie): zapísaná je úplná "
                             "výluka (prísnejší druh), nie len výluka koľaje, trakčného vedenia či obmedzenie.")
                penalties += 0.05
        elif item_kind in ("vyluka_nova", "vyluka_zmena") and rkind == "uplna" and not assumed:
            # úplná výluka len podľa vety o vlakoch, ktoré úsekom neprejdú, hoci text uvádza aj koľaj či trakčné vedenie
            # („Výluka 1. TK …, vlaky nebudú premávať“) – prísnejší druh ostáva, dispečer ho overí
            fq = _blank(f, c.quotes) if c.quotes else f
            for s0, e0 in [(0, len(fq))] if single else self._event_sentences(c, sec, per, ks, ke):
                plain = _blank_cancel_mentions(_CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), fq[s0:e0]))
                hit = _full_closure_hit(plain)
                if hit and hit[2] == "nomove" and (_TRACK_NUMBER.search(plain) or _TV_WORDS.search(plain)):
                    cue = one_line(c.text[s0 + hit[0]:s0 + hit[1]], 80)
                    notes.append(f"Overte druh výluky – text uvádza koľaj či trakčné vedenie, ale aj „{cue}“: zapísaná "
                                 "je úplná výluka (prísnejší druh), nie len výluka koľaje či trakčného vedenia.")
                    penalties += 0.05
                    break
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
                    notes.append(_name_note(m))
            if sec.a is sec.b:
                notes.append(f"Výluka v dopravni {sec.a.name} – úsek je len táto dopravňa (všetky jej koľaje).")
            ev["usek"] = c.line_of(sec.start)
            ev["_pos"] = sec.start
        cancel_term: Optional[tuple[dict, list]] = None
        if item_kind == "vyluka_zrusenie" and not c.speed_cancel:
            # rušený termín z vety o zrušení (nie dni, ktoré ostávajú v platnosti); inak termín odseku
            spans = c.closure_cancels or ([(bs, be)] if not single else [(c.subject_end, len(c.text))])
            cancel_term = self._cancel_term(c, spans[0][0], spans[-1][1], {})
        # obdobie odseku pri zrušení s vlastným rušeným termínom („Výluka, ktorá je naplánovaná od 25.11. do 27.11., sa
        # dňa 26.11. ruší“) termínom nie je – jeho poznámky (celý deň, nepriradený dátum) by dispečera mýlili
        own_cancel = cancel_term is not None and cancel_term[0].get("start") and per is not None and \
            (cancel_term[0].get("start"), cancel_term[0].get("end")) != (_iso(per.start), _iso(per.end))
        if per:
            ev["_ppos"] = per.pos
            data["start"], data["end"] = _iso(per.start), _iso(per.end)
            if per.daily_from:
                data["daily_from"], data["daily_to"] = per.daily_from, per.daily_to
            if "do_odvolania" in per.flags:
                data["until_revoked"] = True
        if per and not own_cancel:
            notes += _flag_notes(per)
            if "rok_doplneny" in per.flags:
                penalties += 0.03
            if "zle_poradie" in per.flags:
                penalties += 0.1
            fc["start"] = 0.85
            fc["end"] = 0.85 if per.end else 0.0
            ev["obdobie"] = c.line_of(per.pos)
            blk = c.block_of(per.pos)
            loose = c.unused_times(*blk, skip_cancel=item_kind != "vyluka_zrusenie") if blk else []
            if loose:          # čas v tom istom odseku, ktorý sa nikam nepriradil – termín nemusí byť úplný
                penalties += 0.1
                notes.append("V texte je čas " + ", ".join(f"„{x}“" for x in loose[:3]) +
                             ", ktorý sa nepodarilo priradiť k obdobiu – overte termín.")
            loose_days = c.unused_dates(*blk, skip_cancel=item_kind != "vyluka_zrusenie") if blk else []
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
        if not ref and not c.first_group(_CLOSURE_REF_RES, c.subject_end, len(c.text), skip):
            ref = c.first_group(_CLOSURE_REF_RES[:1], 0, c.subject_end)   # ROV z predmetu, v tele iné číslo nie je
        if ref:
            data["external_ref"] = ref
        data["line_ref"] = c.line_ref(a, b)
        own = c.block_of(per.pos) if per is not None and sec is not None else None
        if own is not None and own == c.block_of(sec.start):
            own = None                    # úsek aj termín v tom istom odseku – platí celý odsek
        data["track"] = (c.track(*own) if own else None) or (c.track(a, b) if single else next(
            (t for s, e in self._event_sentences(c, sec, per, *(own_span or (a, b))) if (t := c.track(s, e))), None))
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
                term, extra = cancel_term if cancel_term is not None else ({}, [])
                if not term.get("start") and per is not None and data.get("start") and \
                        any(a <= per.pos < b for a, b in _remaining_sentences(c)):
                    # termín odseku je z vety o tom, čo ostáva („… sa uskutoční len od 08:00 do 10:00“) – nie je
                    # rušeným termínom (zrušenie by zrušilo práve časť, ktorá ostáva)
                    data.pop("start", None)
                    data.pop("end", None)
                    notes.append("Zrušenie neuvádza rušený termín a e-mail uvádza, že časť výluky ostáva – overte, "
                                 "ktorý čas sa ruší, a doplňte termín zrušenia.")
                if not term.get("start") and not data.get("start"):
                    # rušený deň len v predmete („ROV č. … – zrušenie výluky dňa 25.11.2026“) – nestratí sa: bez neho
                    # by potvrdenie zrušilo celú (aj viacdennú) výluku
                    subj_term, subj_extra = self._cancel_term(c, 0, c.subject_end, {})
                    if subj_term.get("start"):
                        term, extra = subj_term, subj_extra
                if term.get("start"):
                    data["start"], data["end"] = term["start"], term.get("end")
                    notes += extra
                else:
                    notes += [n for n in extra if n.startswith("Zrušenie uvádza len čas")]
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
                    if vals and (all(datumy.parse_datetime_text(v, c.ref) for v in vals) or (
                            "date" in cols and all(_TIME_CELL.match((v.strip().split() or [""])[0]) for v in vals))):
                        cols.setdefault(tk, idx)
                    elif vals and any(c.st.match(v, allow_fuzzy=False) for v in vals):
                        cols.setdefault(sk, idx)
        if "date" in cols and "start" not in cols:
            cols["start"] = cols["date"]          # len deň – výluka celý deň (ak nie je čas)
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
            day = datumy.parse_datetime_text(cell("date"), c.ref) if "date" in cols and cell("date") else None
            term = _cell_term(c, cell("date"), notes) if day is not None else None
            if term is not None:
                # stĺpec „Termín“/„Dátum“ s celým termínom („24.11. – 26.11.2026“, „24.11.2026 08:00 – 25.11.2026
                # 14:00“, „24.11.2026 od 08:00 do 14:00“) – obdobie ako v texte, nie len prvý deň
                start, end, cell_time = term
                day = None
                daily = cell("daily")
                tms = re.findall(r"(\d{1,2})\s?[:.]\s?(\d{2})", daily) if daily and not cell_time else []
                if len(tms) >= 2:
                    t_from, t_to = time(int(tms[0][0]) % 24, int(tms[0][1])), time(int(tms[1][0]) % 24, int(tms[1][1]))
                    data["daily_from"], data["daily_to"] = f"{t_from:%H:%M}", f"{t_to:%H:%M}"
                    start = datetime.combine(start.date(), t_from)
                    last = end.date() - timedelta(days=1)        # posledný deň termínu (koniec je deň za ním 00:00)
                    end = datetime.combine(last + timedelta(days=1 if t_to <= t_from else 0), t_to)
                    notes.append(f"Opakuje sa denne {t_from:%H:%M} – {t_to:%H:%M}.")
            if day is not None:
                # deň v samostatnom stĺpci, čas od a do v ďalších stĺpcoch („24.11.2026 | 22:00 | 05:00“)
                t_from = _cell_time(cell("start")) if cols.get("start") != cols.get("date") else None
                t_to = _cell_time(cell("end")) if cell("end") else None
                if start is None or cols.get("start") == cols.get("date"):
                    start = datetime.combine(day.date(), time(*map(int, t_from.split(":")))) if t_from else \
                        datetime.combine(day.date(), time(0, 0))
                if t_to and (end is None or not re.search(r"\d{1,2}\.\s?\d{1,2}\.", cell("end"))):
                    end = datetime.combine(day.date(), time(*map(int, t_to.split(":"))))
                    if end <= start:
                        end += timedelta(days=1)
                        notes.append("Koniec je na nasledujúci deň (cez polnoc).")
                elif end is None and not t_from:
                    end = datetime.combine(day.date() + timedelta(days=1), time(0, 0))
                    notes.append("Čas nie je uvedený – predpokladá sa celý deň (00:00 – 24:00).")
            if end is not None and day is None and term is None and not datumy.HAS_TIME.search(cell("end")):
                end += timedelta(days=1)       # dátum bez času = do konca dňa
                notes.append("Koniec bez času – platí do konca uvedeného dňa.")
            daily = cell("daily") if term is None else ""
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
            # obe koľaje v stĺpci koľaje („1.+2. TK“, „1. a 2. TK“, „obe“) či druh „úplná“ – úplná výluka (nie výluka
            # jednej koľaje); trakčné vedenie oboch koľají ostáva výlukou TV
            tcell, kcell = fold(cell("track")), fold(cell("kind"))
            both = bool(re.search(r"\bob[eiao]", tcell)) or len(set(re.findall(r"\d", tcell))) >= 2
            tv_cell = bool(re.search(r"\btv\b|trakcn|napat|napet", tcell + " " + kcell))
            if not speed_col and rkind in ("kolaj", "obmedzenie") and (
                    (both and not tv_cell) or re.match(r"\s*upln", kcell)):
                rkind, assumed = "uplna", False
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
            elif len(c.tables) == 1:
                # jediná tabuľka bez stĺpca čísla: jediné číslo ROV v texte e-mailu („nasledovné výluky podľa ROV č.
                # 3207/2026:“) patrí všetkým riadkom – nie číslo súborného rozkazu (SROV), ten má výluk viac
                heads = {c.text[m.start(1):m.end(1)].rstrip(".,;)") for m in _CLOSURE_REF_RES[0].finditer(c.f)
                         if not re.match(r"srov|suborn", m.group(0)) and not c.in_table(m.start())}
                if len(heads) == 1:
                    data["external_ref"] = heads.pop()
            if cell("line_ref"):
                m = re.search(r"\d{3}[a-zA-Z]?", cell("line_ref"))
                data["line_ref"] = m.group(0).upper() if m else cell("line_ref")
            tr = cell("track")
            if tr:
                m = re.search(r"\d", tr)
                data["track"] = "obe" if both else (m.group(0) if m else tr)
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
                # číslo vlaku z tela (riadok s jeho ETA) má prednosť pred zmienkou v predmete („Avízo vlakov 47250 …“)
                body = next((u for u in trains if u[0] == t[0] and u[2] >= c.subject_end), None)
                uniq_trains.append(body or t)
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
        """Mimoriadnosť (alebo oznámenie o jej skončení). E-mail s viacerými mimoriadnosťami v samostatných odsekoch či
        položkách zoznamu, každou na inom mieste („1) Mimoriadnosť v ŽST Vrútky … 2) Mimoriadnosť v úseku Štrba –
        Poprad-Tatry …“), dá udalosť pre každú – nie len pre prvú."""
        blocks = _incident_blocks(c)
        if len(blocks) >= 2:
            items: list[ExtractedItem] = []
            for bs, be in blocks:
                sub = _Ctx(MailDocument(subject=c.doc.subject, body=c.text[bs:be], sender=c.doc.sender,
                                        sender_name=c.doc.sender_name, received_at=c.doc.received_at))
                items += self._one_incident(sub, kind)
            places = {(fold(str(i.data.get("from_station") or "")), fold(str(i.data.get("to_station") or "")))
                      for i in items if i.kind == "mimoriadnost"}
            if len(places) >= 2:
                for it in items:
                    it.notes.append(f"E-mail ohlasuje {len(blocks)} mimoriadnosti na rôznych miestach – každá je "
                                    "samostatná udalosť.")
                return items
        return self._one_incident(c, kind)

    def _one_incident(self, c: _Ctx, kind: str) -> list[ExtractedItem]:
        notes: list[str] = []
        fc: dict = {}
        ev: dict = {}
        data: dict = {}
        penalties = 0.0
        place, other = _incident_place(c)
        if place is not None:
            a, b = place.a, place.b
            # skloňovaný tvar („Vo Vrútkach“) – zapíše sa názov dopravne v sieti (Vrútky), nie tvar z textu
            data["from_station"], data["to_station"] = [m.name if m.method == "sklonovanie" else m.text for m in (a, b)]
            for m in {id(a): a, id(b): b}.values():
                if m.method == "sklonovanie":
                    notes.append(_name_note(m))
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
        if freight is not None and freight[4] and freight[2] is not None:
            # „nákladné vlaky môžu ísť od 10:30“ v čase obnovenia (nie neskôr) – obmedzenie netrvá
            reopen = datumy.first_datetime_after(c.fq, c.toks, end_notice.start(), 400, fallback_day=c.ref)
            if reopen is not None and freight[2] <= reopen[0]:
                freight = None
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
        tab = _incident_table(c)
        if tab is not None:
            # tabuľka mimoriadnosti („Miesto | Začiatok | Predpokladaný koniec | Dôsledok“) – miesto a čas z jej riadku
            t_place, t_start, t_end, t_est = tab
            if t_place is not None and not data.get("from_station"):
                data["from_station"], data["to_station"] = t_place
                fc["from_station"] = fc["to_station"] = 0.75
            if t_start is not None:
                data["start"] = _iso(t_start)
                fc["start"] = 0.8
            if t_end is not None:
                data["end"] = _iso(t_end)
                fc["end"] = 0.7 if t_est else 0.8
                if t_est:
                    notes.append("Koniec je predpokladaný (odhad ŽSR), nie záväzný.")
            notes.append("Údaje z tabuľky v e-maile.")
        end_m = None if tab is not None and tab[2] is not None else _END_LABEL.search(c.fq, c.subject_end)

        def estimate(t: datumy.Temporal) -> bool:     # čas predpokladaného konca („predpokladané obnovenie … o 9:00“)
            return bool(end_m) and end_m.start() <= t.start <= end_m.end() + 60

        # obdobie „od … do …“ (mimoriadnosť nie je do odvolania) – len z vety o mimoriadnosti, nie okno náhradnej
        # dopravy či kontaktu na dispečera („Náhradná autobusová doprava od 6:00 do 22:00“)
        sentences = _sentence_spans(c.text, c.fq, c.subject_end, len(c.text))
        pers = [p for p in c.periods if p.pos >= c.subject_end and not c.in_table(p.pos) and not c.is_old_period(p)
                and _incident_clause(c, sentences, p.pos)] or [p for p in c.periods if p.pos < c.subject_end]
        start_tok = None
        if tab is not None and tab[1] is not None:
            pers = []                     # začiatok aj koniec sú z tabuľky
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
        elif not data.get("start"):
            cands = [t for t in c.toks if t.start >= c.subject_end and t.kind == "datetime" and not estimate(t)]
            start_tok = next((t for t in cands if _incident_clause(c, sentences, t.start)), None) or \
                next(iter(cands), None)
            if start_tok is not None:
                data["start"] = _iso(start_tok.dt)
                fc["start"] = 0.8
                ev["zaciatok"] = c.line_of(start_tok.start)
        start_day = start_tok.day if start_tok else (pers[0].start.date() if pers else None)
        time_start: Optional[datetime] = None
        if not data.get("start"):
            # oznámenie KD len s časom („Od 06:05 hod. je v žst. Fiľakovo porucha …“) – deň je deň prijatia e-mailu;
            # čas neskorší ako prijatie o viac ako hodinu patrí predchádzajúcemu dňu (nočná mimoriadnosť ohlásená po
            # polnoci), len ak by ešte trvala a veta nehovorí o budúcnosti („Dnes od 14:00 do 18:00 hod. bude …“ je
            # dnešné okno, „zajtra“ nasledujúci deň); overí dispečer
            tm = next((t for t in c.toks if t.start >= c.subject_end and t.kind == "time" and t.tm is not None
                       and not estimate(t) and _incident_clause(c, sentences, t.start)), None)
            if tm is not None:
                from ..cas import local as _local

                rec = _local(c.doc.received_at).replace(tzinfo=None) if c.doc.received_at else \
                    datetime.combine(c.ref, time(12, 0))
                # „dnes od 07:00 do 15:00 hod.“ – koniec v tej istej vete za „do“
                s_e = next(((s, e) for s, e in sentences if s <= tm.start < e), (tm.start, tm.start))
                tm2 = next((t for t in c.toks if tm.start < t.start < s_e[1] and t.kind == "time" and t.tm
                            and re.search(r"(?:\bdo|\baz|-)\s*$", c.fq[max(0, t.start - 4):t.start])), None)
                sent = c.fq[s_e[0]:s_e[1]]
                start_dt = datetime.combine(rec.date(), tm.tm)
                which = "deň prijatia e-mailu"
                if re.search(r"\bzajtra\b|\bzajtrajs\w*", sent):
                    start_dt += timedelta(days=1)
                    which = "nasledujúci deň (zajtra)"
                elif start_dt > rec + timedelta(hours=1) and not _FUTURE_TENSE.search(sent):
                    prev = start_dt - timedelta(days=1)
                    prev_end = None
                    if tm2 is not None:
                        prev_end = datetime.combine(prev.date(), tm2.tm)
                        prev_end += timedelta(days=1) if prev_end <= prev else timedelta(0)
                    if prev_end is None or prev_end > rec:
                        start_dt, which = prev, "predchádzajúci deň (noc pred prijatím e-mailu)"
                data["start"] = _iso(start_dt)
                start_day, time_start = start_dt.date(), start_dt
                fc["start"] = 0.6
                penalties += 0.05
                ev["zaciatok"] = c.line_of(tm.start)
                notes.append(f"Dátum začiatku nie je uvedený – predpokladá sa {which} "
                             f"({start_dt:%d.%m.%Y %H:%M}); overte začiatok.")
                if tm2 is not None:
                    end_dt = datetime.combine(start_dt.date(), tm2.tm)
                    if end_dt <= start_dt:
                        end_dt += timedelta(days=1)
                    data["end"] = _iso(end_dt)
                    fc["end"] = 0.6
        if end_m and not (pers and not data.get("end")):
            got = datumy.first_datetime_after(c.fq, c.toks, end_m.end(), 80, fallback_day=start_day)
            if got:
                end_dt = got[0]
                start_dt = start_tok.dt if start_tok is not None else (min(p.start for p in pers) if pers else
                                                                       time_start)
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
        if re.search(r"zakaz\w*\s+(?:jazdy|vjazdu|prejazdu)\s+(?:\w+\s+){0,3}?(?:s\s+)?(?:nebezpecn|rid\b)", c.fq):
            notes.append("Zákaz platí pre vlaky s nebezpečným tovarom (RID) – zapísaný je ako mimoriadnosť pre všetky vlaky "
                         "(prísnejší prípad); ak vlak RID nevezie, overte to a upravte záznam.")
            penalties += 0.05
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
        freight = _freight_mentions(c)
        if freight:
            # e-mail spomína aj nákladné vlaky a obmedzenie pre ne výslovne neukončuje – mimoriadnosť sa nesmie ukončiť
            # skôr, než môžu ísť aj nákladné vlaky
            notes = [f"Oznámenie o obnovení prevádzky{where}" + (f" od {reopen:%d.%m.%Y %H:%M}" if reopen else "") +
                     ", ale e-mail spomína aj nákladné vlaky („" + "“, „".join(freight[:2]) + "“) – pre ne môže "
                     "obmedzenie trvať. Mimoriadnosť v registri výluk neukončujte, kým ŽSR nepotvrdí, že môžu ísť aj "
                     "nákladné vlaky; do registra sa nič nezapisuje automaticky."]
        else:
            notes = [f"Oznámenie o skončení mimoriadnosti{where} (prevádzka obnovená" +
                     (f" od {reopen:%d.%m.%Y %H:%M}" if reopen else "") + ") – nie je to nová mimoriadnosť a do "
                     "registra výluk sa nezapisuje. Ak ŽSR mimoriadnosť ukončila, ukončite ju v registri výluk ručne: "
                     "automaticky sa nezruší, lebo oznámenie nemá číslo a podľa úseku by sa mohla zrušiť iná výluka."]
        evidence = {**ev, "skoncenie_mimoriadnosti": c.line_of(m.start())}
        if got:
            evidence["obnovenie"] = c.line_of(got[1].start)
        items = [ExtractedItem("ine", {}, 0.5 if not freight else 0.4, notes, {}, evidence)]
        if _SPEED_AFTER.search(c.fq, c.subject_end) and c.speed(c.subject_end, len(c.text)):
            items.append(self._speed_after_incident(c, sec, reopen, fc, ev))
        return items

    def _freight_restriction(self, c: _Ctx, place: dict, fc: dict, ev: dict, notes: list[str], penalties: float,
                             end_notice: Optional[re.Match], freight: tuple) -> ExtractedItem:
        """Prevádzka obnovená len čiastočne – pre nákladné vlaky (vlaky s RID) obmedzenie trvá („Pre nákladné vlaky
        platí zákaz jazdy cez stanicu do 14:00“, „nákladné vlaky do 14:00 nepremávajú“). Dopravca vozí nákladné
        vlaky: pre neho mimoriadnosť trvá do uvedeného času – zapíše sa ako mimoriadnosť (nie oznámenie o skončení,
        po ktorom by dispečer mimoriadnosť ukončil a zrušil jediné obmedzenie)."""
        s, e, until, label, _resume = freight
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
            r"|^plati\s+do|^ukoncenie|^do\s+(?:datum|dna)|^predpoklad\w*\s+(?:koniec|ukoncen\w*|obnoven\w*)"),
    ("daily", r"^denne|^casov\w*\s+okno|^cas\s+vyluky|^denny\s+cas|^cas$"),
    # deň výluky v samostatnom stĺpci, čas od a do v ďalších stĺpcoch („Dátum | Od | Do“, „Dátum | Čas od | Čas do“)
    ("date", r"^datum$|^den$|^dna$|^termin$|^datum\s+vyluky$|^den\s+vyluky$"),
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


def _cell_term(c: _Ctx, text: str, notes: list[str]) -> Optional[tuple[datetime, datetime, bool]]:
    """Bunka stĺpca dňa s celým termínom – rozsah dní, dni zoznamu či dátum s časom: (začiatok, koniec, bunka má
    čas). None pri jednom dni bez času (čas je v ďalších stĺpcoch „Od | Do“)."""
    ft = fold(text)
    toks = datumy.scan(ft, c.ref)
    days = sorted({t.day for t in toks if t.day})
    has_time = any(t.kind in ("datetime", "time") for t in toks)
    if len(days) < 2 and not has_time:
        return None
    pers = [p for p in datumy.periods(ft, toks) if p.end is not None]
    if pers:
        return min(p.start for p in pers), max(p.end for p in pers), any(p.start.time() != time(0, 0) or
                                                                          p.end.time() != time(0, 0) for p in pers)
    if len(days) >= 2:
        notes.append(f"Bunka termínu „{one_line(text, 60)}“ uvádza viac dní – zapísané je celé obdobie od prvého po "
                     "posledný deň (prísnejší prípad); overte termín.")
        return datetime.combine(days[0], time(0, 0)), datetime.combine(days[-1] + timedelta(days=1), time(0, 0)), False
    return None


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
    # 3) próza: „s odchodom zo stanice Nové Zámky o 14:35 a príchodom do stanice Štúrovo o 15:20“
    prose = []
    for which, rx in (("dep", _PROSE_DEP), ("arr", _PROSE_ARR)):
        for m in rx.finditer(c.f, c.subject_end):
            st = c._station_at(tokens(c.text[m.end():min(len(c.text), m.end() + 80)], m.end()))
            tm = re.match(r"\s*(?:o|v|vo|at)?\s*(\d{1,2}\s?[:.]\s?\d{2})", c.text[st.end:st.end + 25]) if st else None
            if st and tm:
                prose.append((m.start(), which, st, _cell_time(tm.group(1))))
    deps = [x for x in prose if x[1] == "dep"]
    arrs = [x for x in prose if x[1] == "arr"]
    if deps and arrs:
        d, a = min(deps), max(arrs)
        if d[2].name != a[2].name:
            return [(d[2], None, d[3]), (a[2], a[3], None)], "veta"
    # 4) reťaz „Trasa: A – B – C“
    for ls, le in _lines(c.text):
        for sec in c.sections(ls, le):
            chain = [sec.a, *sec.via, sec.b]
            if len(chain) >= 2 and (sec.via or re.search(r"tras|relaci|smer|cez", c.f[ls:le])):
                return [(s, None, None) for s in chain], "retaz"
    return [], ""


# odchod a príchod vlaku v texte („s odchodom zo stanice A o 14:35 a príchodom do stanice B o 15:20“)
_PROSE_DEP = re.compile(r"\bodchod\w*\s+(?:z[o]?\s+)?(?:stanice\s+|zst\.?\s+|z\.\s?st\.\s+)?")
_PROSE_ARR = re.compile(r"\bprichod\w*\s+(?:do\s+)?(?:stanice\s+|zst\.?\s+|z\.\s?st\.\s+)?")


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
    r"|\beta\b|cas\s+prichodu|prichod\s+(?:vlaku\s+)?na\s+hranic\w*|prijezd\s+(?:vlaku\s+)?na\s+hranic\w*"
    # anglické avízo partnera („is expected to arrive at Komárno border station on 22.11.2026 at 14:20“)
    r"|\bexpected\s+(?:to\s+)?arrive(?:\s+at)?|\bestimated\s+(?:time\s+of\s+)?arrival(?:\s+at)?")
# pôvodný čas v aktualizácii avíza („(pôvodne 13:35)“, „namiesto 13:35“) – nie je novým ETA
_OLD_TIME_BEFORE = re.compile(r"(?:povodn\w*|puvodn\w*|namiesto|misto|predtym|doteraz\w*|predchadzajuc\w*|stary|stare"
                              r"|stareho)\s*:?\s*(?:o\s+|v\s+|cas\s+|eta\s+)?\(?\s*$")
_END_LABEL = re.compile(
    r"predpoklad\w*\s+(?:cas\s+)?(?:obnoveni\w*|ukonceni\w*|odstraneni\w*|sprejazdneni\w*|uvolneni\w*|zprovozneni\w*"
    r"|obnova|koniec|konec)|obnoveni\w*\s+(?:prevadzky|dopravy|provozu)\s+(?:sa\s+)?(?:predpoklad\w*\s+)?(?:o|na|do)?"
    r"|odstranenie\s+(?:poruchy|nasledkov)\s+(?:sa\s+)?predpoklad\w*")
_INCIDENT_WORDS = re.compile(r"mimoriadn|mimoradn|porucha|poruchy|poruchu|nehod|neprejazd|nesjizd|vykolaj|vykolej|"
                             r"zrazk|prerusen|preruseni|pretrhnut|lom\s+kolajnice|poskoden|zakaz\w*\s+(?:jazd|vjazd"
                             r"|prejazd)\w*|" + _BAN_TRAINS)
# mimoriadnosť v jednej dopravni: „v (železničnej) stanici X“, „v žst. X“, „v ŽST X“, „v obvode stanice X“,
# „v dopravni X“, „v zastávke X“ (česky „ve stanici X“, „v železniční stanici X“)
_STATION_PLACE = re.compile(r"\bv[eo]?\s+(?:obvode\s+|obvodu\s+)?(?:(?:zeleznicn\w*\s+)?(?:stanici|stanice|dopravni"
                            r"|dopravne|zastavke|zastavky)|zst\b\.?|z\.\s?st\.|zel\.\s?st\.|zel\.\s+stanici)\s*"
                            r"|\bna\s+(?:zastavke|zastavku)\s*")
# zákaz vjazdu do stanice („Zákaz vjazdu vlakov do žst. Plaveč …“) – miestom je táto stanica
_ENTRY_BAN_PLACE = re.compile(r"\bzakaz\w*\s+(?:vjazd|prejazd|jazd)\w*\s+(?:\w+\s+){0,3}?(?:do|cez|v|vo)\s+"
                              r"(?:zeleznicn\w*\s+)?(?:zst\b\.?|z\.\s?st\.|zel\.\s?st\.|stanic\w*|dopravn\w*)?\s*")
# bez predložky: „stanica X je neprejazdná“, „Žst. X je …“, „ŽST X – neprejazdná“ (nie „zo žst. X do …“, „do stanice“)
_STATION_NOUN = re.compile(r"(?<![\w.])(?<!z\s)(?<!zo\s)(?<!do\s)(?<!od\s)(?<!ku\s)(?<!k\s)(?<!cez\s)(?<!smer\s)"
                           r"(?:zeleznicn\w*\s+)?(?:stanica|stanicu|stanice|dopravna|dopravnu|dopravne|zst\b\.?|z\.\s?st\."
                           r"|zel\.\s?st\.)\s*")
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
    r"|\bprerusen\w*(?:\s+\S+){0,8}?\s+(?:je|bolo|bola|bol|bylo|byla|byl)\s+(?:uz\s+)?ukoncen"
    # zrušený či neplatný zákaz jazdy („Zákaz jazdy nákladných vlakov … bol dňa 15.12. o 10:00 zrušený“, „sa ruší zákaz
    # jazdy …“, „zákaz … neplatí“) – koniec obmedzenia, nie nová mimoriadnosť
    r"|\bzakaz\w*(?:\s+\S+){0,10}?\s+(?:je|bol|bola|bolo|bude)\s+(?:\S+\s+){0,5}?(?:zrusen|odvolan|ukoncen)\w*"
    r"|\b(?:rusi|zrusuje|odvolava|rusime|odvolavame)\s+(?:sa\s+)?zakaz"
    r"|\bzakaz\w*(?:\s+\S+){0,10}?\s+(?:uz\s+)?neplati\b")
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
# veta o budúcom okne („bude neprejazdná“, „dnes od 14:00“, „plánuje sa“) – čas bez dátumu je dnešný, nie včerajší
_FUTURE_TENSE = re.compile(r"\b(?:bude|budu|budeme|dnes|dnesn\w*|planuj\w*|planovan\w*|zacne|zacnu|nastane|nastanu)\b")
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
    obnovená“) zmienku ruší a sám znamená trvanie.

    Skončenie na inom mieste, než je mimoriadnosť („Mimoriadnosť: v úseku Púchov – Považská Bystrica strhnuté TV …
    Úsek Žilina – Varín je už prejazdný.“), mimoriadnosť nekončí. Časti viet o nákladných vlakoch (vlakoch s RID)
    posúdi :func:`_freight_ban` – obmedzenie len pre ne neznamená, že obnovenie prevádzky neplatí."""
    found = None
    ongoing = False
    place = None
    for s, e in [(0, c.subject_end)] + _sentence_spans(c.text, c.fq, c.subject_end, len(c.text)):
        for m in _INCIDENT_END.finditer(c.fq, s, e):
            a, _b = _clause_span(c.fq, (s, e), m.start())
            if _NEGATION.search(c.fq, a, m.end()):
                ongoing = True
            elif found is None:
                if place is None:
                    place = [_incident_place(c)[0]]
                if not _other_place(c, s, e, place[0]):
                    found = m
        rest = _INCIDENT_END_PHRASE.sub(lambda x: " " * len(x.group(0)), c.fq[s:e])
        rest = _DETOUR_RETURN.sub(lambda x: " " * len(x.group(0)), rest)
        for a, b in _clauses(c.fq, s, e):
            seg = c.fq[a:b]
            # časť vety o zákaze či uvoľnení nákladných vlakov posúdi _freight_ban (odklon nákladných vlakov však trvá)
            if _FREIGHT.search(seg) and not re.search(r"odklon|odklan", _DETOUR_RETURN.sub(" ", seg)) and (
                    _FREIGHT_BAN.search(seg) or _BAN_LIFTED.search(seg) or _FREIGHT_MOVES.search(seg)):
                rest = rest[:a - s] + " " * (b - a) + rest[b - s:]
        if _INCIDENT_ONGOING.search(rest) or (_INCIDENT_CONTINUES.search(rest) and _INCIDENT_WORDS.search(rest)
                                              and not _SPEED_AFTER.search(rest)):
            ongoing = True
    if found is None or ongoing:
        return None
    rest = _INCIDENT_END_PHRASE.sub(" ", c.fq)
    if re.search(r"do\s+odvolani", rest) and not _SPEED_AFTER.search(rest):
        return None                       # „… do odvolania“ bez obmedzenia rýchlosti – mimoriadnosť trvá
    return found


def _incident_table(c: _Ctx) -> Optional[tuple]:
    """Mimoriadnosť v tabuľke („Miesto | Začiatok | Predpokladaný koniec“): (miesto (od, do) či None, začiatok, koniec,
    koniec je odhad) z prvého riadku – None, ak taká tabuľka nie je."""
    for t in c.tables:
        cols = _map_columns(t, _CLOSURE_COLS)
        if "start" not in cols or not t.rows:
            continue
        row = t.rows[0]

        def cell(key: str) -> str:
            i = cols.get(key)
            return row[i].strip() if i is not None and i < len(row) else ""

        start = datumy.parse_datetime_text(cell("start"), c.ref) if cell("start") else None
        if start is None:
            continue
        end = datumy.parse_datetime_text(cell("end"), c.ref) if cell("end") else None
        place = None
        if "section" in cols and cell("section"):
            secs = _cell_sections(c, cell("section"))
            if secs:
                place = (secs[0].text, secs[1].text)
            else:
                st = c.st.match(cell("section"))
                place = (st.text, st.text) if st else None
        est = "end" in cols and bool(re.search(r"predpoklad", t.header[cols["end"]]))
        return place, start, end, est
    return None


def _incident_blocks(c: _Ctx) -> list[tuple[int, int]]:
    """Odseky (položky zoznamu) nového textu, z ktorých každý ohlasuje mimoriadnosť na vlastnom mieste – len ak sú
    aspoň dva s rôznymi miestami (inak je e-mail jedna mimoriadnosť)."""
    out, places = [], set()
    for bs, be in c.blocks[1:]:
        if c.in_quote(bs) or c.in_table(bs) or not _INCIDENT_WORDS.search(c.fq, bs, be):
            continue
        secs = [x for x in c.sections(bs, be) if not x.line_desc]
        st = _station_place(c, bs, be)
        if not secs and st is None:
            continue
        out.append((bs, be))
        places.add(frozenset((secs[0].a.name, secs[0].b.name)) if secs else frozenset((st.name,)))
    return out if len(out) >= 2 and len(places) >= 2 else []


# odklonené vlaky sa vracajú na pôvodnú trasu – koniec odklonu, nie trvanie mimoriadnosti
_DETOUR_RETURN = re.compile(r"odklon\w*(?:\s+[^\s,;.]+){0,4}?\s+(?:sa\s+)?(?:vracaj\w*|vratia|vrati)\b")


def _other_place(c: _Ctx, s: int, e: int, place: Optional[Section]) -> bool:
    """Veta ``s`` – ``e`` hovorí o inom mieste (úsek, dopravňa), než je miesto mimoriadnosti ``place``."""
    if place is None:
        return False
    names = {frozenset((x.a.name, x.b.name)) for x in c.sections(s, e) if not x.line_desc}
    names |= {frozenset((st.name,)) for _pos, st in _station_places(c, s, e)}
    if not names:
        return False
    key = frozenset((place.a.name, place.b.name)) if place.a is not place.b else frozenset((place.a.name,))
    return key not in names and not (place.a is place.b and any(place.a.name in n for n in names))


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
                          r"|nerealizuju|nevykona|nevykonaju|odvolava|odvolavaju|stornuje|stornuju)\b")
# „nie je zrušená“ zrušením nie je – výluka ostáva (:data:`_STAYS`)
_CANCEL_PRED = re.compile(
    r"(?<!\bnie\s)\b(?:je|bola|bol|bolo|su|boli|bude|budu|byla|byl|bylo)\s+(?:zrusen|odvolan|stornovan)\w*"
    r"|\bnebud(?:e|u)\s+(?:[^\s,;.]+\s+){0,6}?(?:vykonan|realizovan|uskutocnen)\w*"
    r"|\bnebud(?:e|u)\s+(?:sa\s+)?(?:[^\s,;.]+\s+){0,6}?kona"
    r"|\bodpada(?:ju)?\b|\bodpadne\b"
    r"|\b(?:rusime|zrusujeme|stornujeme|odvolavame|zrusili\s+sme|stornovali\s+sme|odvolali\s+sme)\b"
    r"|\bodvolava\w*\s+(?:\w+\s+){0,2}?(?:vyluk|rov\b)"
    r"|\b(?:zrusen|odvolan|storn)\w*\s+(?:\w+\s+){0,2}(?:vyluk|rov\b|rozkaz\w*\s+o\s+vyluke)")
# zmienka o inej, už zrušenej výluke (nie oznámenie zrušenia): „ako náhrada za výluku, ktorá bola zrušená“, „po
# zrušení výluky podľa ROV …“, „je náhradou za zrušenú výluku“, „…, pretože pôvodná výluka z 20.11. bola zrušená“
_CANCEL_MENTION = re.compile(
    r"\b(?:za|po|namiesto|miesto|vzhladom\s+na|kvoli|nahrad\w*\s+za)\s+(?:\w+\s+)?(?:zrusen|odvolan|stornovan)\w*"
    r"(?:\s+(?:vyluk\w*|rov\b|rozkaz\w*|podla|c\.|\w*\d[\w/]*)){0,4}"
    r"|\bktor\w*\s+(?:\w+\s+){0,2}?(?:je|bola|bol|bolo|boli|su|bude|budu)\s+(?:\w+\s+)?(?:zrusen|odvolan|stornovan)\w*"
    r"|\bktor\w*\s+(?:sa\s+)?(?:\w+\s+)?(?:nekonal\w*|nekona|odpadl\w*|odpada|zrusil\w*)"
    r"|\b(?:pretoze|lebo|nakolko|kedze|aj\s+ked|hoci)\b(?:[^,;.]|\.(?=\S))*")


_CLOSURE_NOUN = re.compile(r"vyluk|\brov\b|rozkaz\w*\s+o\s+vyluke")
_CHANGE_WORDS = re.compile(r"presuv|presun|posuv|posun|\bmeni\b|\bmenia\b|\bzmen|nov\w*\s+termin|nahradn\w*\s+termin"
                           r"|namiesto|nahradz|prelozen|preklad|odklad|odloz")
# dni či termíny výluky, ktoré po zrušení časti ostávajú v platnosti („v ostatných dňoch platí bez zmeny“)
_REMAINING = re.compile(r"ostatn\w*\s+(?:dn|termin|noc|cast)|\bv\s+ostatnych\b"
                        r"|(?:ostava|zostava|ostane|zostane|ostanu|zostanu)\w*\s+v\s+platnosti"
                        r"|\bbez\s+zmeny\b|nadalej\s+plat")
# výluka sa koná len v časti pôvodného času („… sa dňa 12.11.2026 uskutoční len od 08:00 do 10:00“) – pri zrušení zvyšku
# výluky veta novú výluku neohlasuje
_ONLY_PART = re.compile(r"(?:(?<!ne)uskutocn\w*|\bvykona\w*|\bplat(?:i|ia)\b|\bkona(?:ju)?\b)\s+(?:sa\s+)?(?:\w+\s+)?"
                        r"(?:len|iba|jen|pouze)\b|\b(?:len|iba|jen|pouze)\s+(?:\S+\s+){0,6}?(?:sa\s+)?(?:(?<!ne)uskutocn"
                        r"\w*|vykona\w*|plat(?:i|ia)\b|kona(?:ju)?\b)")
# výluka ostáva – veta nič nové neohlasuje („Výluka v úseku Trnava – Senica dňa 09.12. sa nemení / nie je zrušená“)
_STAY_KEEP = re.compile(r"\bnemen(?:i|ia)\b|\bnie\s+(?:je|su)\s+(?:\w+\s+)?(?:zrusen|odvolan|stornovan)\w*"
                        r"|\bnebol\w*\s+(?:\w+\s+)?(?:zrusen|odvolan|stornovan)\w*"
                        r"|\bnebud(?:e|u)\s+(?:\w+\s+)?(?:zrusen|odvolan|stornovan)\w*")
_PARTIAL_CANCEL = re.compile(r"\b(?:len|iba|jen|pouze|vylucne)\s+(?:v\s+den|v\s+dnoch|dna|dne|na\s+den|pre\s+den"
                             r"|pro\s+den|v\s+termine)|" + _REMAINING.pattern)
# zrušenie celej výluky – dátum vo vete ju len označuje („platná od 23.11.“), nie je rušeným termínom
_WHOLE_CANCEL = re.compile(r"\bv\s+(?:celom|plnom)\s+rozsahu\b|\bcel(?:a|y|u|eho|ej|ou)\s+(?:vyluk\w*|rov\b|rozkaz\w*)"
                           r"|\bvsetk\w*\s+(?:terminy|dni|noci)\b|\bkompletne\s+(?:sa\s+)?rusi")


# výluka sa nevykoná len v plnom rozsahu („… sa nevykoná v plnom rozsahu, ale len od 08:00 do 10:00“) – nie je to
# zrušenie výluky, ale zmena (skrátenie); celú výluku nezruší
_NOT_FULLY = re.compile(
    r"\b(?:nekona|nekonaju|nevykona|nevykonaju|neuskutocni|neuskutocnia|nerealizuje|nerealizuju)\s+(?:sa\s+)?"
    r"v\s+(?:plnom|celom)\s+rozsahu"
    r"|\bv\s+(?:plnom|celom)\s+rozsahu\s+(?:sa\s+)?(?:nekona|nekonaju|nevykona|nevykonaju|neuskutocni|neuskutocnia"
    r"|nerealizuje|nerealizuju|nebud(?:e|u)\s+(?:\w+\s+)?(?:vykonan|realizovan|uskutocnen|konat))\w*"
    r"|\bnebud(?:e|u)\s+(?:sa\s+)?(?:\w+\s+){0,2}?(?:vykonan\w*|realizovan\w*|uskutocnen\w*|konat)\s+"
    r"v\s+(?:plnom|celom)\s+rozsahu")


def _blank_cancel_mentions(text: str) -> str:
    """Text bez zmienok o inej, už zrušenej výluke (:data:`_CANCEL_MENTION`) a bez „nevykoná sa v plnom rozsahu“
    (:data:`_NOT_FULLY` – zmena, nie zrušenie) – dĺžka ostáva."""
    text = _CANCEL_MENTION.sub(lambda m: " " * len(m.group(0)), text)
    return _NOT_FULLY.sub(lambda m: " " * len(m.group(0)), text)


# podmet časti vety so záporom „nebude … realizovaná“ nie je výluka: doprava, preprava, vlaky, cestujúci, porada …
# („…, vlaková doprava nebude v tomto úseku realizovaná“ výluku neruší – výluka práve preto bude)
_OTHER_SUBJECT = re.compile(r"\b(?:doprav|preprav|premavk)\w*|\bvlak(?:y|ov)?\b|\bjazd[ay]\b|\bcestujuc\w*"
                            r"|\bporad[auy]?\b|\bstretnut\w*|\brokovani\w*|\bskoleni\w*|\bautobus\w*")


def _cancel_predicate(c: _Ctx, s: int, seg: str) -> Optional[re.Match]:
    """Prvý výraz zrušenia vo vete ``seg`` (začína na pozícii ``s``), ktorého časť vety má za podmet výluku (ROV,
    rozkaz, práce) – nie dopravu, prepravu, vlaky či poradu. Časť vety siaha od poslednej čiarky, bodkočiarky či
    pomlčky (nie pomlčky v úseku „Kysak – Košice“) po výraz zrušenia."""
    cands = list(_CANCEL_PRED.finditer(seg))
    if re.search(r"\b(?:sa|se)\b", seg):
        cands += list(_CANCEL_VERB.finditer(seg))
    for m in sorted(cands, key=lambda x: x.start()):
        if not _foreign_subject(c, s, seg, m.start(), m.end()):
            return m
    return None


def _foreign_subject(c: _Ctx, s: int, seg: str, start: int, end: int) -> bool:
    """Časť vety ``seg`` (začína na pozícii ``s``) s výrazom zrušenia na ``start`` – ``end`` má za podmet niečo iné
    než výluku (dopravu, vlaky, poradu …). Vedľajšia časť vety (za čiarkou, bodkočiarkou či pomlčkou) bez výluky
    a s vlastným podmetom („…, spoje nebudú realizované“, „…, NAD nebude realizovaná“, „– rýchliky …“) výluku
    neruší; bez vlastného podmetu („…, z dôvodu počasia nebude realizovaná“) sa zápor týka výluky vety."""
    spans = [(x.start - s, x.end - s) for x in c.sections(s, s + len(seg))]
    seps = [y.end() for y in re.finditer(r"[,;:]|\s-\s", seg[:start]) if not any(a <= y.start() < b for a, b in spans)]
    clause = seg[seps[-1] if seps else 0:end]
    if _closure_subject(clause) or re.search(r"\bprac\w*", clause):
        return False
    if _OTHER_SUBJECT.search(clause):
        return True
    # vlastný podmet len vo vedľajšej časti vety za skutočným oddeľovačom (nie dvojbodka v čase „14:00“)
    real = [y.end() for y in re.finditer(r"[,;]|:(?!\d)|\s-\s", seg[:start])
            if not any(a <= y.start() < b for a, b in spans)]
    return bool(real) and _own_subject(seg[real[-1]:start])


# časť vety pred výrazom zrušenia bez vlastného podmetu: dôvod („z technických dôvodov“), predložková väzba („na
# žiadosť zhotoviteľa“, „v tomto čase“), častice, zámená, čísla, dátumy a časy
_HEAD_REASON = re.compile(r"\b(?:z\s+(?:\w+\s+){0,2}?(?:dovod|duvod|pricin)\w*|kvoli|vzhladom\s+na|v\s+dosledku"
                          r"|v\s+suvislosti\s+s)\b.*$")
_HEAD_PREP = re.compile(r"\b(?:na|po|podla|v|vo|z|zo|pre|pri|o|s|so|k|ku|za|cez|pocas|pred|medzi|od|do|okrem)\s+"
                        r"(?:[a-z]+\s+){0,2}")
_HEAD_FILLER = re.compile(r"(?:sa|se|vsak|preto|teda|uz|zial|bohuzial|napokon|nakoniec|ale|a|aj|i|tiez|takze|tak"
                          r"|potom|nasledne|zatial|este|opat|znovu|vobec|celkom|len|iba|ani|nie|ktor\w*|ta|tato|tento"
                          r"|toto|tieto|tato|uvedena|uvedeny|uvedene|vyssie|nizsie|planovan\w*|povodn\w*|dna|dne|hod|h"
                          r"|min|termin\w*|den|dni|noc\w*|case|cas|tomto|tejto|tom|danom|celom|cela|cely|cele|vobec"
                          r"|ziadn\w*|nijak\w*)")


def _own_subject(head: str) -> bool:
    """Časť vety pred výrazom zrušenia má vlastný podmet – slovo, ktoré nie je dôvodom, predložkovou väzbou, časticou,
    zámenom, dátumom ani časom („spoje“, „NAD“, „obsluha vlečky“, „výkony“)."""
    head = _HEAD_PREP.sub(" ", _HEAD_REASON.sub(" ", head))
    words = [w for w in re.findall(r"[a-z]+", re.sub(r"\d[\d.:/,\s]*", " ", head)) if not _HEAD_FILLER.fullmatch(w)]
    return bool(words)


def _closure_subject(clause: str) -> bool:
    """Časť vety má výluku (ROV, rozkaz) ako podmet – nie len ako predmet s predložkou („porada k výluke“, „preprava
    počas výluky“, „informácie o výluke“)."""
    for m in _CLOSURE_NOUN.finditer(clause):
        if not re.search(r"\b(?:k|ku|o|na|pre|po|pri|z|zo|od|do|ohladom|voci|vzhladom|pocas|pred|v|vo)\s+"
                         r"(?:\w+\s+)?$", clause[max(0, m.start() - 30):m.start()]):
            return True
    return False


# pôvodná výluka sa ruší a nahrádza týmto e-mailom („Pôvodný ROV č. 2240/2026 sa ruší a nahrádza týmto rozkazom.“) –
# zrušenie pôvodnej výluky; nová výluka je v tomto e-maile
_REPLACED_BY_THIS = re.compile(r"\bnahradz\w*\s+(?:\w+\s+){0,2}?(?:tymto|tento|touto|nasledujuc\w*|nizsie)\b")


def _replaced_here(seg: str) -> bool:
    """Veta ruší výluku a nahrádza ju týmto e-mailom – zmenou termínu nie je (iné slová zmeny v nej nie sú)."""
    return bool(_REPLACED_BY_THIS.search(seg)) and not _change_words(_REPLACED_BY_THIS.sub(" ", seg))


def _change_words(seg: str) -> bool:
    """Veta mení termín výluky (presun, náhrada, nový termín) – nie „ostatné dni platia bez zmeny“."""
    return bool(_CHANGE_WORDS.search(_REMAINING.sub(lambda m: " " * len(m.group(0)), seg)))


# „Termín 15.12.2026 sa ruší“, „Termín výluky dňa 15.12.2026 odpadá“ – rušený termín výluky (ROV e-mailu); nie termín
# inej veci („Termín na zaslanie pripomienok 15.11.2026 sa ruší“, „Termín kontrolnej prehliadky … odpadá“)
_TERM_CANCEL = re.compile(r"\s*(?:\S+\s+){0,2}?termin\w*\s+(?:vyluk\w*\s+)?(?:(?:dna|dne|na\s+den|zo\s+dna|v\s+dnoch"
                          r"|c\.\s*\d+\s*:?)\s+)?\d{1,2}\.\s?\d{1,2}\.(?:\S*\s+){0,6}?(?:sa\s+)?(?:rusi|zrusuje|nekona"
                          r"|odpada|je\s+zrusen|bol\w*\s+zrusen|stornuje)")


def _closure_cancels(c: _Ctx) -> list[tuple[int, int]]:
    """Vety nového textu, ktoré rušia výluku – nie pomalú jazdu, nie zmenu termínu, nie zmienku „po výluke“ ani
    zmienku o inej, už zrušenej výluke („ako náhrada za výluku, ktorá bola zrušená“, „po zrušení výluky podľa ROV …“,
    „…, pretože pôvodná výluka bola zrušená“). Veta, ktorá ohlasuje výluku s termínom a v časti za čiarkou ruší inú
    výluku („…, výluka 2. TK z 20.11. odpadá“, „ROV … sa nekoná, nová výluka bude …“), dá len túto časť."""
    out = []
    # e-mail o výluke s číslom (ROV v predmete či texte): „Termín 15.12.2026 sa ruší.“ ruší termín tejto výluky
    rov = bool(c.first_group(_CLOSURE_REF_RES[:1], 0, len(c.text)))
    for s, e in c.sentences():
        if c.in_quote(s):
            continue
        seg = _blank_cancel_mentions(_CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), c.fc[s:e]))
        if not (_CLOSURE_NOUN.search(seg) or (rov and _TERM_CANCEL.match(seg))):
            continue
        m = _cancel_predicate(c, s, seg)
        if m is None:
            continue
        if (_change_words(seg) and not _replaced_here(seg)) or _INTRODUCES.search(seg) or _FUTURE_NOTICE.search(seg) \
                or _HYPOTHETICAL.search(seg):
            continue
        sp = _SPEED_CANCEL.search(seg)
        if sp and not re.search(r"(?:vyluk\w*|\brov\b|rozkaz\w*\s+o\s+vyluke)", seg[:sp.start()] + seg[sp.end():]):
            continue                          # ruší sa pomalá jazda (POTR) – rieši _speed_cancels
        a = max(seg.rfind(",", 0, m.start()), seg.rfind(";", 0, m.start())) + 1
        ends = [x for x in (seg.find(",", m.end()), seg.find(";", m.end())) if x != -1]
        b = min(ends) if ends else len(seg)
        rest = seg[:a] + " " * (b - a) + seg[b:]
        own = _CLOSURE_NOUN.search(seg, a, b) or any(s + a <= t.start < s + b and t.kind in ("date", "datetime")
                                                     for t in c.toks)
        if (a > 0 or b < len(seg)) and not own and _CLOSURE_NOUN.search(rest) and \
                re.search(r"\b(?:bude|budu)\b|\bsa\s+(?:uskutocni|vykona)\b",
                          re.sub(r"\bktor\w*\s+(?:sa\s+)?bud\w*", " ", rest)) and \
                any(s <= p.pos < e and not (s + a <= p.pos < s + b) for p in c.periods):
            # časť vety bez vlastnej výluky a termínu vo vete, ktorá výluku s termínom ohlasuje („Úplná výluka … bude
            # dňa 20.11. od 8:00 do 14:00, … nebude realizovaná“) – výluku nezrušuje (zápor patrí niečomu inému)
            continue
        # vedľajšia veta („…, ktorá trvá od 07:00 do 13:00, sa dňa 09.12. ruší“) rušenú výluku len opisuje – jej termín
        # nie je ďalšou výlukou vety
        rel = [(s + m.start(), s + m.end()) for m in re.finditer(r"(?<=[,;])[^,;]*", seg) if _RELATIVE.match(m.group(0))]
        if (a > 0 or b < len(seg)) and own and _CLOSURE_NOUN.search(rest) and \
                any(s <= p.pos < e and not (s + a <= p.pos < s + b) and not any(x <= p.pos < y for x, y in rel)
                    for p in c.periods):
            # samostatná časť vety o zrušení inej výluky či iných dní („…, v dňoch 10. a 11.12. sa nekoná“) – termín
            # vo zvyšku vety platí
            out.append((s + a, s + b))
        else:
            out.append((s, e))
    return out


# vedľajšia informácia v e-maile o výluke: náhradná doprava, porada, pripomienky či žiadosti, objednávky trás, platnosť
# samotného rozkazu, ďalšie plánované výluky, premávka podľa GVD, kontakt – jej termín ani úsek nie sú výlukou
_META = re.compile(
    r"nahradn\w*\s+(?:autobusov\w*\s+|vlakov\w*\s+)?(?:doprav|preprav)|autobus\w*"
    r"|\bporad[auy]?\b|\bporade\b|stretnut\w*|rokovani\w*|konzultaci\w*|skoleni\w*"
    r"|pripomien\w*|ziadost\w*|ziadame|objednav\w*|prijimame|zasielajte|zaslite|predlozte|nahlaste"
    r"|\bgvd\b|cestovn\w*\s+poriad\w*|grafikon\w*"
    r"|planuj\w*|pripravuj\w*|bud(?:e|u)\s+oznamen\w*|oznamime|samostatn\w*\s+(?:rov|rozkaz\w*|oznamen\w*)"
    r"|rozkaz\w*\s+(?:plati|je\s+platn|nadobuda)|platnost\w*\s+(?:rozkaz|rov\b|dokument|oznamen)|ucinnost\w*"
    r"|vybavuj\w*|\bkontakt\s*:|\bkontaktuj\w*|kontaktn\w*\s+(?:osob|cisl|telefon|adres|e-mail)|telefon\w*|\btel\.|infolink"
    r"|premavaj\w*\s+(?:\w+\s+)?(?:podla|normaln|bez\s+obmedzen)|bez\s+obmedzen\w*")


# zmienka o výluke vo vedľajšej informácii („k návrhu výluky“, „ďalšie výluky plánujeme“, „o výluke“) – výluku neohlasuje
_META_MENTION = re.compile(r"\b(?:navrh\w*|k|ku|o|na|ohladom|dalsi\w*|buduc\w*|planovan\w*)\s+(?:\w+\s+)?vyluk\w*"
                           r"|\(\s*vyluk\w*\s*\)")


def _meta_clause(c: _Ctx, start: int, end: int) -> bool:
    """Časť vety s pozíciami ``start`` – ``end`` je vedľajšou informáciou (:data:`_META`), nie ohlásením výluky –
    časť vety, ktorá výluku sama ohlasuje („Výluka … dňa 01.12. … (náhradná autobusová doprava)“), ňou nie je."""
    sent = next(((s, e) for s, e in c.sentences() if s <= start < e), None)
    if sent is None:
        return False
    s, e = sent
    a = max(c.fc.rfind(",", s, start), c.fc.rfind(";", s, start))
    a = s if a == -1 else a + 1
    bs = [x for x in (c.fc.find(",", end, e), c.fc.find(";", end, e)) if x != -1]
    b = min(bs) if bs else e
    # položka zoznamu („1. …“, „- …“, „a) …“) je samostatná časť vety – poznámka inej položky ju neovplyvní
    ls = c.fc.rfind("\n", s, start) + 1 or s
    le = c.fc.find("\n", end, e)
    le = e if le == -1 else le
    if _ENUM_RE.match(c.fc[ls:le]):
        a, b = max(a, ls), min(b, le)
    # poznámka hneď za termínom či úsekom – v zátvorke alebo za pomlčkou („27.11.2026 od 08:00 do 14:00 (NAD)“,
    # „… (v oboch termínoch NAD)“, „… – zabezpečená NAD“) – ho len opisuje, vedľajšou informáciou ho nerobí
    if re.match(r"\s*(?:hod\.?\s*)?(?:\(|-\s)", c.fc[end:b]):
        b = end
    if not (_META.search(c.fc, a, b) or re.search(r"\bNAD\b", c.text[a:b])):
        return False
    plain = _blank_cancel_mentions(_CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), c.fc[a:b]))
    plain = _META_MENTION.sub(lambda m: " " * len(m.group(0)), plain)
    return not (_closure_words(plain) or re.search(r"pomal\w*\s+j[ai]zd|\bpotr\b", plain))


def _moved_cancels(c: _Ctx) -> list[tuple[int, int]]:
    """Rušený deň vo vete, ktorá výluku ruší a zároveň presúva na nový termín („Výluka podľa ROV … dňa 25.11.2026 sa
    ruší a presúva sa na 28.11.2026 …“): časť vety pred slovom presunu, ak v nej je sloveso zrušenia a vlastný deň
    a za ňou je nový termín. „… sa ruší a nahrádza ho ROV č. …“ bez rušeného dňa je zmena výluky."""
    out = []
    for s, e in c.sentences():
        if c.in_quote(s) or (s, e) in c.closure_cancels:
            continue
        seg = _blank_cancel_mentions(_CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), c.fc[s:e]))
        ch = _CHANGE_WORDS.search(_REMAINING.sub(lambda m: " " * len(m.group(0)), seg))
        if not ch or not _CLOSURE_NOUN.search(seg, 0, ch.start()):
            continue
        head = seg[:ch.start()]
        if _cancel_predicate(c, s, head) is None:
            continue
        if _FUTURE_NOTICE.search(seg) or _HYPOTHETICAL.search(seg):
            continue
        cut = s + ch.start()
        days = [t for t in c.toks if s <= t.start < cut and t.kind in ("date", "datetime") and t.day
                and not datumy.not_term_date(c.fq, t)]
        if days and any(cut <= p.pos < e for p in c.periods):
            out.append((s, cut))
    return out


# výluka (úsek) ostáva v platnosti či sa koná („… sa uskutoční podľa ROV …“, „… platí“, „… nie je dotknutá“, „…
# ostáva“, „… sa vykoná podľa plánu“, „bez zmeny“)
_STAYS = re.compile(_REMAINING.pattern + "|" + _STAY_KEEP.pattern +
                    r"|(?<!ne)uskutocn\w*|\bvykona\w*|\bplat(?:i|ia)\b|nie\s+(?:je|su)\s+dotknut\w*"
                    r"|\b(?:ostava|ostavaju|zostava|zostavaju|ostane|ostanu|zostane|zostanu)\b|podla\s+planu"
                    r"|\bkona(?:ju)?\b|\bbud(?:e|u)\s+(?:sa\s+)?konat|\bkonat\s+sa\s+bud|\btrva(?:ju)?\b"
                    r"|\b(?:bud(?:e|u)|ostava\w*|zostava\w*|ostan(?:e|u)|zostan(?:e|u))\s+(?:\w+\s+)?(?:uzavret|uzavren"
                    r"|uzatvoren|vylucen)\w*")
# výnimka zo zrušenia („Rušíme výluku podľa ROV … okrem úseku Trnava – Senica“, „… sa ruší s výnimkou úseku …“) –
# výluka v úseku za ňou ostáva
_EXCEPT_BEFORE = re.compile(r"\b(?:okrem|s\s+vynimkou|vynimajuc|mimo)\s+(?:(?:useku|usekov|usekoch|stanice|stanic"
                            r"|dopravne|trate|trati|vyluky|vyluk)\s+)?(?:(?:v|vo|na)\s+(?:useku|usekoch|stanici)\s+)?$")


_RELATIVE = re.compile(r"\s*(?:ktor\w*|kde|kedy)\b")
# zrušenie iných výluk, nie výluky vety („…, ostatné výluky sa rušia“, „ďalšie termíny sa rušia“)
_OTHER_CLOSURES = re.compile(r"\b(?:ostatn|dalsi|zvysn)\w*\s+(?:\w+\s+)?(?:vyluk|usek|termin|dn|noc)"
                             r"|\b(?:ine|ina|iny|inych|inu)\s+(?:vyluk|usek|termin)")
# zrušenie zvyšku výluky, ktorá ostáva len v uvedenom čase („… sa uskutoční len od 08:00 do 10:00. Zvyšok výluky sa
# ruší.“) – týka sa úseku tej výluky
_REST_OF = re.compile(r"\bzvysok\b|\b(?:ostatn|zvysn)\w*\s+(?:cas|cast)\w*")
# „Ruší sa výluka Senica – Kúty, nie Trnava – Senica.“, „…, výluka v úseku Trnava – Senica nie.“ – časť vety so záporom
# za zrušením hovorí, čo ostáva
_NOT_THIS = re.compile(r"\s*(?:ale\s+|a\s+)?nie\b|.*\bnie\s*[.!]?\s*$", re.S)
# hranica časti vety: čiarka, bodkočiarka, spojka pred ďalšou výlukou, úsekom či termínom („… sa ruší a výluka v úseku
# … zostáva“, „… platí bez zmeny a od 10:00 do 14:00 sa ruší“)
_PRED_CUT = re.compile(r"[,;]|\s(?:a|ale|avsak|pricom|no)\s+(?=(?:\w+\s+){0,2}?(?:vyluk|usek|rov\b|v\s+(?:case|dnoch|noci)\b"
                       r"|od\s+\d|dna\s+\d|\d{1,2}\.\s?\d{1,2}\.))")


def _fmt_local(dt: Optional[datetime]) -> str:
    return f"{dt:%d.%m.%Y %H:%M}" if dt else "?"


def _sentences_in(c: _Ctx, s: int, e: int) -> list[tuple[int, int]]:
    """Vety (aj predmet ako jedna veta) v časti textu ``s:e``, orezané na ňu."""
    out = [(s, min(e, c.subject_end))] if s < c.subject_end else []
    out += [(max(a, s), min(b, e)) for a, b in c.sentences() if a < e and b > s]
    return [(a, b) for a, b in out if b > a]


def _pred_parts(c: _Ctx, s: int, e: int) -> list[tuple[int, int, Optional[str], list]]:
    """Časti viet v ``s:e`` (medzi čiarkami, bodkočiarkami a spojkami pred ďalšou výlukou či termínom) s výrokom
    o výluke: „cancel“ – časť ju ruší, „stay“ – hovorí, že výluka (jej časť) ostáva (:data:`_STAYS`), None – bez
    výroku. Časť bez výroku patrí k nasledujúcej časti („Výluka v úseku A – B, …, sa ruší“, „v dňoch 24., 25.
    a 26.11. platí bez zmeny“); za časťou s výrokom, ak nemá vlastnú výluku ani úsek (ďalší deň zoznamu, čas), a na
    konci vety patrí k predchádzajúcej („… sa ruší v dňoch 24.11. a 25.11.2026“). Vedľajšia veta („…, ktorá trvá od
    07:00 do 13:00, …“) výrok nemá, len výluku opisuje – je v časti, ku ktorej patrí, a jej rozsahy sú vo štvrtej
    položke."""
    out: list = []
    for ss, se in _sentences_in(c, s, e):
        cuts = [(ss, ss)] + [(ss + m.start(), ss + m.end()) for m in _PRED_CUT.finditer(c.fc[ss:se])] + [(se, se)]
        raw = [(cuts[i][1], cuts[i + 1][0]) for i in range(len(cuts) - 1)]
        first = len(out)                  # prvá časť tejto vety v ``out``
        start: Optional[int] = None
        rels: list = []
        for i, (a, b) in enumerate(raw):
            last = i + 1 == len(raw)
            if start is None:
                start = a
            if _RELATIVE.match(c.fc, a) and not last:
                rels.append((a, b))
                continue
            plain = _blank_cancel_mentions(_CLOSURE_MENTION.sub(lambda m: " " * len(m.group(0)), c.fc[start:b]))
            for ra, rb in rels:
                plain = plain[:ra - start] + " " * (rb - ra) + plain[rb - start:]
            kind = "cancel" if _cancel_predicate(c, start, plain) is not None else (
                "stay" if _STAYS.search(plain) else None)
            if kind is None and len(out) > first and out[-1][2] == "cancel" and _NOT_THIS.match(plain):
                kind = "stay"                      # „Ruší sa výluka Senica – Kúty, nie Trnava – Senica.“
            if kind is None:
                # časť bez výroku: pokračovanie predchádzajúcej časti (ďalší deň zoznamu „… sa ruší v dňoch 24.11.
                # a 25.11.“, čas), na konci vety vždy; s vlastnou výlukou či úsekom patrí k nasledujúcej časti
                cont = len(out) > first and start == a and not _CLOSURE_NOUN.search(plain) and \
                    not any(not x.line_desc for x in c.sections(a, b))
                if len(out) > first and (last or cont):
                    pa, _pb, pk, pr = out[-1]
                    out[-1] = (pa, b, pk, pr + rels)
                    start, rels = None, []
                    continue
                if not last:
                    continue
            out.append((start, b, kind, rels))
            start, rels = None, []
    return out


def _span_term(c: _Ctx, spans: list[tuple[int, int]], skip: list = ()
               ) -> Optional[tuple[datetime, Optional[datetime], str, str]]:
    """Termín v častiach textu ``spans`` bez častí ``skip``: (začiatok, koniec, ako, dni) v miestnom čase – „obdobie“
    (obdobie z textu), „okno“ / „okno_den“ (čas bez dňa vo vete o zrušení, deň z tej istej vety / z inej vety
    e-mailu), „den“ (celé dni), „noc“ / „noc_okno“ (noc bez času: 22:00 – 06:00 / okno nočnej výluky z e-mailu);
    None – termín v nich nie je."""
    if not spans:
        return None

    def inside(pos: int) -> bool:
        return any(a <= pos < b for a, b in spans) and not any(a <= pos < b for a, b in skip)

    def term_day(t: datumy.Temporal) -> bool:      # nie dátum vydania, podpisu či kontaktu
        ls = c.fq.rfind("\n", 0, t.start) + 1
        le = c.fq.find("\n", t.end)
        le = len(c.fq) if le == -1 else le
        return not (datumy.not_term_date(c.fq, t) or _DOC_LINE.search(c.fq, ls, le)
                    or _MENTION_BEFORE.search(c.fq, max(0, t.start - 30), t.start)
                    or any(a <= t.start < b for a, b in c.sig))

    pers = [p for p in c.periods if (inside(p.pos) or inside(p.end_pos - 1))
            and not any(a < p.end_pos <= b for a, b in skip)]
    if pers:
        ends = [p.end for p in pers if p.end is not None]
        return (min(p.start for p in pers), max(ends) if ends and len(ends) == len(pers) else None, "obdobie", "")
    days = [t for t in c.toks if inside(t.start) and t.kind in ("date", "datetime") and t.day and term_day(t)]
    wins = [w for a, b in spans for w in datumy.time_windows(c.fq, c.toks, a, b) if inside(w[0].start)]
    if wins:
        how = "okno"
        if not days:
            # deň z tej istej vety (aj z časti o tom, čo ostáva: „dňa 12.11. od 08:00 do 10:00 platí, od 10:00 do 14:00
            # sa ruší“), inak najbližší skorší deň v tom istom odseku („… sa dňa 12.11. uskutoční len od 08:00 do 10:00.
            # Od 10:00 do 14:00 sa výluka ruší.“)
            first = min(a for a, _b in spans)
            sent = c.sentence_of(first) if first >= c.subject_end else (0, c.subject_end)
            days = [t for t in c.toks if sent[0] <= t.start < sent[1] and t.kind in ("date", "datetime") and t.day
                    and term_day(t)]
            if not days:
                blk = c.block_of(first) or (first, first)
                prev = [t for t in c.toks if blk[0] <= t.start < first and t.kind in ("date", "datetime") and t.day
                        and term_day(t) and not c.in_quote(t.start)]
                days, how = prev[-1:], "okno_den"
        if days:
            d0, d1 = days[0], days[-1]
            wa, wb = wins[0][0], wins[-1][1]
            # dátum s časom má deň noci už posunutý (okno po polnoci), dátum noci nie
            start = datetime.combine(datumy.night_window_day(d0.day, d0.night and d0.kind == "date", wa.tm), wa.tm)
            end = datetime.combine(datumy.night_window_day(d1.day, d1.night and d1.kind == "date", wa.tm), wb.tm)
            if wb.plus_day or end <= start:
                end += timedelta(days=1)
            listed = ", ".join(dict.fromkeys(f"{t.day:%d.%m.%Y}" for t in days))
            return start, end, how, listed
    if not days:
        return None
    if all(t.night and t.kind == "date" for t in days):
        nights = sorted({t.day for t in days})
        # okno nočnej výluky uvedené v e-maile („denne 22:30 – 04:30“) – inak noc 22:00 – 06:00
        win = next(((a.tm, b.tm) for a, b in datumy.time_windows(c.fq, c.toks, 0, len(c.fq))
                    if b.tm <= a.tm and not c.in_quote(a.start) and not any(x <= a.start < y for x, y in c.sig)),
                   None) or next(
            ((time(*map(int, p.daily_from.split(":"))), time(*map(int, p.daily_to.split(":")))) for p in c.periods
             if p.daily_from and p.daily_to and p.daily_to <= p.daily_from and not c.in_quote(p.pos)), None)
        how = "noc_okno"
        if not win:
            win, how = (time(22, 0), time(6, 0)), "noc"
        t1, t2 = win
        start = datetime.combine(nights[0], t1)
        end = datetime.combine(nights[-1] + timedelta(days=1), t2)
        listed = ", ".join(f"{d:%d.%m.}/{d + timedelta(days=1):%d.%m.%Y}" for d in nights)
        return start, end, how, listed
    uniq = sorted({t.day for t in days})
    if len(uniq) == 1 and all(t.kind == "datetime" and t.dt is not None and re.search(
            r"\bod\s*\d{1,2}\s?[:.]\s?\d{2}", c.fq[max(0, t.start - 4):t.end]) for t in days):
        # „dňa 12.11.2026 od 10:00 sa ruší“ – ruší sa od uvedeného času do konca dňa (nie celý deň od polnoci)
        first = min(t.dt for t in days)
        return first, datetime.combine(uniq[0] + timedelta(days=1), time(0, 0)), "od", f"{uniq[0]:%d.%m.%Y}"
    return (datetime.combine(uniq[0], time(0, 0)), datetime.combine(uniq[-1] + timedelta(days=1), time(0, 0)), "den",
            ", ".join(f"{d:%d.%m.%Y}" for d in uniq))


def _staying_sections(c: _Ctx) -> dict:
    """Úseky, o ktorých e-mail hovorí, že ich výluka ostáva či sa koná (časť vety bez zrušenia) – nie úseky, ktoré
    sa v inej časti rušia (zrušenie len časti dní toho istého úseku ostáva zrušením). Kľúč je dvojica staníc
    (frozenset), hodnota úsek „A – B“ v poradí z textu (do poznámky).

    Zrušenie bez vlastného úseku sa týka úseku vety („… v čase od 08:00 do 10:00 platí bez zmeny, v čase od 10:00 do
    14:00 sa ruší“) a úseku, pri ktorom e-mail uvádza, že výluka ostáva len v inom čase („… sa dňa 12.11. uskutoční
    len od 08:00 do 10:00. Od 10:00 do 14:00 sa výluka ruší.“) – taký úsek sa ruší v rušenom čase; nie pri zrušení
    ostatných výluk („…, ostatné výluky sa rušia“)."""
    stay: dict = {}
    cancel: set = set()
    timed: set = set()          # úseky, pri ktorých e-mail uvádza čas či deň, v ktorom výluka ostáva
    loose = False               # zrušenie bez vlastného úseku s vlastným termínom
    for s, e in c.sentences():
        if c.in_quote(s):
            continue
        anaphoric = False
        sent_stays: list = []
        for a, b, kind, rels in _pred_parts(c, s, e):
            secs = [x for x in c.sections(a, b) if not x.line_desc and not any(ra <= x.start < rb for ra, rb in rels)]
            if kind == "cancel":
                if not secs and not _OTHER_CLOSURES.search(c.fc, a, b):
                    anaphoric = True
                    loose = loose or _span_term(c, [(a, b)], skip=rels) is not None or \
                        bool(_REST_OF.search(c.fc, a, b))
                for x in secs:
                    key = frozenset((x.a.name, x.b.name))
                    if _EXCEPT_BEFORE.search(c.fc, max(a, x.start - 40), x.start):
                        stay.setdefault(key, f"{x.a.name} – {x.b.name}")   # „… okrem úseku A – B“ – ostáva
                    else:
                        cancel.add(key)
            elif kind == "stay":
                own_term = _span_term(c, [(a, b)], skip=rels) is not None
                for x in secs:
                    key = frozenset((x.a.name, x.b.name))
                    stay.setdefault(key, f"{x.a.name} – {x.b.name}")
                    sent_stays.append(key)
                    if own_term:
                        timed.add(key)
        if anaphoric:
            cancel.update(sent_stays)
    if loose:
        cancel.update(timed)
    return {k: v for k, v in stay.items() if k not in cancel}


def _remaining_sentences(c: _Ctx) -> list[tuple[int, int]]:
    """Vety o dňoch výluky, ktoré ostávajú v platnosti („v ostatných dňoch platí bez zmeny“, „… zostane v platnosti“,
    „… sa nemení“, „… nie je zrušená“, „… sa dňa 12.11. uskutoční len od 08:00 do 10:00“) – nie sú novou výlukou."""
    cancel_days = {t.day for t in c.toks if t.kind in ("date", "datetime") and t.day
                   and any(a <= t.start < b for a, b in c.closure_cancels)}

    def only_part(s: int, e: int) -> bool:
        # „uskutoční sa len …“ je zvyšok rušenej výluky, len ak nehovorí o novom termíne ani o inom dni, než ktorý sa
        # ruší („… dňa 12.11. sa ruší. Výluka sa uskutoční len dňa 14.11. …“ je nový termín – nesmie sa stratiť)
        if not _ONLY_PART.search(c.fc, s, e) or re.search(r"\b(?:nov|nahradn)\w*", c.fc[s:e]):
            return False
        days = {t.day for t in c.toks if s <= t.start < e and t.kind in ("date", "datetime") and t.day}
        return not cancel_days or days <= cancel_days

    return [(s, e) for s, e in c.sentences() if not c.in_quote(s) and (s, e) not in c.closure_cancels
            and (_REMAINING.search(c.fc, s, e) or only_part(s, e)
                 or (_STAY_KEEP.search(c.fc, s, e) and not _OTHER_CLOSURES.search(c.fc, s, e)))]


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
                   locative: bool = False, skip: tuple = ()) -> Optional[StationMatch]:
    """Jedna dopravňa (prvá v texte, za ktorou je názov dopravne): „v (železničnej) stanici X“, „v žst. X“, „v obvode
    stanice X“, „na zastávke X“, bez predložky „stanica X je …“, „Žst. X“, „ŽST X“; ``locative`` aj „v Kraľovanoch“,
    „V Ružomberku“ (miestny pád názvu – len vo vete o mimoriadnosti). ``skip`` – úseky (začiatok, koniec), v ktorých
    dopravňa miestom nie je (koniec úseku „žst. Púchov – žst. Žilina“)."""
    end = len(c.text) if end is None else end
    best: Optional[tuple[int, StationMatch]] = None
    pats = [(_STATION_PLACE, c.f), (_STATION_NOUN, c.f), (_ENTRY_BAN_PLACE, c.f)] + \
        ([(_LOCATIVE, c.text)] if locative else [])
    for pat, src in pats:
        for m in pat.finditer(src, start, end):
            if best is not None and m.start() >= best[0]:
                break
            st = c.st.prefix(tokens(c.text[m.end():min(end, m.end() + 80)], m.end()), c.text)
            if st and any(a <= st.start < b for a, b in skip):
                continue
            if st:
                best = (m.start(), st)
                break
    return best[1] if best else None


def _locative_closure_place(c: _Ctx, start: int, end: int) -> Optional[StationMatch]:
    """Dopravňa v miestnom páde hneď za slovom výluky („Výluka v Ružomberku dňa …“) – len ak e-mail iné miesto nemá."""
    for m in re.finditer(r"\b(?:vyluk|vyluceni|uzavret|uzavren)\w*\s+", c.f[start:end]):
        pos = start + m.end()
        lm = _LOCATIVE.match(c.text, pos)
        if lm is None:
            continue
        st = c.st.prefix(tokens(c.text[lm.end():min(end, lm.end() + 60)], lm.end()), c.text)
        if st is not None:
            return st
    return None


def _station_places(c: _Ctx, start: int, end: int) -> list[tuple[int, StationMatch]]:
    """Všetky dopravne za „v žst. X“, „v (železničnej) stanici X“, „stanica X“ … v časti textu: (pozícia, dopravňa)."""
    key = (start, end)
    hit = c._place_cache.get(key)
    if hit is not None:
        return hit
    out: dict[int, tuple[int, StationMatch]] = {}
    for pat in (_STATION_PLACE, _STATION_NOUN):
        for m in pat.finditer(c.f, start, end):
            st = c.st.prefix(tokens(c.text[m.end():min(end, m.end() + 80)], m.end()), c.text)
            if st and st.start not in out:
                out[st.start] = (m.start(), st)
    res = sorted(out.values(), key=lambda x: x[0])
    c._place_cache[key] = res
    return res


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
    # zátvorka s termínom predchádzajúcej dopravne („v žst. Štrba (24.11.2026 od 08:00 do 12:00) a v žst. …“) nie je
    # vedľajšou informáciou
    gap = re.sub(r"\([^()]*\d{1,2}\s?\.\s?\d{1,2}\s?\.[^()]*\)", lambda m: " " * len(m.group(0)), c.text[gap_start:pos])
    gap = re.sub(r"(?i)(?<!\w)(?:žst|zst|ž\.\s?st|st|č|c)\.", lambda m: m.group(0)[:-1] + " ", gap)   # skratky, nie koniec vety
    if re.search(r"[();!?]|\.\s+[A-ZÁČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ]", gap) or _STATION_SIDE.search(c.fc, gap_start, pos):
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


# organizačná jednotka ŽSR pred názvom mesta („OR Košice“, „KD OR Žilina“, „Oblastné riaditeľstvo Trnava“)
_ORG_UNIT = re.compile(r"or|ore|kd|riaditelstvo|riaditelstva|riaditelstvom|oblastne|oblastneho|smsu|sts|zts|pmd|see"
                       r"|sprava|spravy|uzemie|uzemia")


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
        # dopravňa, ktorá je koncom úseku („v úseku žst. Púchov – žst. Žilina“), nie je samostatným miestom
        in_sec = [(x.a.start, x.b.end) for a, b in spans for x in c.sections(a, b) if not x.line_desc]
        found = [st for a, b in spans for st in [_station_place(c, a, b, locative, skip=in_sec)] if st]
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
        if i > 0 and _ORG_UNIT.fullmatch(fold(toks[i - 1].raw).strip(".,:;()–-")):
            continue                      # „Oznámenie OR Košice“ – organizačná jednotka ŽSR, nie miesto mimoriadnosti
        st = c.st.prefix(toks[i:], c.text)
        if st is not None and st.method == "presne":
            return Section(st, st, [], st.start, st.end), None
    return None, None


# obnovenie prevádzky len pre niektoré vlaky: pre nákladné vlaky (vlaky s RID, ťažké vlaky) obmedzenie trvá
_FREIGHT = re.compile(r"\bnakladn\w*(?:\s+(?:vlak\w*|doprav\w*|preprav\w*))?|\bvlak\w*\s+s\s+(?:nebezpecn\w*|rid\b)"
                      r"|\bnebezpecn\w*\s+(?:tovar|vec|naklad|latk|zasielk)\w*|\brid\b|\bvlak\w*\s+nad\s+\d+\s*t\b")
# obmedzenie trvá: zákaz, nepremávajú, musia čakať („čakajúce vlaky“ – príčastie – obmedzením nie je), až od, budú
# púšťané či obnovené neskôr („nákladná doprava bude obnovená o 15:00“)
_FUTURE_RESTORE = re.compile(r"\bbud(?:e|u)\s+(?:\w+\s+){0,2}?(?:obnoven|povolen|umoznen|pustan|pusten|spusten)\w*")
_FREIGHT_BAN = re.compile(r"\bzakaz\w*|\bnepremava\w*|\bnebud\w*\s+premavat|\bnesm\w*|\bnemoz\w*"
                          r"|\bnie\s+(?:je|su)\s+(?:mozn|povolen|obnoven)\w*|\bnebude\s+(?:mozn|povolen)\w*"
                          r"|\bcak(?:aju|at|a)\b|\bpockaj\w*|\bmusi\w*\s+(?:\w+\s+)?(?:cakat|stat|zostat|ostat|pockat)"
                          r"|\bzastaven\w*|\bprerusen\w*|\bneprejd\w*|\bnepojdu\b|\baz\s+od\b|\baz\s+po\b|\bodstaven\w*"
                          r"|\bneprechadz\w*|\bnejazd\w*|\bobmedzen\w*|\buzavret\w*|\buzatvoren\w*|\bzatvoren\w*"
                          r"|\bnie\b|" + _FUTURE_RESTORE.pattern)
# obmedzenie pre nákladné vlaky skončilo či nie je: „obmedzenia pre nákladné vlaky nie sú“, „žiadne obmedzenia“, vlaky
# pokračujú, budú vypravené, premávajú bez obmedzenia, vracajú sa na pôvodnú trasu
_BAN_LIFTED = re.compile(r"zrusen|odvolan|ukoncen|skoncen|neplati|uz\s+nie|bez\s+obmedzen|vratane\s+nakladn"
                         r"|pre\s+vsetky\s+vlaky|\bnetyk\w*"
                         r"|\bne(?:bude|budu|su|je)\s+(?:\w+\s+)?(?:obmedzen|zastaven|prerusen|uzavret)\w*"
                         r"|\bnie\s+(?:je|su|bude|budu)\s+(?:obmedzen|zastaven|prerusen)\w*"
                         r"|(?:obmedzen|zakaz)\w*(?:\s+[^\s,;.]+){0,5}?\s+nie\s+(?:je|su|bude|budu|plati)\b"
                         r"|\bziadn\w*\s+(?:\w+\s+)?(?:obmedzen|zakaz)")
_FREIGHT_MOVES = re.compile(r"\bpokracuj\w*|\bvypraven\w*|\bvypravuj\w*|\bodchadzaj\w*|\bjazdia\b|\bpojdu\b|\bpremavaj\w*"
                            r"|\bbud(?:e|u)\s+(?:sa\s+)?(?:vypravovat|vypravat|premavat)\b"
                            r"|\bnakladn\w*\s+(?:doprav|preprav)\w*\s+(?:\w+\s+){0,2}?od\s+\d"
                            r"|\bbud(?:e|u)\s+(?:\w+\s+){0,2}?(?:prechadzat|premavat|jazdit|ist|pokracovat)\b"
                            r"|\b(?:uz\s+)?premavaj\w*\s+(?:bez|normal|podla|uz)|\buz\s+(?:premavaj|jazdia|prechadzaj|idu)\w*"
                            r"|\bmozu\s+(?:\w+\s+)?(?:ist|prechadzat|pokracovat|premavat|jazdit)\b"
                            r"|\bvracaj\w*\s+(?:sa\s+)?na\s+povodn")
_UNTIL_TIME = re.compile(r"\b(?:do|az\s+od|az\s+po|od)\s*$")
# obmedzenie rýchlosti (nie zákaz jazdy): „obmedzenie rýchlosti na 40 km/h“, „obmedzenou rýchlosťou“, „40 km/h“
_FREIGHT_SPEED = re.compile(r"(?:obmedzen|omezen|znizen|snizen)\w*\s+(?:\w+\s+)?rychlost\w*(?:\s+na)?"
                            r"|rychlost\w*\s+(?:\w+\s+){0,2}?(?:obmedzen|omezen|znizen|snizen)\w*|\d{1,3}\s?km\s?/\s?h")


# zrušenie zákazu v zápore („zákaz … zatiaľ nebol zrušený“, „obmedzenie nie je odvolané“) – zákaz trvá
_LIFT_NEGATED = re.compile(r"\b(?:nebol|nebola|nebolo|neboli|nie\s+je|nie\s+su|nebude|nebudu|zatial\s+nie)\s+"
                           r"(?:(?:este|zatial|doteraz|dosial|uz)\s+)?(?:zrusen|odvolan|ukoncen|skoncen)\w*")
# nákladné vlaky môžu ísť (sú vypravované) až od času – do tohto času obmedzenie trvá („môžu ísť od 15:00“)
_RESUME_TIME = re.compile(r"\b(?:od|po|o)\s+\d{1,2}\s?[:.]\s?\d{2}")


def _blank_end_phrases(seg: str) -> str:
    """Časť vety bez slov skončenia mimoriadnosti („mimoriadna udalosť bola ukončená“, „prevádzka je obnovená“) – tie
    zákaz nákladných vlakov neukončujú; obnovenie samotnej nákladnej dopravy („nákladná doprava bude obnovená o 15:00“)
    ostáva."""
    def blank(m: re.Match) -> str:
        if _FREIGHT.search(m.group(0)) or _FUTURE_RESTORE.search(m.group(0)):
            return m.group(0)
        return " " * len(m.group(0))

    return _INCIDENT_END.sub(blank, _INCIDENT_END_PHRASE.sub(blank, seg))


def _freight_clauses(c: _Ctx, s: int, e: int) -> list[tuple[int, int]]:
    """Časti vety ako :func:`_clauses`, rozdelené aj za „hod.“ pred veľkým písmenom („… ukončená o 11:40 hod. Nákladné
    vlaky …“ sú dve vety – skratka „hod.“ vetu inak nerozdelí)."""
    out: list[tuple[int, int]] = []
    for a, b in _clauses(c.fq, s, e):
        if out and re.match(r"\s*ktor\w*\b", c.fq[a:b]) and re.search(r"\d{1,2}\s?[:.]\s?\d{2}", c.fq[a:b]):
            out[-1] = (out[-1][0], b)     # vedľajšia veta („…, okrem nákladných vlakov, ktoré môžu ísť až o 18:00“)
            continue
        cut = a
        for m in re.finditer(r"\bhod\.\s+(?=[A-ZÁČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ])", c.text[a:b]):
            out.append((cut, a + m.end()))
            cut = a + m.end()
        out.append((cut, b))
    return [x for x in out if c.fq[x[0]:x[1]].strip()]


def _freight_ban(c: _Ctx) -> Optional[tuple[int, int, Optional[datetime], str, bool]]:
    """Časť vety o obmedzení, ktoré po obnovení prevádzky trvá pre nákladné vlaky (vlaky s RID): (začiatok, koniec,
    do kedy – miestny čas alebo None, komu platí, čas je čas obnovenia jazdy – „môžu ísť od 15:00“).

    Slová skončenia mimoriadnosti v tej istej časti vety („mimoriadna udalosť bola ukončená“) zrušením zákazu
    nákladných vlakov nie sú; zrušenie v zápore („zákaz … zatiaľ nebol zrušený“) znamená, že zákaz trvá."""
    for s, e in _sentence_spans(c.text, c.fq, c.subject_end, len(c.text)):
        if c.in_quote(s):
            continue
        for a, b in _freight_clauses(c, s, e):
            seg = c.fq[a:b]
            if not _FREIGHT.search(seg):
                continue
            core = _blank_end_phrases(seg)
            negated = bool(_LIFT_NEGATED.search(core))
            lift_text = _LIFT_NEGATED.sub(lambda x: " " * len(x.group(0)), core)
            timed_move = bool(_FREIGHT_MOVES.search(core) and _RESUME_TIME.search(core))
            # obmedzenie rýchlosti pre nákladné vlaky („platí do 18:00 obmedzenie rýchlosti na 40 km/h“, „premávajú
            # obmedzenou rýchlosťou“) je pomalá jazda, nie zákaz ich jazdy
            ban_text = _FREIGHT_SPEED.sub(lambda x: " " * len(x.group(0)), core)
            if not (negated or timed_move or _FREIGHT_BAN.search(ban_text)) or _BAN_LIFTED.search(lift_text):
                continue
            future = bool(_FUTURE_RESTORE.search(core))
            resume = False
            if _FREIGHT_MOVES.search(core) and not future and not negated and \
                    not re.search(r"\baz\s+(?:od|po)\b|\bdo\s+\d", core):
                if re.search(r"\buz\b", core) or not _RESUME_TIME.search(core):
                    continue              # „Odstavené nákladné vlaky pokračujú v jazde“ – obmedzenie netrvá
                resume = True             # „Nákladné vlaky môžu ísť od 15:00“ – do 15:00 obmedzenie trvá
            label = ("vlaky s nebezpečným tovarom (RID)" if re.search(r"\brid\b|nebezpecn", seg)
                     and not re.search(r"nakladn", seg) else "nákladné vlaky")
            until = None
            for t in c.toks:
                if not (a <= t.start < b) or t.kind not in ("time", "datetime"):
                    continue
                before = c.fq[max(a, t.start - 7):t.start]
                if not (_UNTIL_TIME.search(before) or ((future or resume) and re.search(r"\b(?:o|v|vo|po)\s*$",
                                                                                          before))):
                    continue
                if t.kind == "datetime":
                    until = t.dt
                else:
                    days = [d for d in c.toks if d.kind in ("date", "datetime") and d.day]
                    day = min(days, key=lambda d: abs(d.start - t.start)).day if days else c.ref
                    until = datetime.combine(day, t.tm) + timedelta(days=1 if t.plus_day else 0)
                break
            return a, b, until, label, resume
    return None


def _freight_mentions(c: _Ctx) -> list[str]:
    """Časti viet nového textu o nákladných vlakoch (vlakoch s RID), ktoré obmedzenie výslovne neukončujú – pri
    oznámení o skončení mimoriadnosti ich treba overiť (pre nákladné vlaky môže obmedzenie trvať)."""
    out: list[str] = []
    for s, e in _sentence_spans(c.text, c.fq, c.subject_end, len(c.text)):
        if c.in_quote(s):
            continue
        for a, b in _freight_clauses(c, s, e):
            seg = c.fq[a:b]
            if not _FREIGHT.search(seg):
                continue
            core = _blank_end_phrases(seg)
            if (_BAN_LIFTED.search(_LIFT_NEGATED.sub(" ", core)) or (_FREIGHT_MOVES.search(core) and (
                    not _RESUME_TIME.search(core) or re.search(r"\buz\b", core)))) and not _LIFT_NEGATED.search(core):
                continue
            out.append(one_line(c.text[a:b], 160))
    return out


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
                                     r"|nakladka\s+(?:nejdrive\w*|najskor\w*)"
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


def _dedupe_key(it: ExtractedItem) -> tuple:
    """Kľúč rovnakej udalosti: druh, úsek, termín, druh obmedzenia, koľaj a číslo (aj číslo riadku SROV „60/2026/1“)
    – dve obmedzenia toho istého úseku a času (1. TK a vypnutie TV, 1. TK a 2. TK) sú dve udalosti."""
    d = it.data
    return (it.kind, fold(str(d.get("from_station") or "")), fold(str(d.get("to_station") or "")),
            d.get("start"), d.get("end"), str(d.get("kind") or ""), str(d.get("track") or ""),
            " ".join(fold(str(d.get("external_ref") or "")).split()))


def _dedupe(items: list[ExtractedItem]) -> list[ExtractedItem]:
    """Rovnaká udalosť (:func:`_dedupe_key`) len raz – s vyššou istotou. Riadok súborného rozkazu (SROV) s iným druhom
    obmedzenia, koľajou či číslom riadku sa nikdy potichu nestratí. Udalosť z textu, ktorá len opakuje riadok tabuľky
    (rovnaký úsek a termín; druh obmedzenia v texte neuvedený alebo rovnaký, koľaj neuvedená alebo rovnaká), riadok
    nezdvojí – podrobnosti sú v tabuľke."""
    rows: dict = {}
    for it in items:
        if "riadok_tabulky" in (it.evidence or {}):
            k = _dedupe_key(it)
            rows.setdefault(k[:5], []).append((k[5], k[6]))

    def repeats_row(it: ExtractedItem) -> bool:
        if "riadok_tabulky" in (it.evidence or {}):
            return False
        k = _dedupe_key(it)
        assumed = it.field_confidence.get("kind", 0.8) < 0.8      # druh výluky v texte neuvedený
        return any((assumed or k[5] == rk) and (not k[6] or k[6] == rt) for rk, rt in rows.get(k[:5], ()))

    out: list[ExtractedItem] = []
    seen: dict = {}
    for it in items:
        if rows and repeats_row(it):
            continue
        key = _dedupe_key(it)
        if key in seen:
            prev = out[seen[key]]
            if it.confidence > prev.confidence:
                out[seen[key]] = it
            continue
        seen[key] = len(out)
        out.append(it)
    return out
