/* Aplikácia rušňovodiča: hlásenia (nástup, pripravený, odchod, problém) a poloha každých 30 s.
   Token odkazu je vo fragmente URL (#t=…) – server ho v adrese nikdy nedostane (nie je v záznamoch servera);
   posiela sa len v tele požiadaviek. Uloží sa v zariadení, aby aplikácia fungovala aj po otvorení z plochy. */
(function () {
  'use strict';
  var KEY = 'vlaky-rusnovodic-token';
  var root = document.getElementById('rusnovodic');
  if (!root) { return; }

  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/rusnovodic-sw.js', { scope: '/rusnovodic' }).catch(function () { /* bez offline režimu */ });
  }

  function store(t) { try { if (t) { localStorage.setItem(KEY, t); } else { localStorage.removeItem(KEY); } } catch (e) { /* bez úložiska */ } }
  function stored() { try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; } }

  /** Token z fragmentu (#t=…), zo starého tvaru odkazu (?t=…) alebo uložený v zariadení. */
  function takeToken() {
    var t = '';
    var m = (window.location.hash || '').match(/[#&]t=([^&]+)/);
    if (m) { t = decodeURIComponent(m[1]); }
    var q = new URLSearchParams(window.location.search).get('t');
    if (!t && q) { t = q; }
    if (t) {
      store(t);
      if (window.history && window.history.replaceState) { window.history.replaceState(null, '', window.location.pathname); }
      return t;
    }
    return stored();
  }

  var token = takeToken();
  var statusEl = document.getElementById('driver-status');
  var gpsStatus = document.getElementById('gps-status');

  function section(id, show) { var e = document.getElementById(id); if (e) { e.hidden = !show; } }
  function show(text, error) {
    statusEl.textContent = text;
    statusEl.classList.toggle('err', !!error);
  }
  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'omit', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { var err = new Error(data.detail || ('Chyba ' + r.status)); err.status = r.status; throw err; }
        return data;
      });
    });
  }
  function invalid() {
    store('');
    section('driver-loading', false);
    section('rusnovodic', false);
    section('rusnovodic-chyba', false);
    section('rusnovodic-neplatny', true);
  }

  function load() {
    if (!token) { invalid(); return; }
    section('driver-loading', true);
    section('rusnovodic-chyba', false);
    post('/api/rusnovodic/info', { t: token }).then(function (info) {
      document.getElementById('d-cislo').textContent = info.train.number;
      document.getElementById('d-relacia').textContent = info.train.relation;
      document.getElementById('d-rusnovodic').textContent = info.driver_name
        ? info.driver_name + (info.shift_start_text ? ' · nástup ' + info.shift_start_text : '') : '';
      document.getElementById('d-platnost').textContent = info.valid_until_text || '—';
      document.title = 'Vlak ' + info.train.number + ' · Agent dispečer';
      section('driver-loading', false);
      section('rusnovodic', true);
    }).catch(function (err) {
      if (err.status === 403) { invalid(); return; }
      section('driver-loading', false);
      section('rusnovodic-chyba', true);
    });
  }
  var retry = document.getElementById('btn-znova');
  if (retry) { retry.addEventListener('click', load); }
  load();

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
        if (err.status === 403) { invalid(); return; }
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
      if (err.status === 403) { stopGps(); invalid(); return; }
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
