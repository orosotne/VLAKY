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

  // ------------------------------------------------------------ čas servera
  // Vek údajov („pred 12 min“) a odpočet pracovného času sa počítajú k času servera (data-teraz pri načítaní stránky,
  // „teraz“ v odpovedi na dopyt otvorenej stránky), nie k hodinám počítača dispečera – tie môžu ísť inak.
  var clock = { offset: 0 };
  function syncClock(serverIso, sentAt) {
    var server = Date.parse(serverIso || '');
    if (isNaN(server)) { return; }
    var now = Date.now();
    clock.offset = server - (typeof sentAt === 'number' ? (sentAt + now) / 2 : now);
  }
  function serverNow() { return Date.now() + clock.offset; }
  var clockEl = document.querySelector('[data-teraz]');
  if (clockEl) { syncClock(clockEl.getAttribute('data-teraz')); }

  // ------------------------------------------------------------ odpočet pracovného času (limit 13 h)
  function fmtMinutes(m) {
    var neg = m < 0;
    m = Math.abs(Math.round(m));
    var txt = m >= 60 ? Math.floor(m / 60) + ' h ' + String(m % 60).padStart(2, '0') + ' min' : m + ' min';
    return neg ? 'prekročené o ' + txt : txt;
  }

  // data-countdown="<nástup>" + data-limit-min (zmena rušňovodiča) alebo data-countdown-limit="<koniec limitu>" na <dt>
  // (detail odporúčania – limit z prepočtu, rovnako ako ho počíta server; prepíše sa len úvodný údaj dvojice <dd>).
  // now: čas servera (serverNow), bez neho hodiny prehliadača. Funkcia okrem fmtMinutes nič mimo seba nepoužíva
  // (test ju spúšťa samostatne v Node.js), preto má výber prvkov priamo v sebe.
  var COUNTDOWN = '[data-countdown], [data-countdown-limit]';
  function updateCountdowns(now) {
    now = typeof now === 'number' ? now : Date.now();
    document.querySelectorAll('[data-countdown], [data-countdown-limit]').forEach(function (el) {
      var limit = parseFloat(el.getAttribute('data-limit-min') || '780');
      var buffer = parseFloat(el.getAttribute('data-buffer-min') || '60');
      var limitAt = el.getAttribute('data-countdown-limit');
      var end = limitAt ? Date.parse(limitAt) : Date.parse(el.getAttribute('data-countdown')) + limit * 60000;
      if (isNaN(end)) { return; }
      // nikdy viac ako limit (13 h): hodiny môžu byť mierne pozadu; nástup v budúcnosti stránka s odpočtom nezobrazí
      // (server ho vypíše ako chybu záznamu)
      var left = Math.min((end - now) / 60000, limit);
      if (el.tagName === 'DT') {
        var lead = /^(?:prekročené o |[−-])?(?:\d+ h \d{2} min|\d+ min)/;
        var dd = el.nextElementSibling;
        var node = dd ? dd.firstChild : null;
        if (node && node.nodeType === 3 && lead.test(node.nodeValue)) {
          node.nodeValue = node.nodeValue.replace(lead, fmtMinutes(left));
        }
        return;
      }
      el.textContent = fmtMinutes(left);
      var cls = left > buffer ? 'zelena' : (left > buffer / 2 ? 'oranzova' : 'cervena');
      el.classList.remove('crew-zelena', 'crew-oranzova', 'crew-cervena');
      el.classList.add('crew', 'crew-' + cls);
      el.setAttribute('title', 'Do limitu pracovného času (' + String(Math.round(limit / 60 * 10) / 10).replace('.', ',') + ' h)');
    });
  }
  var countdownTimer = null;
  function startCountdowns() {  // aj po obnove obsahu stránky (nové prvky s odpočtom)
    if (!document.querySelector(COUNTDOWN)) { return; }
    updateCountdowns(serverNow());
    if (!countdownTimer) { countdownTimer = setInterval(function () { updateCountdowns(serverNow()); }, 30000); }
  }
  startCountdowns();

  // ------------------------------------------------------------ vek údajov na otvorenej stránke („pred 12 min“)
  // data-vek="<čas ISO>" na <dt> (alebo priamo na prvku): prvý text dvojice <dd> sa priebežne dopočítava k času servera.
  // data-vek-kluc: pri každom dopyte na stav sa čas údaja znova ukotví podľa servera („vek“ v odpovedi) – vek inak
  // rástol od načítania stránky, hoci agent medzitým schránku skontroloval. data-vek-limit + data-vek-odznak: údaj
  // starší ako prah stránka označí aj bez spojenia so serverom (so spojením ho označí server – zmena stavu stránky).
  var DURATION_RE = /\d+ h \d{2} min|\d+ min/;
  function markOld(el, target, node, age) {
    var limit = parseFloat(el.getAttribute('data-vek-limit') || '');
    var text = el.getAttribute('data-vek-odznak');
    if (isNaN(limit) || !text) { return; }
    var own = target.querySelector('[data-vek-odznak-stranka]');
    var server = Array.prototype.some.call(target.querySelectorAll('.badge'), function (b) {
      return !(own && own.contains(b)) && b.textContent.trim() === text;
    });
    if (age > limit && !own && !server) {
      own = document.createElement('span');
      own.setAttribute('data-vek-odznak-stranka', '1');
      var badge = document.createElement('span');
      badge.className = 'badge badge-warn';
      badge.textContent = text;
      if (!/\s$/.test(node.nodeValue)) { own.appendChild(document.createTextNode(' ')); }
      own.appendChild(badge);
      if (node.nextSibling) { own.appendChild(document.createTextNode(' ')); }
      target.insertBefore(own, node.nextSibling);
    } else if (age <= limit && own) {
      own.parentNode.removeChild(own);
    }
  }
  function updateAges() {
    var now = serverNow();
    document.querySelectorAll('[data-vek]').forEach(function (el) {
      var ts = Date.parse(el.getAttribute('data-vek'));
      var target = el.tagName === 'DT' ? el.nextElementSibling : el;
      var node = target ? target.firstChild : null;
      if (isNaN(ts) || !node || node.nodeType !== 3 || !DURATION_RE.test(node.nodeValue)) { return; }
      var age = Math.max(0, (now - ts) / 60000);
      node.nodeValue = node.nodeValue.replace(DURATION_RE, fmtMinutes(age));
      markOld(el, target, node, age);
    });
  }
  function reanchorAges(ages) {
    if (ages) {
      document.querySelectorAll('[data-vek-kluc]').forEach(function (el) {
        var at = ages[el.getAttribute('data-vek-kluc')];
        if (at) { el.setAttribute('data-vek', at); }
      });
    }
    updateAges();
    updateCountdowns(serverNow());
  }

  // ------------------------------------------------------------ živý stav otvorenej stránky (odporúčanie, vlak)
  // Stránka s data-zivy-stav (odtlačok toho, čo ukazuje) sa každú minútu a po návrate na kartu opýta servera na
  // odtlačok (hlavička X-Vlaky-Stav) – odpoveď nesie aj čas servera a časy údajov. Pri zmene – agent prestal
  // prepočítavať a ZELENÁ je teraz ORANŽOVÁ, nové odporúčanie, rozhodnutie iného dispečera, prepočet – si obsah obnoví
  // (rozpísané poznámky a fokus ostanú; potvrdenie neaktuálnych údajov nie), takže otvorená stránka nikdy neukazuje
  // ZELENÚ bez čerstvých dát agenta. Zmenu hlavných údajov (data-zivy-hlavne: semafor, rozhodnutie, stav…) ohlási
  // výrazným upozornením s pomenovaním zmeny, ktoré pri ďalšej bežnej obnove ostane; obnovu bez takej zmeny (bežný
  // prepočet s rovnakým výsledkom) len nenápadným riadkom. Bez spojenia so serverom (alebo po odhlásení) upozorní, že
  // stav nie je overený.
  var LIVE_MS = 60000;
  var live = { fails: 0, lastOk: serverNow(), busy: false };
  var NOTICES = ['zivy-stav-chyba', 'zivy-stav-sprava', 'zivy-stav-obnova'];  // poradie navrchu stránky

  function liveBox() { return document.querySelector('[data-zivy-stav]'); }
  function liveUrl(box) {
    return (box && box.getAttribute('data-zivy-url')) || (window.location.pathname + window.location.search);
  }
  function hhmm(ms) {
    var box = liveBox();
    var opts = { hour: '2-digit', minute: '2-digit', timeZone: (box && box.getAttribute('data-tz')) || 'Europe/Bratislava' };
    try { return new Date(ms).toLocaleTimeString('sk-SK', opts); } catch (e) { delete opts.timeZone; return new Date(ms).toLocaleTimeString('sk-SK', opts); }
  }
  function noticeEl(id) {
    var main = document.getElementById('obsah');
    if (!main) { return null; }
    var p = document.getElementById(id);
    if (p) { return p; }
    p = document.createElement('p');
    p.id = id;
    var idx = NOTICES.indexOf(id);
    var next = null;
    var prev = null;
    for (var i = idx + 1; i < NOTICES.length && !next; i += 1) { next = document.getElementById(NOTICES[i]); }
    for (var j = idx - 1; j >= 0 && !prev; j -= 1) { prev = document.getElementById(NOTICES[j]); }
    if (next) { next.parentNode.insertBefore(p, next); } else { main.insertBefore(p, prev ? prev.nextSibling : main.firstChild); }
    return p;
  }
  // kind: chyba / varovanie (výrazné, role=alert), info (role=status), '' (nenápadný riadok bez ohlásenia)
  function liveNotice(id, kind, text, reloadUrl) {
    var p = noticeEl(id);
    if (!p) { return; }
    p.className = kind ? 'flash flash-' + kind : 'muted small';
    if (kind === 'chyba' || kind === 'varovanie') { p.setAttribute('role', 'alert'); }
    else if (kind) { p.setAttribute('role', 'status'); }
    else { p.removeAttribute('role'); }
    p.textContent = text + (reloadUrl ? ' ' : '');
    if (reloadUrl) {
      var a = document.createElement('a');
      a.href = reloadUrl;
      a.textContent = 'Obnoviť stránku';
      p.appendChild(a);
    }
  }
  function clearNotice(id) {
    var p = document.getElementById(id);
    if (p) { p.parentNode.removeChild(p); }
  }
  // hlavné údaje stránky [[popis, hodnota], …] → { order, values }; null – stránka ich nemá (staršia verzia)
  function summaryOf(box) {
    if (!box || !box.hasAttribute('data-zivy-hlavne')) { return null; }
    try {
      var items = JSON.parse(box.getAttribute('data-zivy-hlavne') || '[]');
      var values = {};
      items.forEach(function (it) { values[it[0]] = it[1]; });
      return { order: items.map(function (it) { return it[0]; }), values: values };
    } catch (e) { return null; }
  }
  function summaryChanges(before, after) {  // zmenené a nové položky (zmiznuté – napr. odporúčanie na ďalšej strane – nie)
    var out = [];
    after.order.forEach(function (label) {
      var now = after.values[label];
      if (!Object.prototype.hasOwnProperty.call(before.values, label)) { out.push(label + ': ' + now); }
      else if (before.values[label] !== now) { out.push(label + ': ' + before.values[label] + ' → ' + now); }
    });
    return out;
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
    var before = summaryOf(box);
    var sentAt = Date.now();
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
      // upozornenie na zmenu (napr. semafor ZELENÁ → ORANŽOVÁ) ostane aj po ďalšej bežnej obnove
      var kept = document.getElementById('zivy-stav-sprava');
      if (kept) { kept.parentNode.removeChild(kept); }
      main.innerHTML = fresh.innerHTML;
      if (kept) { main.insertBefore(kept, main.firstChild); }
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
      var box2 = liveBox();
      syncClock(box2 && box2.getAttribute('data-teraz'), sentAt);
      updateAges();
      startCountdowns();
      var after = summaryOf(box2);
      var changes = before && after ? summaryChanges(before, after) : null;
      var at = hhmm(serverNow());
      if (changes === null) {
        liveNotice('zivy-stav-sprava', 'info', 'Stav sa zmenil – údaje na stránke sú obnovené (' + at + ').');
      } else if (changes.length) {
        clearNotice('zivy-stav-obnova');
        liveNotice('zivy-stav-sprava', 'varovanie', 'Stav sa zmenil (' + at + ') – ' + changes.slice(0, 3).join('; ') +
          (changes.length > 3 ? '; ďalšie zmeny: ' + (changes.length - 3) : '') + '. Údaje na stránke sú obnovené.');
      } else {
        liveNotice('zivy-stav-obnova', '', 'Údaje obnovené (' + at + ') – bez podstatnej zmeny.');
      }
    });
  }
  function pollLive() {
    var box = liveBox();
    if (!box || live.busy || document.hidden) { return; }
    live.busy = true;
    var sentAt = Date.now();
    var opts = { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Vlaky-Stav': '1', Accept: 'application/json' } };
    fetch(liveUrl(box), opts).then(function (r) {
      if (!r.ok || r.redirected) { throw new Error('stav ' + r.status); }
      return r.json();
    }).then(function (d) {
      live.fails = 0;
      if (d && d.teraz) { syncClock(d.teraz, sentAt); }
      live.lastOk = serverNow();
      clearNotice('zivy-stav-chyba');
      reanchorAges(d && d.vek);
      var cur = liveBox();
      if (!d || !d.stav || !cur || d.stav === cur.getAttribute('data-zivy-stav')) { return null; }
      if (cur.hasAttribute('data-zivy-len-upozornit')) {
        // stránku s jednorazovým obsahom (odkaz pre rušňovodiča) neprepisovať – len upozorniť
        cur.setAttribute('data-zivy-stav', d.stav);
        liveNotice('zivy-stav-sprava', 'varovanie', 'Stav vlaku sa medzitým zmenil (odporúčanie alebo čerstvosť údajov agenta).', liveUrl(cur));
        return null;
      }
      return swapContent(cur);
    }).catch(function () {
      live.fails += 1;
      if (live.fails >= 2) {
        liveNotice('zivy-stav-chyba', 'chyba', 'Stav stránky sa nepodarilo overiť od ' + hhmm(live.lastOk) + ' (bez spojenia so serverom ' +
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
