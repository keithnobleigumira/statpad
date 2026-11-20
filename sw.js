/* Statpad service worker: lets the site install as an app and open offline.
   Always tries the network first, so updates show straight away; the last good copy is kept for offline use.
   Club crests (single and on sheets), flags and the globe's earth pictures never change, so they come from the
   phone's copy once downloaded (saves data). Pictures are matched on their full address: ESPN's resized crests all
   share one path and differ only in the query (?img=…/<club>.png), so matching without it handed every club the first
   ESPN crest the phone had kept. v2 of the picture store drops the copies saved that way.
   A slow or stalled connection (Wi-Fi that's on but barely moving) never leaves the page waiting: if the network
   hasn't answered within a few seconds, the saved copy is shown and the download carries on to refresh it. */
const CACHE = 'statpad-v1', FONTS = 'statpad-fonts-v1', IMGS = 'statpad-crests-v2', IMG_MAX = 900;
const IMG_HOSTS = ['media.api-sports.io', 'flagcdn.com', 'a.espncdn.com'];
const SHELL = ['./', './manifest.webmanifest', './icon-192.png', './icon-512.png'];
const NET_WAIT_PAGE = 4000, NET_WAIT_DATA = 7000;   // ms to wait for the network before the saved copy is used

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

  // crests, crest sheets and the shock map's earth pictures never change under the same address
  if ((url.origin === self.location.origin && (url.pathname.includes('/crests/') || url.pathname.includes('/globe/')))
      || IMG_HOSTS.includes(url.hostname) || (url.hostname === 'cdn.jsdelivr.net' && url.pathname.includes('/three-globe@'))) {
    e.respondWith(imageFirst(req));
    return;
  }

  if (url.origin === self.location.origin) {
    // data files are fetched with a cache-busting query; keep one copy per file
    const data = url.pathname.endsWith('.json');
    const key = data || req.mode === 'navigate' ? url.origin + url.pathname : req;
    const net = fetch(req).then((res) => {
      if (res.ok && res.type === 'basic') {
        const now = Date.now(), k = typeof key === 'string' ? key : key.url;
        if (!data || !lastSave[k] || now - lastSave[k] > 5 * 60e3) {   // the match file is saved at most every 5 minutes
          lastSave[k] = now;
          const copy = res.clone();
          return caches.open(CACHE).then((c) => c.put(key, copy)).catch(() => {}).then(() => res);
        }
      }
      return res;
    });
    e.waitUntil(net.catch(() => {}));   // keep downloading (and saving) even after the saved copy has been shown
    const saved = async () => {
      const hit = await caches.match(key, { ignoreSearch: true });
      if (hit) return hit;
      if (req.mode === 'navigate') return caches.match(new URL('./', self.location).href);
      return undefined;
    };
    const wait = req.mode === 'navigate' ? NET_WAIT_PAGE : data ? NET_WAIT_DATA : 0;
    e.respondWith((async () => {
      if (!wait) return net.catch(async () => (await saved()) || Response.error());
      // the network, if it answers in time; otherwise the saved copy; with nothing saved, wait for the network after all
      const first = await Promise.race([net.then((r) => r, () => null), new Promise((r) => setTimeout(() => r(undefined), wait))]);
      if (first) return first;
      const hit = await saved();
      if (hit) return hit;
      return first === null ? Response.error() : net.catch(() => Response.error());
    })());
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

// crests & flags: cache first, by exact address; only readable (same-site or CORS) pictures are kept, never opaque
// answers or anything that isn't an image (an error page saved as a crest would stick)
async function imageFirst(req) {
  try {
    const c = await caches.open(IMGS);
    const hit = await c.match(req.url, { ignoreVary: true });
    if (hit) return hit;
    const res = await fetch(req);
    const type = res.headers.get('content-type') || '';
    if (res.ok && (res.type === 'basic' || res.type === 'cors') && (!type || type.startsWith('image/'))) c.put(req.url, res.clone()).then(trimImages).catch(() => {});
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
