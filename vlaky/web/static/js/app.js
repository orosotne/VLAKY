/* Spoločné správanie stránok: téma, navigácia, potvrdenia, kopírovanie, našepkávanie staníc, odpočet 13 h,
   vek údajov a živý stav otvorenej stránky (odporúčanie, vlak, zoznam odporúčaní). */
(function () {
  'use strict';

  // ------------------------------------------------------------ téma (auto / tmavá / svetlá)
  var THEME_KEY = 'vlaky-tema';
  var LABELS = { '': 'Režim: automaticky', dark: 'Režim: tmavý', light: 'Režim: svetlý' };

  function getTheme() {
    try { return localStorage.getItem(THEME_KEY) || ''; } catch (e) { return ''; }
  }

  function setTheme(t) {
    try { if (t) { localStorage.setItem(THEME_KEY, t); } else { localStorage.removeItem(THEME_KEY); } } catch (e) { /* bez úložiska */ }
    if (t) { document.documentElement.setAttribute('data-theme', t); } else { document.documentElement.removeAttribute('data-theme'); }
    var btn = document.getElementById('btn-tema');
    if (btn) { btn.textContent = LABELS[t] || LABELS['']; }
    document.dispatchEvent(new CustomEvent('vlaky-tema', { detail: { theme: t, dark: isDark() } }));
  }

  function isDark() {
    var t = document.documentElement.getAttribute('data-theme');
    if (t === 'dark') { return true; }
    if (t === 'light') { return false; }
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  window.vlakyIsDark = isDark;
  window.vlakySetTheme = setTheme;

  var themeBtn = document.getElementById('btn-tema');
  if (themeBtn) {
    themeBtn.textContent = LABELS[getTheme()] || LABELS[''];
    themeBtn.addEventListener('click', function () {
      var order = ['', 'dark', 'light'];
      setTheme(order[(order.indexOf(getTheme()) + 1) % order.length]);
    });
  }
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var onChange = function () { if (!getTheme()) { setTheme(''); } };
    if (mq.addEventListener) { mq.addEventListener('change', onChange); }
  }

  // ------------------------------------------------------------ mobilná navigácia
  var navBtn = document.querySelector('.nav-toggle');
  if (navBtn) {
    navBtn.addEventListener('click', function () {
      var open = navBtn.getAttribute('aria-expanded') === 'true';
      navBtn.setAttribute('aria-expanded', open ? 'false' : 'true');
      document.body.classList.toggle('nav-open', !open);
    });
  }
  document.addEventListener('click', function (ev) {
    document.querySelectorAll('.nav-more details[open]').forEach(function (d) {
      if (!d.contains(ev.target)) { d.removeAttribute('open'); }
    });
  });

  // ------------------------------------------------------------ potvrdenie pred odoslaním
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (form && form.dataset && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      ev.preventDefault();
      return;
    }
    // ochrana pred dvojitým odoslaním
    if (form && form.dataset) {
      if (form.dataset.submitted === '1') { ev.preventDefault(); return; }
      form.dataset.submitted = '1';
      setTimeout(function () { form.dataset.submitted = ''; }, 4000);
    }
  });

  // ------------------------------------------------------------ kopírovanie odkazu
  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('[data-copy]') : null;
    if (!btn) { return; }
    var input = document.querySelector(btn.getAttribute('data-copy'));
    if (!input) { return; }
    var done = function () { var old = btn.textContent; btn.textContent = 'Skopírované'; setTimeout(function () { btn.textContent = old; }, 2000); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(input.value).then(done, function () { input.select(); });
    } else {
      input.select();
      try { document.execCommand('copy'); done(); } catch (e) { /* používateľ skopíruje ručne */ }
    }
  });

  // ------------------------------------------------------------ našepkávanie dopravní (register siete)
  var acSeq = 0;
  document.querySelectorAll('input[data-autocomplete="stanice"]').forEach(function (input) {
    var listId = 'dl-stanice-' + (++acSeq);
    var dl = document.createElement('datalist');
    dl.id = listId;
    document.body.appendChild(dl);
    input.setAttribute('list', listId);
    var timer = null;
    var last = '';
    input.addEventListener('input', function () {
      var q = input.value.trim();
      if (q.length < 2 || q === last) { return; }
      clearTimeout(timer);
      timer = setTimeout(function () {
        last = q;
        fetch('/api/siet/stanice?q=' + encodeURIComponent(q), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
          .then(function (r) { return r.ok ? r.json() : []; })
          .then(function (items) {
            dl.textContent = '';
            (items || []).forEach(function (s) {
              var o = document.createElement('option');
              o.value = s.name;
              o.label = s.type === 'station' ? 'stanica' : s.type;
              dl.appendChild(o);
            });
          })
          .catch(function () { /* bez našepkávania */ });
      }, 200);
    });
  });

  // ------------------------------------------------------------ odpočet pracovného času (limit 13 h)
  function fmtMinutes(m) {
    var neg = m < 0;
    m = Math.abs(Math.round(m));
    var txt = m >= 60 ? Math.floor(m / 60) + ' h ' + String(m % 60).padStart(2, '0') + ' min' : m + ' min';
    return neg ? 'prekročené o ' + txt : txt;
  }

  function updateCountdowns() {
    var now = Date.now();
    document.querySelectorAll('[data-countdown]').forEach(function (el) {
      var start = Date.parse(el.getAttribute('data-countdown'));
      var limit = parseFloat(el.getAttribute('data-limit-min') || '780');
      var buffer = parseFloat(el.getAttribute('data-buffer-min') || '60');
      if (isNaN(start)) { return; }
      // nikdy viac ako limit (13 h): hodiny prehliadača môžu byť mierne pozadu; nástup v budúcnosti stránka
      // s odpočtom nezobrazí (server ho vypíše ako chybu záznamu)
      var left = Math.min((start + limit * 60000 - now) / 60000, limit);
      el.textContent = fmtMinutes(left);
      var cls = left > buffer ? 'zelena' : (left > buffer / 2 ? 'oranzova' : 'cervena');
      el.classList.remove('crew-zelena', 'crew-oranzova', 'crew-cervena');
      el.classList.add('crew', 'crew-' + cls);
      el.setAttribute('title', 'Do limitu pracovného času (' + String(Math.round(limit / 60 * 10) / 10).replace('.', ',') + ' h)');
    });
  }
  var countdownTimer = null;
  function startCountdowns() {  // aj po obnove obsahu stránky (nové prvky s odpočtom)
    if (!document.querySelector('[data-countdown]')) { return; }
    updateCountdowns();
    if (!countdownTimer) { countdownTimer = setInterval(updateCountdowns, 30000); }
  }
  startCountdowns();

  // ------------------------------------------------------------ vek údajov na otvorenej stránke („pred 12 min“)
  // data-vek="<čas ISO>" na <dt> (alebo priamo na prvku): prvý text dvojice <dd> sa priebežne dopočítava k teraz
  var DURATION_RE = /\d+ h \d{2} min|\d+ min/;
  function updateAges() {
    var now = Date.now();
    document.querySelectorAll('[data-vek]').forEach(function (el) {
      var ts = Date.parse(el.getAttribute('data-vek'));
      var target = el.tagName === 'DT' ? el.nextElementSibling : el;
      var node = target ? target.firstChild : null;
      if (isNaN(ts) || !node || node.nodeType !== 3 || !DURATION_RE.test(node.nodeValue)) { return; }
      node.nodeValue = node.nodeValue.replace(DURATION_RE, fmtMinutes(Math.max(0, (now - ts) / 60000)));
    });
  }

  // ------------------------------------------------------------ živý stav otvorenej stránky (odporúčanie, vlak)
  // Stránka s data-zivy-stav (odtlačok toho, čo ukazuje) sa každú minútu a po návrate na kartu opýta servera na
  // odtlačok (hlavička X-Vlaky-Stav). Pri zmene – agent prestal prepočítavať a ZELENÁ je teraz ORANŽOVÁ, nové
  // odporúčanie, rozhodnutie iného dispečera – si obsah obnoví (rozpísané poznámky a fokus ostanú; potvrdenie
  // neaktuálnych údajov nie), takže otvorená stránka nikdy neukazuje ZELENÚ bez čerstvých dát agenta. Bez spojenia so
  // serverom (alebo po odhlásení) upozorní, že stav nie je overený.
  var LIVE_MS = 60000;
  var live = { fails: 0, lastOk: Date.now(), busy: false };

  function liveBox() { return document.querySelector('[data-zivy-stav]'); }
  function liveUrl(box) {
    return (box && box.getAttribute('data-zivy-url')) || (window.location.pathname + window.location.search);
  }
  function hhmm(ms) {
    var box = liveBox();
    var opts = { hour: '2-digit', minute: '2-digit', timeZone: (box && box.getAttribute('data-tz')) || 'Europe/Bratislava' };
    try { return new Date(ms).toLocaleTimeString('sk-SK', opts); } catch (e) { delete opts.timeZone; return new Date(ms).toLocaleTimeString('sk-SK', opts); }
  }
  function liveNotice(kind, text, reloadUrl) {
    var main = document.getElementById('obsah');
    if (!main) { return; }
    var p = document.getElementById('zivy-stav-sprava');
    if (!p) {
      p = document.createElement('p');
      p.id = 'zivy-stav-sprava';
      main.insertBefore(p, main.firstChild);
    }
    p.className = 'flash flash-' + kind;
    p.setAttribute('role', kind === 'chyba' ? 'alert' : 'status');
    p.textContent = text + ' ';
    if (reloadUrl) {
      var a = document.createElement('a');
      a.href = reloadUrl;
      a.textContent = 'Obnoviť stránku';
      p.appendChild(a);
    }
  }
  function clearNotice(kind) {
    var p = document.getElementById('zivy-stav-sprava');
    if (p && p.classList.contains('flash-' + kind)) { p.parentNode.removeChild(p); }
  }
  function fieldKey(el) {
    if (el.id) { return '#' + el.id; }
    return (el.form ? (el.form.getAttribute('action') || '') + '|' : '') + (el.name || '');
  }
  function keepable(el) {  // rozpísaný text a výbery; nie skryté polia, tlačidlá ani zaškrtávacie políčka (potvrdenia)
    var type = (el.type || '').toLowerCase();
    return !el.readOnly && !el.disabled && ['hidden', 'submit', 'button', 'checkbox', 'radio', 'file'].indexOf(type) < 0;
  }
  function swapContent(box) {
    var url = liveUrl(box);
    var opts = { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Vlaky-Obnova': '1', Accept: 'text/html' } };
    return fetch(url, opts).then(function (r) {
      if (!r.ok || r.redirected) { throw new Error('obnova ' + r.status); }
      return r.text();
    }).then(function (html) {
      var fresh = new DOMParser().parseFromString(html, 'text/html').getElementById('obsah');
      var main = document.getElementById('obsah');
      if (!fresh || !main || !fresh.querySelector('[data-zivy-stav]')) { throw new Error('obnova: bez obsahu'); }
      var saved = {};
      main.querySelectorAll('input, textarea, select').forEach(function (el) {
        if (keepable(el)) { saved[fieldKey(el)] = el.value; }
      });
      var active = document.activeElement;
      var focusKey = active && main.contains(active) && keepable(active) && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName) ? fieldKey(active) : null;
      var range = null;
      try { if (focusKey && typeof active.selectionStart === 'number') { range = [active.selectionStart, active.selectionEnd]; } } catch (e) { range = null; }
      main.innerHTML = fresh.innerHTML;
      var focusEl = null;
      main.querySelectorAll('input, textarea, select').forEach(function (el) {
        var key = fieldKey(el);
        if (!keepable(el) || !Object.prototype.hasOwnProperty.call(saved, key)) { return; }
        el.value = saved[key];
        if (key === focusKey) { focusEl = el; }
      });
      if (focusEl) {
        focusEl.focus({ preventScroll: true });
        try { if (range) { focusEl.setSelectionRange(range[0], range[1]); } } catch (e) { /* pole bez výberu textu */ }
      }
      updateAges();
      startCountdowns();
      liveNotice('info', 'Stav sa zmenil – údaje na stránke sú obnovené (' + hhmm(Date.now()) + ').');
    });
  }
  function pollLive() {
    var box = liveBox();
    if (!box || live.busy || document.hidden) { return; }
    live.busy = true;
    var opts = { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Vlaky-Stav': '1', Accept: 'application/json' } };
    fetch(liveUrl(box), opts).then(function (r) {
      if (!r.ok || r.redirected) { throw new Error('stav ' + r.status); }
      return r.json();
    }).then(function (d) {
      live.fails = 0;
      live.lastOk = Date.now();
      clearNotice('chyba');
      var cur = liveBox();
      if (!d || !d.stav || !cur || d.stav === cur.getAttribute('data-zivy-stav')) { return null; }
      if (cur.hasAttribute('data-zivy-len-upozornit')) {
        // stránku s jednorazovým obsahom (odkaz pre rušňovodiča) neprepisovať – len upozorniť
        cur.setAttribute('data-zivy-stav', d.stav);
        liveNotice('varovanie', 'Stav vlaku sa medzitým zmenil (odporúčanie alebo čerstvosť údajov agenta).', liveUrl(cur));
        return null;
      }
      return swapContent(cur);
    }).catch(function () {
      live.fails += 1;
      if (live.fails >= 2) {
        liveNotice('chyba', 'Stav stránky sa nepodarilo overiť od ' + hhmm(live.lastOk) + ' (bez spojenia so serverom ' +
          'alebo ste boli odhlásení) – semafor a údaje nemusia byť aktuálne. Pred rozhodnutím stránku obnovte.',
          liveUrl(liveBox()));
      }
    }).then(function () { live.busy = false; });
  }
  if (liveBox()) {
    setInterval(pollLive, LIVE_MS);
    setInterval(updateAges, 30000);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { updateAges(); pollLive(); }
    });
    window.addEventListener('pageshow', function (ev) { if (ev.persisted) { pollLive(); } });
  }
}());
