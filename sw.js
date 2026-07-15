// IMPORTANT: bump version on every deployment
const CACHE = "metaweather-v6";
// Chart.js is required for the app to render at all — precache it so the
// installed PWA keeps working offline. Keep the version in sync with the
// <script src> in index.html.
const CHART_JS = "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js";
const STATIC = ["./", "index.html", "manifest.json", "icon.svg", CHART_JS];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Chart.js CDN: cache first (it is versioned, so the content never changes),
  // fill the cache on first fetch in case precaching was skipped.
  if (e.request.url === CHART_JS) {
    e.respondWith(
      caches.match(e.request).then(
        (cached) =>
          cached ||
          fetch(e.request).then((res) => {
            const clone = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, clone));
            return res;
          })
      )
    );
    return;
  }

  // Other cross-origin API requests: let the browser handle them directly so the
  // page's AbortController timeouts and native networking apply (some Android
  // WebViews don't propagate aborts cleanly through service worker fetch).
  if (url.hostname !== location.hostname) return;

  // HTML: network first, fall back to cache (always get latest version)
  if (e.request.mode === "navigate" || url.pathname.endsWith(".html") || url.pathname.endsWith("/")) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Other static assets: cache first
  e.respondWith(
    caches.match(e.request).then((cached) => cached || fetch(e.request))
  );
});
