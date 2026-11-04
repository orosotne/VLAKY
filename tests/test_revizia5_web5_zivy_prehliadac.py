"""Živý stav otvorenej stránky odporúčania v skutočnom prehliadači (piata revízia, oblasť D – po kontrole opravy R5U0).

Otvorená karta s odporúčaním (hodiny prehliadača idú o 10 min napred) počas niekoľkých hodín:
  - vek údajov („Posledná kontrola schránky pred N min“, „Posledný cyklus agenta“, „Poloha vlaku: stará N min“) sa pri
    každom dopyte na stav znova ukotví k času zo servera – pri bežiacom agentovi ostáva pár minút, nerastie od načítania
    stránky; čas sa počíta k času servera, nie k (posunutým) hodinám prehliadača,
  - „Zostáva do limitu“ (13 h) sa odpočítava, bez spojenia so serverom stránka sama označí starú kontrolu schránky,
  - bežný prepočet s rovnakým výsledkom obnoví údaje len nenápadným riadkom, zmenu semaforu (výpadok agenta) stránka
    ohlási výrazným upozornením s pomenovaním zmeny a to pri ďalšej bežnej obnove nezmizne.

Playwright + Chromium cez Node.js; všetky požiadavky mimo lokálneho servera prehliadač zablokuje (test ich ohlási).
Test sa preskočí, ak nie je k dispozícii `node` s balíkom `playwright` (alebo VLAKY_SKIP_BROWSER=1).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from test_web_spolocne import HESLO, make_recommendation, make_train, make_user

pytest_plugins = ["test_web_spolocne"]

ROOT = Path(__file__).resolve().parent.parent

NODE_SCRIPT = r"""
const { chromium } = require('playwright');
const fs = require('fs');
const [base, user, password, recId, sync] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));  // skutočný čas (Node), nie hodiny stránky
const mins = (txt) => {
  const m = /(?:(\d+) h )?(\d+) min/.exec(txt || '');
  return m ? parseInt(m[1] || '0', 10) * 60 + parseInt(m[2], 10) : null;
};
async function step(name) {  // test v Pythone zmení údaje v DB a dá signál
  fs.writeFileSync(sync + '/' + name + '.ready', '1');
  for (let i = 0; i < 1800 && !fs.existsSync(sync + '/' + name + '.go'); i += 1) { await sleep(100); }
  if (!fs.existsSync(sync + '/' + name + '.go')) { throw new Error('bez signálu ' + name); }
}
function snap(page) {
  return page.evaluate(() => {
    const text = (e) => (e ? e.textContent.replace(/\s+/g, ' ').trim() : null);
    const dd = (label) => {
      const dt = Array.from(document.querySelectorAll('dl.meta dt')).find((e) => e.textContent.trim() === label);
      return dt ? text(dt.nextElementSibling) : null;
    };
    const notice = (id) => {
      const e = document.getElementById(id);
      return e ? { cls: e.className, role: e.getAttribute('role'), text: text(e) } : null;
    };
    return { semafor: text(document.querySelector('section.decision .semafor')), position: dd('Poloha vlaku'),
             mailbox: dd('Posledná kontrola schránky'), cycle: dd('Posledný cyklus agenta'),
             left: dd('Zostáva do limitu'), sprava: notice('zivy-stav-sprava'), chyba: notice('zivy-stav-chyba'),
             obnova: notice('zivy-stav-obnova') };
  });
}
async function until(page, cond, ms) {  // stav stránky, ktorý spĺňa podmienku – inak posledný (test ho ohlási)
  const end = Date.now() + ms;
  let s = await snap(page);
  while (!cond(s) && Date.now() < end) { await sleep(150); s = await snap(page); }
  return s;
}
function statePoll(page) {
  return page.waitForResponse((r) => r.url() === base + '/odporucania/' + recId &&
                              r.request().headers()['x-vlaky-stav'] === '1', { timeout: 60000 });
}
async function minuteLater(page) {  // ďalší pravidelný dopyt na stav (o minútu)
  const poll = statePoll(page);
  await page.clock.fastForward(61000);
  await poll;
}
(async () => {
  const out = { pageErrors: [], external: [], steps: {} };
  const proxy = process.env.HTTPS_PROXY ? { server: process.env.HTTPS_PROXY, bypass: 'localhost,127.0.0.1' } : undefined;
  const browser = await chromium.launch({ proxy });
  try {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block',
                                           locale: 'sk-SK', timezoneId: 'Europe/Bratislava' });
    await ctx.route('**/*', (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host === '127.0.0.1' || host === 'localhost') { return route.continue(); }
      out.external.push(route.request().url());
      return route.abort();
    });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => out.pageErrors.push(String(e.message || e)));
    // hodiny počítača dispečera idú o 10 min napred
    await page.clock.install({ time: Date.now() + 10 * 60000 });
    await page.goto(base + '/login?next=/odporucania/' + recId);
    await page.fill('#pole-username', user);
    await page.fill('#pole-password', password);
    await Promise.all([page.waitForNavigation(), page.click('button[type=submit]')]);
    if (!page.url().includes('/odporucania/' + recId)) { await page.goto(base + '/odporucania/' + recId); }
    out.steps.nacitana = await snap(page);
    // návrat na kartu: vek údajov sa prepočíta hneď (posunuté hodiny prehliadača ho nesmú zväčšiť)
    let poll = statePoll(page);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await poll;
    out.steps.otvorena = await until(page, (s) => mins(s.mailbox) !== null && mins(s.mailbox) <= 1, 5000);

    // 1) agent beží (cyklus aj schránka práve teraz, čerstvá poloha), karta je otvorená 40 min
    await step('zdravy');
    poll = statePoll(page);
    await page.clock.fastForward(40 * 60000);
    await poll;
    out.steps.zdravy = await until(page, (s) => [s.mailbox, s.cycle, s.position].every((v) => mins(v) !== null && mins(v) <= 2), 5000);

    // 2) agent stojí 12 min (pod prahom 30 min) – vek údajov podľa servera, nie od načítania stránky
    await step('zastaveny');
    await minuteLater(page);
    out.steps.zastaveny = await until(page, (s) => [s.mailbox, s.cycle].every((v) => mins(v) >= 11 && mins(v) <= 14), 5000);

    // 3) bez spojenia so serverom 3 h: odpočet limitu beží ďalej, stará kontrola schránky je označená, upozornenie
    await page.route('**/odporucania/' + recId, (route) => route.abort());
    await page.clock.fastForward(3 * 3600000);
    for (let i = 0; i < 2; i += 1) {
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await sleep(500);
    }
    out.steps.bez_spojenia = await until(page, (s) => s.chyba && mins(s.left) !== null && mins(s.left) < 10 * 60, 5000);
    await page.unroute('**/odporucania/' + recId);

    // 4) spojenie je späť, agent beží, bežný prepočet s rovnakým výsledkom (nový čas prepočtu)
    await step('bezna_obnova');
    await minuteLater(page);
    out.steps.bezna_obnova = await until(page, (s) => s.obnova && !s.chyba && mins(s.mailbox) !== null && mins(s.mailbox) <= 2, 5000);

    // 5) výpadok agenta: ZELENÁ → ORANŽOVÁ
    await step('vypadok');
    await minuteLater(page);
    out.steps.vypadok = await until(page, (s) => s.sprava && /ORANŽOVÁ/.test(s.semafor || ''), 5000);

    // 6) ďalší bežný prepočet počas výpadku – upozornenie na zmenu semaforu ostane
    await step('obnova_pri_vypadku');
    await minuteLater(page);
    out.steps.obnova_pri_vypadku = await until(page, (s) => s.obnova && s.sprava, 5000);
    await ctx.close();
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e && e.message || e) })); process.exit(1); });
"""


def _playwright_available() -> bool:
    if os.environ.get("VLAKY_SKIP_BROWSER") == "1" or not shutil.which("node"):
        return False
    try:
        return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _playwright_available(), reason="Playwright/Chromium (Node.js) nie je k dispozícii")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _minutes(text) -> int:
    """„pred 3 h 12 min“, „stará 0 min“, „12 h 00 min“ → minúty (prvý údaj v texte)."""
    m = re.search(r"(?:(\d+) h )?(\d+) min", text or "")
    assert m, f"Bez údaju v minútach: {text!r}"
    return int(m.group(1) or 0) * 60 + int(m.group(2))


def _agent(at, mailbox_at=None) -> None:
    from vlaky.agent.zdravie import set_kv
    from vlaky.db import session_scope

    with session_scope() as s:
        set_kv(s, "agent_last_cycle", {"ts": at.isoformat(), "errors": 0})
        set_kv(s, "mail_last_ok", {"ts": (mailbox_at or at).isoformat(), "fetched": 0})


def _position(train_id: int, at) -> None:
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    with session_scope() as s:
        s.add(PositionReport(train_id=train_id, train_number="95016", ts_utc=at, lat=49.4381, lon=18.7880,
                             source="gps", speed_kmh=0))


def _recomputed(rec_id: int, at) -> None:
    """Bežný prepočet s rovnakým výsledkom: plánovač prepíše len čas prepočtu (data_age.computed_at)."""
    from vlaky.db import session_scope
    from vlaky.models import Recommendation

    with session_scope() as s:
        rec = s.get(Recommendation, rec_id)
        rec.data_age = {**(rec.data_age or {}), "computed_at": at.isoformat()}


def test_otvorena_stranka_vek_udajov_odpocet_a_upozornenia_na_zmenu(cfg, tmp_path):
    from vlaky.cas import now_utc

    make_user("zivy6", "dispecer")
    now = now_utc()
    _agent(now)
    tid = make_train(number="95016", status="planovany", ready_utc=now + timedelta(hours=1),
                     deadline_utc=now + timedelta(hours=10))
    _position(tid, now)
    chosen = {"route": "Čadca – Žilina", "depart": (now + timedelta(hours=1)).isoformat(), "kind": "VYPRAVIŤ",
              "status": "ZELENÁ", "p_on_time": 0.97, "p_hold": 0.0}
    crew = {"driver": "Ján FIKTÍVNY", "start": (now - timedelta(hours=1)).isoformat(),
            "limit": (now + timedelta(hours=12)).isoformat(), "end_p50": (now + timedelta(hours=3)).isoformat(),
            "end_p90": (now + timedelta(hours=4)).isoformat(), "left_min": 720, "status": "ZELENÁ",
            "max_duty_h": 13.0}
    rid = make_recommendation(tid, decision="VYPRAVIŤ", color="ZELENÁ", priority="INFO", chosen=chosen,
                              variants=[dict(chosen)], reasons=[], summary="Vlak 95016: VYPRAVIŤ (FIKTÍVNE).",
                              crew=crew, data_age={"computed_at": now.isoformat(), "mailbox_min": 0.0})
    actions = {
        "zdravy": lambda: (_agent(now_utc()), _position(tid, now_utc())),
        "zastaveny": lambda: _agent(now_utc() - timedelta(minutes=12)),
        # pravidelný prepočet (každých 15 min) – čas prepočtu sa líši aj pri krokoch v tej istej sekunde
        "bezna_obnova": lambda: (_agent(now_utc()), _recomputed(rid, now + timedelta(minutes=15))),
        "vypadok": lambda: _agent(now_utc() - timedelta(hours=2)),
        "obnova_pri_vypadku": lambda: _recomputed(rid, now + timedelta(minutes=30)),
    }

    port = _free_port()
    env = {**os.environ, "VLAKY_DATABASE_URL": cfg.database_url, "VLAKY_SECRET_KEY": cfg.secret_key,
           "VLAKY_ENV": "test", "VLAKY_PUBLIC_URL": f"http://127.0.0.1:{port}", "VLAKY_MAP_TILE_URL": "",
           "PYTHONPATH": str(ROOT)}
    log_path = tmp_path / "uvicorn.log"
    with open(log_path, "w") as log:
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "vlaky.web.app:create_app", "--factory",
                                   "--host", "127.0.0.1", "--port", str(port)], cwd=str(tmp_path), env=env,
                                  stdout=log, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        while True:
            try:
                if httpx.get(base + "/health", timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline or server.poll() is not None:
                pytest.fail("Server sa nespustil:\n" + log_path.read_text()[-3000:])
            time.sleep(0.3)
        sync = tmp_path / "sync"
        sync.mkdir()
        path = tmp_path / "skript.js"
        path.write_text(NODE_SCRIPT, encoding="utf-8")
        node = subprocess.Popen(["node", str(path), base, "zivy6", HESLO, str(rid), str(sync)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=str(tmp_path))
        deadline = time.time() + 400
        pending = list(actions)
        while pending and node.poll() is None and time.time() < deadline:
            if (sync / f"{pending[0]}.ready").exists():
                actions[pending[0]]()
                (sync / f"{pending[0]}.go").write_text("1")
                pending.pop(0)
            else:
                time.sleep(0.1)
        try:
            stdout, stderr = node.communicate(timeout=max(30, deadline - time.time()))
        except subprocess.TimeoutExpired:
            node.kill()
            stdout, stderr = node.communicate()
        lines = [ln for ln in stdout.splitlines() if ln.startswith("{")]
        assert lines, f"Node bez výsledku: {stdout[-2000:]} {stderr[-2000:]}"
        out = json.loads(lines[-1])
        if "fatal" in out and ("launch" in out["fatal"] or "Executable doesn't exist" in out["fatal"]):
            pytest.skip("Chromium sa nepodarilo spustiť: " + out["fatal"][:300])
        assert "fatal" not in out, out.get("fatal", "") + "\n" + log_path.read_text()[-3000:]
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    assert out["pageErrors"] == [], out["pageErrors"]
    assert out["external"] == [], out["external"][:10]
    st = out["steps"]
    loaded = st["nacitana"]
    assert loaded["semafor"] == "ZELENÁ" and _minutes(loaded["left"]) in (719, 720), loaded
    # hodiny prehliadača o 10 min napred: vek údajov ostane podľa servera
    opened = st["otvorena"]
    assert _minutes(opened["mailbox"]) <= 1 and _minutes(opened["cycle"]) <= 1, opened
    assert _minutes(opened["position"]) <= 1, opened

    # 1) karta otvorená 40 min, agent beží – vek údajov z posledného dopytu, nie 40 min od načítania stránky
    healthy = st["zdravy"]
    for key in ("mailbox", "cycle", "position"):
        assert _minutes(healthy[key]) <= 2, (key, healthy)
    assert "staršia" not in healthy["mailbox"] and "neistá" not in healthy["position"], healthy
    assert healthy["semafor"] == "ZELENÁ" and healthy["sprava"] is None, healthy

    # 2) agent stojí 12 min – stránka ukazuje 12 min (nie 0 ani 41 min)
    stopped = st["zastaveny"]
    for key in ("mailbox", "cycle"):
        assert 11 <= _minutes(stopped[key]) <= 14, (key, stopped)

    # 3) 3 h bez spojenia: odpočet limitu klesol o 3 h, kontrola schránky je stará (označená), upozornenie
    offline = st["bez_spojenia"]
    assert 8 * 60 + 50 <= _minutes(offline["left"]) <= 9 * 60 + 1, offline
    assert _minutes(offline["mailbox"]) >= 3 * 60 + 10, offline
    assert "staršia ako 30 min (pri prepočte" in offline["mailbox"], offline  # stránka sama označí starý údaj
    assert offline["chyba"] and "flash-chyba" in offline["chyba"]["cls"], offline
    assert offline["chyba"]["text"].startswith("Stav stránky sa nepodarilo overiť od "), offline

    # 4) bežný prepočet s rovnakým výsledkom: len nenápadný riadok, nie upozornenie ako pri zmene semaforu
    routine = st["bezna_obnova"]
    assert routine["semafor"] == "ZELENÁ" and routine["sprava"] is None and routine["chyba"] is None, routine
    assert routine["obnova"] and "flash" not in routine["obnova"]["cls"] and routine["obnova"]["role"] is None, routine
    assert routine["obnova"]["text"].startswith("Údaje obnovené"), routine
    assert _minutes(routine["mailbox"]) <= 2 and "staršia" not in routine["mailbox"], routine

    # 5) výpadok agenta: výrazné upozornenie, ktoré zmenu pomenuje
    outage = st["vypadok"]
    assert outage["semafor"] == "ORANŽOVÁ", outage
    notice = outage["sprava"]
    assert notice and "flash-varovanie" in notice["cls"] and notice["role"] == "alert", outage
    assert "Semafor: ZELENÁ → ORANŽOVÁ (bez čerstvých údajov agenta)" in notice["text"], notice
    assert "Údaje agenta: čerstvé → nie sú čerstvé" in notice["text"], notice

    # 6) ďalšia bežná obnova upozornenie na zmenu semaforu nezmaže
    later = st["obnova_pri_vypadku"]
    assert later["semafor"] == "ORANŽOVÁ" and later["sprava"] == notice, later
    assert later["obnova"] and later["obnova"]["text"].startswith("Údaje obnovené"), later
