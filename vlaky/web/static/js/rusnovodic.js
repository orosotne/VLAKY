/* Aplikácia rušňovodiča: hlásenia (nástup, pripravený, odchod, problém) a poloha každých 30 s. */
(function () {
  'use strict';
  var KEY = 'vlaky-rusnovodic-token';
  var root = document.getElementById('rusnovodic');

  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/rusnovodic-sw.js', { scope: '/rusnovodic' }).catch(function () { /* bez offline režimu */ });
  }

  // ------------------------------------------------------------ neplatný odkaz: ponúknuť uložený odkaz
  if (!root) {
    var invalid = document.getElementById('rusnovodic-neplatny');
    var stored = '';
    try { stored = localStorage.getItem(KEY) || ''; } catch (e) { stored = ''; }
    if (invalid && stored) {
      if (invalid.getAttribute('data-token') === stored) {
        try { localStorage.removeItem(KEY); } catch (e) { /* nič */ }
      } else {
        var box = document.getElementById('ulozeny-odkaz');
        var btn = document.getElementById('btn-ulozeny');
        if (box && btn) {
          box.hidden = false;
          btn.addEventListener('click', function () { window.location.href = '/rusnovodic?t=' + encodeURIComponent(stored); });
        }
      }
    }
    return;
  }

  var token = root.getAttribute('data-token');
  try { localStorage.setItem(KEY, token); } catch (e) { /* bez úložiska */ }
  var statusEl = document.getElementById('driver-status');
  var gpsStatus = document.getElementById('gps-status');

  function show(text, error) {
    statusEl.textContent = text;
    statusEl.classList.toggle('err', !!error);
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'omit',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { throw new Error(data.detail || ('Chyba ' + r.status)); }
        return data;
      });
    });
  }

  // ------------------------------------------------------------ hlásenia
  root.querySelectorAll('[data-typ]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var typ = btn.getAttribute('data-typ');
      var text = '';
      if (typ === 'problem') {
        text = (document.getElementById('problem-text').value || '').trim();
        if (!text) { show('Popíšte problém.', true); return; }
      }
      btn.disabled = true;
      post('/api/hlasenie', { t: token, typ: typ, text: text }).then(function (data) {
        show(data.message || 'Odoslané.', false);
        if (typ === 'problem') { document.getElementById('problem-text').value = ''; }
        if (navigator.vibrate) { navigator.vibrate(80); }
      }).catch(function (err) {
        show('Neodoslané: ' + err.message + ' Skúste znova alebo volajte dispečing.', true);
      }).then(function () { btn.disabled = false; });
    });
  });

  // ------------------------------------------------------------ poloha (watchPosition, odoslanie najviac raz za 30 s)
  var gpsBtn = document.getElementById('btn-gps');
  var watchId = null;
  var lastSent = 0;
  var latest = null;
  var timer = null;
  var INTERVAL = 30000;

  function sendLatest() {
    if (!latest || Date.now() - lastSent < INTERVAL - 500) { return; }
    var c = latest.coords;
    lastSent = Date.now();
    var body = { t: token, lat: c.latitude, lon: c.longitude, ts: new Date(latest.timestamp).toISOString(),
                 accuracy_m: c.accuracy !== null ? Math.round(c.accuracy) : null,
                 speed_kmh: (c.speed !== null && !isNaN(c.speed)) ? Math.round(c.speed * 3.6 * 10) / 10 : null };
    post('/api/pozicie/rusnovodic', body).then(function (data) {
      var when = new Date().toLocaleTimeString('sk-SK');
      gpsStatus.textContent = 'Poloha odoslaná o ' + when + ' (presnosť ' + (body.accuracy_m || '?') + ' m' +
        (data.nearest_station ? ', pri ' + data.nearest_station : '') + ').';
    }).catch(function (err) {
      gpsStatus.textContent = 'Polohu sa nepodarilo odoslať: ' + err.message;
    });
  }

  function startGps() {
    if (!('geolocation' in navigator)) { gpsStatus.textContent = 'Zariadenie nepodporuje určovanie polohy.'; return; }
    if (!window.isSecureContext) { gpsStatus.textContent = 'Poloha vyžaduje zabezpečené pripojenie (HTTPS).'; return; }
    watchId = navigator.geolocation.watchPosition(function (pos) {
      latest = pos;
      sendLatest();
    }, function (err) {
      gpsStatus.textContent = err.code === 1 ? 'Prístup k polohe nie je povolený – povoľte ho v nastaveniach prehliadača.' : 'Poloha nie je dostupná (' + err.message + ').';
    }, { enableHighAccuracy: true, maximumAge: 10000, timeout: 60000 });
    timer = setInterval(sendLatest, INTERVAL);
    gpsBtn.setAttribute('aria-pressed', 'true');
    gpsBtn.textContent = 'Zastaviť zdieľanie polohy';
    gpsStatus.textContent = 'Zisťujem polohu…';
    try { localStorage.setItem(KEY + '-gps', '1'); } catch (e) { /* nič */ }
  }

  function stopGps() {
    if (watchId !== null) { navigator.geolocation.clearWatch(watchId); }
    if (timer) { clearInterval(timer); }
    watchId = null;
    timer = null;
    gpsBtn.setAttribute('aria-pressed', 'false');
    gpsBtn.textContent = 'Zdieľať polohu';
    gpsStatus.textContent = 'Poloha sa neposiela.';
    try { localStorage.removeItem(KEY + '-gps'); } catch (e) { /* nič */ }
  }

  if (gpsBtn) {
    gpsBtn.addEventListener('click', function () { if (watchId === null) { startGps(); } else { stopGps(); } });
    var wanted = false;
    try { wanted = localStorage.getItem(KEY + '-gps') === '1'; } catch (e) { wanted = false; }
    if (wanted && navigator.permissions && navigator.permissions.query) {
      navigator.permissions.query({ name: 'geolocation' }).then(function (st) { if (st.state === 'granted') { startGps(); } }).catch(function () {});
    }
  }

  // ------------------------------------------------------------ obrazovka zapnutá (Wake Lock)
  var wake = document.getElementById('chk-wake');
  var lock = null;
  function requestLock() {
    if (!('wakeLock' in navigator)) { return; }
    navigator.wakeLock.request('screen').then(function (l) { lock = l; }).catch(function () { wake.checked = false; });
  }
  if (wake) {
    if (!('wakeLock' in navigator)) { wake.disabled = true; wake.parentNode.title = 'Prehliadač nepodporuje'; }
    wake.addEventListener('change', function () {
      if (wake.checked) { requestLock(); } else if (lock) { lock.release(); lock = null; }
    });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible' && wake.checked) { requestLock(); }
    });
  }
}());
