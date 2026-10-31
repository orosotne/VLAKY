/* Téma (svetlá / tmavá / automatická) – načítané v <head>, aby stránka neblikla. */
(function () {
  'use strict';
  var t = '';
  try { t = localStorage.getItem('vlaky-tema') || ''; } catch (e) { t = ''; }
  if (t === 'dark' || t === 'light') { document.documentElement.setAttribute('data-theme', t); }
}());
