/* Spoločné správanie stránok: téma, navigácia, potvrdenia, kopírovanie, našepkávanie staníc, odpočet 13 h. */
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
      var left = (start + limit * 60000 - now) / 60000;
      el.textContent = fmtMinutes(left);
      var cls = left > buffer ? 'zelena' : (left > buffer / 2 ? 'oranzova' : 'cervena');
      el.classList.remove('crew-zelena', 'crew-oranzova', 'crew-cervena');
      el.classList.add('crew', 'crew-' + cls);
      el.setAttribute('title', 'Do limitu pracovného času (' + Math.round(limit / 60 * 10) / 10 + ' h)');
    });
  }
  if (document.querySelector('[data-countdown]')) {
    updateCountdowns();
    setInterval(updateCountdowns, 30000);
  }
}());
