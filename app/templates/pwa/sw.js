/* Zentriq Service Worker {{ version }}
   Speichert nur statische Dateien (/static/) und die Offline-Seite - keine Seiten und keine
   Kunden-, Memo-, Leipziger- oder Zeiterfassungsdaten. */
const CACHE = "zentriq-static-{{ version }}";
const OFFLINE_URL = "/offline";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.add(new Request(OFFLINE_URL, { cache: "reload" }))).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key.startsWith("zentriq-") && key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Seitenaufrufe immer vom Server; nur ohne Verbindung die Offline-Seite zeigen.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
    return;
  }

  // Statische Dateien (versionierte URLs): zuerst aus dem Cache.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone();
              caches.open(CACHE).then((cache) => cache.put(request, copy));
            }
            return response;
          })
      )
    );
  }
  // Alles andere (Daten, Formulare, HTMX/JSON) geht unveraendert an den Server.
});
