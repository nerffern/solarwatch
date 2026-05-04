/**
 * SolarWatch — sw.js (Service Worker v3)
 *
 * All assets are now self-hosted — no CDN dependencies.
 * Fonts, chart.js, and the date adapter all serve from /static/.
 *
 * Caching strategy:
 *
 *  ┌─────────────────────────────┬──────────────────────────────────────────┐
 *  │ Request type                │ Strategy                                 │
 *  ├─────────────────────────────┼──────────────────────────────────────────┤
 *  │ /api/*  /health             │ Network Only — live data, never cache    │
 *  │ /manifest.json  /sw.js      │ Network Only — must always be fresh      │
 *  │ /dashboard  /auth/*         │ Network First → offline page fallback    │
 *  │ /static/js/vendor/*         │ Cache First (versioned libs, safe)       │
 *  │ /static/fonts/*             │ Cache First (font files never change)    │
 *  │ /static/icons/*             │ Cache First → Network fallback           │
 *  │ Everything else             │ Stale-While-Revalidate                   │
 *  └─────────────────────────────┴──────────────────────────────────────────┘
 *
 * OFFLINE: API calls return a structured JSON error so the dashboard handles
 * them gracefully (shows stale indicator rather than crashing).
 * The SW broadcasts SW_OFFLINE / SW_ONLINE to the app for the banner.
 */

const CACHE_NAME = 'solarwatch-v3';

// Paths that must NEVER be cached — always fetch live
const NETWORK_ONLY_PREFIXES = ['/api/', '/health'];
const NETWORK_ONLY_EXACT    = ['/manifest.json', '/sw.js'];

// Static assets to pre-cache on SW install — all same-origin, all local
const PRECACHE_URLS = [
  '/dashboard',
  '/static/css/fonts.css',
  '/static/js/vendor/chart.umd.min.js',
  '/static/js/vendor/chartjs-adapter-date-fns.bundle.min.js',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/favicon-32x32.png',
  '/static/icons/favicon-16x16.png',
];

// ── OFFLINE PAGE ──────────────────────────────────────────────────────────────
const OFFLINE_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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
@keyframes glow{from{filter:drop-shadow(0 0 8px rgba(245,166,35,.4))}to{filter:drop-shadow(0 0 22px rgba(245,166,35,.9))}}
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
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Only handle same-origin requests — ignore cross-origin (there are none now)
  if (url.origin !== self.location.origin) return;

  // ── 1. API + health — Network Only ────────────────────────────────────────
  if (NETWORK_ONLY_PREFIXES.some(p => url.pathname.startsWith(p))) {
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

  // ── 2. Manifest + SW — Network Only ───────────────────────────────────────
  if (NETWORK_ONLY_EXACT.includes(url.pathname)) {
    event.respondWith(fetch(req).catch(() => new Response('', { status: 503 })));
    return;
  }

  // ── 3. Vendor JS + fonts — Cache First (content never changes) ────────────
  if (url.pathname.startsWith('/static/js/vendor/') ||
      url.pathname.startsWith('/static/fonts/')) {
    event.respondWith(
      caches.match(req).then(cached => {
        if (cached) return cached;
        return fetch(req).then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then(c => c.put(req, clone));
          }
          return res;
        });
      })
    );
    return;
  }

  // ── 4. Icons — Cache First ─────────────────────────────────────────────────
  if (url.pathname.startsWith('/static/icons/')) {
    event.respondWith(
      caches.match(req).then(cached => cached || fetch(req).then(res => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, clone));
        }
        return res;
      }))
    );
    return;
  }

  // ── 5. Everything else — Stale-While-Revalidate ───────────────────────────
  event.respondWith(
    caches.match(req).then(cached => {
      const networkFetch = fetch(req).then(res => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, clone));
        }
        notifyOnline();
        return res;
      }).catch(() => {
        notifyOffline();
        if (req.mode === 'navigate') {
          return new Response(OFFLINE_HTML, {
            status: 200,
            headers: { 'Content-Type': 'text/html; charset=utf-8' }
          });
        }
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
