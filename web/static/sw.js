/* Caches only the app shell. API calls and media always go to the network. */
const CACHE = "ledposter-shell-v4";  // bump when app.js/app.css change, or installed apps keep the old copy
const SHELL = ["/static/app.css", "/static/app.js", "/static/render.js", "/static/icon.svg",
               "/static/icon-192.png", "/static/icon-512.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
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
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request).then((r) => {
      if (r.ok) caches.open(CACHE).then((c) => c.put(e.request, r.clone()));
      return r;
    })));
  }
});
