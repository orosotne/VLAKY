"""UI4 (tretia revízia): výber vlaku v zozname „Vlaky“ na nástenke vycentruje vlak a otvorí jeho okno celé
viditeľné v mape – aj pri vlakoch pri okraji mapy, na veľkom monitore a na šírke mobilu (390 px).

Skutočný prehliadač (Playwright + Chromium cez Node.js) a MapLibre z unpkg.com. Test sa preskočí, ak nie je
k dispozícii `node` s balíkom `playwright` (alebo VLAKY_SKIP_BROWSER=1), alebo ak sa MapLibre nenačíta (offline).
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

from test_web_spolocne import HESLO, make_train, make_user

pytest_plugins = ["test_web_spolocne"]

ROOT = Path(__file__).resolve().parent.parent

NODE_SCRIPT = r"""
const { chromium } = require('playwright');
const [base, user, password] = process.argv.slice(2);
(async () => {
  const out = { pageErrors: [], runs: [] };
  const proxy = process.env.HTTPS_PROXY
    ? { server: process.env.HTTPS_PROXY, bypass: 'localhost,127.0.0.1' } : undefined;
  const browser = await chromium.launch({ proxy });
  try {
    for (const vp of [[1440, 900], [1920, 1080], [390, 844]]) {
      const page = await browser.newPage({ viewport: { width: vp[0], height: vp[1] } });
      page.on('pageerror', (e) => out.pageErrors.push(String(e.message || e)));
      await page.goto(base + '/login');
      await page.fill('#pole-username', user);
      await page.fill('#pole-password', password);
      await Promise.all([page.waitForNavigation(), page.click('button[type=submit]')]);
      await page.waitForSelector('#tabulka-vlakov tbody tr[data-id]', { timeout: 20000 });
      await page.waitForSelector('#mapa .maplibregl-canvas, #mapa canvas.fallback-canvas', { timeout: 30000 });
      await page.waitForTimeout(2500);
      const run = { viewport: vp, gl: !!(await page.$('#mapa .maplibregl-canvas')), clicks: [] };
      const rows = await page.$$eval('#tabulka-vlakov tbody tr[data-id]',
        (r) => r.map((x) => [x.getAttribute('data-id'), x.querySelector('.vlak-btn').textContent.trim()]));
      for (const [id, number] of rows) {
        await page.click('#tabulka-vlakov tbody tr[data-id="' + id + '"] .vlak-btn');
        await page.waitForTimeout(1800);   // pohyb mapy trvá 0,8 s
        run.clicks.push(await page.evaluate((arg) => {
          const rect = (e) => { if (!e) { return null; } const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; };
          const pop = document.querySelector('.maplibregl-popup');
          const bar = document.querySelector('.topbar');
          const sticky = bar && getComputedStyle(bar).position === 'sticky';
          return { id: arg[0], number: arg[1], map: rect(document.querySelector('#mapa')), popup: rect(pop),
                   marker: rect(document.querySelector('.vm[data-id="' + arg[0] + '"] .vm-dot')),
                   text: pop ? pop.textContent.replace(/\s+/g, ' ').trim().slice(0, 80) : '', vh: window.innerHeight,
                   barBottom: sticky ? bar.getBoundingClientRect().bottom : 0 };
        }, [id, number]));
      }
      out.runs.push(run);
      await page.close();
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e && e.message || e) })); process.exit(1); });
"""

# vlaky pri okrajoch Slovenska (východ, západ, juh, sever) a v strede – pri pôvodnom zobrazení celej SR
POSITIONS = {"71001": (48.4167, 22.0833), "71002": (48.1240, 17.1090), "71003": (47.7980, 18.7200),
             "71004": (49.4380, 18.7920), "71005": (48.7206, 21.2581), "71006": (49.2265, 18.7395)}


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


def _distance(point, rect) -> float:
    """Vzdialenosť bodu od obdĺžnika [ľavo, hore, vpravo, dole] (0 = bod je v ňom)."""
    dx = max(rect[0] - point[0], 0.0, point[0] - rect[2])
    dy = max(rect[1] - point[1], 0.0, point[1] - rect[3])
    return (dx * dx + dy * dy) ** 0.5


def _inside(inner, outer, tol: float = 1.0) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol and inner[2] <= outer[2] + tol
            and inner[3] <= outer[3] + tol)


def test_vyber_vlaku_v_zozname_vycentruje_vlak_a_okno_je_cele_viditelne(cfg, tmp_path):
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    make_user("mapa3", "dispecer")
    now = now_utc()
    for number, (lat, lon) in POSITIONS.items():
        tid = make_train(number=number, origin="Čadca", destination="Žilina", customer="FIKTÍVNY zákazník")
        with session_scope() as s:
            s.add(PositionReport(train_id=tid, train_number=number, ts_utc=now - timedelta(minutes=1), lat=lat,
                                 lon=lon, source="gps"))

    port = _free_port()
    env = {**os.environ, "VLAKY_DATABASE_URL": cfg.database_url, "VLAKY_SECRET_KEY": cfg.secret_key,
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
        script = tmp_path / "mapa.js"
        script.write_text(NODE_SCRIPT, encoding="utf-8")
        proc = subprocess.run(["node", str(script), base, "mapa3", HESLO], capture_output=True, text=True,
                              timeout=240, cwd=str(tmp_path))
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
    if not all(run["gl"] for run in out["runs"]):
        pytest.skip("MapLibre sa nenačítal (offline) – okná vlakov MapLibre sa nedajú overiť")
    bad = []
    for run in out["runs"]:
        assert len(run["clicks"]) == len(POSITIONS), run
        for c in run["clicks"]:
            box, pop, mk = c["map"], c["popup"], c["marker"]
            # viditeľná časť mapy: pod lištou stránky (na mobile je pevná hore) a v okne prehliadača
            seen = [box[0], max(box[1], c["barBottom"]), box[2], min(box[3], c["vh"])]
            center = mk and [(mk[0] + mk[2]) / 2, (mk[1] + mk[3]) / 2] * 2
            problems = []
            if pop is None or f"Vlak {c['number']}" not in c["text"]:
                problems.append("okno vlaku chýba")
            elif not _inside(pop, seen):
                problems.append(f"okno {pop} nie je celé vo viditeľnej mape {seen}")
            if center is None or not _inside(center, seen):
                problems.append(f"vlak {center} nie je vo viditeľnej mape {seen}")
            elif abs(center[0] - (box[0] + box[2]) / 2) > 3:
                problems.append(f"vlak {center} nie je vycentrovaný v mape {box}")
            elif pop is not None and _distance(center[:2], pop) > 30:
                problems.append(f"okno {pop} nie je pri vlaku {center}")  # značka inde ako jej okno
            if problems:
                bad.append((run["viewport"], c["number"], problems))
    assert not bad, bad
