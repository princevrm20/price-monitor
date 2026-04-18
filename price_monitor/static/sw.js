const CACHE = 'pricemon-v25';
const SHELL = ['/static/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  if (e.request.url.includes('/api/')) return;
  const url = new URL(e.request.url);
  if (url.pathname === '/' || url.pathname === '/login' || url.pathname === '/register') return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
