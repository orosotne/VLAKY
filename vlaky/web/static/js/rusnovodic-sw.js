/* Service worker aplikácie rušňovodiča: len statické súbory (CSS, JS, ikona) pre rýchle načítanie.
   API a stránky s odkazom sa nikdy neukladajú do cache. */
'use strict';
const CACHE = 'vlaky-rusnovodic-v1';
const ASSETS = ['/static/css/app.css', '/static/js/rusnovodic.js', '/static/js/tema.js', '/static/img/ikona.svg'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || !url.pathname.startsWith('/static/')) {
    return; // sieť bez zásahu (API, stránky)
  }
  event.respondWith(
    fetch(event.request).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(url.pathname, copy)).catch(() => {});
      return resp;
    }).catch(() => caches.match(url.pathname))
  );
});
