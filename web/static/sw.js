/* Caches only the app shell, as an offline fallback. API calls and media always go to the network.
   Network first, revalidating the browser's HTTP cache (cache: "no-cache", a cheap 304 when nothing
   changed): a deploy shows on the next normal reload, no hard refresh and no version bump needed.
   (Cache-first served the old app.js after a deploy: the new worker filled its cache from the
   browser's heuristically fresh copy.) */
const CACHE = "ledposter-shell-v6";
const SHELL = ["/static/app.css", "/static/app.js", "/static/render.js", "/static/icon.svg",
               "/static/icon-192.png", "/static/icon-512.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" }))))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  if (e.request.mode === "navigate") {
    // Shell pages: network first; offline, fall back to the last good copy of "/".
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok && !r.redirected && url.pathname === "/") caches.open(CACHE).then((c) => c.put("/", r.clone()));
      return r;
    }).catch(() => caches.match("/")));
    return;
  }
  if (SHELL.includes(url.pathname)) {
    e.respondWith(fetch(e.request, { cache: "no-cache" }).then((r) => {
      if (r.ok) caches.open(CACHE).then((c) => c.put(e.request, r.clone()));
      return r;
    }).catch(() => caches.match(e.request)));
  }
});
