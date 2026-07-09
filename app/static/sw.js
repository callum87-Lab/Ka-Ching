// Minimal service worker - just enough to satisfy browser install criteria.
// Deliberately does no caching: Ka-Ching! is a live dashboard, not something
// that should work from stale offline data.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
