"""5. revízia – integrácia požiadaviek medzi oblasťami (strana registra, webu a príkazového riadku).

- (1) zmena, zrušenie či nový termín, ktorý uvádza len číslo ROV (bez úseku): od 6. revízie sa úsek z registra výluk
  automaticky nedopĺňa – udalosť ostáva bez miesta (poistka pre celý vozový park, upozornenie DÔLEŽITÉ) a fronta ponúkne
  výluky s týmto číslom ako návrh (úsek, termín, OR, druh dokumentu); dispečer jednu vyberie alebo úsek opraví;
- (2) hlavičky distribučného zoznamu (Precedence: bulk/list, List-Unsubscribe) sú slabým signálom: e-mail s výlukou,
  číslom dokumentu ŽSR a termínom či úsekom sa nikdy nepotlačí (6. revízia), newsletter bez čísla dokumentu je „iné“;
- (6) zastaraný formulár dočasného záznamu zmeny výluky, ktorý potvrdenie či zamietnutie zmeny odstránilo: nie 404 a
  strata zadania – formulár výluky, do ktorej sa zmena zapísala, s neuloženým zadaním a vysvetlením;
- (7) test-notify --kanal sms|teams v tieňovej prevádzke vysvetlí, že SMS a Teams sú vypnuté (kód 1 ostáva).

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

import html
import re

import pytest
from sqlalchemy import select

from vlaky.models import AuditLog, Closure, ExtractedEvent, Notification
from test_revizia3_terminy import as_utc, drop, events, raw_mail, utc
from test_revizia4_pravidla import ROV_KE, ROV_ZA, _decide, _drop_at, _mail, _process, env  # noqa: F401 – fixture env
from test_web_spolocne import client, csrf, login_as, web_app  # noqa: F401 – fixtures

ROV_TT = "ZSR OR Trnava <vyluky.ortt@zsr.example>"
ROV_ZV = "ZSR OR Zvolen <vyluky.orzv@zsr.example>"


def _confirm(session, ev_id: int, actor: str = "dispecer1", **edits):
    from vlaky.posta.spracovanie import confirm_event

    ev = confirm_event(session, ev_id, actor, edits=edits or None)
    session.commit()
    return ev


def _register(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> list[int]:
    """E-mail ŽSR s výlukou spracovaný a potvrdený (výluka v registri, overená). Vráti čísla jeho udalostí."""
    before = {e.id for e in events(session)}
    drop(env, name, raw_mail(subject, body, name.replace(".", "-"), sender=sender, date=date))
    _process(session, now)
    new = [e for e in events(session) if e.id not in before]
    for e in new:
        _confirm(session, e.id)
    return [e.id for e in new]


def _new_events(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> list:
    before = {e.id for e in events(session)}
    drop(env, name, raw_mail(subject, body, name.replace(".", "-"), sender=sender, date=date))
    _process(session, now)
    return [e for e in events(session) if e.id not in before]


def _active(session) -> list[Closure]:
    return [c for c in session.scalars(select(Closure).order_by(Closure.id))
            if c.status != "zrusene" and c.proposed_change_of is None]


def _alerts(session) -> list[str]:
    return list(session.scalars(select(Notification.subject).where(Notification.subject.contains("Na potvrdenie čaká"))
                                .order_by(Notification.id)))


def _texts(ev: ExtractedEvent) -> str:
    val = ev.validation or {}
    return " | ".join(str(x) for k in ("warnings", "notes", "confirmation_reasons") for x in (val.get(k) or []))


def _green(r) -> bool:
    return r.color == "ZELENÁ"


# ====================================================================== (1) číslo ROV bez úseku – návrh z registra
def _candidates(session, ev) -> list:
    from vlaky.posta.spracovanie import register_candidates

    return register_candidates(session, ev)


def test_odklad_len_podla_cisla_rov_ostava_bez_miesta_s_navrhom(env, session):
    """B04b (6. revízia – úsek z registra sa nedopĺňa): „Výluka podľa ROV 5310/2026 sa odkladá o týždeň, na noc
    14./15.12.2026, v rovnakom čase.“ – e-mail neuvádza úsek ani čitateľný termín. ROV 5310/2026 (OR Košice) je v registri
    raz (Kysak – Prešov): udalosť úsek nedostane (ostáva udalosťou bez miesta), fronta výluku ponúkne ako návrh. Poistka
    pre celý vozový park – vlak Senec → Galanta ani Kysak → Prešov nie je ZELENÝ; upozornenie DÔLEŽITÉ. Zmena sa nezapíše
    bez dispečera."""
    _register(env, session, "rov5310.eml", "ROV c. 5310/2026 Kysak - Presov",
              "Dobrý deň,\n\nnočná výluka v úseku Kysak – Prešov v noci 07./08.12.2026 od 22:00 do 04:00 hod. "
              "(ROV 5310/2026).\n\nOR Košice\n", ROV_KE, "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (ev,) = _new_events(env, session, "odklad.eml", "ROV 5310/2026 - odklad vyluky",
                        "Výluka podľa ROV 5310/2026 sa odkladá o týždeň, na noc 14./15.12.2026, v rovnakom čase.\n\n"
                        "OR Košice\n", ROV_KE, "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert ev.kind == "vyluka_zmena" and ev.status == "navrh" and ev.needs_confirmation
    assert not ev.data.get("from_station") and not ev.data.get("to_station"), ev.data
    [c] = _active(session)
    assert [x["id"] for x in _candidates(session, ev)] == [c.id]
    assert _alerts(session), "udalosť bez miesta musí dostať upozornenie DÔLEŽITÉ"
    assert (c.from_station, c.to_station, as_utc(c.start_utc)) == ("Kysak", "Prešov", utc(2026, 12, 7, 21))
    other = _decide(session, "70101", "Senec", "Galanta", utc(2026, 12, 14, 22), utc(2026, 12, 14, 19))
    assert not _green(other), other.summary
    on = _decide(session, "70102", "Kysak", "Prešov", utc(2026, 12, 14, 22), utc(2026, 12, 14, 19))
    assert not _green(on), on.summary


def test_zmena_len_podla_cisla_s_terminom_po_vybere_useku(env, session):
    """B07b: „Zmena ROV 6100/2026: výluka dňa 02.12.2026 sa posúva na 04.12.2026 (8:00 – 14:00)“ – úsek sa z registra
    nedoplní (vlaky kdekoľvek nie sú ZELENÉ, kým ju dispečer nevybaví); po potvrdení s úsekom výluky z návrhu sa zmena
    zapíše do výluky v registri."""
    _register(env, session, "rov6100.eml", "ROV 6100/2026 Hronska Dubrava - Ziar nad Hronom",
              "Dobrý deň,\n\núplná výluka v úseku Hronská Dúbrava – Žiar nad Hronom dňa 02.12.2026 od 08:00 do 14:00 "
              "hod. (ROV 6100/2026).\n", ROV_ZV, "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (ev,) = _new_events(env, session, "zmena6100.eml", "Zmena ROV 6100/2026",
                        "Zmena ROV 6100/2026: výluka dňa 02.12.2026 sa posúva na 04.12.2026 (8:00 – 14:00), ostatné "
                        "termíny bez zmeny.\n", ROV_ZV, "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert ev.kind == "vyluka_zmena" and ev.needs_confirmation
    assert not ev.data.get("from_station"), ev.data
    [cand] = _candidates(session, ev)
    on = _decide(session, "70201", "Hronská Dúbrava", "Žiar nad Hronom", utc(2026, 12, 4, 9), utc(2026, 12, 4, 6))
    assert not _green(on), on.summary
    other = _decide(session, "70202", "Senec", "Galanta", utc(2026, 12, 4, 9), utc(2026, 12, 4, 6))
    assert not _green(other), other.summary
    ev = _confirm(session, ev.id, from_station=cand["from_station"], to_station=cand["to_station"])
    assert ev.validation["apply_result"]["ok"] is True, ev.validation["apply_result"]
    [c] = _active(session)
    assert (c.from_station, as_utc(c.start_utc), as_utc(c.end_utc)) == ("Hronská Dúbrava", utc(2026, 12, 4, 7),
                                                                         utc(2026, 12, 4, 13))


def test_novy_termin_len_podla_cisla_ostava_bez_miesta(env, session):
    """C08b: „Termín 15.12.2026 sa ruší. Výluka sa doplňuje o termín 17.12.2026 od 8:00 do 14:00 hod.“ – nový termín
    úsek z registra nedostane (poistka pre celý vozový park: vlak Prešov → Lipany aj Senec → Galanta 17.12. o 10:00 nie je
    ZELENÝ), fronta ponúkne výluku ROV 5340/2026; zrušenie 15.12. ostáva zrušením tej istej výluky (podľa čísla)."""
    _register(env, session, "rov5340.eml", "ROV 5340/2026 Presov - Lipany",
              "Dobrý deň,\n\núplná výluka v úseku Prešov – Lipany dňa 15.12.2026 od 08:00 do 14:00 hod. "
              "(ROV 5340/2026).\n", ROV_KE, "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    new = _new_events(env, session, "uprava5340.eml", "ROV 5340/2026 - uprava terminov",
                      "Termín 15.12.2026 sa ruší. Výluka sa doplňuje o termín 17.12.2026 od 8:00 do 14:00 hod. "
                      "Ostatné termíny platia bez zmeny.\n", ROV_KE, "Mon, 07 Dec 2026 09:10:00 +0100",
                      utc(2026, 12, 7, 8, 12))
    nova = next(e for e in new if e.kind == "vyluka_nova")
    assert not nova.data.get("from_station") and nova.needs_confirmation, nova.data
    assert [x["from_station"] for x in _candidates(session, nova)] == ["Prešov"]
    on = _decide(session, "70301", "Prešov", "Lipany", utc(2026, 12, 17, 9), utc(2026, 12, 17, 6))
    assert not _green(on), on.summary
    other = _decide(session, "70302", "Senec", "Galanta", utc(2026, 12, 17, 9), utc(2026, 12, 17, 6))
    assert not _green(other), other.summary
    cancel = next(e for e in new if e.kind == "vyluka_zrusenie")
    assert any("Zruší výluku #1" in w for w in cancel.validation["warnings"]), cancel.validation["warnings"]


def test_storno_len_podla_cisla_zrusi_celu_vyluku_po_potvrdeni(env, session):
    """„STORNO – ROV 5330/2026“ bez úseku: zrušenie úsek nedostane, k výluke ho priradí číslo (fronta to povie –
    „Zruší výluku #1 …“); automaticky sa nezapíše, po potvrdení je výluka zrušená."""
    _register(env, session, "rov5330.eml", "ROV 5330/2026 Kosice - Cana",
              "Dobrý deň,\n\núplná výluka v úseku Košice – Čaňa dňa 10.12.2026 od 07:00 do 12:00 hod. "
              "(ROV 5330/2026).\n", ROV_KE, "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (ev,) = _new_events(env, session, "storno.eml", "STORNO - ROV 5330/2026",
                        "Dobrý deň,\n\nvýluku v predmete stornujeme.\n\nOR Košice\n", ROV_KE,
                        "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert ev.kind == "vyluka_zrusenie" and ev.status == "navrh"
    assert not ev.data.get("from_station") and not ev.data.get("start"), ev.data
    assert any("Zruší výluku #1 Košice – Čaňa" in w for w in ev.validation["warnings"]), ev.validation["warnings"]
    assert _active(session), "zrušenie sa bez dispečera nezapíše"
    ev = _confirm(session, ev.id)
    assert ev.validation["apply_result"]["ok"] is True and not _active(session)


def test_cislo_rov_na_viacerych_usekoch_ostava_na_vyber_dispecerovi(env, session):
    """ROV 3360/2026 je v registri na dvoch úsekoch (Trnava – Senica, Senica – Kúty): zmena len podľa čísla úsek
    nedostane, fronta ponúkne obe výluky; poistka pre celý vozový park ostáva (vlak Senec → Galanta nie je ZELENÝ)."""
    _register(env, session, "rov3360.eml", "ROV 3360/2026 - vyluky na trati 116",
              "Dňa 09.12.2026 od 7:00 do 13:00 hod. budú úplné výluky v úsekoch Trnava – Senica a Senica – Kúty "
              "(ROV 3360/2026).\n", ROV_TT, "Fri, 27 Nov 2026 09:00:00 +0100", utc(2026, 11, 27, 8, 2))
    assert len(_active(session)) == 2
    (ev,) = _new_events(env, session, "odklad3360.eml", "ROV 3360/2026 - odklad",
                        "Výluka podľa ROV 3360/2026 sa odkladá o týždeň.\n\nOR Trnava\n", ROV_TT,
                        "Thu, 03 Dec 2026 09:00:00 +0100", utc(2026, 12, 3, 8, 2))
    assert ev.kind == "vyluka_zmena"
    assert not ev.data.get("from_station") and not ev.data.get("to_station"), ev.data
    text = " | ".join(x["text"] for x in _candidates(session, ev))
    assert "Trnava – Senica" in text and "Senica – Kúty" in text, text
    other = _decide(session, "70401", "Senec", "Galanta", utc(2026, 12, 9, 9), utc(2026, 12, 9, 6))
    assert not _green(other), other.summary


def test_cislo_rov_inej_or_navrh_s_upozornenim(env, session):
    """ROV 15/2026 v registri je od OR Žilina (Žilina – Púchov); odklad „ROV 15/2026“ od OR Košice úsek nedostane, návrh
    vo fronte upozorní, že výluka je od inej OR (číslo ROV má každá OR vlastné); poistka pre celý vozový park ostáva."""
    _register(env, session, "rov15.eml", "ROV c. 15/2026 Zilina - Puchov",
              "Dobrý deň,\n\núplná výluka v úseku Žilina – Púchov dňa 10.12.2026 od 08:00 do 14:00 hod. "
              "(ROV č. 15/2026).\n", ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (ev,) = _new_events(env, session, "odklad15.eml", "ROV 15/2026 - odklad",
                        "Výluka podľa ROV 15/2026 sa odkladá o týždeň.\n\nOR Košice\n", ROV_KE,
                        "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert ev.kind == "vyluka_zmena"
    assert not ev.data.get("from_station"), ev.data
    [cand] = _candidates(session, ev)
    assert cand["from_station"] == "Žilina" and any("OR Žilina" in f and "OR Košice" in f for f in cand["flags"]), cand


@pytest.mark.parametrize("body", [
    "Výluka podľa ROV 4120/2026 sa rozširuje aj na úsek N. Zámky – Šurany dňa 11.12.2026.",
    "Výluka podľa ROV 4120/2026 bude dňa 11.12.2026 aj v žst. Šurany.",
    "Výluka podľa ROV 4120/2026 sa odkladá o týždeň, v Senci v rovnakom čase.",
    "Dobrý deň,\n\nvýluka podľa ROV 4120/2026 sa odkladá o týždeň.\n\nS pozdravom\nIng. Ján Novák\nOR Bratislava\n",
], ids=["usek", "zst", "ta_ista_dopravna", "podpis"])
def test_usek_z_registra_sa_nikdy_nedoplni(env, session, body):
    """E-mail len s číslom ROV (aj keď uvádza dopravňu úseku z registra či len podpis): úsek sa z registra nedoplní –
    udalosť ostáva bez miesta, výluka ROV 4120/2026 (Senec – Galanta) je len návrh vo fronte."""
    session.add(Closure(kind="uplna", from_station="Senec", to_station="Galanta", start_utc=utc(2026, 12, 4, 7),
                        end_utc=utc(2026, 12, 4, 13), status="overene", source="email", external_ref="ROV 4120/2026"))
    session.commit()
    new = _new_events(env, session, "z.eml", "ROV 4120/2026", body, "ZSR OR Bratislava <vyluky.orba@zsr.example>",
                      "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert new
    for ev in new:
        assert (ev.validation or {}).get("field_source", {}).get("from_station") != "register", ev.validation
    for ev in new:
        if ev.data.get("from_station"):                # miesto prečítali pravidlá – nie z registra
            assert {ev.data.get("from_station"), ev.data.get("to_station")} != {"Senec", "Galanta"}, ev.data
        elif ev.kind in ("vyluka_nova", "vyluka_zmena", "vyluka_zrusenie"):
            assert [x["from_station"] for x in _candidates(session, ev)] == ["Senec"], ev.data


# ====================================================================== (2) hromadná pošta (zoznam)
_NEWS_BODY = ("ŽSR Spravodaj 11/2026\n\nModernizácia pokračuje\nV decembri pokračujú práce v úseku Kysak – Prešov "
              "(ROV 5211/2026), výluky budú v dňoch 7.12. až 11.12.2026 vždy od 8:00 do 14:00 hod.\n\n"
              "Personálne zmeny\nNa OR Košice nastúpil nový vedúci oddelenia výluk.\n\n"
              "Odhlásiť odber: newsletter-off@zsr.example\n")
_LIST = {"List-Unsubscribe": "<mailto:newsletter-off@zsr.example>"}
_NEWS_FROM = "ŽSR Spravodaj <newsletter@zsr.example>"


def test_newsletter_s_vylukou_cislom_rov_a_terminom_sa_nepotlaci(env, session):
    """r5k_I04 (6. revízia – FR3): ŽSR Spravodaj (Precedence: bulk, List-Unsubscribe, newsletter@) s výlukou, číslom ROV
    a termínom sa nikdy nepotlačí ako hromadný e-mail – výluka čaká na dispečera s poznámkou „overte“ a vlak Kysak →
    Prešov 08.12. o 10:00 nie je ZELENÝ. Prevádzková správa OR (vyluky.orke@) cez distribučný zoznam (aj s predmetom
    bulletinu) a správa bez hlavičiek zoznamu sa spracujú; spravodaj bez čísla dokumentu je „iné“."""
    _drop_at(env, "news.eml", _mail("ŽSR Spravodaj 11/2026", _NEWS_BODY, "news1", _NEWS_FROM,
                                    "Thu, 19 Nov 2026 07:00:00 +0100", headers={"Precedence": "bulk", **_LIST}),
             utc(2026, 11, 19, 6))
    _process(session, utc(2026, 11, 19, 6, 2))
    evs = events(session)
    assert evs and all(e.kind == "vyluka_nova" and e.needs_confirmation for e in evs), [(e.kind, e.data) for e in evs]
    assert any("overte" in n for n in evs[0].validation["notes"]), evs[0].validation["notes"]
    r = _decide(session, "70501", "Kysak", "Prešov", utc(2026, 12, 8, 9), utc(2026, 12, 8, 6))
    assert not _green(r), r.summary
    # prevádzková správa OR cez distribučný zoznam – aj s predmetom bulletinu – sa spracuje
    for i, subject in enumerate(("ROV c. 5212/2026 Kysak - Presov", "Bulletin vyluk OR Kosice - 50. tyzden")):
        _drop_at(env, f"list{i}.eml", _mail(subject, "Dobrý deň,\n\núplná výluka v úseku Kysak – Prešov dňa 10.12.2026 "
                                                     "od 08:00 do 14:00 hod. (ROV č. 5212/2026).\n",
                                            f"list{i}", ROV_KE, "Fri, 20 Nov 2026 07:00:00 +0100",
                                            headers={"Precedence": "list", **_LIST}), utc(2026, 11, 20, 6))
        _process(session, utc(2026, 11, 20, 6, 2 + i))
        last = events(session)[-1]
        assert last.kind == "vyluka_nova", (subject, last.kind, last.data)
    # spravodaj bez čísla dokumentu s hlavičkami zoznamu – hromadný e-mail („iné“)
    _drop_at(env, "news3.eml", _mail("ŽSR Spravodaj 12/2026", "ŽSR Spravodaj 12/2026\n\nModernizácia pokračuje, "
                                     "podrobnosti zverejníme.\n", "news3", _NEWS_FROM, "Sat, 21 Nov 2026 07:00:00 +0100",
                                     headers={"Precedence": "bulk", **_LIST}), utc(2026, 11, 21, 6))
    _process(session, utc(2026, 11, 21, 6, 2))
    assert events(session)[-1].kind == "ine", events(session)[-1].data


# ====================================================================== (6) zastaraný formulár dočasného záznamu zmeny
_ROV_1142 = ("Dobrý deň,\n\núplná výluka oboch traťových koľají v úseku Kysak – Košice dňa 12.11.2026 od 08:00 do "
             "14:00 hod. (ROV č. 1142/2026).\n")
_ZMENA_1142 = ("Dobrý deň,\n\nZmena ROV č. 1142/2026: úplná výluka v úseku Kysak – Košice sa presúva na 14.11.2026 od "
               "08:00 do 14:00 hod.\n")


def _form_fields(page: str, action: str) -> dict:
    """Polia formulára (input, select, textarea) tak, ako by ich odoslal prehliadač."""
    form = re.search(rf'<form method="post" action="{re.escape(action)}".*?</form>', page, re.S).group(0)
    out: dict = {}
    for m in re.finditer(r"<input([^>]*)>", form):
        attrs = dict(re.findall(r'(\w[\w-]*)="([^"]*)"', m.group(1)))
        if attrs.get("type") in ("submit", "button") or not attrs.get("name"):
            continue
        if attrs.get("type") == "checkbox" and "checked" not in m.group(1):
            continue
        out[attrs["name"]] = html.unescape(attrs.get("value", ""))
    for m in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, re.S):
        sel = re.search(r'<option value="([^"]*)"[^>]*selected', m.group(2)) or \
            re.search(r'<option value="([^"]*)"', m.group(2))
        out[m.group(1)] = html.unescape(sel.group(1)) if sel else ""
    for m in re.finditer(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', form, re.S):
        out[m.group(1)] = html.unescape(m.group(2)).lstrip("\n")
    return out


def _temp_form(env, session, client, change_body: str = _ZMENA_1142):
    """ROV 1142/2026 v registri, čakajúca zmena s dočasným záznamom; dispečer otvorí formulár dočasného záznamu."""
    _register(env, session, "rov1142.eml", "ROV c. 1142/2026 Kysak - Kosice", _ROV_1142, ROV_KE,
              "Mon, 02 Nov 2026 09:00:00 +0100", utc(2026, 11, 2, 8, 2))
    (ev,) = _new_events(env, session, "zmena1142.eml", "Zmena ROV c. 1142/2026", change_body, ROV_KE,
                        "Thu, 05 Nov 2026 09:00:00 +0100", utc(2026, 11, 5, 8, 2))
    temp = session.scalar(select(Closure).where(Closure.source_event_id == ev.id))
    assert temp is not None and temp.proposed_change_of is not None
    login_as(client, "disp1")
    page = client.get(f"/vyluky/{temp.id}/upravit")
    assert page.status_code == 200
    fields = _form_fields(page.text, f"/vyluky/{temp.id}/upravit")
    fields.update(end_utc="2026-11-14T16:00", note="ŽSR: predĺžené do 16:00 (FIKTÍVNE)")
    return ev, temp.id, temp.proposed_change_of, fields


def test_zastaraly_formular_docasneho_zaznamu_po_potvrdeni_zmeny(env, session, client):
    """R5L5 bod 5: dispečer upravuje dočasný záznam zmeny, iný dispečer medzitým zmenu potvrdí (záznam zanikne).
    Uloženie nie je 404 so stratou zadania: formulár výluky, do ktorej sa zmena zapísala (409), s vysvetlením a
    neuloženým zadaním (koniec aj poznámka); GET na starý odkaz presmeruje na túto výluku s informáciou."""
    ev, temp_id, target_id, fields = _temp_form(env, session, client)
    _confirm(session, ev.id, actor="disp2")
    assert session.get(Closure, temp_id) is None
    r = client.post(f"/vyluky/{temp_id}/upravit", data=fields, follow_redirects=False)
    assert r.status_code == 409, (r.status_code, r.text[:300])
    page = html.unescape(r.text)
    assert f'action="/vyluky/{target_id}/upravit"' in page
    assert f"Dočasný záznam #{temp_id} medzitým nahradila potvrdená zmena (udalosť #{ev.id})" in page, page[:3000]
    assert f"údaje sú vo výluke #{target_id}" in page and "vaše neuložené zadanie je nižšie" in page
    assert "ŽSR: predĺžené do 16:00 (FIKTÍVNE)" in page and "2026-11-14T16:00" in page
    c = session.get(Closure, target_id)
    session.refresh(c)
    assert as_utc(c.end_utc) == utc(2026, 11, 14, 13)           # nič sa neuložilo potichu
    g = client.get(f"/vyluky/{temp_id}/upravit", follow_redirects=False)
    assert g.status_code == 303 and g.headers["location"] == f"/vyluky/{target_id}/upravit", g.status_code
    shown = html.unescape(client.get(g.headers["location"]).text)
    assert f"Dočasný záznam #{temp_id} medzitým nahradila potvrdená zmena (udalosť #{ev.id})" in shown
    # audit zápisu dočasného záznamu už vymazaný (uchovávanie záznamov): udalosť sa nájde podľa svojho zápisu
    for a in session.scalars(select(AuditLog).where(AuditLog.action == "vyluka_navrh_na_potvrdenie")):
        session.delete(a)
    session.commit()
    g = client.get(f"/vyluky/{temp_id}/upravit", follow_redirects=False)
    assert g.status_code == 303 and g.headers["location"] == f"/vyluky/{target_id}/upravit", g.status_code
    assert client.get("/vyluky/999/upravit").status_code == 404           # neznámy záznam ostáva 404


def test_zastaraly_formular_docasneho_zaznamu_po_zamietnuti_zmeny(env, session, client):
    """Zmenu dispečer zamietol (dočasný záznam zanikol, výluka ostala bez zmeny): uloženie zobrazí formulár pôvodnej
    výluky s vysvetlením a neuloženým zadaním (409), nie 404."""
    from vlaky.posta.spracovanie import reject_event

    ev, temp_id, target_id, fields = _temp_form(env, session, client)
    reject_event(session, ev.id, "disp2", "FIKTÍVNY test")
    session.commit()
    r = client.post(f"/vyluky/{temp_id}/upravit", data=fields, follow_redirects=False)
    assert r.status_code == 409, (r.status_code, r.text[:300])
    page = html.unescape(r.text)
    assert f'action="/vyluky/{target_id}/upravit"' in page
    assert f"Dočasný záznam zmeny #{temp_id} medzitým zanikol" in page and "zamietol" in page, page[:3000]
    assert "ŽSR: predĺžené do 16:00 (FIKTÍVNE)" in page


def test_zastaraly_formular_docasneho_zaznamu_po_rozdeleni_vyluky(env, session, client):
    """Zmena jedného dňa viacdennej výluky výluku rozdelila: formulár ukáže časť s novým termínom a vymenuje časti."""
    c = Closure(kind="uplna", from_station="Kysak", to_station="Košice", start_utc=utc(2026, 11, 23, 7),
                end_utc=utc(2026, 11, 27, 13), daily_from="08:00", daily_to="14:00", status="overene", source="email",
                external_ref="1142/2026")
    session.add(c)
    session.commit()
    (ev,) = _new_events(env, session, "zmena1142d.eml", "Zmena ROV c. 1142/2026",
                        "Dobrý deň,\n\nZmena ROV č. 1142/2026: výluka v úseku Kysak – Košice dňa 24.11.2026 od 08:00 "
                        "do 18:00 hod.\n", ROV_KE, "Thu, 05 Nov 2026 09:00:00 +0100", utc(2026, 11, 5, 8, 2))
    temp = session.scalar(select(Closure).where(Closure.source_event_id == ev.id))
    assert temp is not None and temp.proposed_change_of == c.id
    temp_id = temp.id
    login_as(client, "disp1")
    page = client.get(f"/vyluky/{temp_id}/upravit")
    fields = _form_fields(page.text, f"/vyluky/{temp_id}/upravit")
    fields.update(note="FIKTÍVNA poznámka dispečera")
    ev = _confirm(session, ev.id, actor="disp2")
    applied = ev.validation["applied"]
    assert applied["action"] == "rozdelene", applied
    new_part = next(t["id"] for t in applied["terms"] if t["part"] == "novy")
    r = client.post(f"/vyluky/{temp_id}/upravit", data=fields, follow_redirects=False)
    assert r.status_code == 409, (r.status_code, r.text[:300])
    text = html.unescape(r.text)
    assert f'action="/vyluky/{new_part}/upravit"' in text
    assert f"údaje sú vo výluke #{new_part}" in text and "rozdelená" in text, text[:3000]
    assert all(f"#{t['id']}" in text for t in applied["terms"])
    assert "FIKTÍVNA poznámka dispečera" in text


# ====================================================================== (7) test-notify v tieňovej prevádzke
@pytest.mark.parametrize("channel", ["sms", "teams"])
def test_test_notify_sms_v_tienovej_prevadzke_vysvetli_vypnutie(cfg, capsys, channel):
    """Tieňová prevádzka s VLAKY_SHADOW_EMAILS: test-notify --kanal sms|teams nič nepošle (kód 1), ale výsledok
    vysvetlí, že SMS a Teams sú v tieňovej prevádzke vypnuté – nie „žiadny príjemca – nastavte interný zoznam“."""
    from vlaky import config as config_mod
    from vlaky.__main__ import main

    config_mod.override_settings(**{**cfg.model_dump(), "shadow_mode": True,
                                    "shadow_emails": "veduci@dopravca.example",
                                    "teams_webhook_url": "https://teams.example/webhook"})
    assert main(["test-notify", "--kanal", channel]) == 1
    out = capsys.readouterr()
    assert "SMS a Teams sú v tieňovej prevádzke vypnuté" in out.err, out.err
    assert "žiadny príjemca" not in out.err, out.err
