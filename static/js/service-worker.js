// Service worker for the template application.
// This uses a network-first strategy for HTML navigation and cache-first for static assets.

const CACHE_VERSION = 'template-cache-v1';
const STATIC_ASSETS = [
  '/',
  '/offline',
  '/manifest.json',
  '/favicon.ico',
  '/static/css/bootstrap.min.css',
  '/static/css/theme.css',
  '/static/css/material-symbols.css',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/android-chrome-192x192.png',
  '/static/icons/android-chrome-512x512.png',
  '/static/branding/wts-logo-horizontal.png'
];

self.addEventListener('install', event => {
  // Pre-cache the shell so the app loads quickly when offline.
  event.waitUntil(
    caches.open(CACHE_VERSION).then(cache => cache.addAll(STATIC_ASSETS))
  );
});

self.addEventListener('activate', event => {
  // Clean up old caches to avoid serving stale assets.
  event.waitUntil(
    caches.keys().then(cacheNames =>
      Promise.all(
        cacheNames.map(cacheName => {
          if (cacheName !== CACHE_VERSION) {
            return caches.delete(cacheName);
          }
          return null;
        })
      )
    )
  );
});

self.addEventListener('fetch', event => {
  const { request } = event;

  // Only handle GET requests from our own origin.
  if (request.method !== 'GET' || new URL(request.url).origin !== self.location.origin) {
    return;
  }

  // Avoid caching authenticated or API responses.
  if (request.url.includes('/auth') || request.url.includes('/api')) {
    return;
  }

  // For navigation requests, try the network first and fall back to offline page.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then(response => {
          const responseClone = response.clone();
          caches.open(CACHE_VERSION).then(cache => cache.put(request, responseClone));
          return response;
        })
        .catch(() => caches.match('/offline'))
    );
    return;
  }

  // For static assets, use cache-first with a network fallback.
  event.respondWith(
    caches.match(request).then(cachedResponse => {
      if (cachedResponse) {
        return cachedResponse;
      }

      return fetch(request).then(response => {
        const responseClone = response.clone();
        caches.open(CACHE_VERSION).then(cache => cache.put(request, responseClone));
        return response;
      });
    })
  );
});
