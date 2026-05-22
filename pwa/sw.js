// Service worker: cache app-shell + /templates; network-first for /sessions/*.
// Spec §9.3.

const CACHE = "scribe-shell-v1";
const SHELL = [
  "/",
  "/index.html",
  "/styles.css",
  "/manifest.webmanifest",
  "/js/main.js",
  "/js/api.js",
  "/js/sse.js",
  "/js/outbox.js",
  "/js/recorder.js",
  "/js/util.js",
  "/js/screens/list.js",
  "/js/screens/new.js",
  "/js/screens/active.js",
  "/js/screens/open.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // Best-effort: don't fail install if one file is missing.
    await Promise.all(SHELL.map((u) => cache.add(u).catch(() => null)));
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // Don't touch SSE.
  if (req.headers.get("accept") && req.headers.get("accept").includes("text/event-stream")) return;

  // Network-first for /sessions/* and /healthz (always want fresh).
  if (url.pathname.startsWith("/sessions") || url.pathname === "/healthz") {
    event.respondWith((async () => {
      try { return await fetch(req); }
      catch (err) {
        const cached = await caches.match(req);
        if (cached) return cached;
        throw err;
      }
    })());
    return;
  }

  // /templates: stale-while-revalidate (cache first, refresh in background).
  if (url.pathname === "/templates") {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const cached = await cache.match(req);
      const fetchPromise = fetch(req).then((res) => {
        if (res && res.ok) cache.put(req, res.clone());
        return res;
      }).catch(() => null);
      return cached || (await fetchPromise) || new Response("[]", { headers: { "content-type": "application/json" } });
    })());
    return;
  }

  // Cache-first for app shell / static assets.
  event.respondWith((async () => {
    const cached = await caches.match(req);
    if (cached) return cached;
    try {
      const res = await fetch(req);
      if (res && res.ok && url.origin === self.location.origin) {
        const cache = await caches.open(CACHE);
        cache.put(req, res.clone());
      }
      return res;
    } catch (err) {
      const fallback = await caches.match("/index.html");
      if (fallback) return fallback;
      throw err;
    }
  })());
});
