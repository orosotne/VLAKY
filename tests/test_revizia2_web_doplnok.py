"""Regresné testy k doplneniu opráv druhej revízie webu (nálezy kontroly opráv).

- história v detaile vlaku: doplnenie (hlásenia o probléme, posledné odmietnutie) jedným dotazom obmedzeným časom,
  nie tromi prechodmi celého auditu pri každom otvorení detailu,
- [36] poloha z mobilu so zle nastavenými hodinami: aplikácia prepočíta čas polohy podľa servera (hlavička Date),
  starú polohu (GPS bez novej polohy) znova neposiela a server pri polohe staršej ako posledná uložená nehlási
  „príliš skoro“,
- prázdne meno alebo heslo pri prihlásení: bez overenia hesla a bez zápisu do auditu (inak stovky záznamov za sekundu
  z meniacich sa adries),
- [44] zvyšok: stránky /vlaky a /vlaky/{id} zatiaľ vek dát agenta nedostanú (pohľady nie sú v tejto oprave) –
  očakávané zlyhanie, kým ich vlastník neupraví.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import func, select

from test_web_spolocne import HESLO, login_as, make_recommendation, make_train, make_user

pytest_plugins = ["test_web_spolocne"]

ZILINA = (49.2265, 18.7395)


def _driver(**train_kw):
    from vlaky.db import session_scope
    from vlaky.models import Train
    from vlaky.web.rusnovodic import make_driver_token

    train_id = make_train(**train_kw)
    with session_scope() as s:
        return train_id, make_driver_token(s.get(Train, train_id))


# ------------------------------------------------------------------ história v detaile vlaku
def test_detail_vlaku_doplni_historiu_jednym_dotazom_obmedzenym_casom(client, fixed_now):
    """Audit sa nečistí: doplnenie histórie nesmie pri každom otvorení detailu prechádzať celý audit – jeden dotaz
    s dolnou hranicou času (hľadanie cez index audit_log.ts). Hlásenie o probléme a odmietnutie ostanú viditeľné."""
    from vlaky.audit import audit
    from vlaky.db import engine, session_scope

    train_id, token = _driver(status="v_jazde")
    r = client.post("/api/hlasenie", json={"t": token, "typ": "problem", "text": "Horúce ložisko vozňa 3 (FIKTÍVNE)"})
    assert r.status_code == 200
    with session_scope() as s:
        for i in range(40):  # neskoršie záznamy k vlaku (úpravy dispečera) – hlásenie vytlačia z 30 riadkov
            audit(s, "dispecer", "train_update", "train", train_id, changes={"notes": [str(i), str(i + 1)]})
    for i in range(10):  # ďalšie hlásenia, nad limit odmietnuté
        client.post("/api/hlasenie", json={"t": token, "typ": "problem", "text": f"spam {i}"})
    login_as(client, "disp51")

    statements: list = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM audit_log" in statement:
            statements.append((statement, parameters))

    eng = engine()
    sa_event.listen(eng, "before_cursor_execute", capture)
    try:
        page = client.get(f"/vlaky/{train_id}")
    finally:
        sa_event.remove(eng, "before_cursor_execute", capture)
    assert page.status_code == 200
    assert "Horúce ložisko vozňa 3" in page.text and "hlasenie_obmedzene" in page.text
    assert len(statements) == 2, [st for st, _ in statements]  # 30 posledných + jedno doplnenie
    bounded = [(st, p) for st, p in statements if "audit_log.ts >=" in st]
    assert len(bounded) == 1, [st for st, _ in statements]
    with eng.connect() as conn:
        plan = " ".join(str(row[-1]) for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + bounded[0][0],
                                                                      bounded[0][1]))
    assert re.search(r"SEARCH (TABLE )?audit_log USING (COVERING )?INDEX", plan) and "SCAN" not in plan, plan


# ------------------------------------------------------------------ [36] poloha a hodiny mobilu
def test_poloha_z_mobilu_s_hodinami_pozadu_po_polohe_so_spravnym_casom(client, fixed_now):
    """Po polohe zo správne nastaveného mobilu posiela druhý mobil (hodiny 8 min pozadu) polohy každých 30 s:
    stopa sa spätne neprepisuje, ale odpoveď nesmie tvrdiť „príliš skoro“ – rada skontrolovať čas v mobile."""
    from vlaky.cas import set_fake_now
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    train_id, token = _driver(status="v_jazde")

    def post(ts, lat_shift=0.0):
        return client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": ZILINA[0] + lat_shift,
                                                             "lon": ZILINA[1], "ts": ts.isoformat()})

    first = post(fixed_now)
    assert first.status_code == 200 and not first.json().get("skipped")
    details = []
    for i in range(1, 4):
        now = fixed_now + timedelta(seconds=30 * i)
        set_fake_now(now)
        r = post(now - timedelta(minutes=8), 0.0004 * i)
        assert r.status_code == 200 and r.json().get("skipped"), r.json()
        details.append(r.json()["detail"])
    assert all("staršia ako posledná uložená" in d and "automatický dátum a čas" in d for d in details), details
    assert not any("príliš skoro" in d for d in details), details

    # oneskorene doručená poloha zo správne nastaveného mobilu (o 20 s staršia ako posledná) – bez rady o hodinách
    now = fixed_now + timedelta(seconds=150)
    set_fake_now(now)
    assert not post(now, 0.002).json().get("skipped")
    late = post(now - timedelta(seconds=20), 0.0019).json()
    assert late.get("skipped") and late["detail"] == "Poloha je staršia ako posledná uložená.", late
    # skutočne príliš skoro (o 5 s neskôr) – pôvodná správa
    set_fake_now(now + timedelta(seconds=5))
    soon = post(now + timedelta(seconds=5), 0.0021).json()
    assert soon.get("skipped") and "príliš skoro" in soon["detail"], soon
    with session_scope() as s:
        assert s.scalar(select(func.count(PositionReport.id)).where(PositionReport.train_id == train_id)) == 2


_NODE_HARNESS = r"""
'use strict';
// Aplikácia rušňovodiča v izolovanom kontexte (vm) s náhradou prehliadača: hodiny mobilu, GPS a server pod kontrolou.
const fs = require('fs');
const vm = require('vm');
const SRC = fs.readFileSync(process.argv[2], 'utf8');
const flush = () => new Promise((r) => setImmediate(r)).then(() => new Promise((r) => setImmediate(r)));

async function scenario(serverOffset, answer) {
  let now = Date.UTC(2026, 10, 2, 4, 0, 0);  // hodiny mobilu; server = now + serverOffset
  class FakeDate extends Date {
    constructor(...a) { if (a.length === 0) { super(now); } else { super(...a); } }
    static now() { return now; }
  }
  const els = {};
  const el = (id) => els[id] || (els[id] = {
    id, hidden: false, textContent: '', value: '', disabled: false, checked: false, parentNode: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    attrs: {}, setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k] || null; },
    listeners: {}, addEventListener(t, f) { this.listeners[t] = f; }, querySelectorAll() { return []; } });
  const positions = [];
  let watchCb = null;
  let errCb = null;
  const timers = [];
  let n = 0;
  const ctx = {
    window: { location: { hash: '#t=' + 'x'.repeat(40), search: '', pathname: '/rusnovodic' },
              history: { replaceState() {} }, isSecureContext: true },
    document: { getElementById: el, addEventListener() {}, visibilityState: 'visible', title: '' },
    navigator: { geolocation: { watchPosition(ok, err) { watchCb = ok; errCb = err; return 1; }, clearWatch() {} } },
    localStorage: { d: {}, getItem(k) { return this.d[k] || null; }, setItem(k, v) { this.d[k] = String(v); },
                    removeItem(k) { delete this.d[k]; } },
    setInterval(f) { timers.push(f); return timers.length; },
    clearInterval() {},
    Date: FakeDate,
    fetch(url, opts) {
      const body = JSON.parse(opts.body);
      let res = { status: 200, data: { ok: true, train: { number: '50001', relation: 'Čadca → Žilina' } } };
      if (url === '/api/pozicie/rusnovodic') {
        positions.push(body);
        res = answer(n++);
        if (res === 'network') { return Promise.reject(new TypeError('Failed to fetch')); }
      }
      const date = new Date(now + serverOffset).toUTCString();
      return Promise.resolve({ ok: res.status < 400, status: res.status,
        headers: { get(h) { return String(h).toLowerCase() === 'date' ? date : null; } },
        json() { return Promise.resolve(res.data); } });
    },
  };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  await flush();                      // /api/rusnovodic/info – odhad posunu hodín z hlavičky Date
  el('btn-gps').listeners.click();    // zdieľať polohu
  const out = { steps: [] };
  const fix = (lat) => ({ coords: { latitude: lat, longitude: 18.7395, accuracy: 12, speed: 10 }, timestamp: now });
  const step = async (name, action) => {
    const before = positions.length;
    action();
    await flush();
    out.steps.push({ name, sent: positions.slice(before).map((p) => p.ts), status: el('gps-status').textContent,
                     phone: new Date(now).toISOString(), server: new Date(now + serverOffset).toISOString() });
  };
  await step('fix1', () => watchCb(fix(49.2265)));
  now += 30000;
  await step('tick_bez_novej_polohy', () => timers[0]());
  errCb({ code: 3, message: 'Timeout expired' });
  now += 30000;
  await step('tick_po_chybe_gps', () => timers[0]());
  now += 30000;
  await step('fix2', () => watchCb(fix(49.2290)));
  return out;
}

(async () => {
  const ok = { status: 200, data: { ok: true, nearest_station: 'Žilina' } };
  const result = {
    pozadu: await scenario(8 * 60000, () => ok),           // hodiny mobilu 8 min pozadu
    vpredu: await scenario(-9 * 60000, () => ok),          // hodiny mobilu 9 min vpredu
    spravne: await scenario(0, () => ok),
    vypadok_siete: await scenario(0, (i) => (i === 0 ? 'network' : ok)),
    odmietnuta: await scenario(0, (i) => (i === 0 ? { status: 422, data: { ok: false,
      detail: 'Nepravdepodobný skok polohy – poloha sa neuložila.' } } : ok)),
    preskocena: await scenario(0, () => ({ status: 200, data: { ok: true, skipped: true,
      detail: 'Poloha je staršia ako posledná uložená.' } })),
  };
  console.log(JSON.stringify(result));
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e && e.stack || e) })); process.exit(1); });
"""


def test_aplikacia_rusnovodica_opravi_cas_polohy_a_neopakuje_staru_polohu(tmp_path):
    """Aplikácia (skutočný rusnovodic.js v Node): pri zle nastavených hodinách mobilu pošle čas polohy prepočítaný
    na čas servera (podľa hlavičky Date), polohu, na ktorú server už odpovedal, znova neposiela (GPS bez novej
    polohy – napr. tunel), po výpadku siete ju pošle znova a pri správnych hodinách čas nemení."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js nie je k dispozícii")
    from vlaky.web.common import STATIC_DIR

    harness = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(STATIC_DIR / "js" / "rusnovodic.js")], capture_output=True,
                          text=True, timeout=60)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    out = json.loads(lines[-1])
    assert "fatal" not in out, out.get("fatal")

    def ms(iso: str) -> int:
        from datetime import datetime

        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)

    def steps(name: str) -> dict:
        return {s["name"]: s for s in out[name]["steps"]}

    for name in ("pozadu", "vpredu"):
        st = steps(name)
        fix1, tick, fix2 = st["fix1"], st["tick_bez_novej_polohy"], st["fix2"]
        assert len(fix1["sent"]) == 1 and abs(ms(fix1["sent"][0]) - ms(fix1["server"])) <= 2000, (name, fix1)
        assert "Poloha odoslaná" in fix1["status"] and "Hodiny mobilu sa líšia od servera o" in fix1["status"]
        assert tick["sent"] == [] and "Čakám na novú polohu z GPS" in tick["status"], (name, tick)
        error_tick = st["tick_po_chybe_gps"]  # chyba GPS ostane viditeľná, stará poloha sa neposiela
        assert error_tick["sent"] == [] and "Poloha nie je dostupná (Timeout expired)." in error_tick["status"]
        assert "Čakám na novú polohu z GPS" in error_tick["status"]
        assert len(fix2["sent"]) == 1 and abs(ms(fix2["sent"][0]) - ms(fix2["server"])) <= 2000, (name, fix2)
        assert "Poloha nie je dostupná" not in fix2["status"]
    st = steps("spravne")
    assert st["fix1"]["sent"] == [st["fix1"]["phone"]] and st["fix2"]["sent"] == [st["fix2"]["phone"]]  # bez zmeny
    assert "Hodiny mobilu" not in st["fix1"]["status"] and st["tick_bez_novej_polohy"]["sent"] == []
    st = steps("vypadok_siete")
    assert "nepodarilo odoslať" in st["fix1"]["status"]
    assert st["tick_bez_novej_polohy"]["sent"] == st["fix1"]["sent"]  # po výpadku siete tú istú polohu znova
    st = steps("odmietnuta")
    assert "Nepravdepodobný skok" in st["fix1"]["status"] and st["tick_bez_novej_polohy"]["sent"] == []
    st = steps("preskocena")
    assert "sa neuložila: Poloha je staršia ako posledná uložená." in st["fix1"]["status"]
    assert "Poloha odoslaná" not in st["fix1"]["status"]


# ------------------------------------------------------------------ prihlásenie s prázdnym heslom
async def _post_login(app, data: dict, client_addr):
    transport = httpx.ASGITransport(app=app, client=client_addr)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies={"vlaky_login": "t" * 32}) as ac:
        return await ac.post("/login", data={"csrf_token": "t" * 32, "next": "/", **data})


def test_prazdne_heslo_bez_overenia_hesla_a_bez_zaznamu_v_audite(web_app, monkeypatch):
    """Prázdne meno alebo heslo (posiela ich len skript – formulár ich nepustí): bez bcryptu a bez riadku v audite;
    z jednej adresy sa počítajú do limitu, účet nimi zablokovať nejde."""
    import vlaky.web.auth as auth
    from vlaky.db import session_scope
    from vlaky.models import AuditLog

    make_user("veduci51", "veduci")
    real_check = auth._check_login
    checked: list = []

    def check(username, password, ip):
        checked.append(username)
        return real_check(username, password, ip)

    monkeypatch.setattr(auth, "_check_login", check)

    async def flood():  # z meniacich sa sietí /64 – limit adresy nezasiahne
        return await asyncio.gather(*[_post_login(web_app, {"username": f"nikto{i}" if i % 3 else "",
                                                            "password": "" if i % 3 else "heslo-12345"},
                                                  (f"2001:db8:{i + 1:x}::1", 1)) for i in range(60)])

    res = asyncio.run(flood())
    assert [r.status_code for r in res] == [401] * 60
    assert all("Zadajte meno aj heslo" in r.text for r in res)
    assert checked == []  # žiadne overenie hesla
    with session_scope() as s:
        assert s.scalar(select(func.count(AuditLog.id)).where(AuditLog.action.like("login%"))) == 0

    async def same_address():
        out = []
        for _ in range(12):  # prázdne heslo pre existujúci účet z jednej adresy
            out.append((await _post_login(web_app, {"username": "veduci51", "password": ""},
                                          ("198.51.100.77", 1))).status_code)
        # účet nie je zablokovaný: z inej adresy sa prihlási
        out.append((await _post_login(web_app, {"username": "veduci51", "password": HESLO},
                                      ("198.51.100.78", 1))).status_code)
        return out

    assert asyncio.run(same_address()) == [401] * 5 + [429] * 7 + [303]
    assert checked == ["veduci51"]


# ------------------------------------------------------------------ [44] zvyšok mimo tejto opravy
def _stale_agent(now) -> None:
    from vlaky.agent.zdravie import set_kv
    from vlaky.db import session_scope

    with session_scope() as s:
        for key in ("mail_last_ok", "agent_last_cycle"):
            set_kv(s, key, {"ts": (now - timedelta(minutes=120)).isoformat()})


@pytest.mark.xfail(raises=AssertionError, strict=False,
                   reason="[44] zvyšok: views_evidencia.trains_page neodovzdá šablóne vlaky.html vek dát (fresh) – "
                          "šablóna je pripravená, úprava pohľadu je u vlastníka (needs_from_others)")
def test_stranka_zoznamu_vlakov_bez_cerstvych_dat_neukaze_zelenu(client, fixed_now):
    train_id = make_train(number="44310", status="planovany")
    make_recommendation(train_id, color="ZELENÁ", decision="VYPRAVIŤ", priority="INFO")
    login_as(client, "d4431")
    _stale_agent(fixed_now)
    page = client.get("/vlaky").text
    assert "44310" in page
    assert "semafor-zelena" not in page and "bez čerstvých údajov agenta" in page


@pytest.mark.xfail(raises=AssertionError, strict=False,
                   reason="[44] zvyšok: _train_detail neodovzdá vlak.html vek dát a vlak.html ukáže rec.color – "
                          "úprava pohľadu a šablóny je u vlastníka (needs_from_others)")
def test_detail_vlaku_bez_cerstvych_dat_neukaze_zelenu(client, fixed_now):
    train_id = make_train(number="44311", status="planovany")
    make_recommendation(train_id, color="ZELENÁ", decision="VYPRAVIŤ", priority="INFO")
    login_as(client, "d4432")
    _stale_agent(fixed_now)
    page = client.get(f"/vlaky/{train_id}").text
    assert "44311" in page
    assert "semafor-zelena" not in page and "čerstv" in page
