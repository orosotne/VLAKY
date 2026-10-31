"""Web v skutočnom prehliadači (Playwright + Chromium cez Node.js): prihlásenie, mapa, tabuľka vlakov, bez chýb JS.

Test sa preskočí, ak nie je k dispozícii `node` s balíkom `playwright` (alebo ak VLAKY_SKIP_BROWSER=1).
Snímku obrazovky uloží, ak je nastavené VLAKY_SCREENSHOT_DIR.
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
const [base, user, password, shot] = process.argv.slice(2);
(async () => {
  const out = { pageErrors: [], consoleErrors: [], csp: [] };
  const proxy = process.env.HTTPS_PROXY
    ? { server: process.env.HTTPS_PROXY, bypass: 'localhost,127.0.0.1' } : undefined;
  const browser = await chromium.launch({ proxy });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
    await page.addInitScript(() => {
      document.addEventListener('securitypolicyviolation',
        (e) => console.error('CSP: ' + e.violatedDirective + ' ' + e.blockedURI));
    });
    page.on('pageerror', (e) => out.pageErrors.push(String(e.message || e)));
    page.on('console', (m) => {
      if (m.type() !== 'error') return;
      const t = m.text();
      if (t.startsWith('CSP:')) out.csp.push(t);
      else if (!/Failed to load resource|net::ERR_|ERR_TUNNEL|ERR_PROXY/i.test(t)) out.consoleErrors.push(t);
    });
    await page.goto(base + '/login');
    await page.fill('#pole-username', user);
    await page.fill('#pole-password', password);
    await Promise.all([page.waitForNavigation(), page.click('button[type=submit]')]);
    out.url = page.url();
    await page.waitForSelector('#mapa', { timeout: 20000 });
    await page.waitForSelector('#tabulka-vlakov tbody tr[data-id]', { timeout: 20000 });
    await page.waitForSelector('#mapa .maplibregl-canvas, #mapa canvas.fallback-canvas', { timeout: 30000 });
    await page.waitForTimeout(2500);
    out.glCanvas = !!(await page.$('#mapa .maplibregl-canvas'));
    out.fallbackCanvas = !!(await page.$('#mapa canvas.fallback-canvas'));
    out.mapBox = await page.$eval('#mapa', (e) => {
      const r = e.getBoundingClientRect();
      return [r.width, r.height];
    });
    out.rows = await page.$$eval('#tabulka-vlakov tbody tr[data-id]',
      (r) => r.map((x) => x.textContent.replace(/\s+/g, ' ').trim()));
    out.markers = await page.$$eval('.vm', (r) => r.length);
    out.borders = await page.$$eval('.hm', (r) => r.length);
    await page.click('#tabulka-vlakov tbody tr[data-id]');
    await page.waitForSelector('.maplibregl-popup .pop, .fallback-popup .pop', { timeout: 10000 });
    out.popup = await page.$eval('.maplibregl-popup .pop, .fallback-popup .pop',
      (e) => e.textContent.replace(/\s+/g, ' ').trim());
    out.gantt = !!(await page.$('#casova-os svg'));
    if (shot) await page.screenshot({ path: shot });
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


def test_dashboard_v_prehliadaci(cfg, tmp_path):
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    make_user("prehliadac", "dispecer")
    train_id = make_train(number="51001", origin="Čadca", destination="Žilina")
    make_recommendation(train_id, color="ČERVENÁ", priority="KRITICKÉ")
    with session_scope() as s:
        s.add(PositionReport(train_id=train_id, train_number="51001", ts_utc=now_utc() - timedelta(minutes=1),
                             lat=49.2265, lon=18.7395, snapped_lat=49.228345, snapped_lon=18.740581, source="gps"))

    port = _free_port()
    env = {**os.environ, "VLAKY_DATABASE_URL": cfg.database_url, "VLAKY_SECRET_KEY": "browser-test-secret",
           "VLAKY_POSITIONS_TOKEN": "test-token", "VLAKY_ENV": "test", "VLAKY_PUBLIC_URL": f"http://127.0.0.1:{port}",
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
        script = tmp_path / "prehliadac.js"
        script.write_text(NODE_SCRIPT, encoding="utf-8")
        shot_dir = os.environ.get("VLAKY_SCREENSHOT_DIR", "")
        shot = str(Path(shot_dir) / "test_dashboard.png") if shot_dir else ""
        proc = subprocess.run(["node", str(script), base, "prehliadac", HESLO, shot], capture_output=True, text=True,
                              timeout=150, cwd=str(tmp_path))
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

    assert out["url"].rstrip("/") == base, out["url"]
    assert out["pageErrors"] == [], out["pageErrors"]
    assert out["consoleErrors"] == [], out["consoleErrors"]
    assert out["csp"] == [], out["csp"]
    assert out["glCanvas"] or out["fallbackCanvas"]
    assert out["mapBox"][0] > 300 and out["mapBox"][1] > 300, out["mapBox"]
    assert any("51001" in r and "ČERVENÁ" in r for r in out["rows"]), out["rows"]
    assert "Vlak 51001" in out["popup"] and "Sledovať vlak" in out["popup"]
    assert out["gantt"] is True
    if out["glCanvas"]:
        assert out["markers"] >= 1 and out["borders"] == 23
