/**
 * SolarWatch — sw.js (Service Worker v4)
 *
 * All assets self-hosted — no CDN dependencies.
 * v4 changes from v3:
 *   - Added /my-sites/* to network-first navigation routes
 *   - Added admin.css to precache list
 *   - Added /static/css/admin.css Cache-First strategy
 *   - Improved offline page styling consistency with dark theme
 *   - Added background sync hint for when connectivity restores
 *   - Fixed share page scope — /share/* handled by stale-while-revalidate
 *
 * Caching strategy:
 *
 *  ┌──────────────────────────────┬──────────────────────────────────────────┐
 *  │ Request type                 │ Strategy                                 │
 *  ├──────────────────────────────┼──────────────────────────────────────────┤
 *  │ /api/*  /health              │ Network Only — live data, never cache    │
 *  │ /manifest.json  /sw.js       │ Network Only — must always be fresh      │
 *  │ /share/*/manifest.json       │ Network Only — per-site manifests        │
 *  │ Navigate: dashboard/auth/    │ Network First → cached → offline page    │
 *  │           my-sites/sites/    │                                          │
 *  │ /static/js/vendor/*          │ Cache First (versioned libs)             │
 *  │ /static/fonts/*              │ Cache First (immutable)                  │
 *  │ /static/css/*                │ Cache First (versioned with cache bump)  │
 *  │ /static/icons/*              │ Cache First → Network fallback           │
 *  │ /share/* (page + data)       │ Network First → cached → offline JSON    │
 *  │ Everything else              │ Stale-While-Revalidate                   │
 *  └──────────────────────────────┴──────────────────────────────────────────┘
 */

const CACHE_NAME = 'solarwatch-v4';

const NETWORK_ONLY_PREFIXES = ['/api/', '/health'];
const NETWORK_ONLY_EXACT    = ['/manifest.json', '/sw.js', '/favicon.ico'];

// Navigation paths served network-first with offline fallback
const NAV_PREFIXES = [
  '/dashboard', '/auth/', '/my-sites', '/sites',
  '/share/',
];

const PRECACHE_URLS = [
  '/dashboard',
  '/auth/login',
  '/static/css/admin.css',
  '/static/css/fonts.css',
  '/static/js/vendor/chart.umd.min.js',
  '/static/js/vendor/chartjs-adapter-date-fns.bundle.min.js',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/favicon-32x32.png',
  '/static/icons/favicon-16x16.png',
  '/static/icons/favicon.ico',
];

// ── OFFLINE PAGE ──────────────────────────────────────────────────────────────
const OFFLINE_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0a0c10">
<title>SolarWatch — Offline</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0a0c10;color:#e8eaf2;
  font-family:'Barlow',system-ui,sans-serif;
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;height:100vh;gap:20px;padding:24px;text-align:center;
}
.sun{font-size:56px;animation:glow 2s ease-in-out infinite alternate}
@keyframes glow{
  from{filter:drop-shadow(0 0 8px rgba(245,166,35,.4))}
  to{filter:drop-shadow(0 0 22px rgba(245,166,35,.9))}
}
h1{font-size:clamp(22px,5vw,34px);font-weight:800;letter-spacing:-.02em;color:#f5a623}
p{color:#8090b8;font-size:clamp(13px,2.5vw,17px);max-width:360px;line-height:1.55}
.hint{font-size:13px;color:#4a5070;margin-top:4px}
button{
  margin-top:4px;padding:11px 26px;border-radius:8px;border:none;cursor:pointer;
  background:#f5a623;color:#000;font-weight:700;font-size:15px;
  font-family:inherit;letter-spacing:.04em;transition:background .2s;
}
button:hover{background:#e09510}
</style>
</head>
<body>
  <div class="sun">☀️</div>
  <h1>SolarWatch</h1>
  <p>You're offline — the server isn't reachable right now.</p>
  <p class="hint">Live data will resume automatically when your connection is restored.</p>
  <button onclick="location.reload()">Try Again</button>
</body>
</html>`;

// ── INSTALL ───────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache =>
        Promise.allSettled(
          PRECACHE_URLS.map(url =>
            cache.add(url).catch(e =>
              console.warn('[SW] Pre-cache skipped:', url, e.message)
            )
          )
        )
      )
      .then(() => self.skipWaiting())
  );
});

// ── ACTIVATE ──────────────────────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => {
          console.log('[SW] Purging old cache:', k);
          return caches.delete(k);
        })
      ))
      .then(() => self.clients.claim())
  );
});

// ── FETCH ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const req  = event.request;
  const url  = new URL(req.url);

  if (req.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;

  const path = url.pathname;

  // ── 1. API + health — Network Only ────────────────────────────────────────
  if (NETWORK_ONLY_PREFIXES.some(p => path.startsWith(p))) {
    event.respondWith(
      fetch(req).then(res => { notifyOnline(); return res; })
        .catch(() => {
          notifyOffline();
          return new Response(
            JSON.stringify({ error: 'offline', stale: true }),
            { status: 503, headers: { 'Content-Type': 'application/json' } }
          );
        })
    );
    return;
  }

  // ── 2. Manifests + SW — Network Only ──────────────────────────────────────
  if (NETWORK_ONLY_EXACT.includes(path) || path.endsWith('/manifest.json')) {
    event.respondWith(fetch(req).catch(() => new Response('', { status: 503 })));
    return;
  }

  // ── 3. Vendor JS + fonts + CSS — Cache First (immutable) ──────────────────
  if (
    path.startsWith('/static/js/vendor/') ||
    path.startsWith('/static/fonts/') ||
    path.startsWith('/static/css/')
  ) {
    event.respondWith(
      caches.match(req).then(cached => {
        if (cached) return cached;
        return fetch(req).then(res => {
          if (res.ok) caches.open(CACHE_NAME).then(c => c.put(req, res.clone()));
          return res;
        });
      })
    );
    return;
  }

  // ── 4. Icons — Cache First ─────────────────────────────────────────────────
  if (path.startsWith('/static/icons/')) {
    event.respondWith(
      caches.match(req).then(cached => cached || fetch(req).then(res => {
        if (res.ok) caches.open(CACHE_NAME).then(c => c.put(req, res.clone()));
        return res;
      }))
    );
    return;
  }

  // ── 5. Navigation pages — Network First with offline fallback ──────────────
  if (req.mode === 'navigate' || NAV_PREFIXES.some(p => path.startsWith(p))) {
    event.respondWith(
      fetch(req)
        .then(res => {
          if (res.ok) caches.open(CACHE_NAME).then(c => c.put(req, res.clone()));
          notifyOnline();
          return res;
        })
        .catch(async () => {
          notifyOffline();
          const cached = await caches.match(req);
          if (cached) return cached;
          return new Response(OFFLINE_HTML, {
            status: 200,
            headers: { 'Content-Type': 'text/html; charset=utf-8' }
          });
        })
    );
    return;
  }

  // ── 6. Everything else — Stale-While-Revalidate ───────────────────────────
  event.respondWith(
    caches.match(req).then(cached => {
      const networkFetch = fetch(req).then(res => {
        if (res.ok) caches.open(CACHE_NAME).then(c => c.put(req, res.clone()));
        notifyOnline();
        return res;
      }).catch(() => {
        notifyOffline();
        return new Response('', { status: 503 });
      });
      return cached || networkFetch;
    })
  );
});

// ── ONLINE / OFFLINE BROADCAST ────────────────────────────────────────────────
let _offlineState = false;

function notifyOffline() {
  if (_offlineState) return;
  _offlineState = true;
  broadcast({ type: 'SW_OFFLINE' });
}

function notifyOnline() {
  if (!_offlineState) return;
  _offlineState = false;
  broadcast({ type: 'SW_ONLINE' });
}

function broadcast(msg) {
  self.clients.matchAll({ includeUncontrolled: true, type: 'window' })
    .then(clients => clients.forEach(c => c.postMessage(msg)));
}
