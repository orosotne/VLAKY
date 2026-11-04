"""R5U0 (piata revízia) v skutočnom prehliadači: otvorená stránka odporúčania ZELENÁ sa po výpadku agenta sama zmení na
ORANŽOVÚ (živý stav stránky, static/js/app.js) – bez opätovného načítania, s rozpísanou poznámkou dispečera, so skrytou
farbou ORANŽOVÁ a s povinným potvrdením neaktuálnych údajov pri „Prijať“. Bez spojenia so serverom stránka upozorní,
že stav nie je overený.

Playwright + Chromium cez Node.js; všetky požiadavky mimo lokálneho servera prehliadač zablokuje (test ich ohlási).
Test sa preskočí, ak nie je k dispozícii `node` s balíkom `playwright` (alebo VLAKY_SKIP_BROWSER=1).
"""
from __future__ import annotations

import json
import os
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
function snap(page) {
  return page.evaluate(() => {
    const head = document.querySelector('section.decision');
    const sem = head ? head.querySelector('.semafor') : null;
    const akcia = document.querySelector('form.decide input[name=akcia][value=prijat]');
    const form = akcia ? akcia.form : null;
    const cb = form ? form.querySelector('input[name=potvrdit_neaktualne]') : null;
    const active = document.activeElement;
    const notice = document.getElementById('zivy-stav-sprava');
    return { semafor: sem ? sem.className : '', header: head ? head.textContent.replace(/\s+/g, ' ').trim() : '',
             farba: form && form.querySelector('input[name=farba]') ? form.querySelector('input[name=farba]').value : null,
             confirm: cb ? { required: cb.required, checked: cb.checked } : null,
             note: (document.getElementById('pozn-prijat') || {}).value || '', focus: active ? active.id : '',
             notice: notice ? notice.className + ' | ' + notice.textContent : '',
             variants: Array.from(document.querySelectorAll('table .semafor')).map((e) => e.textContent.trim()) };
  });
}
(async () => {
  const out = { pageErrors: [], external: [] };
  const proxy = process.env.HTTPS_PROXY ? { server: process.env.HTTPS_PROXY, bypass: 'localhost,127.0.0.1' } : undefined;
  const browser = await chromium.launch({ proxy });
  try {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
    await ctx.route('**/*', (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host === '127.0.0.1' || host === 'localhost') { return route.continue(); }
      out.external.push(route.request().url());
      return route.abort();
    });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => out.pageErrors.push(String(e.message || e)));
    await page.goto(base + '/login?next=/odporucania/' + recId);
    await page.fill('#pole-username', user);
    await page.fill('#pole-password', password);
    await Promise.all([page.waitForNavigation(), page.click('button[type=submit]')]);
    if (!page.url().includes('/odporucania/' + recId)) { await page.goto(base + '/odporucania/' + recId); }
    out.before = await snap(page);
    await page.fill('#pozn-prijat', 'rozpísaná poznámka');
    await page.focus('#pozn-prijat');
    // stránka je otvorená – test teraz „zastaví“ agenta (posunie čas jeho posledného cyklu a kontroly schránky)
    fs.writeFileSync(sync + '/otvorena', '1');
    for (let i = 0; i < 1200 && !fs.existsSync(sync + '/zastarane'); i += 1) { await page.waitForTimeout(100); }
    out.staleSignal = fs.existsSync(sync + '/zastarane');
    // návrat na kartu = okamžitý dopyt na stav (inak do minúty)
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForSelector('#zivy-stav-sprava.flash-info', { timeout: 20000 });
    out.after = await snap(page);
    // bez spojenia so serverom: po dvoch neúspešných dopytoch upozornenie
    await page.route('**/odporucania/' + recId, (route) => route.abort());
    for (let i = 0; i < 2; i += 1) {
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await page.waitForTimeout(1000);
    }
    await page.waitForSelector('#zivy-stav-sprava.flash-chyba', { timeout: 10000 });
    out.offline = await snap(page);
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


def test_otvorena_stranka_odporucania_po_vypadku_agenta_nie_je_zelena(cfg, tmp_path):
    from vlaky.agent.zdravie import set_kv
    from vlaky.cas import now_utc
    from vlaky.db import session_scope

    make_user("zivy5", "dispecer")
    now = now_utc()
    tid = make_train(number="95006", status="planovany", ready_utc=now + timedelta(hours=1),
                     deadline_utc=now + timedelta(hours=10))
    chosen = {"route": "Čadca – Žilina", "depart": (now + timedelta(hours=1)).isoformat(), "kind": "VYPRAVIŤ",
              "status": "ZELENÁ", "p_on_time": 0.97, "p_hold": 0.0}
    rid = make_recommendation(tid, decision="VYPRAVIŤ", color="ZELENÁ", priority="INFO", chosen=chosen,
                              variants=[dict(chosen)], reasons=[], summary="Vlak 95006: VYPRAVIŤ (FIKTÍVNE).")

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
        # skript otvorí stránku pri čerstvých dátach agenta, potom agent „zastane“ (posledný cyklus a kontrola schránky
        # pred 2 h) a stránka sa má sama zmeniť na ORANŽOVÚ – bez časových rezerv (synchronizácia cez súbory)
        sync = tmp_path / "sync"
        sync.mkdir()
        path = tmp_path / "skript.js"
        path.write_text(NODE_SCRIPT, encoding="utf-8")
        node = subprocess.Popen(["node", str(path), base, "zivy5", HESLO, str(rid), str(sync)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=str(tmp_path))
        deadline = time.time() + 150
        while not (sync / "otvorena").exists() and node.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if (sync / "otvorena").exists():
            stale = (now_utc() - timedelta(hours=2)).isoformat()
            with session_scope() as s:
                set_kv(s, "agent_last_cycle", {"ts": stale, "errors": 0})
                set_kv(s, "mail_last_ok", {"ts": stale, "fetched": 0})
            (sync / "zastarane").write_text("1")
        try:
            stdout, stderr = node.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            node.kill()
            stdout, stderr = node.communicate()
        proc = subprocess.CompletedProcess(node.args, node.returncode, stdout, stderr)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        assert lines, f"Node bez výsledku: {proc.stdout[-2000:]} {proc.stderr[-2000:]}"
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
    assert out["staleSignal"], "skript nedostal signál o výpadku agenta"
    before, after, offline = out["before"], out["after"], out["offline"]
    assert "semafor-zelena" in before["semafor"] and before["farba"] == "ZELENÁ" and before["confirm"] is None, before
    # bez opätovného načítania: ORANŽOVÁ s dôvodom, nikde ZELENÁ, potvrdenie neaktuálnych údajov pri „Prijať“
    assert "semafor-oranzova" in after["semafor"], after
    assert "Údaje agenta nie sú čerstvé" in after["header"] and "pôvodne ZELENÁ" in after["header"], after
    assert "ZELENÁ" not in after["variants"], after
    assert after["farba"] == "ORANŽOVÁ" and after["confirm"] == {"required": True, "checked": False}, after
    assert after["note"] == "rozpísaná poznámka" and after["focus"] == "pozn-prijat", after
    assert after["notice"].startswith("flash flash-info | Stav sa zmenil – údaje na stránke sú obnovené"), after
    assert offline["notice"].startswith("flash flash-chyba | Stav stránky sa nepodarilo overiť od "), offline
    assert "semafor-oranzova" in offline["semafor"], offline
