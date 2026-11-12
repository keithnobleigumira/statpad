/* Statpad service worker: lets the site install as an app and open offline.
   Always tries the network first, so updates show straight away; the last good copy is kept for offline use.
   Club crests and flags never change, so they come from the phone's copy once downloaded (saves data). */
const CACHE = 'statpad-v1', FONTS = 'statpad-fonts-v1', IMGS = 'statpad-crests-v1', IMG_MAX = 600;
const IMG_HOSTS = ['media.api-sports.io', 'flagcdn.com'];
const SHELL = ['./', './manifest.webmanifest', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (e) => {
  // the page and the latest data are saved straight away, so the app opens offline after the first visit
  e.waitUntil(caches.open(CACHE).then((c) => Promise.all([...SHELL, './matches.json', './track.json'].map((u) => c.add(new Request(u, { cache: 'no-store' })).catch(() => {})))));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== CACHE && k !== FONTS && k !== IMGS) await caches.delete(k);
    await self.clients.claim();
  })());
});

let lastSave = {};
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if ((url.origin === self.location.origin && url.pathname.includes('/crests/')) || IMG_HOSTS.includes(url.hostname)) {
    e.respondWith(imageFirst(req));
    return;
  }

  if (url.origin === self.location.origin) {
    // data files are fetched with a cache-busting query; keep one copy per file
    const data = url.pathname.endsWith('.json');
    const key = data || req.mode === 'navigate' ? url.origin + url.pathname : req;
    e.respondWith(fetch(req).then((res) => {
      if (res.ok && res.type === 'basic') {
        const now = Date.now(), k = typeof key === 'string' ? key : key.url;
        if (!data || !lastSave[k] || now - lastSave[k] > 5 * 60e3) {   // the match file is saved at most every 5 minutes
          lastSave[k] = now;
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(key, copy)).catch(() => {});
        }
      }
      return res;
    }).catch(async () => {
      const hit = await caches.match(key, { ignoreSearch: true });
      if (hit) return hit;
      if (req.mode === 'navigate') { const home = await caches.match(new URL('./', self.location).href); if (home) return home; }
      return Response.error();
    }));
    return;
  }

  // fonts: cache first, they never change
  if (url.hostname === 'fonts.gstatic.com' || url.hostname === 'fonts.googleapis.com') {
    e.respondWith(caches.open(FONTS).then(async (c) => {
      const hit = await c.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok || res.type === 'opaque') c.put(req, res.clone()).catch(() => {});
      return res;
    }));
  }
});

// crests & flags: cache first; only readable (same-site or CORS) answers are kept, never opaque ones
async function imageFirst(req) {
  try {
    const c = await caches.open(IMGS);
    const hit = await c.match(req, { ignoreSearch: true, ignoreVary: true });
    if (hit) return hit;
    const res = await fetch(req);
    if (res.ok && (res.type === 'basic' || res.type === 'cors')) c.put(req, res.clone()).then(trimImages).catch(() => {});
    return res;
  } catch (err) {
    return Response.error();
  }
}
let trimT = 0;
function trimImages() {
  clearTimeout(trimT);
  trimT = setTimeout(async () => {
    const c = await caches.open(IMGS), keys = await c.keys();
    for (const k of keys.slice(0, Math.max(0, keys.length - IMG_MAX))) await c.delete(k);
  }, 8000);
}
