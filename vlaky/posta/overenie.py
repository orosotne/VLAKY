"""Overenie odosielateľa, SPF/DKIM/DMARC a odhalenie pokusov o prompt injection (sekcie 2.5 a M5).

Verdikty:
  * ``ok`` – povolený odosielateľ s overenou autentifikáciou,
  * ``karantena`` – nepovolený odosielateľ alebo neúspešné overenie; e-mail sa uloží, ale nespracuje,
  * ``podozrivy`` – povolený odosielateľ, ale text obsahuje pokyny typu „ignoruj predchádzajúce pokyny“;
    e-mail sa spracuje len ako DÁTA a každá udalosť z neho vyžaduje potvrdenie dispečerom.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..config import settings
from .parser import ParsedMail
from .text import clean_text, fold

OK, KARANTENA, PODOZRIVY = "ok", "karantena", "podozrivy"


@dataclass
class AuthResult:
    authserv_id: str
    results: dict = field(default_factory=dict)    # metóda → výsledok (spf/dkim/dmarc → pass/fail/…)
    props: dict = field(default_factory=dict)      # metóda → {vlastnosť: hodnota}
    raw: str = ""
    dkim_pass_domains: list = field(default_factory=list)   # domény (d=) všetkých platných podpisov DKIM


@dataclass
class Verification:
    verdict: str
    reasons: list = field(default_factory=list)
    sender_allowed: bool = False
    auth_ok: Optional[bool] = None
    auth: Optional[AuthResult] = None
    injection: list = field(default_factory=list)
    forwarded_from: list = field(default_factory=list)  # pôvodní odosielatelia preposlaného obsahu („?“ = neznámy)

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)


# ---------------------------------------------------------------------- povolení odosielatelia
def _allowed_entries() -> list[str]:
    s = settings()
    return [e.strip().lower().lstrip("@").removeprefix("*.") for e in s.csv(s.allowed_senders)]


def zsr_entries() -> list[str]:
    """Povolení odosielatelia, ktorí sú ŽSR (správca infraštruktúry): organizačná doména „zsr.<tld>“.

    Odvodené zo zoznamu povolených odosielateľov (napr. ``zsr.sk``, ``or.zsr.sk``) – iné povolené domény
    (partneri, zákazníci, vlastní dispečeri) výluky ani trasy ŽSR automaticky meniť nesmú.
    """
    return [e for e in _allowed_entries() if e and _org(e.split("@")[-1]).split(".")[0] == "zsr"]


def zsr_sender(address: str) -> bool:
    """Odosielateľ je ŽSR (výluky, pomalé jazdy, mimoriadnosti a trasy od neho možno zapísať automaticky)."""
    entries = zsr_entries()
    return bool(entries) and sender_allowed(address, entries)


def sender_allowed(address: str, entries: Optional[list[str]] = None) -> bool:
    """Adresa je povolená, ak sa zhoduje celá alebo ak jej doména je povolená doména či jej subdoména.

    Pozor na podvrhy: „falosne-zsr.example“ NIE JE subdoména „zsr.example“.
    """
    address = (address or "").strip().lower()
    if address.count("@") != 1:
        return False
    local, domain = address.split("@")
    if not local or not domain or ".." in domain or not re.fullmatch(r"[a-z0-9.-]+", domain):
        return False
    for e in entries if entries is not None else _allowed_entries():
        if not e:
            continue
        if "@" in e:
            if address == e:
                return True
        elif domain == e or domain.endswith("." + e):
            return True
    return False


# ---------------------------------------------------------------------- Authentication-Results
def _strip_comments(value: str) -> str:
    out, depth, quoted, esc = [], 0, False, False
    for ch in value:
        if esc:
            esc = False
            if depth == 0:
                out.append(ch)
            continue
        if ch == "\\":
            esc = True
            if depth == 0:
                out.append(ch)
            continue
        if ch == '"' and depth == 0:
            quoted = not quoted
            out.append(ch)
            continue
        if not quoted:
            if ch == "(":
                depth += 1
                continue
            if ch == ")" and depth:
                depth -= 1
                continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def parse_authentication_results(header: str) -> Optional[AuthResult]:
    """Rozoberie jednu hlavičku Authentication-Results (RFC 8601)."""
    if not header:
        return None
    text = " ".join(_strip_comments(header).split())
    parts = [p.strip() for p in text.split(";")]
    if not parts or not parts[0]:
        return None
    authserv = parts[0].split()[0].lower()
    res = AuthResult(authserv_id=authserv, raw=header)
    for p in parts[1:]:
        if not p or p.lower() == "none":
            continue
        m = re.match(r"([a-z0-9_-]+)\s*=\s*([a-z0-9_-]+)(.*)$", p, re.I)
        if not m:
            continue
        method, result, rest = m.group(1).lower(), m.group(2).lower(), m.group(3)
        props = {k.lower(): v.strip('"').lower() for k, v in re.findall(r"([a-z0-9_.-]+)\s*=\s*(\"[^\"]*\"|\S+)",
                                                                           rest, re.I)}
        if method == "dkim" and result == "pass":
            d = props.get("header.d") or props.get("header.i", "").split("@")[-1]
            if d:
                res.dkim_pass_domains.append(d)
        # pri viacerých podpisoch DKIM stačí jeden platný
        if method in res.results and method == "dkim" and res.results[method] == "pass":
            continue
        res.results[method] = result
        res.props[method] = props
    return res


# druhá úroveň domény, pod ktorou sa registrujú domény organizácií (co.uk, com.pl, co.hu, gv.at …) –
# zjednodušená náhrada zoznamu verejných prípon (Public Suffix List) pre národné domény
_PUBLIC_SLD = {"ac", "co", "com", "edu", "gov", "gv", "go", "or", "org", "net", "nom", "mil", "sch", "ltd", "plc",
               "info", "biz", "priv", "firm", "gen", "int", "web", "me", "tm", "in", "kiev", "waw"}


def _org(domain: str) -> str:
    """Organizačná doména na kontrolu zarovnania (DMARC „relaxed“).

    Posledné dve časti; pri národnej doméne s verejnou druhou úrovňou (``evil.co.hu``, ``firma.com.pl``)
    posledné tri – inak by ``evil.co.hu`` bola „tá istá organizácia“ ako ``partner.co.hu``.
    """
    parts = [p for p in (domain or "").lower().strip(".").split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _PUBLIC_SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else ".".join(parts)


def check_auth(auth_headers: list[str], from_domain: str,
               trusted_ids: Optional[list[str]] = None) -> tuple[bool, str, Optional[AuthResult]]:
    """Vyhodnotí SPF/DKIM/DMARC. Vráti (ok, dôvod, použitá hlavička).

    Dôverujeme len hlavičkám s authserv-id vlastného poštového servera (``trusted_authserv_ids``);
    ak nie sú nastavené, len najvrchnejšej hlavičke (pridal ju posledný server – náš).
    """
    if trusted_ids is None:
        s = settings()
        trusted_ids = [x.lower() for x in s.csv(s.trusted_authserv_ids)]
    parsed = [r for r in (parse_authentication_results(h) for h in auth_headers) if r]
    if trusted_ids:
        cands = [r for r in parsed if r.authserv_id in trusted_ids]
        if not cands:
            return False, "chýba hlavička Authentication-Results od dôveryhodného servera", None
    else:
        cands = parsed[:1]
        if not cands:
            return False, "chýba hlavička Authentication-Results (SPF/DKIM/DMARC)", None
    ar = cands[0]
    from_org = _org(from_domain)
    dmarc = ar.results.get("dmarc")
    if dmarc is not None:
        if dmarc != "pass":
            return False, f"DMARC={dmarc}", ar
        hfrom = ar.props.get("dmarc", {}).get("header.from")
        if hfrom and _org(hfrom) != from_org:
            return False, f"DMARC overoval inú doménu ({hfrom})", ar
        return True, "DMARC=pass", ar
    spf, dkim = ar.results.get("spf"), ar.results.get("dkim")
    if spf == "pass" and dkim == "pass":
        # ako DMARC: aspoň jedna overená identita musí patriť organizácii odosielateľa; chýbajúca identita
        # (napr. SPF len na HELO pri prázdnom MAIL FROM) zarovnaná nie je
        mailfrom = ar.props.get("spf", {}).get("smtp.mailfrom", "")
        spf_d = mailfrom.split("@")[-1] if mailfrom else ""
        aligned = [f"DKIM ({d})" for d in ar.dkim_pass_domains if from_org and _org(d) == from_org]
        if spf_d and from_org and _org(spf_d) == from_org:
            aligned.append(f"SPF ({spf_d})")
        if not aligned:
            return False, "SPF/DKIM nie sú zarovnané s doménou odosielateľa", ar
        return True, "SPF=pass a DKIM=pass (bez DMARC, zarovnané: " + ", ".join(aligned) + ")", ar
    return False, f"SPF={spf or 'chýba'}, DKIM={dkim or 'chýba'}, DMARC chýba", ar


# ---------------------------------------------------------------------- prompt injection
_INJECTION_PATTERNS: list[tuple[str, str]] = [
    # pozor na falošné poplachy: zdvorilé „pošlite/ignorujte“ (pre dispečera) je bežné, hľadáme pokyny pre stroj
    (r"ignoruj\w*\s+(?:\w+\s+){0,3}(?:predchadzajuc|predosl|doterajs|vsetk|tiet|vyssie|uvedene|predchozi|predesl)\w*"
     r"\s+(?:\w+\s+)?(?:pokyn|instrukci|instrukc|prikaz|pravidl|nastaven|zadani)", "ignoruj predchádzajúce pokyny"),
    (r"\bignoruj\s+(?:\w+\s+){0,2}(?:pokyny|instrukcie|instrukce|pravidla|system)", "ignoruj pokyny/pravidlá"),
    (r"\bzabudni\s+(?:\w+\s+){0,2}(?:pokyn|instrukci|pravidl|co\s+ti)", "zabudni pokyny"),
    (r"\bzapomen\s+(?:\w+\s+){0,2}(?:pokyn|instrukc|pravidl)", "zapomeň pokyny"),
    (r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions|prompts?"
     r"|rules|directions|messages)", "ignore previous instructions"),
    (r"disregard\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier)", "disregard previous"),
    (r"forget\s+(?:all\s+|your\s+|the\s+)?(?:previous\s+)?(?:instructions|rules|prompt)", "forget instructions"),
    (r"system\s*prompt|systemov\w*\s+(?:prompt|pokyn|instrukci)|system\s+message", "system prompt"),
    (r"<\|?\s*(?:im_start|im_end|system|endoftext)\s*\|?>|\[/?inst\]|<<\s*sys\s*>>|###\s*(?:instruction|system)",
     "formátovacie značky jazykových modelov"),
    (r"(?:si|jsi)\s+(?:teraz|odteraz|ted|nyni)\s+(?:\w+\s+){0,3}(?:asistent|agent|model|ai\b)", "zmena roly"),
    (r"you\s+are\s+now\s+|act\s+as\s+(?:an?\s+)?(?:ai|assistant|agent|admin)|jailbreak|developer\s+mode|\bdan\s+mode",
     "zmena roly (EN)"),
    (r"\b(?:posli|odosli)\s+(?:\w+\s+){0,4}na\s+(?:adresu\s+|e-?mail\s+|cislo\s+)?\S+", "pošli e-mail na"),
    (r"\b(?:preposli|preposlij)\b", "prepošli"),
    (r"forward\s+(?:this|the|all|these|it|every)\b[^\n]{0,80}\bto\b", "forward this to"),
    (r"send\s+(?:this|the|all|an?\s+e-?mail|a\s+message|it|everything)\b[^\n]{0,60}\bto\s+\S+@", "send to"),
    (r"(?:automaticky|bez\s+(?:potvrdenia|kontroly|overenia))\s+(?:\w+\s+){0,2}(?:schval|potvrd|aplikuj|zapis|odosli)\b",
     "obídenie potvrdenia"),
    (r"\b(?:schval|potvrd|aplikuj|zapis)\s+(?:\w+\s+){0,3}(?:automaticky|bez\s+(?:potvrdenia|kontroly|overenia))",
     "obídenie potvrdenia"),
    (r"\b(?:vymaz|zmaz|odstran)\s+(?:vsetk\w*\s+)?(?:vyluky|register|databaz\w*|zaznamy|vlaky|trasy)|drop\s+table"
     r"|delete\s+all", "mazanie dát"),
    (r"\b(?:zmen|uprav)\s+(?:svoje\s+)?(?:nastaven\w*|konfigurac\w*|opravnen\w*|zoznam\s+(?:povolen|prijemc))",
     "zmena nastavení"),
    (r"\bapi[_ -]?key\b|\b(?:posli|uved|napis|zadaj)\s+(?:\w+\s+)?(?:heslo|prihlasovac\w*\s+udaj)",
     "žiadosť o tajné údaje"),
]
_INJECTION_RES = [(re.compile(p), label) for p, label in _INJECTION_PATTERNS]


def detect_injection(*texts: str) -> list[str]:
    """Zoznam nájdených podozrivých pokynov (popis + citácia)."""
    hits: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        cleaned = clean_text(text)
        folded = " ".join(fold(cleaned).split())
        for rx, label in _INJECTION_RES:
            m = rx.search(folded)
            if m and label not in seen:
                seen.add(label)
                quote = folded[max(0, m.start() - 10):m.end() + 30].strip()
                hits.append(f"{label}: „…{quote}…“")
    return hits


# ---------------------------------------------------------------------- celkové overenie
def verify(pm: ParsedMail, attachment_texts: Optional[list[str]] = None) -> Verification:
    """Overí odosielateľa a autentifikáciu; pri povolenom odosielateľovi hľadá prompt injection."""
    s = settings()
    v = Verification(verdict=OK)
    if pm.from_count > 1:
        v.verdict = KARANTENA
        v.reasons.append("hlavička From obsahuje viac adries")
        return v
    if not pm.sender:
        v.verdict = KARANTENA
        v.reasons.append("chýba adresa odosielateľa")
        return v
    v.sender_allowed = sender_allowed(pm.sender)
    if not v.sender_allowed:
        v.verdict = KARANTENA
        v.reasons.append(f"odosielateľ {pm.sender} nie je na zozname povolených")
        return v
    if s.require_dmarc_pass:
        ok, why, ar = check_auth(pm.auth_results, pm.sender.split("@")[-1])
        v.auth_ok, v.auth = ok, ar
        if not ok:
            v.verdict = KARANTENA
            v.reasons.append(f"neúspešné overenie odosielateľa: {why}")
            return v
        v.reasons.append(f"overenie odosielateľa: {why}")
    problem = _check_forwarded(pm, v, s.require_dmarc_pass)
    if problem:
        v.verdict = KARANTENA
        v.reasons.append(problem)
        return v
    texts = [pm.subject, pm.body_text] + list(attachment_texts or [a.text for a in pm.attachments])
    v.injection = detect_injection(*texts)
    if v.injection:
        v.verdict = PODOZRIVY
        v.reasons.append("e-mail obsahuje pokyny pre systém (možný pokus o prompt injection) – spracuje sa len "
                         "ako dáta a všetko vyžaduje potvrdenie dispečerom: " + "; ".join(v.injection[:5]))
    return v


def _check_forwarded(pm: ParsedMail, v: Verification, require_auth: bool) -> str:
    """Preposlaný obsah: pôvodný odosielateľ musí byť povolený a (ak sa vyžaduje) overený ako pri priamom e-maile.

    Overenie preposielateľa (napr. pravidla dispečerskej schránky) nestačí – podvrh ŽSR by inak prešiel
    len vďaka DMARC vlastnej domény. Vráti dôvod karantény; prázdny reťazec = v poriadku. Aj overený
    preposlaný obsah sa nikdy nezapisuje automaticky (rozhoduje spracovanie podľa ``forwarded_from``).
    """
    senders: list[str] = []
    for fw in pm.forwarded:
        if fw.from_count > 1:
            return f"preposlaná správa ({fw.how}) má v hlavičke From viac adries"
        if not fw.sender:
            if fw.inline:
                senders.append("?")          # vložené preposlanie bez adresy – nedá sa overiť, len potvrdenie
                continue
            return f"preposlaná správa ({fw.how}) nemá adresu pôvodného odosielateľa"
        if not sender_allowed(fw.sender):
            return f"preposlaná správa ({fw.how}) je od odosielateľa {fw.sender}, ktorý nie je na zozname povolených"
        if require_auth and not fw.inline:
            ok, why, _ar = check_auth(fw.auth_results, fw.sender.split("@")[-1])
            if not ok:
                return f"preposlaná správa ({fw.how}) od {fw.sender} neprešla overením odosielateľa: {why}"
        senders.append(fw.sender)
    uniq = list(dict.fromkeys(senders))
    if "?" in uniq and len(uniq) > 1:
        uniq.remove("?")
    v.forwarded_from = uniq
    if uniq:
        who = ", ".join(x if x != "?" else "neznámy odosielateľ" for x in uniq)
        v.reasons.append(f"obsahuje preposlanú správu (pôvodne od: {who}) – nič z nej sa nezapíše bez potvrdenia "
                         "dispečerom")
    return ""
