/* Správanie UI komponentov (templates/_ui.html) cez data-ui-* atribúty. Progresívne vylepšenie: bez tohto skriptu
   stránka funguje celá (formuláre sa odošlú, tabuľky sa dajú posúvať, hlásenia sú len stále viditeľné).

   data-ui-busy-label="Ukladá sa…"   tlačidlo odosielajúce formulár: počas odosielania aria-busy + zablokovanie
   data-ui-filter="<id tabuľky>"     pole filtra: skrýva riadky bez hľadaného textu, hlási počet (aria-live)
   data-ui-stack                     obal tabuľky: na úzkej obrazovke riadky ako karty (popis stĺpca z <th>)
   data-ui-dismiss                   tlačidlo zavrie najbližšie [data-ui-dismissible]
   API pre ostatné skripty: window.vlakyUI.{setBusy, refreshTable} */
(function () {
  'use strict';

  var BUSY_TIMEOUT_MS = 20000;   // ak odpoveď nepríde (výpadok siete), tlačidlo sa po čase znova povolí

  // ------------------------------------------------------------ tlačidlo počas odosielania
  function setBusy(btn, busy) {
    if (!btn) { return; }
    if (busy) {
      btn.setAttribute('aria-busy', 'true');
      btn.setAttribute('data-ui-busy-text', btn.getAttribute('data-ui-busy-label') || 'Pracujem…');
      btn.disabled = true;
      btn._uiBusyTimer = setTimeout(function () { setBusy(btn, false); }, BUSY_TIMEOUT_MS);
    } else {
      btn.removeAttribute('aria-busy');
      btn.removeAttribute('data-ui-busy-text');
      btn.disabled = false;
      if (btn._uiBusyTimer) { clearTimeout(btn._uiBusyTimer); btn._uiBusyTimer = null; }
    }
  }

  // Poslucháč v bublaní za app.js: potvrdenie (data-confirm) a ochrana pred dvojitým odoslaním rozhodnú skôr.
  document.addEventListener('submit', function (ev) {
    if (ev.defaultPrevented) { return; }
    var form = ev.target;
    var btn = ev.submitter && ev.submitter.hasAttribute('data-ui-busy-label') ? ev.submitter
      : (form.querySelector ? form.querySelector('button[type="submit"][data-ui-busy-label]') : null);
    if (form.hasAttribute && form.hasAttribute('data-ui-demo')) { ev.preventDefault(); }   // katalóg /komponenty
    if (!btn) { return; }
    // zablokovať až po zostavení údajov formulára – zablokované tlačidlo by vypadlo z odoslaných polí (name/value)
    setTimeout(function () { setBusy(btn, true); }, 0);
  });
  // návrat tlačidlom Späť (stránka z bfcache): tlačidlá znova povoliť
  window.addEventListener('pageshow', function (ev) {
    if (!ev.persisted) { return; }
    document.querySelectorAll('[aria-busy="true"][data-ui-busy-label]').forEach(function (b) { setBusy(b, false); });
  });

  // ------------------------------------------------------------ filter tabuľky
  function norm(s) {
    return String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, ' ').trim();
  }
  function plural(n) { return n === 1 ? 'riadok' : (n >= 2 && n <= 4 ? 'riadky' : 'riadkov'); }

  function applyFilter(input) {
    var id = input.getAttribute('data-ui-filter');
    var table = document.getElementById(id);
    if (!table) { return; }
    var q = norm(input.value);
    var rows = table.tBodies.length ? Array.prototype.slice.call(table.tBodies[0].rows) : [];
    var shown = 0;
    rows.forEach(function (tr) {
      if (tr._uiText === undefined) { tr._uiText = norm(tr.textContent); }
      var ok = !q || tr._uiText.indexOf(q) !== -1;
      tr.hidden = !ok;
      if (ok) { shown += 1; }
    });
    var count = document.querySelector('[data-ui-filter-count="' + id + '"]');
    if (count) {
      count.textContent = q ? ('Zobrazené ' + shown + ' z ' + rows.length) : (rows.length + ' ' + plural(rows.length));
    }
    var empty = document.querySelector('[data-ui-filter-empty="' + id + '"]');
    if (empty) { empty.hidden = shown !== 0; }
    table.hidden = shown === 0 && rows.length > 0;
  }

  function debounce(fn, ms) {
    var t = null;
    return function () { var a = arguments, self = this; clearTimeout(t); t = setTimeout(function () { fn.apply(self, a); }, ms); };
  }

  function initFilters(root) {
    (root || document).querySelectorAll('input[data-ui-filter]').forEach(function (input) {
      if (input._uiBound) { return; }
      input._uiBound = true;
      var run = debounce(function () { applyFilter(input); }, 120);
      input.addEventListener('input', run);
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && input.value) { ev.preventDefault(); input.value = ''; applyFilter(input); }
      });
      if (input.value) { applyFilter(input); }   // hodnotu obnovil prehliadač (Späť)
    });
  }

  // ------------------------------------------------------------ tabuľka ako karty na mobile
  function initStack(root) {
    (root || document).querySelectorAll('[data-ui-stack]').forEach(function (wrap) {
      var table = wrap.querySelector('table');
      if (!table || !table.tHead) { return; }
      var labels = Array.prototype.map.call(table.tHead.rows[0].cells, function (th) { return (th.textContent || '').trim(); });
      Array.prototype.forEach.call(table.tBodies, function (tb) {
        Array.prototype.forEach.call(tb.rows, function (tr) {
          var i = 0;
          Array.prototype.forEach.call(tr.cells, function (td) {
            if (!td.hasAttribute('data-label')) { td.setAttribute('data-label', labels[i] || ''); }
            i += td.colSpan || 1;
          });
        });
      });
      wrap.classList.add('is-stacked');
    });
  }

  // ------------------------------------------------------------ zavretie hlásenia
  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('[data-ui-dismiss]') : null;
    if (!btn) { return; }
    var box = btn.closest('[data-ui-dismissible]');
    if (!box) { return; }
    // fokus nesmie ostať na odstránenom prvku – presunie sa na hlavný obsah
    var main = document.getElementById('obsah');
    box.remove();
    if (main && document.activeElement === document.body) { main.focus({ preventScroll: true }); }
  });

  function refreshTable(root) { initStack(root); initFilters(root); }
  window.vlakyUI = { setBusy: setBusy, refreshTable: refreshTable };
  refreshTable(document);
}());
