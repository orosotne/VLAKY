"""Knižnica UI komponentov (templates/_ui.html, static/js/ui.js, static/css/ui.css) a jej nasadenie na stránkach.

Kontroluje API makier (varianty, neznáme hodnoty, escapovanie), prístupnosť (aria, popisy, nadpisy), prázdne stavy
s filtrom a bez neho, stránku /komponenty a to, že komponenty nevkladajú inline štýly (CSP style-src ich blokuje)."""
from __future__ import annotations

import re

import pytest

from test_web_spolocne import client, login_as, make_recommendation, make_train, web_app  # noqa: F401 – fixtures


@pytest.fixture()
def ui(cfg):
    from vlaky.web.common import build_templates

    env = build_templates().env

    def render(src: str, **ctx) -> str:
        return env.from_string('{% import "_ui.html" as ui with context %}' + src).render(**ctx)

    return render


def _norm(html: str) -> str:
    return re.sub(r"\s+", " ", html).strip()


# ---------------------------------------------------------------------------------------------------- button
def test_button_varianty_velkosti_a_neznama_hodnota(ui):
    assert 'class="btn btn-primary btn-sm"' in ui('{{ ui.button("Uložiť", variant="primary", size="sm") }}')
    html = ui('{{ ui.button("X", variant="neexistuje", size="obrovske") }}')
    assert 'class="btn btn-secondary"' in html          # bezpečný predvolený vzhľad
    assert 'type="button"' in html                       # predvolene nie submit (náhodné odoslanie formulára)
    assert 'type="button"' in ui('{{ ui.button("X", type="javascript:alert(1)") }}')


def test_button_odkaz_zablokovany_odkaz_a_odosielanie(ui):
    assert '<a class="btn btn-secondary" href="/vlaky">' in ui('{{ ui.button("Vlaky", href="/vlaky") }}')
    dis = ui('{{ ui.button("Vlaky", href="/vlaky", disabled=True) }}')
    assert 'href=' not in dis and 'aria-disabled="true"' in dis
    busy = ui('{{ ui.button("Potvrdiť", type="submit", busy_label="Potvrdzuje sa…") }}')
    assert 'type="submit"' in busy and 'data-ui-busy-label="Potvrdzuje sa…"' in busy
    icon = ui('{% call ui.button("Zavrieť", icon_only=True) %}<svg aria-hidden="true"></svg>{% endcall %}')
    assert 'aria-label="Zavrieť"' in icon and "btn-label" not in icon


def test_button_escapuje_text_a_atributy(ui):
    html = ui('{{ ui.button(t, attrs={"title": t, "data-x": None, "formnovalidate": True}) }}', t='<b>"x"</b>')
    assert "<b>" not in html and "&lt;b&gt;" in html and "&#34;x&#34;" in html
    assert "data-x" not in html and " formnovalidate" in html


# ---------------------------------------------------------------------------------------------------- alert, status
def test_alert_role_len_pri_live(ui):
    assert "role=" not in ui('{{ ui.alert("Text") }}')
    assert 'role="alert"' in ui('{{ ui.alert("Chyba", tone="danger", live=True) }}')
    assert 'role="status"' in ui('{{ ui.alert("OK", tone="success", live=True) }}')
    assert "ui-alert-info" in ui('{{ ui.alert("x", tone="cosi") }}')
    dm = ui('{{ ui.alert("x", dismissible=True) }}')
    assert "data-ui-dismissible" in dm and 'aria-label="Zavrieť hlásenie"' in dm and 'type="button"' in dm


def test_status_ton_a_bod(ui):
    assert "ui-status-neutral" in ui('{{ ui.status("x", tone="zly") }}')
    html = ui('{{ ui.status("overené", tone="success", dot=True) }}')
    assert "ui-status-success" in html and 'class="ui-status-dot" aria-hidden="true"' in html


# ---------------------------------------------------------------------------------------------------- empty, stat, skeleton
def test_empty_state_akcie_a_zrusenie_filtra(ui):
    html = _norm(ui('{{ ui.empty_state("Žiadne vlaky", "Zadajte vlak.", action_label="Nový vlak", action_href="/vlaky/novy", '
                    'reset_href="/vlaky") }}'))
    assert "Žiadne vlaky" in html and 'href="/vlaky/novy"' in html and "Zrušiť filter" in html
    bare = ui('{{ ui.empty_state("Nič") }}')
    assert "ui-empty-actions" not in bare and "ui-empty-text" not in bare
    assert "ui-empty-success" in ui('{{ ui.empty_state("Hotovo", tone="success") }}')


def test_stat_chybajuca_hodnota_a_nula(ui):
    missing = _norm(ui('{{ ui.stat("Rezerva", None, unit="min") }}'))
    assert "—" in missing and "bez údajov" in missing and "min" not in missing
    zero = _norm(ui('{{ ui.stat("Eskalácie", 0, tone="danger") }}'))
    assert ">0<" in zero and "bez údajov" not in zero            # nula je hodnota, nie chýbajúci údaj
    assert '<a class="ui-stat ui-stat-warning" href="/fronta">' in ui('{{ ui.stat("Fronta", 3, tone="warning", href="/fronta") }}')


def test_skeleton_pristupnost_a_hranice(ui):
    html = ui('{{ ui.skeleton(lines=0, label="Načítava sa…") }}')
    assert 'aria-busy="true"' in html and 'role="status"' in html and "Načítava sa…" in html
    assert html.count("ui-skeleton-line") == 1
    assert ui('{{ ui.skeleton(lines=500) }}').count("ui-skeleton-line") == 12


# ---------------------------------------------------------------------------------------------------- card, data_table
def test_card_nadpis_uroven_a_aria(ui):
    html = ui('{% call ui.card("Vlaky", id="k", level=7) %}obsah{% endcall %}')
    assert 'aria-labelledby="k-nadpis"' in html and '<h2 class="ui-card-title" id="k-nadpis">' in html
    assert "<h3" in ui('{% call ui.card("A", level=3) %}x{% endcall %}')
    assert "ui-card-head" not in ui('{% call ui.card() %}x{% endcall %}')


def test_data_table_prazdna_s_filtrom_a_bez(ui):
    src = ('{% call ui.data_table("Vlaky", ["Číslo", {"label": "ETA", "align": "end"}, {"label": "Akcie", "sr_only": True}], '
           'count=n, filter=True, empty_title="Žiadne vlaky", empty_reset_href=r) %}<tr><td>1</td><td>2</td><td>3</td></tr>'
           '{% endcall %}')
    full = ui(src, n=1, r=None)
    assert "<caption" in full and '<th scope="col" class="num">ETA</th>' in full
    assert '<span class="visually-hidden">Akcie</span>' in full
    assert 'data-ui-filter="tabulka-vlaky"' in full and 'aria-live="polite"' in full and "1 riadok" in full
    assert 'role="region"' in full and 'tabindex="0"' in full          # posúvateľná oblasť dostupná klávesnicou
    empty = ui(src, n=0, r="/vlaky")
    assert "<table" not in empty and "Žiadne vlaky" in empty and "Zrušiť filter" in empty
    assert "data-ui-filter" not in empty                                # nad prázdnou tabuľkou filter nedáva zmysel


@pytest.mark.parametrize("n,word", [(1, "riadok"), (3, "riadky"), (5, "riadkov"), (21, "riadkov")])
def test_data_table_sklonovanie_poctu(ui, n, word):
    html = ui('{% call ui.data_table("T", ["A"], count=n, filter=True) %}<tr><td>a</td></tr>{% endcall %}', n=n)
    assert f"{n} {word}" in html


def test_komponenty_nevkladaju_inline_styly(ui):
    """CSP style-src nepovoľuje inline štýly – ani skeleton, ani tabuľka ich nesmú použiť."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vlaky" / "web" / "templates" / "_ui.html").read_text(encoding="utf-8")
    assert 'style="' not in src and " on" not in re.sub(r"\{#.*?#\}", "", src, flags=re.S).replace(" only", "")


# ---------------------------------------------------------------------------------------------------- stránky
def test_stranka_komponenty_pre_kazdeho_prihlaseneho(client):
    assert client.get("/komponenty", follow_redirects=False).status_code in (302, 303, 401)
    login_as(client, "dispecer")
    r = client.get("/komponenty")
    assert r.status_code == 200
    for sec in ("button", "status", "alert", "stat", "empty", "skeleton", "table", "form"):
        assert f'id="k-{sec}"' in r.text, sec
    assert "onsubmit=" not in r.text and 'style="' not in r.text
    assert '/static/js/ui.js' in r.text and '/static/css/ui.css' in r.text
    ids = re.findall(r'\sid="([^"]+)"', r.text)
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]


def test_odporucania_prazdny_stav_rozlisuje_filter(client):
    login_as(client, "dispecer")
    r = _norm(client.get("/odporucania").text)
    assert "Žiadne odporúčania" in r and "Zrušiť filter" not in r
    f = _norm(client.get("/odporucania?farba=ČERVENÁ").text)
    assert "Nič nezodpovedá filtru" in f and 'href="/odporucania"' in f and "Zrušiť filter" in f


def test_odporucania_tabulka_s_filtrom(client):
    login_as(client, "dispecer")
    tid = make_train(number="95010")
    make_recommendation(tid)
    r = client.get("/odporucania").text
    assert 'id="tabulka-odporucani"' in r and 'data-ui-filter="tabulka-odporucani"' in r and "95010" in r


def test_vlaky_prazdny_stav_s_akciou_podla_opravnenia(client):
    login_as(client, "dispecer")
    r = _norm(client.get("/vlaky").text)
    assert "Žiadne aktívne vlaky" in r and 'href="/vlaky/novy"' in r
    q = _norm(client.get("/vlaky?q=neexistuje").text)
    assert "Nič nezodpovedá hľadaniu" in q and "Zrušiť filter" in q


def test_vlaky_prazdny_stav_bez_akcie_pre_obchod(client):
    login_as(client, "obchod", "obchod")
    r = client.get("/vlaky")
    if r.status_code != 200:
        pytest.skip("rola obchod zoznam vlakov nevidí")
    assert 'href="/vlaky/novy"' not in r.text
