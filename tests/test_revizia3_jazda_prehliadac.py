"""Regresný test k 3. revízii (UI6): aplikácia rušňovodiča v skutočnom prehliadači – po „Nástup na zmenu“ ukazuje
hlavička zaznamenaný nástup (od neho beží limit 13 h), nie naplánovaný, aj po ďalšom hlásení a bez obnovenia stránky.

Test sa preskočí, ak nie je k dispozícii `node` s balíkom `playwright` (alebo ak VLAKY_SKIP_BROWSER=1).
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

from test_web_spolocne import make_train

pytest_plugins = ["test_web_spolocne"]

ROOT = Path(__file__).resolve().parent.parent

NODE_SCRIPT = r"""
const { chromium } = require('playwright');
const [base, driverToken] = process.argv.slice(2);
(async () => {
  const out = { pageErrors: [] };
  const proxy = process.env.HTTPS_PROXY
    ? { server: process.env.HTTPS_PROXY, bypass: 'localhost,127.0.0.1' } : undefined;
  const browser = await chromium.launch({ proxy });
  try {
    const drv = await browser.newPage({ viewport: { width: 390, height: 844 } });
    drv.on('pageerror', (e) => out.pageErrors.push(String(e.message || e)));
    await drv.goto(base + '/rusnovodic#t=' + driverToken);
    await drv.waitForSelector('#rusnovodic:not([hidden])', { timeout: 15000 });
    out.before = await drv.textContent('#d-rusnovodic');
    await drv.click('[data-typ=nastup]');
    await drv.waitForFunction(() => document.getElementById('driver-status').textContent.length > 0, null,
      { timeout: 10000 });
    out.status = await drv.textContent('#driver-status');
    await drv.waitForFunction((b) => document.getElementById('d-rusnovodic').textContent !== b, out.before,
      { timeout: 10000 }).catch(() => {});
    out.after = await drv.textContent('#d-rusnovodic');
    await drv.click('[data-typ=pripraveny]');
    await drv.waitForFunction(() => /pripravený/.test(document.getElementById('driver-status').textContent), null,
      { timeout: 10000 });
    await drv.waitForTimeout(1500);
    out.afterReady = await drv.textContent('#d-rusnovodic');
    out.statusReady = await drv.textContent('#driver-status');
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


def test_hlavicka_po_nastupe_ukazuje_zaznamenany_nastup(cfg, tmp_path):
    from sqlalchemy import select

    from vlaky.cas import fmt, now_utc
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift, Train
    from vlaky.web.rusnovodic import make_driver_token

    planned = now_utc().replace(second=0, microsecond=0) + timedelta(hours=2)
    train_id = make_train(number="53001", status="planovany", origin="Čadca", destination="Žilina",
                          ready_utc=planned + timedelta(minutes=60))
    with session_scope() as s:
        d = Driver(name="Ján FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=train_id, start_utc=planned, status="planovana"))
        token = make_driver_token(s.get(Train, train_id))

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
        script = tmp_path / "rusnovodic.js"
        script.write_text(NODE_SCRIPT, encoding="utf-8")
        proc = subprocess.run(["node", str(script), base, token], capture_output=True, text=True, timeout=120,
                              cwd=str(tmp_path))
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

    with session_scope() as s:
        sh = s.scalar(select(DriverShift).where(DriverShift.train_id == train_id))
        assert sh.status == "prebieha"
        recorded = fmt(sh.start_utc)
    assert out["pageErrors"] == [], out["pageErrors"]
    assert out["before"] == f"Ján FIKTÍVNY · nástup {fmt(planned)}", out["before"]
    assert "Nástup zaznamenaný" in out["status"]
    assert out["after"] == f"Ján FIKTÍVNY · nástup {recorded}", out["after"]
    assert out["afterReady"] == f"Ján FIKTÍVNY · nástup {recorded}" and "pripravený" in out["statusReady"]
    assert recorded != fmt(planned)
