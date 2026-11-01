/* Živá mapa Slovenska (M1): sieť ŽSR, hraničné priechody, výluky s časovým posuvníkom, vlaky so semaforom,
   tabuľka podľa naliehavosti, upozornenia, časová os a živé aktualizácie cez SSE.
   Bez knižnice MapLibre (offline) sa použije zjednodušená schéma na plátne (canvas). */
(function () {
  'use strict';

  var cfgEl = document.getElementById('dash-config');
  if (!cfgEl) { return; }
  var CFG = JSON.parse(cfgEl.textContent || '{}');
  var TZ = CFG.tz || 'Europe/Bratislava';
  var TILE_URL = String(CFG.tileUrl || '');
  var TILE_ATTRIBUTION = String(CFG.tileAttribution || '');
  var SK_BOUNDS = [[16.83, 47.73], [22.57, 49.62]];
  var HOURS = [0, 6, 24, 48];
  var HOURS_LABEL = ['teraz', '+6 h', '+24 h', '+48 h'];
  var COLOR_CLASS = { 'ZELENÁ': 'zelena', 'ORANŽOVÁ': 'oranzova', 'ČERVENÁ': 'cervena', 'SIVÁ': 'seda' };
  var DOT_COLOR = { zelena: '#22a447', oranzova: '#f08c00', cervena: '#e03131', seda: '#8a949e' };
  var EMPTY_FC = { type: 'FeatureCollection', features: [] };
  var POPUP_OFFSET = 18;   // odstup okna vlaku od značky [px]
  var POPUP_MARGIN = 8;    // najmenší odstup okna od okraja mapy [px]
  var POPUP_MAX_W = 360;   // najväčšia šírka okna vlaku [px]
  var POPUP_SIDE_MIN_W = 240;  // najmenšia šírka okna vedľa vlaku na nízkej mape (mobil na šírku) [px]
  var POPUP_MIN_H = 240;   // okno pod vlakom nižšie ako toto – na nízkej mape radšej vedľa vlaku [px]
  var ARROW_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 2l7.5 18-7.5-4.5L4.5 20z"/></svg>';

  var state = {
    trains: [], byId: {}, selected: null, following: null, hours: 0, ids: null, closures: EMPTY_FC,
    borders: CFG.borders || [], stations: [], view: null, routeFor: null, pending: {}, timer: null,
    trailFor: null, trailLoaded: 0, layers: { miesta: null, vlecky: null },
    firstLoad: true, focusParam: null
  };

  // ------------------------------------------------------------ pomocné funkcie
  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) { return; }
        if (k === 'class') { e.className = v; } else if (k === 'text') { e.textContent = v; } else { e.setAttribute(k, v === true ? '' : String(v)); }
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) { return; }
      e.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    });
    return e;
  }

  var dtf = new Intl.DateTimeFormat('sk-SK', { timeZone: TZ, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  function fmt(iso, withDate) {
    if (!iso) { return '—'; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return '—'; }
    var p = {};
    dtf.formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
    var hm = p.hour + ':' + p.minute;
    return withDate ? p.day + '.' + p.month + '. ' + hm : hm;
  }
  function fmtMin(m) {
    if (m === null || m === undefined || isNaN(m)) { return '—'; }
    var neg = m < 0;
    m = Math.abs(Math.round(m));
    var t = m >= 60 ? Math.floor(m / 60) + ' h ' + String(m % 60).padStart(2, '0') + ' min' : m + ' min';
    return (neg ? '−' : '') + t;
  }
  function api(url) {
    return fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } }).then(function (r) {
      if (r.status === 401) {
        window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
        throw new Error('neprihlásený');
      }
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    });
  }
  function semaforBadge(color) {
    return el('span', { class: 'semafor semafor-' + (COLOR_CLASS[color] || 'seda') }, [color || 'bez odporúčania']);
  }
  function isDark() { return window.vlakyIsDark ? window.vlakyIsDark() : false; }
  function setStatus(text) {
    var box = document.getElementById('map-status');
    if (!box) { return; }
    box.textContent = text || '';
    box.hidden = !text;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  /** Rastrové dlaždice podkladovej mapy (šablóna {s} sa rozvinie na a, b, c). */
  function tileUrls() {
    if (!TILE_URL) { return []; }
    return TILE_URL.indexOf('{s}') !== -1 ? ['a', 'b', 'c'].map(function (x) { return TILE_URL.replace('{s}', x); }) : [TILE_URL];
  }
  function relationOf(t) { return t.origin + ' → ' + t.destination; }

  /** Skrátený názov PPS pre štítok na mape (celý text je v titulku). */
  function shortPps(text) {
    var t = String(text || '').split(/[;(]/)[0].trim();
    return t.length > 26 ? t.slice(0, 25) + '…' : t;
  }

  /** Kde je vlak na mape: poloha (na koľaji) alebo kotva (pri hranici / vo východiskovej stanici). */
  function posOf(t) {
    if (t.estimate) { return { lngLat: [t.estimate.lon, t.estimate.lat], anchor: false, estimate: true }; }
    if (t.position) { return { lngLat: [t.position.lon, t.position.lat], anchor: false }; }
    if (t.anchor) { return { lngLat: [t.anchor.lon, t.anchor.lat], anchor: true, label: t.anchor.label }; }
    return null;
  }

  // ------------------------------------------------------------ filtre
  var fQ = document.getElementById('f-hladat');
  var fStav = document.getElementById('f-stav');
  var fFarba = document.getElementById('f-farba');
  var fHranica = document.getElementById('f-hranica');
  var fZakaznik = document.getElementById('f-zakaznik');
  var fRelacia = document.getElementById('f-relacia');
  function passes(t) {
    var q = (fQ && fQ.value || '').trim().toLowerCase();
    if (q && String(t.number).toLowerCase().indexOf(q) === -1) { return false; }
    if (fStav && fStav.value && t.status !== fStav.value) { return false; }
    if (fFarba && fFarba.value) {
      if (fFarba.value === 'SIVÁ') { if (!t.stale) { return false; } } else if (t.color !== fFarba.value) { return false; }
    }
    if (fHranica && fHranica.value) {
      var b = fHranica.value;
      var ok = (t.entry_border && String(t.entry_border.id) === b) || (t.exit_border && String(t.exit_border.id) === b);
      if (!ok) { return false; }
    }
    if (fZakaznik && fZakaznik.value && (t.customer || '') !== fZakaznik.value) { return false; }
    if (fRelacia && fRelacia.value && relationOf(t) !== fRelacia.value) { return false; }
    return true;
  }
  function visibleTrains() { return state.trains.filter(passes); }

  /** Doplní možnosti filtrov zákazník a relácia podľa aktuálnych vlakov (výber sa zachová). */
  function refreshFilterOptions() {
    [[fZakaznik, function (t) { return t.customer || ''; }, 'Všetci zákazníci'],
     [fRelacia, relationOf, 'Všetky relácie']].forEach(function (cfg) {
      var sel = cfg[0];
      if (!sel) { return; }
      var values = {};
      state.trains.forEach(function (t) { var v = cfg[1](t); if (v) { values[v] = true; } });
      var current = sel.value;
      if (current) { values[current] = true; }
      var keys = Object.keys(values).sort(function (a, b) { return a.localeCompare(b, 'sk'); });
      var signature = keys.join('\n');
      if (sel.getAttribute('data-sig') === signature) { return; }
      sel.setAttribute('data-sig', signature);
      sel.textContent = '';
      sel.appendChild(el('option', { value: '' }, [cfg[2]]));
      keys.forEach(function (k) { sel.appendChild(el('option', { value: k }, [k])); });
      sel.value = current;
    });
  }

  // ------------------------------------------------------------ obsah okna (popup) vlaku
  function popupContent(t) {
    var p = t.position;
    var r = t.recommendation || {};
    var d = t.driver;
    var pos = posOf(t);
    var moving = p ? (p.moving === true ? ' · ide' : (p.moving === false ? ' · stojí' : '')) : '';
    var waiting = t.handover && (t.handover.status === 'ocakavany' || t.handover.status === 'prisiel') ? ' · čaká na prevzatie' : '';
    var rows = [
      ['Zákazník', t.customer || '—'],
      ['Relácia', t.relation],
      ['Stav', (t.status_text || t.status) + moving + waiting],
      ['Rušeň', t.locomotive ? t.locomotive.number + (t.locomotive.type ? ' (' + t.locomotive.type + ')' : '') : '—'],
      ['Rušňovodič', d ? (d.name || 'nepriradený') + (d.left_min !== null && d.left_min !== undefined ? ' · do limitu 13 h: ' + fmtMin(d.left_min) : '') : '—']
    ];
    if (d && d.unknown_start) { rows.push(['Pracovný čas', d.relieve_note || 'limit 13 h sa nedá overiť – zadajte nástup rušňovodiča']); }
    else if (d && d.relieve_by && (d.color === 'cervena' || d.color === 'oranzova')) { rows.push(['Vystriedať najneskôr', fmt(d.relieve_by, true)]); }
    rows.push(['Odchýlka', t.slack_min === null || t.slack_min === undefined ? '—' :
      (t.slack_min < 0 ? 'meškanie ' + fmtMin(-t.slack_min) + ' voči termínu' : 'rezerva ' + fmtMin(t.slack_min) + ' do termínu')]);
    rows.push(['ETA P50 / P90', fmt(t.eta_p50, true) + ' / ' + fmt(t.eta_p90, true) + (t.stale && t.eta_p50 ? ' (neistá)' : '')]);
    if (t.handover && t.handover.avizo_eta && t.handover.status === 'ocakavany') { rows.push(['Avízo na hranicu', fmt(t.handover.avizo_eta, true) + ' · ' + (t.handover.border || '')]); }
    if (t.next_station) { rows.push(['Nasledujúca dopravňa', t.next_station.name + ' (' + fmt(t.next_station.arr) + ')']); }
    else if (p && p.nearest_station) { rows.push(['Najbližšia dopravňa', p.nearest_station]); }
    else { rows.push(['Nasledujúca dopravňa', '—']); }
    rows.push(['Najbližšie riziko', t.risk || '—']);
    rows.push(['Posledná správa', p ? fmt(p.ts, true) + ' · ' + p.source_text + ' (pred ' + fmtMin(p.age_min) + ')' : 'žiadna poloha']);
    if (t.estimate) {
      rows.push(['Poloha', 'odhad – posledná správa pred ' + fmtMin(p ? p.age_min : t.estimate.minutes) + '; +' +
        t.estimate.km.toFixed(1).replace('.', ',') + ' km podľa ' + (t.estimate.method === 'plán' ? 'plánu odporúčania' : 'poslednej rýchlosti') +
        ' (' + t.estimate.speed_kmh + ' km/h)' + (t.estimate.capped && t.estimate.next_station ? ', najďalej po ' + t.estimate.next_station : '')]);
    }
    var dl = el('dl');
    rows.forEach(function (row) { dl.appendChild(el('dt', null, [row[0]])); dl.appendChild(el('dd', null, [row[1]])); });
    var note = null;
    if (pos && pos.estimate) { note = el('p', { class: 'odhad small' }, ['Odhad polohy – posledná správa pred ' + fmtMin(p ? p.age_min : 0) + '.']); }
    else if (pos && pos.anchor) { note = el('p', { class: 'odhad small' }, ['Bez polohy – zobrazené ' + (pos.label || 'približne') + '.']); }
    else if (t.stale) { note = el('p', { class: 'odhad small' }, ['Poloha je staršia ako ' + CFG.staleMinutes + ' min – ETA je neistá.']); }
    else if (p && !p.snapped) { note = el('p', { class: 'odhad small' }, ['Poloha mimo koľaje' + (p.off_track_m ? ' (' + Math.round(p.off_track_m) + ' m)' : '') + '.']); }
    var follow = el('button', { type: 'button', class: 'btn btn-sm btn-primary', 'data-ctl': 'sledovat' }, [state.following === t.id ? 'Nesledovať' : 'Sledovať vlak']);
    follow.addEventListener('click', function () { if (state.following === t.id) { stopFollow(); } else { startFollow(t.id); } });
    var trailBtn = el('button', { type: 'button', class: 'btn btn-sm', 'data-ctl': 'stopa', 'aria-pressed': state.trailFor === t.id ? 'true' : 'false' }, ['Stopa 24 h']);
    trailBtn.addEventListener('click', function () { toggleTrail(t.id); });
    var routeNote = null;
    if (state.routeVariant && state.routeVariant.id === t.id && state.routeVariant.variant === 'obchadzka') {
      routeNote = el('p', { class: 'small odhad' }, ['Zvolená varianta: ' + (state.routeVariant.name || 'obchádzka') + ' (na mape oranžovo).']);
    }
    var actions = el('div', { class: 'pop-actions' }, [
      follow, trailBtn,
      el('a', { class: 'btn btn-sm', href: '/vlaky/' + t.id, 'data-ctl': 'detail' }, ['Detail vlaku']),
      r.id ? el('a', { class: 'btn btn-sm', href: '/odporucania/' + r.id, 'data-ctl': 'odporucanie' }, ['Odporúčanie']) : null
    ]);
    // agent neprepočítava alebo nečíta schránku: posledná ZELENÁ sa zobrazuje ako ORANŽOVÁ – vysvetliť prečo
    var dataNote = t.downgraded && t.data_note ? el('p', { class: 'odhad small' }, [t.data_note]) : null;
    return el('div', { class: 'pop' }, [
      // nadpis dostane fokus pri otvorení okna (tlačidlá sú až na konci dlhého obsahu, ktorý sa v okne posúva)
      el('h3', { tabindex: '-1', 'data-ctl': 'nadpis' }, ['Vlak ' + t.number + ' ', semaforBadge(t.color)]),
      dataNote,
      r.decision ? el('p', { class: 'small' }, [el('strong', null, [r.decision + ': ']), r.summary || '']) : null,
      dl, note, routeNote, actions
    ]);
  }

  // ------------------------------------------------------------ klávesnica: fokus v okne vlaku
  /** Prvok okna vlaku (box), ktorý má fokus – aby ho po prekreslení okna dostal znova; inak null. */
  function focusedControl(box) {
    var a = document.activeElement;
    if (!box || !a || a === document.body || !box.contains(a)) { return null; }
    return a.getAttribute('data-ctl') || 'nadpis';
  }
  /** Presunie fokus do okna vlaku na prvok ctl (inak na nadpis okna) – bez posunu obsahu mapy alebo stránky
      (okno môže byť ešte mimo viditeľnej časti mapy, kým sa mapa neposunie). */
  function focusControl(box, ctl) {
    if (!box || !ctl) { return; }
    var e = box.querySelector('[data-ctl="' + ctl + '"]') || box.querySelector('[data-ctl="nadpis"]');
    if (e) { e.focus({ preventScroll: true }); }
  }

  /** Prerušované spojnice posledná správa → odhad polohy. */
  function estimateLines(list) {
    return { type: 'FeatureCollection', features: (list || []).filter(function (t) { return t.estimate && t.estimate.from; }).map(function (t) {
      return { type: 'Feature', geometry: { type: 'LineString', coordinates: [t.estimate.from, [t.estimate.lon, t.estimate.lat]] }, properties: { id: t.id } };
    }) };
  }

  function yesNo(v) { return v === true || v === 'true' ? 'áno' : (v === false || v === 'false' ? 'nie' : 'neoverené'); }

  /** Obsah okna pre miesto na čakanie/odstavenie alebo vlečku. */
  function placeContent(p) {
    var rows = [];
    if (p.druh) { rows.push(['Druh', p.druh]); }
    if (p.zdroj) { rows.push(['Zdroj', p.zdroj]); }
    if (p.can_wait !== undefined) {
      rows.push(['Čakanie', yesNo(p.can_wait)], ['Odstavenie', yesNo(p.can_stable)], ['Výmena rušňovodiča', yesNo(p.can_change_crew)]);
      rows.push(['Max. dĺžka vlaku', p.max_train_length_m ? Math.round(Number(p.max_train_length_m)) + ' m' : 'neuvedená']);
    }
    if (p.note) { rows.push(['Poznámka', p.note]); }
    var dl = el('dl');
    rows.forEach(function (r) { dl.appendChild(el('dt', null, [r[0]])); dl.appendChild(el('dd', null, [r[1]])); });
    return el('div', { class: 'pop' }, [el('h3', null, [p.name || '']), dl,
      p.can_wait !== undefined ? el('p', { class: 'small muted' }, ['Údaje dopraví upravíte na stránke Dopravne.']) : null]);
  }

  function isTrue(v) { return v === true || v === 'true'; }

  /** Obsah okna výluky (aj s potvrdením výluky v stave „návrh“). */
  function closureContent(p) {
    var children = [
      el('h3', null, [(p.druh_text || 'Výluka') + ' ', isTrue(p.nepotvrdena) ? el('span', { class: 'badge badge-warn' }, ['nepotvrdená']) : null]),
      el('p', null, [el('strong', null, [p.od_stanice + ' – ' + p.do_stanice])]),
      el('dl', null, [
        el('dt', null, ['Platnosť']), el('dd', null, [fmt(p.zaciatok, true) + ' – ' + fmt(p.koniec, true)]),
        p.denne_od ? el('dt', null, ['Denne']) : null, p.denne_od ? el('dd', null, [p.denne_od + '–' + p.denne_do]) : null,
        el('dt', null, ['Stav']), el('dd', null, [(isTrue(p.aktivna) ? 'platí teraz' : 'najbližšie od ' + fmt(p.najblizsie_od, true)) + ' · ' + (p.stav_text || p.stav)]),
        el('dt', null, ['Dopad']), el('dd', null, [isTrue(p.pomala) ? 'pomalá jazda ' + (p.rychlost || '?') + ' km/h' : (isTrue(p.len_elektricka) ? 'len elektrická trakcia' : 'všetky vlaky')]),
        p.cislo ? el('dt', null, ['Číslo']) : null, p.cislo ? el('dd', null, [p.cislo]) : null,
        p.zdroj ? el('dt', null, ['Zdroj']) : null, p.zdroj ? el('dd', null, [p.zdroj === 'email' ? 'e-mail' + (p.udalost_id ? ' (udalosť #' + p.udalost_id + ')' : '') : (p.zdroj === 'manual' ? 'ručne' : p.zdroj)]) : null
      ])
    ];
    var actions = [el('a', { class: 'btn btn-sm', href: '/vyluky#vyluka-' + p.id }, ['Register výluk'])];
    var csrf = document.querySelector('meta[name="csrf-token"]');
    if (isTrue(p.nepotvrdena) && CFG.canOperate && csrf && csrf.content) {
      var form = el('form', { method: 'post', action: '/vyluky/' + p.id + '/overit', class: 'inline' }, [
        el('input', { type: 'hidden', name: 'csrf_token', value: csrf.content }),
        el('input', { type: 'hidden', name: 'spat', value: '/' }),
        el('button', { type: 'submit', class: 'btn btn-sm btn-primary' }, ['Potvrdiť'])
      ]);
      actions.unshift(form);
    }
    children.push(el('div', { class: 'pop-actions' }, actions));
    return el('div', { class: 'pop' }, children);
  }

  // ============================================================ MapLibre
  function lineColor(dark) {
    return ['case',
      ['in', '1520', ['to-string', ['get', 'g']]], '#7b3fa0',
      ['==', ['get', 'el'], 1], ['match', ['get', 'v'], 3000, '#1f6fd1', 25000, '#c2185b', '#00897b'],
      dark ? '#c3ccd5' : '#3c4650'];
  }

  function MapLibreView(container, handlers) {
    var self = this;
    this.h = handlers;
    this.markers = {};
    this.borderMarkers = [];
    this.stationMarkers = {};
    this.popup = null;
    this.popupId = null;
    this.loaded = false;
    this.pendingClosures = null;
    this.pendingRoute = null;
    this.tileOk = false;
    this.tileErrors = 0;
    this.offline = false;
    this.pendingTrail = null;
    this.pendingLayers = {};
    this.hasTiles = tileUrls().length > 0;
    var sources = {};
    var layers = [{ id: 'pozadie', type: 'background', paint: { 'background-color': '#dfe5ea' } }];
    if (this.hasTiles) {
      sources.osm = { type: 'raster', tiles: tileUrls(), tileSize: 256, maxzoom: 19, attribution: escapeHtml(TILE_ATTRIBUTION) };
      layers.push({ id: 'osm', type: 'raster', source: 'osm', paint: { 'raster-saturation': -0.4 } });
    }
    var map = this.map = new maplibregl.Map({
      container: container,
      style: { version: 8, sources: sources, layers: layers },
      bounds: SK_BOUNDS,
      fitBoundsOptions: { padding: 24 },
      minZoom: 5, maxZoom: 17,
      maxBounds: [[11.0, 45.0], [28.0, 52.5]],
      attributionControl: false,
      dragRotate: false, pitchWithRotate: false, touchPitch: false
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');
    map.addControl(new maplibregl.AttributionControl({ compact: container.clientWidth < 700, customAttribution: 'Trate: © OpenStreetMap contributors (ODbL)' }), 'bottom-right');

    map.on('error', function (e) {
      if (e && e.sourceId === 'osm') {
        self.tileErrors += 1;
        if (!self.tileOk && self.tileErrors >= 2) { self.setOffline(true); }
      }
    });
    map.on('data', function (e) {
      if (e && e.sourceId === 'osm' && e.tile && !self.tileOk) {
        self.tileOk = true;
        self.setOffline(false);
      }
    });
    if (this.hasTiles) {
      setTimeout(function () { if (!self.tileOk) { self.setOffline(true); } }, 12000);
      if (navigator.onLine === false) { setTimeout(function () { if (!self.tileOk) { self.setOffline(true); } }, 1500); }
    } else {
      setStatus('Mapový podklad nie je nastavený (VLAKY_MAP_TILE_URL) – zobrazuje sa železničná sieť bez podkladu.');
    }

    map.on('load', function () { self.onLoad(); });
    map.on('zoom', function () { self.zoomClasses(); });
    map.on('moveend', function () { self.updateStationLabels(); });
    map.on('dragstart', function () { if (state.following) { stopFollow(); } });
    this.zoomClasses();
  }

  MapLibreView.prototype.setOffline = function (off) {
    if (this.offline === off) { return; }
    this.offline = off;
    if (!this.hasTiles) { return; }
    if (this.loaded || this.map.getLayer('osm')) {
      this.map.setLayoutProperty('osm', 'visibility', off ? 'none' : 'visible');
    }
    setStatus(off ? 'Mapa nie je dostupná (offline) – železničná sieť sa zobrazuje bez podkladovej mapy.' : '');
  };

  MapLibreView.prototype.onLoad = function () {
    var map = this.map;
    var self = this;
    var dark = isDark();
    map.addSource('trate', { type: 'geojson', data: '/api/siet/trate' });
    map.addLayer({ id: 'trate-vedlajsie', type: 'line', source: 'trate', minzoom: 10, filter: ['!=', ['get', 's'], ''],
      paint: { 'line-color': lineColor(dark), 'line-width': 1, 'line-opacity': 0.65 } });
    map.addLayer({ id: 'trate', type: 'line', source: 'trate', filter: ['==', ['get', 's'], ''],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': lineColor(dark),
               'line-width': ['interpolate', ['linear'], ['zoom'], 6, ['case', ['==', ['get', 'u'], 'main'], 1.8, 1.0],
                              10, ['case', ['==', ['get', 'u'], 'main'], 3.2, 2.0], 14, 5] } });
    map.addSource('stopa', { type: 'geojson', data: EMPTY_FC });
    map.addLayer({ id: 'stopa', type: 'line', source: 'stopa', layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#6a1b9a', 'line-width': 4, 'line-opacity': 0.75 } });
    map.addSource('trasa', { type: 'geojson', data: EMPTY_FC });
    map.addLayer({ id: 'trasa', type: 'line', source: 'trasa', layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': ['case', ['==', ['get', 'variant'], 'obchadzka'], '#f57c00', '#00b4d8'],
               'line-width': 7, 'line-opacity': 0.55 } });
    map.addSource('odhady', { type: 'geojson', data: EMPTY_FC });
    map.addLayer({ id: 'odhady', type: 'line', source: 'odhady',
      paint: { 'line-color': '#37474f', 'line-width': 2.5, 'line-dasharray': [1.5, 1.5], 'line-opacity': 0.8 } });
    map.addSource('miesta', { type: 'geojson', data: EMPTY_FC });
    map.addLayer({ id: 'miesta', type: 'circle', source: 'miesta', layout: { visibility: 'none' },
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 6, 4, 12, 8],
               'circle-color': ['case', ['get', 'overene'], '#2e7d32', '#81c784'],
               'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2 } });
    map.addSource('vlecky', { type: 'geojson', data: EMPTY_FC });
    map.addLayer({ id: 'vlecky', type: 'circle', source: 'vlecky', layout: { visibility: 'none' },
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 6, 3, 12, 6], 'circle-color': '#8d6e63',
               'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1.5 } });
    map.addSource('vyluky', { type: 'geojson', data: EMPTY_FC });
    map.addLayer({ id: 'vyluky-obrys', type: 'line', source: 'vyluky', paint: { 'line-color': '#ffffff', 'line-width': 9, 'line-opacity': 0.6 } });
    map.addLayer({ id: 'vyluky-neskor', type: 'line', source: 'vyluky', filter: ['!', ['get', 'aktivna']],
      paint: { 'line-color': ['case', ['get', 'pomala'], '#f08c00', '#d7191c'], 'line-width': 5, 'line-dasharray': [1.6, 1.2] } });
    map.addLayer({ id: 'vyluky-teraz', type: 'line', source: 'vyluky', filter: ['get', 'aktivna'],
      paint: { 'line-color': ['case', ['get', 'pomala'], '#f08c00', '#d7191c'], 'line-width': 6 } });
    map.addSource('stanice', { type: 'geojson', data: EMPTY_FC });
    map.addLayer({ id: 'stanice', type: 'circle', source: 'stanice', minzoom: 9,
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 9, 2.5, 14, 5], 'circle-color': '#ffffff',
               'circle-stroke-color': '#3c4650', 'circle-stroke-width': 1.5 } });
    ['miesta', 'vlecky'].forEach(function (layer) {
      map.on('click', layer, function (e) { self.placePopup(e); });
      map.on('mouseenter', layer, function () { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', layer, function () { map.getCanvas().style.cursor = ''; });
    });
    ['vyluky-teraz', 'vyluky-neskor'].forEach(function (layer) {
      map.on('click', layer, function (e) { self.closurePopup(e); });
      map.on('mouseenter', layer, function () { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', layer, function () { map.getCanvas().style.cursor = ''; });
    });
    this.loaded = true;
    this.setDark(dark);
    if (this.offline && this.hasTiles) { map.setLayoutProperty('osm', 'visibility', 'none'); }
    if (this.pendingClosures) { this.setClosures(this.pendingClosures); }
    if (this.pendingRoute !== null) { this.showRoute(this.pendingRoute); }
    if (this.pendingTrail !== null) { this.showTrail(this.pendingTrail); }
    Object.keys(this.pendingLayers).forEach(function (name) {
      var l = self.pendingLayers[name];
      self.setLayer(name, l.fc, l.visible);
    });
    this.setStations(state.stations);
    this.setEstimates(this.lastTrains || []);
  };

  MapLibreView.prototype.setLayer = function (name, fc, visible) {
    if (!this.loaded) { this.pendingLayers[name] = { fc: fc, visible: visible }; return; }
    if (fc) { this.map.getSource(name).setData(fc); }
    this.map.setLayoutProperty(name, 'visibility', visible ? 'visible' : 'none');
  };

  MapLibreView.prototype.placePopup = function (e) {
    var f = e.features && e.features[0];
    if (!f) { return; }
    new maplibregl.Popup({ offset: 8, maxWidth: '320px' }).setLngLat(e.lngLat).setDOMContent(placeContent(f.properties || {})).addTo(this.map);
  };

  MapLibreView.prototype.setEstimates = function (list) {
    this.lastTrains = list;
    if (!this.loaded) { return; }
    this.map.getSource('odhady').setData(estimateLines(list));
  };

  MapLibreView.prototype.showTrail = function (feature) {
    if (!this.loaded) { this.pendingTrail = feature; return; }
    this.map.getSource('stopa').setData(feature && feature.geometry ? { type: 'FeatureCollection', features: [feature] } : EMPTY_FC);
  };

  MapLibreView.prototype.zoomClasses = function () {
    var z = this.map.getZoom();
    var c = this.map.getContainer();
    c.classList.toggle('z-low', z < 7.2);
    c.classList.toggle('z-verylow', z < 6.2);
  };

  MapLibreView.prototype.setDark = function (dark) {
    var map = this.map;
    map.getContainer().classList.toggle('night', dark);
    if (!this.loaded) { return; }
    map.setPaintProperty('pozadie', 'background-color', dark ? '#0b0f14' : '#dfe5ea');
    if (this.hasTiles) {
      map.setPaintProperty('osm', 'raster-brightness-max', dark ? 0.32 : 1);
      map.setPaintProperty('osm', 'raster-saturation', dark ? -0.85 : -0.4);
      map.setPaintProperty('osm', 'raster-contrast', dark ? 0.15 : 0);
    }
    map.setPaintProperty('odhady', 'line-color', dark ? '#cfd8dc' : '#37474f');
    map.setPaintProperty('trate', 'line-color', lineColor(dark));
    map.setPaintProperty('trate-vedlajsie', 'line-color', lineColor(dark));
    map.setPaintProperty('stanice', 'circle-color', dark ? '#0b0f14' : '#ffffff');
    map.setPaintProperty('stanice', 'circle-stroke-color', dark ? '#c3ccd5' : '#3c4650');
    map.setPaintProperty('vyluky-obrys', 'line-color', dark ? '#000000' : '#ffffff');
  };

  MapLibreView.prototype.setBorders = function (list) {
    var map = this.map;
    this.borderMarkers.forEach(function (m) { m.remove(); });
    this.borderMarkers = [];
    var self = this;
    list.forEach(function (b) {
      if (b.lat === null || b.lon === null || b.lat === undefined) { return; }
      var name = b.sk_station || b.name;
      var e = el('div', { class: 'hm' + (b.freight === 'ziadny' ? ' hm-dim' : ''), title: b.name + ' – PPS: ' + b.handover_station + ' (preberanie P50 ' + Math.round(b.dwell_p50_min) + ' / P90 ' + Math.round(b.dwell_p90_min) + ' min)' + (b.freight === 'ziadny' ? ' – bez nákladnej dopravy' : '') }, [
        el('span', { class: 'hm-ikona', 'aria-hidden': 'true' }),
        el('span', { class: 'hm-text' }, [name, el('small', null, ['PPS: ' + shortPps(b.handover_station)])])
      ]);
      self.borderMarkers.push(new maplibregl.Marker({ element: e, anchor: 'left', offset: [-8, 0] }).setLngLat([b.lon, b.lat]).addTo(map));
    });
  };

  MapLibreView.prototype.setStations = function (list) {
    if (!this.loaded || !list || !list.length) { return; }
    this.map.getSource('stanice').setData({ type: 'FeatureCollection', features: list.map(function (s) {
      return { type: 'Feature', geometry: { type: 'Point', coordinates: [s.lon, s.lat] }, properties: { name: s.name, type: s.type } };
    }) });
    this.updateStationLabels();
  };

  MapLibreView.prototype.updateStationLabels = function () {
    var map = this.map;
    var self = this;
    var z = map.getZoom();
    var keep = {};
    if (z >= 10 && state.stations.length) {
      var b = map.getBounds();
      var shown = 0;
      state.stations.forEach(function (s) {
        if (shown >= 250 || !b.contains([s.lon, s.lat])) { return; }
        if (s.type !== 'station' && s.type !== 'border' && z < 12) { return; }
        shown += 1;
        keep[s.name] = true;
        if (!self.stationMarkers[s.name]) {
          var e = el('div', { class: 'st-label' }, [s.name]);
          self.stationMarkers[s.name] = new maplibregl.Marker({ element: e, anchor: 'left', offset: [7, 0] }).setLngLat([s.lon, s.lat]).addTo(map);
        }
      });
    }
    Object.keys(this.stationMarkers).forEach(function (name) {
      if (!keep[name]) { self.stationMarkers[name].remove(); delete self.stationMarkers[name]; }
    });
  };

  MapLibreView.prototype.setClosures = function (fc) {
    if (!this.loaded) { this.pendingClosures = fc; return; }
    this.map.getSource('vyluky').setData(fc || EMPTY_FC);
  };

  MapLibreView.prototype.closurePopup = function (e) {
    var f = e.features && e.features[0];
    if (!f) { return; }
    new maplibregl.Popup({ offset: 8, maxWidth: '340px' }).setLngLat(e.lngLat).setDOMContent(closureContent(f.properties || {})).addTo(this.map);
  };

  MapLibreView.prototype.trainElement = function (t) {
    var self = this;
    var e = el('div', { class: 'vm', role: 'button', tabindex: '0' });
    var dot = el('div', { class: 'vm-dot' });
    dot.innerHTML = ARROW_SVG; // statický obsah (nie dáta)
    e.appendChild(dot);
    e.appendChild(el('span', { class: 'vm-label' }));
    var open = function (ev) { ev.stopPropagation(); self.h.onTrainClick(Number(e.getAttribute('data-id'))); };
    e.addEventListener('click', open);
    e.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(ev); } });
    return e;
  };

  function decorateTrain(e, t) {
    var pos = posOf(t);
    var cls = COLOR_CLASS[t.display_color] || 'seda';
    var c = 'vm c-' + cls;
    if (t.stale) { c += ' stale'; if (t.color) { c += ' ring-' + COLOR_CLASS[t.color]; } }
    if (pos && pos.anchor) { c += ' anchor'; }
    if (pos && pos.estimate) { c += ' estimate'; }
    if (state.selected === t.id) { c += ' selected'; }
    // triedy MapLibre (maplibregl-marker – absolútna poloha značky) musia ostať, inak sa značky po prvej
    // aktualizácii poskladajú pod seba a vlak je na mape inde ako jeho okno
    var keep = Array.prototype.filter.call(e.classList, function (x) { return x.indexOf('maplibregl-') === 0; });
    e.className = c + (keep.length ? ' ' + keep.join(' ') : '');
    e.setAttribute('data-id', t.id);
    var label = t.number;
    if (pos && pos.anchor) {
      label += t.handover && t.handover.avizo_eta ? ' · ETA ' + fmt(t.handover.avizo_eta) : ' · bez polohy';
    }
    if (pos && pos.estimate) { label += ' · odhad'; }
    e.querySelector('.vm-label').textContent = label;
    var dot = e.querySelector('.vm-dot');
    var bearing = t.estimate ? t.estimate.bearing : (t.position ? t.position.bearing : null);
    if (bearing !== null && bearing !== undefined && (t.estimate || t.position.moving !== false)) {
      dot.classList.remove('no-dir');
      dot.querySelector('svg').style.transform = 'rotate(' + bearing + 'deg)';
    } else {
      dot.classList.add('no-dir');
    }
    e.setAttribute('aria-label', 'Vlak ' + t.number + ', ' + (t.color || 'bez odporúčania') + (t.stale ? ', poloha neistá' : '') +
      (t.downgraded ? ', bez čerstvých údajov agenta' : '') +
      (pos && pos.anchor ? ', bez polohy' : '') + (pos && pos.estimate ? ', poloha je odhad' : ''));
    e.title = 'Vlak ' + t.number + ' – ' + t.relation;
  }

  MapLibreView.prototype.setTrains = function (list) {
    var self = this;
    var seen = {};
    list.forEach(function (t) {
      var pos = posOf(t);
      if (!pos) { return; }
      seen[t.id] = true;
      var m = self.markers[t.id];
      if (!m) {
        var e = self.trainElement(t);
        decorateTrain(e, t);
        m = new maplibregl.Marker({ element: e, anchor: 'left', offset: [-13, 0] }).setLngLat(pos.lngLat).addTo(self.map);
        self.markers[t.id] = m;
      } else {
        m.setLngLat(pos.lngLat);
        decorateTrain(m.getElement(), t);
      }
      if (self.popup && self.popupId === t.id) {
        // nové údaje: obsah okna sa prekreslí – fokus ostane na tom istom tlačidle (ak bol v okne), inde sa nemení
        var ctl = focusedControl(self.popup.getElement());
        self.popup.setLngLat(pos.lngLat);
        self.popup.setDOMContent(popupContent(t));
        focusControl(self.popup.getElement(), ctl);
      }
    });
    Object.keys(this.markers).forEach(function (id) {
      if (!seen[id]) { self.markers[id].remove(); delete self.markers[id]; }
    });
    this.setEstimates(list);
  };

  /** Rozloženie okna vlaku: vedľa značky (široká mapa – vlak v strede mapy, okno vpravo od neho; aj nízka mapa,
      napr. mobil na šírku) alebo pod ňou (úzka mapa, mobil na výšku – vlak hore v strede). Okno je najviac také
      široké a vysoké ako miesto v mape, obsah sa v ňom posúva, takže je vždy celé viditeľné.
      target = kam na mape (px) posunúť značku vlaku. */
  MapLibreView.prototype.popupLayout = function () {
    var c = this.map.getContainer();
    var W = c.clientWidth;
    var H = c.clientHeight;
    var side = Math.floor(Math.min(POPUP_MAX_W, W / 2 - POPUP_OFFSET - POPUP_MARGIN));  // šírka okna vedľa vlaku
    var top = 44;  // okno pod vlakom: značka s číslom vlaku zostane viditeľná nad oknom
    var below = H - top - POPUP_OFFSET - 12 - POPUP_MARGIN;                       // výška okna pod vlakom
    if (side >= POPUP_MAX_W || (side >= POPUP_SIDE_MIN_W && below < POPUP_MIN_H)) {
      return { anchor: 'left', width: side, maxHeight: H - 2 * POPUP_MARGIN, target: [W / 2, H / 2], W: W, H: H };
    }
    // nikdy vyššie ako miesto pod vlakom – inak by spodok okna (tlačidlá) bol mimo mapy
    return { anchor: 'top', width: Math.max(180, Math.min(POPUP_MAX_W, W - 2 * POPUP_MARGIN)),
             maxHeight: Math.max(60, below), target: [W / 2, top], W: W, H: H };
  };

  /** Zmestí sa okno vlaku (rozmery box) pri značke v bode p celé do mapy? */
  function popupFits(layout, p, box) {
    var m = POPUP_MARGIN;
    if (layout.anchor === 'left') {
      return p.x - 14 >= m && p.x + POPUP_OFFSET + box.width <= layout.W - m &&
        p.y - box.height / 2 >= m && p.y + box.height / 2 <= layout.H - m;
    }
    return p.x - box.width / 2 >= m && p.x + box.width / 2 <= layout.W - m && p.y - 24 >= m &&
      p.y + POPUP_OFFSET + box.height <= layout.H - m;
  }

  /** Otvorí okno vlaku. fly – vlak vybraný v zozname alebo vyhľadaný (priblížiť a vycentrovať), zoom – úroveň,
      focus – vlak vybral používateľ (aj klávesnicou): fokus prejde do okna.
      Mapa sa posunie jediným pohybom tak, aby bol vlak aj celé okno viditeľné (okno sa pohybuje s mapou). */
  MapLibreView.prototype.openPopup = function (t, fly, zoom, focus) {
    var pos = posOf(t);
    if (!pos) { return; }
    // okno sa otvára znova (napr. tlačidlom v ňom): fokus ostane na tom istom tlačidle
    var ctl = focusedControl(this.popup && this.popup.getElement()) || (focus ? 'nadpis' : null);
    this.closePopup(true);
    var self = this;
    var map = this.map;
    var layout = this.popupLayout();
    this.layout = layout;
    map.getContainer().style.setProperty('--pop-max-h', Math.round(layout.maxHeight) + 'px');
    this.popupId = t.id;
    // bez automatického zamerania prvého tlačidla v MapLibre: prehliadač by posunul obsah mapy, aby bolo tlačidlo
    // vidieť, a pri každom prekreslení okna by fokus preskočil do okna – fokus sa nastavuje nižšie (focusControl)
    this.popup = new maplibregl.Popup({ offset: POPUP_OFFSET, maxWidth: Math.round(layout.width) + 'px', closeOnClick: true,
                                        anchor: layout.anchor, focusAfterOpen: false })
      .setLngLat(pos.lngLat).setDOMContent(popupContent(t)).addTo(map);
    this.popup.on('close', function () {
      if (self.popupId === t.id) { self.popup = null; self.popupId = null; self.h.onPopupClose(t.id); }
    });
    var popEl = this.popup.getElement();
    if (popEl) {
      popEl.setAttribute('role', 'dialog');
      popEl.setAttribute('aria-label', 'Detail vlaku ' + t.number);
      focusControl(popEl, ctl);
    }
    var box = popEl ? popEl.getBoundingClientRect() : { width: layout.width, height: layout.maxHeight };
    var targetZoom = fly ? (zoom || Math.max(map.getZoom(), 9)) : map.getZoom();
    if (fly || state.following === t.id || !popupFits(layout, map.project(pos.lngLat), box)) {
      map.stop();
      map.easeTo({ center: pos.lngLat, zoom: targetZoom, offset: [layout.target[0] - layout.W / 2, layout.target[1] - layout.H / 2],
                   duration: fly ? 800 : 400 });
    }
  };

  /** Posun posledného rozloženia okna (vlak sledovaný s otvoreným oknom ostane tam, kde sa okno zmestí). */
  MapLibreView.prototype.popupOffset = function () {
    if (!this.popup || !this.layout) { return [0, 0]; }
    return [this.layout.target[0] - this.layout.W / 2, this.layout.target[1] - this.layout.H / 2];
  };

  MapLibreView.prototype.closePopup = function (silent) {
    if (this.popup) {
      var p = this.popup;
      this.popup = null;
      if (silent) { this.popupId = null; }
      p.remove();
    }
  };

  MapLibreView.prototype.focus = function (lngLat, zoom) {
    this.map.easeTo({ center: lngLat, zoom: zoom || Math.max(this.map.getZoom(), 9), offset: this.popupOffset(),
                      duration: 800 });
  };

  MapLibreView.prototype.showRoute = function (feature) {
    if (!this.loaded) { this.pendingRoute = feature; return; }
    this.map.getSource('trasa').setData(feature ? { type: 'FeatureCollection', features: [feature] } : EMPTY_FC);
  };

  // ============================================================ záložná schéma (canvas) bez MapLibre
  function CanvasView(container, handlers) {
    var self = this;
    this.h = handlers;
    this.container = container;
    this.lines = null;
    this.buckets = null;
    this.trains = [];
    this.borders = [];
    this.closures = EMPTY_FC;
    this.route = null;
    this.trail = null;
    this.layerData = {};
    this.layerVisible = {};
    this.popupEl = null;
    this.popupId = null;
    this.cy0 = 48.7;
    this.cosLat = Math.cos(this.cy0 * Math.PI / 180);
    var c = this.canvas = document.createElement('canvas');
    c.className = 'fallback-canvas';
    c.setAttribute('role', 'img');
    c.setAttribute('aria-label', 'Zjednodušená schéma železničnej siete SR s polohou vlakov');
    container.appendChild(c);
    var zoomBox = el('div', { class: 'fallback-zoom' });
    var zin = el('button', { type: 'button', class: 'btn btn-sm', 'aria-label': 'Priblížiť' }, ['+']);
    var zout = el('button', { type: 'button', class: 'btn btn-sm', 'aria-label': 'Oddialiť' }, ['−']);
    zin.addEventListener('click', function () { self.zoomAt(1.5); });
    zout.addEventListener('click', function () { self.zoomAt(1 / 1.5); });
    zoomBox.appendChild(zin);
    zoomBox.appendChild(zout);
    container.appendChild(zoomBox);
    this.resize();
    this.fit();
    window.addEventListener('resize', function () { self.resize(); self.draw(); });
    var drag = null;
    c.addEventListener('pointerdown', function (ev) { drag = { x: ev.clientX, y: ev.clientY, moved: false }; c.setPointerCapture(ev.pointerId); });
    c.addEventListener('pointermove', function (ev) {
      if (!drag) { return; }
      var dx = ev.clientX - drag.x;
      var dy = ev.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) { drag.moved = true; if (state.following) { stopFollow(); } }
      if (drag.moved) {
        self.cx -= dx / self.k;
        self.cy += dy / (self.k / self.cosLat);
        drag.x = ev.clientX;
        drag.y = ev.clientY;
        self.draw();
      }
    });
    c.addEventListener('pointerup', function (ev) {
      var wasDrag = drag && drag.moved;
      drag = null;
      if (wasDrag) { return; }
      var r = c.getBoundingClientRect();
      var hit = self.hitTrain(ev.clientX - r.left, ev.clientY - r.top);
      if (hit !== null) { self.h.onTrainClick(hit); } else { self.closePopup(); }
    });
    c.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      var r = c.getBoundingClientRect();
      self.zoomAt(ev.deltaY < 0 ? 1.25 : 0.8, ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });
  }

  CanvasView.prototype.resize = function () {
    var r = this.container.getBoundingClientRect();
    this.w = Math.max(100, r.width);
    this.hgt = Math.max(100, r.height);
    this.dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(this.w * this.dpr);
    this.canvas.height = Math.round(this.hgt * this.dpr);
  };
  CanvasView.prototype.fit = function () {
    var lonSpan = SK_BOUNDS[1][0] - SK_BOUNDS[0][0];
    var latSpan = SK_BOUNDS[1][1] - SK_BOUNDS[0][1];
    this.k = 0.94 * Math.min(this.w / lonSpan, this.hgt / (latSpan / this.cosLat));
    this.cx = (SK_BOUNDS[0][0] + SK_BOUNDS[1][0]) / 2;
    this.cy = (SK_BOUNDS[0][1] + SK_BOUNDS[1][1]) / 2;
  };
  CanvasView.prototype.project = function (lon, lat) {
    return [(lon - this.cx) * this.k + this.w / 2, (this.cy - lat) * (this.k / this.cosLat) + this.hgt / 2];
  };
  CanvasView.prototype.zoomAt = function (factor, px, py) {
    if (px === undefined) { px = this.w / 2; py = this.hgt / 2; }
    var lon = this.cx + (px - this.w / 2) / this.k;
    var lat = this.cy - (py - this.hgt / 2) / (this.k / this.cosLat);
    this.k = Math.min(Math.max(this.k * factor, 40), 60000);
    this.cx = lon - (px - this.w / 2) / this.k;
    this.cy = lat + (py - this.hgt / 2) / (this.k / this.cosLat);
    this.draw();
  };
  CanvasView.prototype.setLines = function (geojson) {
    var buckets = {};
    (geojson.features || []).forEach(function (f) {
      var p = f.properties || {};
      var key;
      if (String(p.g || '').indexOf('1520') !== -1) { key = 'broad'; } else if (p.el === 1) { key = p.v === 3000 ? 'dc' : (p.v === 25000 ? 'ac' : 'el'); } else { key = 'none'; }
      if (p.s) { key = 'service'; }
      (buckets[key] = buckets[key] || []).push(f.geometry);
    });
    this.buckets = buckets;
    this.draw();
  };
  CanvasView.prototype.setBorders = function (list) { this.borders = list; this.draw(); };
  CanvasView.prototype.setStations = function () { /* názvy staníc sa v schéme nezobrazujú */ };
  CanvasView.prototype.setClosures = function (fc) { this.closures = fc || EMPTY_FC; this.draw(); };
  CanvasView.prototype.setTrains = function (list) {
    this.trains = list;
    if (this.popupEl && this.popupId !== null) {
      var t = state.byId[this.popupId];
      var ctl = focusedControl(this.popupEl);
      if (t) { this.fillPopup(t); focusControl(this.popupEl, ctl); }
    }
    this.draw();
  };
  CanvasView.prototype.setDark = function () { this.draw(); };
  CanvasView.prototype.showRoute = function (feature) { this.route = feature; this.draw(); };
  CanvasView.prototype.showTrail = function (feature) { this.trail = feature; this.draw(); };
  CanvasView.prototype.setEstimates = function () { this.draw(); };
  CanvasView.prototype.setLayer = function (name, fc, visible) {
    if (fc) { this.layerData[name] = fc; }
    this.layerVisible[name] = visible;
    this.draw();
  };
  CanvasView.prototype.focus = function (lngLat, zoom) {
    this.cx = lngLat[0];
    this.cy = lngLat[1];
    if (zoom && this.k < 600) { this.k = 600; }
    this.draw();
  };
  CanvasView.prototype.hitTrain = function (x, y) {
    var best = null;
    var bd = 16;
    var self = this;
    this.trains.forEach(function (t) {
      var pos = posOf(t);
      if (!pos) { return; }
      var p = self.project(pos.lngLat[0], pos.lngLat[1]);
      var d = Math.hypot(p[0] - x, p[1] - y);
      if (d < bd) { bd = d; best = t.id; }
    });
    return best;
  };
  CanvasView.prototype.fillPopup = function (t) {
    var box = this.popupEl;
    box.textContent = '';
    var self = this;
    var close = el('button', { type: 'button', class: 'close', 'aria-label': 'Zavrieť' }, ['×']);
    close.addEventListener('click', function () { self.closePopup(); });
    box.appendChild(close);
    box.appendChild(popupContent(t));
  };
  CanvasView.prototype.openPopup = function (t, fly, zoom, focus) {
    var pos = posOf(t);
    if (fly && pos) { this.focus(pos.lngLat, zoom || 9); }
    if (!this.popupEl) {
      this.popupEl = el('div', { class: 'fallback-popup', role: 'dialog', 'aria-label': 'Detail vlaku' });
      this.container.appendChild(this.popupEl);
    }
    var ctl = focusedControl(this.popupEl) || (focus ? 'nadpis' : null);
    this.popupId = t.id;
    this.popupEl.hidden = false;
    this.popupEl.setAttribute('aria-label', 'Detail vlaku ' + t.number);
    this.fillPopup(t);
    focusControl(this.popupEl, ctl);
  };
  CanvasView.prototype.closePopup = function (silent) {
    if (this.popupEl) {
      // fokus v skrytom okne by ostal „nikde“ – uvoľniť ho (onPopupClose ho vráti na vlak v zozname)
      if (!silent && focusedControl(this.popupEl)) { document.activeElement.blur(); }
      this.popupEl.hidden = true;
    }
    var id = this.popupId;
    this.popupId = null;
    if (!silent && id !== null) { this.h.onPopupClose(id); }
  };
  CanvasView.prototype.draw = function () {
    var self = this;
    if (this.raf) { return; }
    this.raf = window.requestAnimationFrame(function () { self.raf = null; self.render(); });
  };
  CanvasView.prototype.strokeGeom = function (ctx, g) {
    var self = this;
    var parts = g.type === 'MultiLineString' ? g.coordinates : (g.type === 'LineString' ? [g.coordinates] : []);
    parts.forEach(function (line) {
      line.forEach(function (c, i) {
        var p = self.project(c[0], c[1]);
        if (i === 0) { ctx.moveTo(p[0], p[1]); } else { ctx.lineTo(p[0], p[1]); }
      });
    });
  };
  CanvasView.prototype.render = function () {
    var ctx = this.canvas.getContext('2d');
    var dark = isDark();
    var self = this;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = dark ? '#0b0f14' : '#e9edf0';
    ctx.fillRect(0, 0, this.w, this.hgt);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    var styles = { service: [dark ? '#3a4652' : '#b9c2cb', 0.8], none: [dark ? '#c3ccd5' : '#3c4650', 1.3],
                   ac: ['#c2185b', 1.6], dc: ['#1f6fd1', 1.6], el: ['#00897b', 1.4], broad: ['#7b3fa0', 1.6] };
    if (this.buckets) {
      ['service', 'none', 'el', 'dc', 'ac', 'broad'].forEach(function (key) {
        var list = self.buckets[key];
        if (!list) { return; }
        if (key === 'service' && self.k < 1500) { return; }
        ctx.beginPath();
        list.forEach(function (g) { self.strokeGeom(ctx, g); });
        ctx.strokeStyle = styles[key][0];
        ctx.lineWidth = styles[key][1] * (self.k > 1500 ? 2 : 1);
        ctx.stroke();
      });
    } else {
      ctx.fillStyle = dark ? '#9aa8b5' : '#5b6875';
      ctx.font = '14px system-ui, sans-serif';
      ctx.fillText('Načítavam železničnú sieť…', 16, 28);
    }
    if (this.trail && this.trail.geometry) {
      ctx.beginPath();
      this.strokeGeom(ctx, this.trail.geometry);
      ctx.strokeStyle = 'rgba(106, 27, 154, .75)';
      ctx.lineWidth = 4;
      ctx.stroke();
    }
    if (this.route && this.route.geometry) {
      ctx.beginPath();
      this.strokeGeom(ctx, this.route.geometry);
      ctx.strokeStyle = (this.route.properties || {}).variant === 'obchadzka' ? 'rgba(245, 124, 0, .6)' : 'rgba(0, 180, 216, .6)';
      ctx.lineWidth = 7;
      ctx.stroke();
    }
    [['miesta', '#2e7d32', 5], ['vlecky', '#8d6e63', 4]].forEach(function (cfgL) {
      var fc = self.layerData[cfgL[0]];
      if (!fc || !self.layerVisible[cfgL[0]]) { return; }
      ctx.fillStyle = cfgL[1];
      (fc.features || []).forEach(function (f) {
        var c = f.geometry && f.geometry.coordinates;
        if (!c) { return; }
        var pt = self.project(c[0], c[1]);
        ctx.beginPath();
        ctx.arc(pt[0], pt[1], cfgL[2], 0, Math.PI * 2);
        ctx.fill();
      });
    });
    (this.closures.features || []).forEach(function (f) {
      if (!f.geometry) { return; }
      var p = f.properties || {};
      ctx.beginPath();
      self.strokeGeom(ctx, f.geometry);
      ctx.setLineDash(p.aktivna ? [] : [8, 6]);
      ctx.strokeStyle = p.pomala ? '#f08c00' : '#d7191c';
      ctx.lineWidth = 5;
      ctx.stroke();
      ctx.setLineDash([]);
    });
    ctx.font = '600 11px system-ui, sans-serif';
    this.borders.forEach(function (b) {
      if (b.lat === null || b.lat === undefined) { return; }
      var p = self.project(b.lon, b.lat);
      ctx.save();
      ctx.translate(p[0], p[1]);
      ctx.rotate(Math.PI / 4);
      ctx.fillStyle = '#0b5cad';
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 2;
      ctx.fillRect(-5, -5, 10, 10);
      ctx.strokeRect(-5, -5, 10, 10);
      ctx.restore();
      ctx.fillStyle = dark ? '#e3e9ef' : '#0b3d73';
      ctx.fillText((b.sk_station || b.name) + (self.k > 300 ? ' (PPS: ' + shortPps(b.handover_station) + ')' : ''), p[0] + 9, p[1] + 4);
    });
    ctx.font = '700 12px system-ui, sans-serif';
    this.trains.forEach(function (t) {
      if (!t.estimate || !t.estimate.from) { return; }
      var a = self.project(t.estimate.from[0], t.estimate.from[1]);
      var b = self.project(t.estimate.lon, t.estimate.lat);
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = dark ? '#cfd8dc' : '#37474f';
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.setLineDash([]);
    });
    this.trains.forEach(function (t) {
      var pos = posOf(t);
      if (!pos) { return; }
      ctx.globalAlpha = pos.estimate ? 0.65 : 1;
      var p = self.project(pos.lngLat[0], pos.lngLat[1]);
      var cls = COLOR_CLASS[t.display_color] || 'seda';
      ctx.beginPath();
      ctx.arc(p[0], p[1], 10, 0, Math.PI * 2);
      ctx.fillStyle = DOT_COLOR[cls];
      ctx.fill();
      ctx.lineWidth = 3;
      ctx.strokeStyle = t.stale && t.color ? DOT_COLOR[COLOR_CLASS[t.color]] : '#ffffff';
      if (t.stale || pos.estimate) { ctx.setLineDash([3, 2]); }
      ctx.stroke();
      ctx.setLineDash([]);
      var bearing = t.estimate ? t.estimate.bearing : (t.position ? t.position.bearing : null);
      if (bearing !== null && bearing !== undefined && (t.estimate || t.position.moving !== false)) {
        ctx.save();
        ctx.translate(p[0], p[1]);
        ctx.rotate(bearing * Math.PI / 180);
        ctx.beginPath();
        ctx.moveTo(0, -7); ctx.lineTo(5, 6); ctx.lineTo(0, 3); ctx.lineTo(-5, 6); ctx.closePath();
        ctx.fillStyle = '#ffffff';
        ctx.fill();
        ctx.restore();
      }
      var label = t.number + (pos.anchor ? ' (bez polohy)' : '') + (pos.estimate ? ' · odhad' : '');
      var w = ctx.measureText(label).width + 8;
      ctx.fillStyle = 'rgba(255,255,255,.94)';
      ctx.fillRect(p[0] + 13, p[1] - 9, w, 18);
      ctx.fillStyle = '#111111';
      ctx.fillText(label, p[0] + 17, p[1] + 4);
      if (state.selected === t.id) {
        ctx.strokeStyle = '#0b5cad';
        ctx.lineWidth = 2;
        ctx.strokeRect(p[0] + 13, p[1] - 9, w, 18);
      }
      ctx.globalAlpha = 1;
    });
  };

  // ============================================================ tabuľka, upozornenia, časová os
  function positionCell(t) {
    if (t.position) {
      var est = t.estimate ? el('div', { class: 'odhad' }, ['odhad +' + t.estimate.km.toFixed(1).replace('.', ',') + ' km']) : null;
      return [t.position.nearest_station || 'na trati', el('div', { class: 'muted' }, [fmt(t.position.ts) + ' · ' + t.position.source_text]), est];
    }
    if (t.anchor) { return [el('span', { class: 'muted' }, ['bez polohy'])]; }
    return [el('span', { class: 'muted' }, ['—'])];
  }

  function renderTable(list) {
    var tbody = document.querySelector('#tabulka-vlakov tbody');
    if (!tbody) { return; }
    // klávesnica: tlačidlo vlaku s fokusom ho dostane aj po prekreslení tabuľky (nové údaje prichádzajú stále)
    var active = document.activeElement;
    var focusId = active && tbody.contains(active) && active.classList.contains('vlak-btn') ? active.getAttribute('data-id') : null;
    tbody.textContent = '';
    if (!list.length) {
      tbody.appendChild(el('tr', { class: 'empty' }, [el('td', { colspan: '5' }, [state.trains.length ? 'Žiadny vlak nezodpovedá filtru.' : 'Žiadne aktívne vlaky.'])]));
    }
    list.forEach(function (t) {
      var cls = COLOR_CLASS[t.display_color] || 'seda';
      var dotCls = 'dot dot-' + cls + (t.stale && t.color ? ' ring-' + COLOR_CLASS[t.color] : '');
      var d = t.driver;
      var slack = null;
      if (t.slack_min !== null && t.slack_min !== undefined) {
        slack = el('div', null, [el('span', { class: t.slack_min < 0 ? 'neg' : '' }, [t.slack_min < 0 ? 'meškanie ' + fmtMin(-t.slack_min) : 'rezerva ' + fmtMin(t.slack_min)])]);
      }
      tbody.appendChild(el('tr', { 'data-id': t.id, class: 'vlak-row' + (t.stale ? ' is-stale' : '') + (state.selected === t.id ? ' selected' : '') }, [
        el('td', null, [el('div', { class: 'vlak-cell' }, [
          el('span', { class: dotCls, 'aria-hidden': 'true' }),
          el('div', null, [
            el('button', { type: 'button', class: 'linkbtn vlak-btn', 'data-id': t.id }, [t.number]), ' ',
            el('span', { class: 'sem sem-' + (COLOR_CLASS[t.color] || 'seda') }, [t.color || '—']),
            el('div', { class: 'small muted' }, [t.relation]),
            t.downgraded ? el('div', { class: 'small odhad', title: t.data_note || '' }, ['bez čerstvých údajov agenta – pôvodne ZELENÁ']) : null
          ])
        ])]),
        el('td', { class: 'small' }, positionCell(t)),
        el('td', { class: 'small' }, [fmt(t.eta_p50, true), t.eta_p90 ? el('span', { class: 'muted' }, [' (' + fmt(t.eta_p90) + ')']) : null, slack]),
        el('td', { class: 'small' }, d ? [d.name || '—', el('div', { class: 'crew crew-' + (d.color || 'seda') }, [d.unknown_start ? (d.relieve_note || 'limit 13 h sa nedá overiť – zadajte nástup rušňovodiča') : fmtMin(d.left_min)])] : ['—']),
        el('td', { class: 'small risk' }, [t.risk || '—'])
      ]));
    });
    var again = focusId ? tbody.querySelector('.vlak-btn[data-id="' + focusId + '"]') : null;
    if (again) { again.focus({ preventScroll: true }); }
    var cnt = document.getElementById('vlaky-pocet');
    if (cnt) { cnt.textContent = '(' + list.length + (list.length !== state.trains.length ? ' z ' + state.trains.length : '') + ')'; }
  }

  function renderAlerts(items) {
    var ul = document.getElementById('upozornenia');
    if (!ul) { return; }
    ul.textContent = '';
    if (!items.length) { ul.appendChild(el('li', { class: 'muted' }, ['Žiadne upozornenia.'])); return; }
    var pc = { 'KRITICKÉ': 'kriticke', 'DÔLEŽITÉ': 'dolezite', 'INFO': 'info' };
    items.forEach(function (a) {
      var cls = pc[a.priority] || 'info';
      ul.appendChild(el('li', { class: 'alert alert-' + cls }, [
        el('span', { class: 'prio prio-' + cls }, [a.priority || 'INFO']), ' ',
        a.href ? el('a', { href: a.href }, [a.text]) : a.text, ' ',
        el('span', { class: 'muted small' }, [fmt(a.ts)])
      ]));
    });
  }

  var SVGNS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs, text) {
    var e = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text !== undefined) { e.textContent = text; }
    return e;
  }
  function renderGantt(list) {
    var box = document.getElementById('casova-os');
    if (!box) { return; }
    var now = Date.now();
    var t0 = now;
    var t1 = now + 24 * 3600 * 1000;
    var rows = list.filter(function (t) {
      var a = Date.parse(t.plan && t.plan.depart);
      var b = Date.parse(t.plan && (t.plan.arr_p90 || t.plan.arr_p50));
      return !isNaN(a) && !isNaN(b) && b >= t0 && a <= t1;
    }).slice(0, 30);
    box.textContent = '';
    if (!rows.length) { box.appendChild(el('p', { class: 'muted small' }, ['Žiadne plánované jazdy v najbližších 24 h.'])); return; }
    var W = 640, left = 64, right = 8, top = 20, rowH = 20;
    var H = top + rows.length * rowH + 6;
    var x = function (ms) { return left + (Math.min(Math.max(ms, t0), t1) - t0) / (t1 - t0) * (W - left - right); };
    var s = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'presentation' });
    s.appendChild(svg('text', { x: left - 2, y: 12, class: 'g-text', 'text-anchor': 'end' }, 'teraz'));
    var firstTick = Math.ceil(t0 / 3600000) * 3600000;
    for (var ms = firstTick; ms <= t1; ms += 3600000) {
      var hour = Number(fmt(new Date(ms).toISOString()).slice(0, 2));
      if (hour % 3 !== 0) { continue; }
      s.appendChild(svg('line', { x1: x(ms), x2: x(ms), y1: top - 4, y2: H, class: 'g-axis' }));
      if (x(ms) < W - 24) { s.appendChild(svg('text', { x: x(ms) + 2, y: 12, class: 'g-text' }, fmt(new Date(ms).toISOString()))); }
    }
    rows.forEach(function (t, i) {
      var y = top + i * rowH;
      var cls = COLOR_CLASS[t.display_color] || 'seda';
      var label = svg('text', { x: 2, y: y + 13, class: 'g-label' }, t.number);
      s.appendChild(label);
      (t.route_closures || []).forEach(function (rc) {
        (rc.intervaly || []).forEach(function (iv) {
          var a = Date.parse(iv[0]);
          var b = Date.parse(iv[1]);
          if (isNaN(a) || isNaN(b) || b < t0 || a > t1) { return; }
          var r = svg('rect', { x: x(a), y: y + 1, width: Math.max(2, x(b) - x(a)), height: rowH - 2, class: rc.druh === 'pomala_jazda' ? 'g-slow' : 'g-closure' });
          r.appendChild(svg('title', {}, rc.druh_text + ' ' + rc.od_stanice + ' – ' + rc.do_stanice + ': ' + fmt(iv[0], true) + ' – ' + fmt(iv[1], true)));
          s.appendChild(r);
        });
      });
      var dep = Date.parse(t.plan.depart);
      var a50 = Date.parse(t.plan.arr_p50);
      var a90 = Date.parse(t.plan.arr_p90);
      if (!isNaN(a50)) {
        var bar = svg('rect', { x: x(dep), y: y + 5, width: Math.max(2, x(a50) - x(dep)), height: rowH - 10, rx: 2, class: 'g-bar-' + cls });
        bar.appendChild(svg('title', {}, 'Vlak ' + t.number + ': odchod ' + fmt(t.plan.depart, true) + ', príchod P50 ' + fmt(t.plan.arr_p50, true) + (t.plan.arr_p90 ? ', P90 ' + fmt(t.plan.arr_p90, true) : '')));
        s.appendChild(bar);
      }
      if (!isNaN(a90) && !isNaN(a50) && a90 > a50) {
        s.appendChild(svg('rect', { x: x(a50), y: y + 5, width: Math.max(1, x(a90) - x(a50)), height: rowH - 10, class: 'g-bar-' + cls + ' g-p90' }));
      }
      if (t.conflict) {
        var endMs = !isNaN(a90) ? a90 : a50;
        var c = svg('rect', { x: x(dep) - 2, y: y + 2, width: Math.max(4, x(endMs) - x(dep) + 4), height: rowH - 4, class: 'g-conflict' });
        c.appendChild(svg('title', {}, 'Konflikt: jazda sa prekrýva s výlukou na trase'));
        s.appendChild(c);
      }
    });
    s.appendChild(svg('line', { x1: x(now), x2: x(now), y1: top - 6, y2: H, class: 'g-now' }));
    box.appendChild(s);
  }

  // ============================================================ výber vlaku, sledovanie
  function selectTrain(id, fly) {
    var t = state.byId[id];
    if (!t || !state.view) { return; }
    state.selected = id;
    // jeden pohyb mapy: vlak do stredu (pri úzkej mape hore) a okno celé viditeľné – nie dva súperiace pohyby;
    // vlak vybral používateľ (zoznam, vyhľadávanie, značka na mape – aj klávesnicou): fokus prejde do jeho okna
    state.view.openPopup(t, fly, 9, true);
    var box = document.getElementById('mapa');
    if (box) {  // mobil: zoznam vlakov je pod mapou – po výbere vlaku posunúť stránku k mape (pod lištu stránky)
      var r = box.getBoundingClientRect();
      var bar = document.querySelector('.topbar');
      var top = bar && window.getComputedStyle(bar).position === 'sticky' ? bar.getBoundingClientRect().bottom : 0;
      if (r.top < top || r.bottom > window.innerHeight) { window.scrollBy(0, r.top - top); }
    }
    highlightRows();
    loadRoute(id);
    redrawTrains();
  }
  function highlightRows() {
    document.querySelectorAll('#tabulka-vlakov tbody tr').forEach(function (tr) {
      tr.classList.toggle('selected', Number(tr.getAttribute('data-id')) === state.selected);
    });
  }
  function loadRoute(id) {
    state.routeFor = id;
    api('/api/vlaky/' + id + '/trasa').then(function (feature) {
      if (state.routeFor !== id || !state.view) { return; }
      var props = feature.properties || {};
      state.routeVariant = { id: id, variant: props.variant, name: props.name };
      state.view.showRoute(feature);
      if (props.variant === 'obchadzka' && state.view.popupId === id && state.byId[id]) { state.view.openPopup(state.byId[id]); }
    }).catch(function () { if (state.view) { state.view.showRoute(null); } });
  }

  /** Stopa vlaku za 24 h (pri sledovaní vlaku sa obnovuje s novými polohami). */
  function loadTrail(id) {
    state.trailFor = id;
    state.trailLoaded = Date.now();
    api('/api/vlaky/' + id + '/stopa?hodiny=24').then(function (feature) {
      if (state.trailFor !== id || !state.view) { return; }
      state.view.showTrail(feature);
      if (!feature.geometry) { setStatus('Vlak ' + ((state.byId[id] || {}).number || '') + ' nemá za posledných 24 h dosť polôh na stopu.'); setTimeout(function () { setStatus(''); }, 5000); }
    }).catch(function () { if (state.view) { state.view.showTrail(null); } });
  }
  function toggleTrail(id) {
    if (state.trailFor === id) {
      state.trailFor = null;
      if (state.view) { state.view.showTrail(null); }
    } else {
      loadTrail(id);
    }
    var t = state.byId[id];
    if (t && state.view && state.view.popupId === id) { state.view.openPopup(t); }
  }
  function startFollow(id) {
    state.following = id;
    var t = state.byId[id];
    var banner = document.getElementById('follow-banner');
    if (banner) { banner.hidden = false; document.getElementById('follow-num').textContent = t ? t.number : ''; }
    loadTrail(id);
    if (t) { state.view.openPopup(t, true, 11); }
  }
  function stopFollow() {
    var wasFollowing = state.following;
    state.following = null;
    var banner = document.getElementById('follow-banner');
    if (banner) { banner.hidden = true; }
    if (wasFollowing !== null && state.trailFor === wasFollowing) {
      state.trailFor = null;
      if (state.view) { state.view.showTrail(null); }
    }
    var t = state.selected !== null ? state.byId[state.selected] : null;
    if (t && state.view && (state.view.popupId === t.id)) { state.view.openPopup(t); }
  }
  var stopBtn = document.getElementById('follow-stop');
  if (stopBtn) { stopBtn.addEventListener('click', stopFollow); }

  function redrawTrains() {
    var list = visibleTrains();
    if (state.view) { state.view.setTrains(list); }
    return list;
  }

  // ============================================================ načítanie dát
  function applyTrains(data) {
    state.trains = data.vlaky || [];
    state.byId = {};
    state.trains.forEach(function (t) { state.byId[t.id] = t; });
    refreshFilterOptions();
    var list = redrawTrains();
    renderTable(list);
    renderGantt(list);
    var upd = document.getElementById('vlaky-cas');
    if (upd) { upd.textContent = 'aktualizované ' + fmt(data.ts); }
    if (state.following && state.byId[state.following]) {
      var p = posOf(state.byId[state.following]);
      if (p) { state.view.focus(p.lngLat); }
      if (state.trailFor === state.following && Date.now() - state.trailLoaded > 30000) { loadTrail(state.following); }
    }
    if (state.firstLoad) {
      state.firstLoad = false;
      if (state.focusParam && state.byId[state.focusParam]) { selectTrain(state.focusParam, true); }
    }
  }
  function loadTrains() { return api('/api/vlaky').then(applyTrains).catch(function (e) { console.warn('vlaky', e); }); }
  function loadAlerts() { return api('/api/upozornenia').then(function (d) { renderAlerts(d.upozornenia || []); }).catch(function (e) { console.warn('upozornenia', e); }); }
  function loadClosures() {
    var od = new Date();
    var doTime = new Date(od.getTime() + HOURS[state.hours] * 3600 * 1000);
    return api('/api/vyluky?od=' + encodeURIComponent(od.toISOString()) + '&do=' + encodeURIComponent(doTime.toISOString()))
      .then(function (fc) { state.closures = fc; if (state.view) { state.view.setClosures(fc); } })
      .catch(function (e) { console.warn('vyluky', e); });
  }
  function loadStations() {
    return api('/api/siet/stanice?vsetky=1').then(function (list) {
      state.stations = list || [];
      if (state.view) { state.view.setStations(state.stations); }
    }).catch(function () { /* bez názvov staníc */ });
  }

  function scheduleRefresh(what) {
    state.pending[what] = true;
    if (state.timer) { return; }
    state.timer = setTimeout(function () {
      var p = state.pending;
      state.pending = {};
      state.timer = null;
      if (p.all || p.trains) { loadTrains(); }
      if (p.all || p.alerts) { loadAlerts(); }
      if (p.all || p.closures) { loadClosures(); }
    }, 900);
  }

  // ============================================================ živé aktualizácie (SSE)
  function setLive(on) {
    var live = document.getElementById('live');
    if (!live) { return; }
    live.classList.toggle('on', on);
    live.querySelector('.live-text').textContent = on ? 'živé údaje' : 'bez spojenia';
  }
  function startStream() {
    if (!('EventSource' in window)) { setLive(false); return; }
    var es = new EventSource('/api/stream');
    es.onopen = function () { setLive(true); };
    es.onerror = function () { setLive(false); };
    es.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'hello') {
        if (state.ids && JSON.stringify(state.ids) !== JSON.stringify(msg.ids)) { scheduleRefresh('all'); }
        state.ids = msg.ids;
        return;
      }
      if (state.ids) { state.ids[msg.type] = msg.id; }
      if (msg.type === 'vyluka') { scheduleRefresh('closures'); }
      if (msg.type === 'notifikacia' || msg.type === 'udalost' || msg.type === 'odporucanie') { scheduleRefresh('alerts'); }
      scheduleRefresh('trains');
    };
  }

  // ============================================================ ovládanie
  function bindControls() {
    [fQ, fStav, fFarba, fHranica, fZakaznik, fRelacia].forEach(function (f) {
      if (!f) { return; }
      f.addEventListener(f.tagName === 'INPUT' ? 'input' : 'change', function () {
        var list = redrawTrains();
        renderTable(list);
        renderGantt(list);
      });
    });
    if (fQ) {
      fQ.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter') { return; }
        ev.preventDefault();
        var list = visibleTrains();
        var q = fQ.value.trim();
        var exact = list.filter(function (t) { return String(t.number) === q; });
        var pick = exact[0] || (list.length === 1 ? list[0] : null);
        if (pick) { selectTrain(pick.id, true); }
      });
    }
    [['miesta', 'v-miesta', '/api/siet/miesta'], ['vlecky', 'v-vlecky', '/api/siet/vlecky']].forEach(function (l) {
      var box = document.getElementById(l[1]);
      if (!box) { return; }
      box.addEventListener('change', function () {
        if (!box.checked) { state.view.setLayer(l[0], null, false); return; }
        if (state.layers[l[0]]) { state.view.setLayer(l[0], state.layers[l[0]], true); return; }
        api(l[2]).then(function (fc) {
          state.layers[l[0]] = fc;
          state.view.setLayer(l[0], fc, box.checked);
        }).catch(function () { box.checked = false; setStatus('Vrstvu sa nepodarilo načítať.'); });
      });
    });
    var slider = document.getElementById('f-cas');
    var out = document.getElementById('f-cas-out');
    if (slider) {
      slider.addEventListener('input', function () {
        state.hours = Number(slider.value) || 0;
        if (out) { out.textContent = HOURS_LABEL[state.hours]; }
        slider.setAttribute('aria-valuetext', HOURS_LABEL[state.hours]);
        loadClosures();
      });
    }
    var tbody = document.querySelector('#tabulka-vlakov tbody');
    if (tbody) {
      tbody.addEventListener('click', function (ev) {
        var tr = ev.target.closest('tr[data-id]');
        if (tr) { selectTrain(Number(tr.getAttribute('data-id')), true); }
      });
    }
    var night = document.getElementById('btn-noc');
    if (night) {
      night.setAttribute('aria-pressed', isDark() ? 'true' : 'false');
      night.addEventListener('click', function () {
        if (window.vlakySetTheme) { window.vlakySetTheme(isDark() ? 'light' : 'dark'); }
      });
    }
    document.addEventListener('vlaky-tema', function () {
      var dark = isDark();
      if (night) { night.setAttribute('aria-pressed', dark ? 'true' : 'false'); }
      if (state.view) { state.view.setDark(dark); }
    });
    var wallBtn = document.getElementById('btn-nastenka');
    if (wallBtn) {
      wallBtn.addEventListener('click', function () {
        if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
          document.body.classList.add('wall');
          document.documentElement.requestFullscreen().catch(function () { /* prehliadač nepovolil */ });
        } else if (document.fullscreenElement && document.exitFullscreen) {
          document.exitFullscreen();
        } else {
          document.body.classList.toggle('wall');
        }
        setTimeout(resizeView, 300);
      });
      document.addEventListener('fullscreenchange', function () {
        if (!document.fullscreenElement) { document.body.classList.remove('wall'); }
        setTimeout(resizeView, 300);
      });
    }
    document.addEventListener('keydown', function (ev) {
      var tag = (ev.target && ev.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || ev.ctrlKey || ev.metaKey || ev.altKey) {
        if (ev.key === 'Escape' && ev.target === fQ) { fQ.blur(); }
        return;
      }
      if (ev.key === '/') { ev.preventDefault(); if (fQ) { fQ.focus(); } }
      else if (ev.key === 'n' || ev.key === 'N') { if (night) { night.click(); } }
      else if (ev.key === 'f' || ev.key === 'F') { if (wallBtn) { wallBtn.click(); } }
      else if (ev.key === 'Escape') { stopFollow(); if (state.view) { state.view.closePopup(); } }
    });
    var clock = document.getElementById('wall-clock');
    if (clock) {
      var tick = function () { clock.textContent = fmt(new Date().toISOString()); };
      tick();
      setInterval(tick, 5000);
    }
  }
  function resizeView() {
    if (state.view && state.view.map) { state.view.map.resize(); }
    else if (state.view && state.view.resize) { state.view.resize(); state.view.draw(); }
  }

  // ============================================================ štart
  function init() {
    var container = document.getElementById('mapa');
    if (!container) { return; }
    var handlers = {
      onTrainClick: function (id) { selectTrain(id, false); },
      onPopupClose: function (id) {
        if (state.selected === id && !state.following) {
          state.selected = null;
          state.routeFor = null;
          state.view.showRoute(null);
          highlightRows();
          redrawTrains();
        }
        // fokus bol v zatvorenom okne (Escape, tlačidlo ×): vrátiť ho na vlak v zozname (alebo na jeho značku)
        var a = document.activeElement;
        if (!a || a === document.body) {
          var back = document.querySelector('#tabulka-vlakov tbody tr[data-id="' + id + '"] .vlak-btn') ||
            document.querySelector('.vm[data-id="' + id + '"]');
          if (back) { back.focus({ preventScroll: true }); }
        }
      }
    };
    var params = new URLSearchParams(window.location.search);
    if (params.get('vlak')) { state.focusParam = Number(params.get('vlak')); }
    if (window.maplibregl && typeof window.maplibregl.Map === 'function') {
      try {
        state.view = new MapLibreView(container, handlers);
      } catch (e) {
        console.warn('MapLibre sa nepodarilo spustiť', e);
        state.view = null;
      }
    }
    if (!state.view) {
      container.textContent = '';
      state.view = new CanvasView(container, handlers);
      setStatus('Mapa nie je dostupná (offline) – zobrazuje sa zjednodušená schéma siete.');
      api('/api/siet/trate').then(function (gj) { state.view.setLines(gj); }).catch(function () {
        setStatus('Mapa nie je dostupná (offline) a sieť sa nepodarilo načítať.');
      });
    }
    state.view.setBorders(state.borders);
    bindControls();
    loadStations();
    loadClosures();
    loadTrains();
    loadAlerts();
    startStream();
    setInterval(function () { scheduleRefresh('trains'); scheduleRefresh('alerts'); }, 60000);
    setInterval(function () { scheduleRefresh('closures'); }, 300000);
  }

  init();
}());
