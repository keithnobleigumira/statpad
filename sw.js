/* Statpad service worker: lets the site install as an app, open instantly and work offline.
   v2 (v61): the page opens from the copy on the phone at once, and the network is asked in the background whether a
   newer version is up (a conditional request: a few hundred bytes when nothing changed). A newer page is saved for
   next time and the open page is told, so it can offer "Update". Only a copy older than a week, or none at all, waits
   for the network (up to 4 seconds, then the old copy, as before).
   Club crests (single and on sheets), flags and the globe's earth pictures never change, so they come from the
   phone's copy once downloaded (saves data). Pictures are matched on their full address: ESPN's resized crests all
   share one path and differ only in the query (?img=…/<club>.png), so matching without it handed every club the first
   ESPN crest the phone had kept. v2 of the picture store drops the copies saved that way.
   Data files: the network first; if it hasn't answered within a few seconds, the saved copy is shown and the download
   carries on to refresh it. v.json (which version of each data file is up) always goes straight to the network. */
const CACHE = 'statpad-v1', FONTS = 'statpad-fonts-v1', IMGS = 'statpad-crests-v2', IMG_MAX = 900;
const IMG_HOSTS = ['media.api-sports.io', 'flagcdn.com', 'a.espncdn.com'];
const SHELL = ['./', './manifest.webmanifest', './icon-192.png', './icon-512.png'];
const NET_WAIT_PAGE = 4000, NET_WAIT_DATA = 7000;   // ms to wait for the network before the saved copy is used
const PAGE_FRESH = 7 * 86400e3;   // a saved page younger than this opens at once

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
    if (url.pathname.endsWith('/v.json')) return;   // tiny, and only useful fresh: straight to the network
    if (req.mode === 'navigate') { e.respondWith(page(e, req, url)); return; }
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

// the page: the saved copy at once, and a check in the background for a newer one
async function digest(buf) {
  try { const h = await crypto.subtle.digest('SHA-1', buf); return Array.from(new Uint8Array(h).slice(0, 8)).map((b) => b.toString(16).padStart(2, '0')).join(''); } catch (err) { return String(buf.byteLength); }
}
async function page(e, req, url) {
  const key = url.origin + url.pathname, root = new URL('./', self.location).href, mark = root + '__checked';
  const c = await caches.open(CACHE);
  const hit = (await c.match(key, { ignoreSearch: true })) || (await c.match(root, { ignoreSearch: true }));
  const checked = await c.match(mark).then((r) => (r ? r.text() : '0'), () => '0');
  const net = fetch(req.url, { cache: 'no-cache', credentials: 'same-origin' }).then(async (res) => {
    if (!res.ok || res.type !== 'basic') return res;
    const buf = await res.clone().arrayBuffer(), id = await digest(buf);
    const was = hit ? hit.headers.get('x-statpad-build') : null;
    c.put(mark, new Response(String(Date.now()))).catch(() => {});   // the saved page is known to be current as of now
    if (id !== was) {
      const h = new Headers(res.headers); h.set('x-statpad-build', id); h.set('x-statpad-saved', String(Date.now()));
      const copy = new Response(buf, { status: 200, statusText: 'OK', headers: h });
      await Promise.all([c.put(key, copy.clone()), key === root ? null : c.put(root, copy)]).catch(() => {});
      if (hit && was) {   // a newer page than the one open: tell it (it can offer to switch now; otherwise next time)
        for (const cl of await self.clients.matchAll({ type: 'window' })) cl.postMessage({ type: 'statpad-update', build: id });
      }
    }
    return res;
  });
  e.waitUntil(net.catch(() => {}));
  const age = hit ? Date.now() - Math.max(+checked || 0, +hit.headers.get('x-statpad-saved') || 0, Date.parse(hit.headers.get('date') || 0) || 0) : Infinity;
  if (hit && age < PAGE_FRESH) return hit;
  // nothing saved, or a copy older than a week: the network, with the old copy after a few seconds
  const first = await Promise.race([net.then((r) => (r.ok ? r : null), () => null), new Promise((r) => setTimeout(() => r(undefined), NET_WAIT_PAGE))]);
  if (first) return first;
  if (hit) return hit;
  return net.catch(() => Response.error());
}

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
