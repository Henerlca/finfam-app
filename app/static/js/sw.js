const CACHE_NAME = "__FINFAM_CACHE_NAME__";
const PUBLIC_ASSETS = __FINFAM_PUBLIC_ASSETS__;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PUBLIC_ASSETS.map((url) => new Request(url, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key.startsWith("finfam-shell-") && key !== CACHE_NAME)
        .map((key) => caches.delete(key))
    ))
      .then(() => self.clients.claim())
  );
});

// Cache apenas arquivos estáticos públicos; nunca guardar páginas, sessões, exports ou dados financeiros.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) return;
  // URLs com ?v=hash precisam de match exato: nunca servir outro fingerprint por pathname.
  // Paths antigos sem query ainda podem ser direcionados ao asset fingerprintado atual.
  const fingerprintedUrl = url.search
    ? null
    : PUBLIC_ASSETS.find((asset) => new URL(asset, self.location.origin).pathname === url.pathname);
  event.respondWith(caches.match(fingerprintedUrl || event.request).then((cached) => cached || fetch(event.request)));
});
