/* ===================================================================
 * FileLib Service Worker —— 只做「静态壳」离线兜底
 *
 * 原则：
 *  - 白名单拦截：只缓存 index.html / app.js / app.css / manifest / 图标
 *  - /api、媒体、缩略图、封面、下载等一切动态请求一律直连，绝不缓存
 *  - 缓存名带版本号（注册 URL 的 ?v= 传入），升级后旧缓存整体删除
 *  - 策略：网络优先，失败回退缓存（离线时至少能渲染登录页）
 *  - 注册失败/不支持（http 非本机）时静默跳过，功能零影响
 * =================================================================== */
"use strict";

const VER = (new URL(self.location.href).searchParams.get("v") || "dev").replace(/[^\w.-]/g, "");
const CACHE = "filelib-shell-" + VER;
const SHELL = [
  "/", "/index.html", "/app.js", "/app.css",
  "/manifest.webmanifest", "/favicon.ico",
  "/static/icons/icon-192.png", "/static/icons/icon-512.png",
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => Promise.allSettled(SHELL.map(u => c.add(new Request(u, { cache: "reload" })))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // 跨域不碰

  // 导航请求（地址栏 / 刷新）：等价于拉 index.html
  const isNav = req.mode === "navigate";
  const key = isNav ? "/index.html"
    : (url.pathname === "/" ? "/index.html" : url.pathname);
  if (!SHELL.includes(key)) return;                  // 白名单外全部直连

  e.respondWith(
    fetch(req).then(r => {
      if (r && r.ok) {
        // 顺手刷新缓存；clone 因为响应体要同时给页面和缓存
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(key, copy)).catch(() => {});
      }
      return r;
    }).catch(() =>
      caches.match(key, { ignoreSearch: true })
        .then(hit => hit || Response.error())
    )
  );
});
