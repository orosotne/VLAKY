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

  /** Token z fragmentu (#t=…) alebo uložený v zariadení. Token v adrese (?t=…) sa nepoužije – dostal by sa do
   *  záznamov servera; adresa sa vždy vyčistí. */
  function takeToken() {
    var t = '';
    var m = (window.location.hash || '').match(/[#&]t=([^&]+)/);
    if (m) { t = decodeURIComponent(m[1]); }
    if (window.location.search && window.history && window.history.replaceState) {
      window.history.replaceState(null, '', window.location.pathname + window.location.hash);
    }
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
  // Posun hodín mobilu voči serveru (ms) z hlavičky Date odpovedí servera (presnosť ~ polovica času odozvy + 0,5 s).
  // Pri zle nastavených hodinách mobilu sa čas polohy prepočíta na čas servera – inak by server polohu odmietol
  // (stará / z budúcnosti) alebo zahodil (staršia ako posledná, napr. z iného mobilu so správnym časom).
  var clockOffset = 0;
  var CLOCK_TOLERANCE = 30000;
  function noteServerTime(r, sentAt) {
    var server = Date.parse((r.headers && r.headers.get('Date')) || '');
    var now = Date.now();
    if (isNaN(server) || now - sentAt > 10000) { return; }  // bez hlavičky alebo pri dlhej odozve nepresné
    clockOffset = server + 500 - (sentAt + now) / 2;
  }
  function post(url, body) {
    var sentAt = Date.now();
    return fetch(url, {
      method: 'POST', credentials: 'omit', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      noteServerTime(r, sentAt);
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

  function applyInfo(info) {
    document.getElementById('d-cislo').textContent = info.train.number;
    document.getElementById('d-relacia').textContent = info.train.relation;
    document.getElementById('d-rusnovodic').textContent = info.driver_name
      ? info.driver_name + (info.shift_start_text ? ' · nástup ' + info.shift_start_text : '') : '';
    document.getElementById('d-platnost').textContent = info.valid_until_text || '—';
    document.title = 'Vlak ' + info.train.number + ' · Agent dispečer';
  }

  function load() {
    if (!token) { invalid(); return; }
    section('driver-loading', true);
    section('rusnovodic-chyba', false);
    post('/api/rusnovodic/info', { t: token }).then(function (info) {
      applyInfo(info);
      section('driver-loading', false);
      section('rusnovodic', true);
    }).catch(function (err) {
      if (err.status === 403) { invalid(); return; }
      section('driver-loading', false);
      section('rusnovodic-chyba', true);
    });
  }

  /** Po hlásení stavu (najmä „Nástup na zmenu“) obnoví hlavičku bez načítavacej obrazovky – inak by ostal
   *  naplánovaný nástup, hoci limit pracovného času beží od zaznamenaného. Chyba nevadí: hlásenie už prijaté. */
  function refreshInfo() {
    if (!token) { return; }
    post('/api/rusnovodic/info', { t: token }).then(applyInfo).catch(function (err) {
      if (err.status === 403) { invalid(); }
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
      // omylom ťuknutý „Odchod“ (vlak nevyšiel) by ukončil státie – limit 18 h by sa počítal znova; preto potvrdenie
      if (typ === 'odchod' && !window.confirm('Vlak naozaj odchádza? Odchod ukončí státie vlaku (limit 18 h) – ' +
          'potvrďte len pri skutočnom odchode.')) { return; }
      btn.disabled = true;
      post('/api/hlasenie', { t: token, typ: typ, text: text }).then(function (data) {
        show(data.message || 'Odoslané.', false);
        if (typ === 'problem') { document.getElementById('problem-text').value = ''; } else { refreshInfo(); }
        if (navigator.vibrate) { navigator.vibrate(80); }
      }).catch(function (err) {
        if (err.status === 403) { invalid(); return; }
        // 409 = stav už bol zaznamenaný (opakovanie nič nezmení) – len informácia, nie výzva skúsiť znova
        if (err.status === 409) { show('Už zaznamenané: ' + err.message, false); return; }
        if (err.status === 429) { show('Neodoslané: ' + err.message, true); return; }
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
  var answeredFix = null;  // čas (z GPS) polohy, na ktorú server už odpovedal – tá istá sa znova neposiela
  var gpsError = '';       // posledná chyba GPS (zostane viditeľná, kým nepríde nová poloha)

  function sendLatest() {
    if (!latest || Date.now() - lastSent < INTERVAL - 500) { return; }
    var fixTs = Number(latest.timestamp);
    if (fixTs === answeredFix) {
      // GPS nedal novú polohu (tunel, slabý signál): starú polohu znova neposielať – nie je to aktuálna poloha
      gpsStatus.textContent = (gpsError ? gpsError + ' ' : '') + 'Čakám na novú polohu z GPS (posledná zistená o ' +
        new Date(fixTs).toLocaleTimeString('sk-SK') + ').';
      return;
    }
    var c = latest.coords;
    lastSent = Date.now();
    var skew = Math.abs(clockOffset) > CLOCK_TOLERANCE ? Math.round(clockOffset) : 0;  // hodiny mobilu nesedia
    var body = { t: token, lat: c.latitude, lon: c.longitude, ts: new Date(fixTs + skew).toISOString(),
                 accuracy_m: c.accuracy !== null ? Math.round(c.accuracy) : null,
                 speed_kmh: (c.speed !== null && !isNaN(c.speed)) ? Math.round(c.speed * 3.6 * 10) / 10 : null };
    var clockNote = skew ? ' Hodiny mobilu sa líšia od servera o ' + Math.round(Math.abs(skew) / 60000) +
      ' min – čas polohy sa opravuje.' : '';
    post('/api/pozicie/rusnovodic', body).then(function (data) {
      answeredFix = fixTs;
      var when = new Date().toLocaleTimeString('sk-SK');
      if (data.skipped) {
        // server polohu neuložil (príliš skoro po predchádzajúcej, staršia ako posledná) – nehlásiť ju ako odoslanú
        gpsStatus.textContent = 'Poloha o ' + when + ' sa neuložila: ' +
          (data.detail || 'prišla príliš skoro po predchádzajúcej.') + ' Zdieľanie pokračuje.' + clockNote;
        return;
      }
      gpsStatus.textContent = 'Poloha odoslaná o ' + when + ' (presnosť ' + (body.accuracy_m || '?') + ' m' +
        (data.nearest_station ? ', pri ' + data.nearest_station : '') + ').' + clockNote;
    }).catch(function (err) {
      if (err.status === 403) { stopGps(); invalid(); return; }
      if (err.status === 422) { answeredFix = fixTs; }  // túto polohu server neprijme ani pri ďalšom pokuse
      gpsStatus.textContent = 'Polohu sa nepodarilo odoslať: ' + err.message;
    });
  }

  function startGps() {
    if (!('geolocation' in navigator)) { gpsStatus.textContent = 'Zariadenie nepodporuje určovanie polohy.'; return; }
    if (!window.isSecureContext) { gpsStatus.textContent = 'Poloha vyžaduje zabezpečené pripojenie (HTTPS).'; return; }
    watchId = navigator.geolocation.watchPosition(function (pos) {
      latest = pos;
      gpsError = '';
      sendLatest();
    }, function (err) {
      gpsError = err.code === 1 ? 'Prístup k polohe nie je povolený – povoľte ho v nastaveniach prehliadača.' : 'Poloha nie je dostupná (' + err.message + ').';
      gpsStatus.textContent = gpsError;
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
