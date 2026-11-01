"""UI4 (tretia revízia): výber vlaku v zozname „Vlaky“ na nástenke vycentruje vlak a otvorí jeho okno celé
viditeľné v mape – aj pri vlakoch pri okraji mapy, na veľkom monitore, na šírke mobilu (360 a 390 px) a na nízkej
obrazovke (mobil na šírku, malé okno prehliadača). Vlak vybraný klávesnicou presunie fokus do svojho okna, nové
údaje fokus v okne nezmenia a nevezmú ho inde, Escape ho vráti na vlak v zozname.

Skutočný prehliadač (Playwright + Chromium cez Node.js). Bez podkladovej mapy (VLAKY_MAP_TILE_URL je prázdne –
žiadne dlaždice z internetu); MapLibre sa stiahne z unpkg.com raz za beh (ako v prevádzke), všetky ostatné
požiadavky mimo lokálneho servera prehliadač zablokuje a test ich ohlási. Test sa preskočí, ak nie je
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

# spoločný začiatok skriptov: kontext prehliadača, ktorý mimo lokálneho servera pustí len MapLibre (presná verzia
# z unpkg.com, stiahne sa raz a ďalej sa podáva z pamäte); každú inú požiadavku zablokuje a zapíše do out.external
NODE_PRELUDE = r"""
const { chromium } = require('playwright');
const http = require('http');
const MAPLIBRE = /^https:\/\/unpkg\.com\/maplibre-gl@[0-9.]+\/dist\/maplibre-gl\.(js|css)$/;
const CACHE = new Map();
async function newContext(browser, out, viewport) {
  const ctx = await browser.newContext({ viewport, serviceWorkers: 'block' });
  await ctx.route('**/*', async (route) => {
    const url = route.request().url();
    const host = new URL(url).hostname;
    if (host === '127.0.0.1' || host === 'localhost') { return route.continue(); }
    if (MAPLIBRE.test(url)) {
      try {
        if (!CACHE.has(url)) {
          out.cdn = (out.cdn || 0) + 1;
          const r = await route.fetch();
          CACHE.set(url, { status: r.status(), type: r.headers()['content-type'] || '', body: await r.body() });
        }
        const c = CACHE.get(url);
        return route.fulfill({ status: c.status, contentType: c.type, body: c.body,
                               headers: { 'access-control-allow-origin': '*' } });
      } catch (e) { return route.abort(); }
    }
    out.external.push(url);
    return route.abort();
  });
  return ctx;
}
async function login(page, base, user, password) {
  await page.goto(base + '/login');
  await page.fill('#pole-username', user);
  await page.fill('#pole-password', password);
  await Promise.all([page.waitForNavigation(), page.click('button[type=submit]')]);
  await page.waitForSelector('#tabulka-vlakov tbody tr[data-id]', { timeout: 20000 });
  await page.waitForSelector('#mapa .maplibregl-canvas, #mapa canvas.fallback-canvas', { timeout: 30000 });
  await page.waitForTimeout(2000);
}
function launch() {
  const proxy = process.env.HTTPS_PROXY
    ? { server: process.env.HTTPS_PROXY, bypass: 'localhost,127.0.0.1' } : undefined;
  return chromium.launch({ proxy });
}
async function rowIds(page) {
  return page.$$eval('#tabulka-vlakov tbody tr[data-id]',
    (r) => r.map((x) => [x.getAttribute('data-id'), x.querySelector('.vlak-btn').textContent.trim()]));
}
"""

NODE_SCRIPT = NODE_PRELUDE + r"""
const [base, user, password, few] = process.argv.slice(2);
const FEW = few.split(',');
// [šírka, výška, len vybrané vlaky] – nízke obrazovky: mobil na šírku, malý mobil, malé okno prehliadača
const VIEWPORTS = [[1440, 900, false], [1920, 1080, false], [390, 844, false],
                   [844, 390, true], [667, 375, true], [360, 640, true], [915, 412, true], [1280, 400, true]];
(async () => {
  const out = { pageErrors: [], runs: [], external: [] };
  const browser = await launch();
  try {
    for (const [w, h, onlyFew] of VIEWPORTS) {
      const ctx = await newContext(browser, out, { width: w, height: h });
      const page = await ctx.newPage();
      page.on('pageerror', (e) => out.pageErrors.push(String(e.message || e)));
      await login(page, base, user, password);
      const run = { viewport: [w, h], gl: !!(await page.$('#mapa .maplibregl-canvas')), clicks: [] };
      out.runs.push(run);
      if (!run.gl) { break; }   // MapLibre sa nenačítal (offline) – test sa preskočí
      const rows = (await rowIds(page)).filter((r) => !onlyFew || FEW.includes(r[1]));
      run.expected = onlyFew ? FEW.length : null;
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
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e && e.message || e) })); process.exit(1); });
"""

NODE_KEYBOARD = NODE_PRELUDE + r"""
const [base, user, password, number, token, lat, lon] = process.argv.slice(2);
let shift = 0;
function postPosition() {   // nová poloha vlaku cez rozhranie GPS brány -> SSE -> prekreslenie okna vlaku
  shift += 0.003;
  return new Promise((resolve, reject) => {
    const data = JSON.stringify({ train_number: number, lat: Number(lat) + shift, lon: Number(lon) + shift,
                                  ts: new Date().toISOString(), source: 'gps' });
    const u = new URL(base + '/api/pozicie');
    const req = http.request({ hostname: u.hostname, port: u.port, path: u.pathname, method: 'POST',
      headers: { 'content-type': 'application/json', authorization: 'Bearer ' + token, 'content-length': Buffer.byteLength(data) } },
      (r) => { r.resume(); r.on('end', () => resolve(r.statusCode)); });
    req.on('error', reject);
    req.end(data);
  });
}
async function refreshed(page) {   // čaká, kým sa obsah okna vlaku prekreslí novými údajmi
  await page.evaluate(() => { window.__pop = document.querySelector('.maplibregl-popup .pop'); });
  for (let i = 0; i < 70; i += 1) {
    await page.waitForTimeout(300);
    if (await page.evaluate(() => { const p = document.querySelector('.maplibregl-popup .pop'); return !!p && p !== window.__pop; })) { return true; }
  }
  return false;
}
function focusState(page) {
  return page.evaluate(() => {
    const a = document.activeElement;
    const pop = document.querySelector('.maplibregl-popup');
    const map = document.querySelector('#mapa');
    const rect = (e) => { if (!e) { return null; } const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; };
    return { tag: a ? a.tagName : '', text: a ? (a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40) : '',
             inPopup: !!(a && pop && pop.contains(a)), listButton: !!(a && a.classList.contains('vlak-btn')),
             search: !!(a && a.id === 'f-hladat'), popup: !!pop, role: pop ? pop.getAttribute('role') : null,
             label: pop ? pop.getAttribute('aria-label') : null, popupRect: rect(pop), mapRect: rect(map),
             mapScroll: [map.scrollLeft, map.scrollTop] };
  });
}
(async () => {
  const out = { pageErrors: [], external: [], steps: {} };
  const browser = await launch();
  try {
    const ctx = await newContext(browser, out, { width: 1440, height: 900 });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => out.pageErrors.push(String(e.message || e)));
    await login(page, base, user, password);
    out.gl = !!(await page.$('#mapa .maplibregl-canvas'));
    if (!out.gl) { console.log(JSON.stringify(out)); return; }   // MapLibre sa nenačítal (offline)
    const row = (await rowIds(page)).find((r) => r[1] === number);
    const btn = '#tabulka-vlakov tbody tr[data-id="' + row[0] + '"] .vlak-btn';
    await page.focus(btn);                       // vlak v zozname vybraný klávesnicou
    await page.keyboard.press('Enter');
    await page.waitForTimeout(1800);
    out.steps.enter = await focusState(page);
    await page.keyboard.press('Tab');            // ďalší prvok v okne
    await page.waitForTimeout(300);
    out.steps.tab = await focusState(page);
    out.post1 = await postPosition();            // nové údaje počas práce v okne
    out.steps.refreshed = await refreshed(page);
    out.steps.afterRefresh = await focusState(page);
    await page.keyboard.press('Escape');         // zavrieť okno
    await page.waitForTimeout(600);
    out.steps.escape = await focusState(page);
    await page.click(btn);                       // okno otvorené myšou, potom písanie do vyhľadávania
    await page.waitForTimeout(1800);
    await page.focus('#f-hladat');
    out.post2 = await postPosition();
    out.steps.refreshed2 = await refreshed(page);
    out.steps.search = await focusState(page);
    await ctx.close();
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ fatal: String(e && e.message || e) })); process.exit(1); });
"""

# vlaky pri okrajoch Slovenska (východ, západ, juh, sever) a v strede – pri pôvodnom zobrazení celej SR
POSITIONS = {"71001": (48.4167, 22.0833), "71002": (48.1240, 17.1090), "71003": (47.7980, 18.7200),
             "71004": (49.4380, 18.7920), "71005": (48.7206, 21.2581), "71006": (49.2265, 18.7395)}
FEW = ("71001", "71002", "71004")   # na nízkych obrazovkách stačí východný, západný a severný okraj


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


def _trains_with_positions() -> None:
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


def _run_browser(cfg, tmp_path, script: str, args: list, timeout: int) -> dict:
    """Spustí web (uvicorn) nad testovacou databázou a skript Node.js s Playwright; vráti jeho výsledok (JSON)."""
    port = _free_port()
    env = {**os.environ, "VLAKY_DATABASE_URL": cfg.database_url, "VLAKY_SECRET_KEY": cfg.secret_key,
           "VLAKY_POSITIONS_TOKEN": "test-token", "VLAKY_ENV": "test", "VLAKY_PUBLIC_URL": f"http://127.0.0.1:{port}",
           "VLAKY_MAP_TILE_URL": "", "PYTHONPATH": str(ROOT)}   # bez dlaždíc podkladovej mapy z internetu
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
        path = tmp_path / "skript.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run(["node", str(path), base, *args], capture_output=True, text=True, timeout=timeout,
                              cwd=str(tmp_path))
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        assert lines, f"Node bez výsledku: {proc.stdout[-2000:]} {proc.stderr[-2000:]}"
        out = json.loads(lines[-1])
        if "fatal" in out and ("launch" in out["fatal"] or "Executable doesn't exist" in out["fatal"]):
            pytest.skip("Chromium sa nepodarilo spustiť: " + out["fatal"][:300])
        assert "fatal" not in out, out.get("fatal", "") + "\n" + log_path.read_text()[-3000:]
        return out
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def test_vyber_vlaku_v_zozname_vycentruje_vlak_a_okno_je_cele_viditelne(cfg, tmp_path):
    _trains_with_positions()
    out = _run_browser(cfg, tmp_path, NODE_SCRIPT, ["mapa3", HESLO, ",".join(FEW)], timeout=480)

    assert out["pageErrors"] == [], out["pageErrors"]
    assert out["external"] == [], f"požiadavky mimo lokálneho servera (okrem MapLibre): {out['external'][:10]}"
    if not all(run["gl"] for run in out["runs"]):
        pytest.skip("MapLibre sa nenačítal (offline) – okná vlakov MapLibre sa nedajú overiť")
    assert out.get("cdn", 0) <= 2, out.get("cdn")   # MapLibre (js, css) len raz za beh
    bad = []
    for run in out["runs"]:
        assert len(run["clicks"]) == (run["expected"] or len(POSITIONS)), run
        for c in run["clicks"]:
            box, pop, mk = c["map"], c["popup"], c["marker"]
            # viditeľná časť mapy: pod lištou stránky (na mobile je pevná hore) a v okne prehliadača
            seen = [box[0], max(box[1], c["barBottom"]), box[2], min(box[3], c["vh"])]
            center = mk and [(mk[0] + mk[2]) / 2, (mk[1] + mk[3]) / 2] * 2
            problems = []
            if seen[3] - seen[1] < 200:
                problems.append(f"viditeľná mapa je nízka: {seen}")
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


def test_vlak_vybrany_klavesnicou_presunie_fokus_do_okna_a_naspat(cfg, tmp_path):
    _trains_with_positions()
    lat, lon = POSITIONS["71006"]
    out = _run_browser(cfg, tmp_path, NODE_KEYBOARD, ["mapa3", HESLO, "71006", "test-token", str(lat), str(lon)],
                       timeout=240)

    assert out["pageErrors"] == [], out["pageErrors"]
    assert out["external"] == [], f"požiadavky mimo lokálneho servera (okrem MapLibre): {out['external'][:10]}"
    if not out["gl"]:
        pytest.skip("MapLibre sa nenačítal (offline) – okno vlaku MapLibre sa nedá overiť")
    assert out["post1"] == 200 and out["post2"] == 200, (out["post1"], out["post2"])
    st = out["steps"]
    problems = []
    enter, tab, after, esc, search = st["enter"], st["tab"], st["afterRefresh"], st["escape"], st["search"]
    if not enter["inPopup"]:
        problems.append(f"po Enter na vlaku v zozname fokus nie je v okne vlaku: {enter['tag']} {enter['text']!r}")
    if enter["role"] != "dialog" or "71006" not in (enter["label"] or ""):
        problems.append(f"okno vlaku nie je označené ako dialóg s názvom: {enter['role']!r} {enter['label']!r}")
    if not tab["inPopup"]:
        problems.append(f"Tab z okna vlaku vyšiel von: {tab['tag']} {tab['text']!r}")
    if tab["mapScroll"] != [0, 0] or not (tab["popupRect"] and _inside(tab["popupRect"], tab["mapRect"])):
        problems.append(f"fokus v okne posunul obsah mapy alebo okno nie je v mape: {tab}")
    if not st["refreshed"]:
        problems.append("okno vlaku sa po novej polohe neprekreslilo")
    elif not (after["inPopup"] and after["text"] == tab["text"]):
        problems.append(f"po prekreslení okna je fokus inde: {after['tag']} {after['text']!r} (bol {tab['text']!r})")
    if esc["popup"] or not (esc["listButton"] and esc["text"] == "71006"):
        problems.append(f"po Escape fokus nie je na vlaku v zozname: {esc['tag']} {esc['text']!r} okno={esc['popup']}")
    if not st["refreshed2"]:
        problems.append("okno vlaku sa po druhej novej polohe neprekreslilo")
    elif not search["search"]:
        problems.append(f"prekreslenie okna vzalo fokus z vyhľadávania: {search['tag']} {search['text']!r}")
    assert not problems, problems
