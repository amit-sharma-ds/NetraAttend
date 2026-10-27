// NetraAttend service worker - lets the browser install the website as an app.
// Attendance must always be live, so nothing is cached. Live video, API calls and
// photos go straight to the server; only a page that cannot be opened (office PC
// off, or not on the office network) gets a short "not reachable" page.
const OFFLINE = `<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>NetraAttend</title>
<body style="margin:0;height:100vh;display:grid;place-items:center;background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center;padding:16px">
<div><h2 style="margin:0 0 8px">NetraAttend is not reachable</h2>
<p style="color:#94a3b8;margin:0 0 18px">Check that the office PC is on and that this device is on the office network.</p>
<button onclick="location.reload()" style="padding:10px 20px;border:0;border-radius:10px;background:#4f46e5;color:#fff;font-size:15px;cursor:pointer">Try again</button></div>
</body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() =>
    new Response(OFFLINE, {headers: {"Content-Type": "text/html; charset=utf-8"}})));
});
