/* Téma (tmavá – predvolená / svetlá / automaticky podľa systému) – načítané v <head>, aby stránka neblikla. */
(function () {
  'use strict';
  var t = '';
  try { t = localStorage.getItem('vlaky-tema') || ''; } catch (e) { t = ''; }
  if (t !== 'light' && t !== 'auto') { t = 'dark'; }
  document.documentElement.setAttribute('data-theme', t);
}());
