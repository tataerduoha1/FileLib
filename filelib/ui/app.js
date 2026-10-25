/* ===================================================================
 * FileLib 前端（原生 JS，零依赖）
 *
 * 设计要点
 *  - 每个资料库（library / vol）独立浏览，绝不多库混排
 *  - 所有 POST 带 X-Requested-With: FileLib（服务器 CSRF 校验）
 *  - 主视图统一渲染进 #grid，卡片 = 文件 / 书 / 视频 / 图片 / 系列 / 作者
 *  - 播放器、阅读器、大图、后台任务都是独立浮层，快捷键按层级拦截
 * =================================================================== */
"use strict";

const $ = (s, el = document) => {
  try { return el.querySelector(s); } catch (e) {
    // 容错：id 里含 + 等非法 CSS 字符时（如 #rd-font+）退回 getElementById，
    // 避免一个坏 id 就让整个 bindUI() 崩掉
    if (s[0] === "#" && s.indexOf(" ") < 0) {
      try {
        const byId = document.getElementById(s.slice(1));
        if (byId) return (el === document || el.contains(byId)) ? byId : null;
      } catch (_) {}
    }
    console.error("非法选择器:", s, e);
    return null;
  }
};
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

/* 提前恢复主题 / 侧栏状态（原内联脚本被 CSP 拦截，移到 app.js 里执行） */
try {
  const _t = localStorage.getItem("filelib.theme");
  if (_t) document.body.dataset.theme = _t;
  if (localStorage.getItem("filelib.sb") === "0") document.body.classList.add("sb-hidden");
} catch (e) {}

/* ---------------- 全局状态 ---------------- */

const state = {
  volId: null,
  libs: [],
  tree: null,
  openDirs: new Set(),
  view: "home",       // home|list|topics|authors|series|kinds|continue|random|space
  dir: "",
  q: "",
  kind: null,
  topic: null,
  series: null,
  author: null,
  mark: null,
  tag: null,
  sort: "mtime_desc",
  offset: 0,
  limit: 120,
  itemsBase: 0,     // items[0] 对应的全局偏移（跳页后 > 0），分块回收据此对齐页号
  total: 0,
  items: [],
  loading: false,
  done: false,
  fulltext: false,
  masonry: false,
  hl: null,
};

const ME = {
  logged: false, id: null, username: "", role: "user",
  vols: [], nav: null, must_setup: false, version: "",
};

let LP_FIRED = false;   // 触屏长按刚弹出菜单时，吞掉随之而来的合成 click

const KIND_LABEL = {
  book: "电子书", doc: "文档", video: "视频", image: "图片",
  audio: "音频", archive: "压缩包", code: "代码", subtitle: "字幕",
  app: "程序", other: "其他",
};
const KIND_ICON = {
  book: "📖", doc: "📄", video: "🎬", image: "🖼", audio: "🎵",
  archive: "🗜", code: "💻", subtitle: "💬", app: "⚙️", other: "📦",
};
const MARK_LABEL = { star: "⭐ 收藏", read: "✅ 已读", todo: "📌 待读" };
const MARK_ICON = { star: "⭐", read: "✅", todo: "📌" };
const READER_EXT = ["txt", "md", "markdown", "pdf", "epub", "mobi", "azw3", "azw", "prc", "fb2"];

/* ---------------- 小工具 ---------------- */

function fmtSize(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
}
function fmtDur(s) {
  if (!s || !isFinite(s)) return "0:00";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtDay(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function kindIcon(k) { return KIND_ICON[k] || "📦"; }
function isAdmin() { return ME.role === "admin"; }

function toast(msg, ms = 1800) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden", "out");
  clearTimeout(el._t);
  clearTimeout(el._t2);
  el._t = setTimeout(() => {
    el.classList.add("out");
    el._t2 = setTimeout(() => el.classList.add("hidden"), 150);
  }, ms);
}

/* ---------------- 网络 ---------------- */

async function api(path, opts) {
  const o = Object.assign({}, opts || {});
  o.headers = Object.assign({ "X-Requested-With": "FileLib" }, o.headers || {});
  o.credentials = "same-origin";
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch { /* 非 JSON */ }
  if (!r.ok) {
    const err = new Error((data && data.error) || ("HTTP " + r.status));
    err.status = r.status;
    err.data = data;
    if (r.status === 401 && data && data.auth === false && ME.logged) {
      ME.logged = false;
      showLogin(false);
    }
    throw err;
  }
  return data;
}
function post(obj) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj || {}),
  };
}
function withVol(path, vol) {
  const v = vol === undefined ? state.volId : vol;
  if (v == null) return path;
  return path + (path.includes("?") ? "&" : "?") + "vol=" + v;
}
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------------- 主题 ---------------- */

const THEMES = [
  { id: "light", icon: "☀️", name: "亮色" },
  { id: "dark", icon: "🌙", name: "暗色" },
  { id: "sepia", icon: "🌿", name: "护眼（米黄）" },
  { id: "night", icon: "🕯", name: "夜间（低亮）" },
  { id: "contrast", icon: "◐", name: "高对比" },
];
const THEME_MAP = Object.fromEntries(THEMES.map(t => [t.id, t]));

function applyTheme(t) {
  if (!THEME_MAP[t]) t = "light";
  document.body.dataset.theme = t;
  try { localStorage.setItem("filelib.theme", t); } catch {}
  const btn = $("#btn-theme");
  if (btn) {
    btn.textContent = THEME_MAP[t].icon;
    btn.title = `当前主题：${THEME_MAP[t].name}（点击循环 / 右键挑选 / 快捷键 T）`;
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    const bg = getComputedStyle(document.body).backgroundColor;
    if (bg) meta.setAttribute("content", bg);
  }
  document.body.classList.toggle("theme-dark", ["dark", "night", "contrast"].includes(t));
}

/* ---------------- 侧栏 ---------------- */

const isMobile = () => window.innerWidth <= 860;
function sidebarOpen() {
  return isMobile() ? document.body.classList.contains("sb-open")
                    : !document.body.classList.contains("sb-hidden");
}
function setSidebar(open) {
  if (isMobile()) {
    document.body.classList.toggle("sb-open", open);
  } else {
    document.body.classList.toggle("sb-hidden", !open);
    try { localStorage.setItem("filelib.sb", open ? "1" : "0"); } catch {}
  }
  const b = $("#btn-sidebar");
  if (b) b.title = (open ? "收起" : "展开") + "导航栏 (Ctrl+B)";
}

/* ---------------- URL / history ---------------- */

function serializeState() {
  return {
    vol: state.volId, view: state.view, dir: state.dir || null, q: state.q || null,
    kind: state.kind, topic: state.topic, series: state.series,
    author: state.author, mark: state.mark, tag: state.tag,
    sort: state.sort === "mtime_desc" ? null : state.sort,
    ft: state.fulltext ? 1 : null,
  };
}
function pushHistory(replace) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(serializeState())) {
    if (v != null && v !== "") p.set(k === "vol" ? "lib" : k, v);
  }
  const url = location.pathname + (p.toString() ? "?" + p.toString() : "");
  try {
    if (replace) history.replaceState(serializeState(), "", url);
    else history.pushState(serializeState(), "", url);
  } catch { /* 某些环境不支持 */ }
}
function refresh() {
  pushHistory(false);
  markActive();
  return render();
}
function resetFilters() {
  Object.assign(state, {
    view: "home", dir: "", q: "", kind: null, topic: null, series: null,
    author: null, mark: null, tag: null, fulltext: false, masonry: false,
  });
  const s = $("#search");
  if (s) s.value = "";
  $("#search-clear")?.classList.add("hidden");
  $("#search-full")?.classList.remove("on");
}
function parseURLState() {
  const sp = new URLSearchParams(location.search);
  if (!sp.toString()) return null;
  const s = {};
  if (sp.has("lib")) s.vol = sp.get("lib") === "" ? null : +sp.get("lib");
  for (const k of ["view", "dir", "q", "kind", "topic", "series", "author", "mark", "tag", "sort"]) {
    if (sp.has(k)) s[k] = sp.get(k);
  }
  if (sp.get("ft") === "1") s.fulltext = true;
  return s;
}
async function applyState(s, push) {
  const prevVol = state.volId;
  Object.assign(state, {
    volId: s.vol !== undefined ? s.vol : state.volId,
    view: s.view || (s.q ? "list" : "home"), dir: s.dir || "", q: s.q || "",
    kind: s.kind || null, topic: s.topic || null, series: s.series || null,
    author: s.author || null, mark: s.mark || null, tag: s.tag || null,
    sort: s.sort || "mtime_desc", fulltext: !!s.fulltext,
  });
  const si = $("#search");
  if (si) si.value = state.q;
  $("#search-clear")?.classList.toggle("hidden", !state.q);
  $("#search-full")?.classList.toggle("on", state.fulltext);
  state.offset = 0; state.items = []; state.done = false;
  // 深链/前进后退切换库时，旧库的全局统计必须作废，否则首页横幅会显示上一个库的数字
  if (s.vol !== undefined && s.vol !== prevVol) STATS = null;
  if (push) pushHistory(false);
  markActive();
  renderLibBar();
  await Promise.all([loadTree(), loadTags()]);
  await render();
}

/* ---------------- 资料库 ---------------- */

function currentLib() {
  return state.libs.find(l => l.id === state.volId) || null;
}
function libShortName(lib) {
  if (!lib) return "全部资料库";
  const root = (lib.root_path || lib.root || "").replace(/[\\/]+$/, "");
  const segs = root.split(/[\\/]/).filter(Boolean);
  if (segs.length <= 1) return root || ("库 #" + lib.id);
  return segs[segs.length - 1];
}
function driveOf(lib) {
  const m = ((lib.root_path || lib.root || "")).match(/^([A-Za-z]:)/);
  return m ? m[1] : "";
}
/* 重名库必须能区分：加盘符，仍重就带 #id */
function libLabel(lib) {
  if (!lib) return "全部资料库";
  const base = lib.label || libShortName(lib);
  const same = state.libs.filter(l => (l.label || libShortName(l)) === base);
  if (same.length <= 1) return base;
  const dr = driveOf(lib);
  return dr ? `${base}（${dr}）` : `${base}（#${lib.id}）`;
}
function visibleLibs() {
  if (isAdmin() || ME.vols === "all") return state.libs;
  return state.libs.filter(l => (ME.vols || []).includes(l.id));
}

function renderLibBar() {
  const lib = currentLib();
  const btn = $("#lib-current");
  if (!btn) return;
  if (lib) {
    $("#lib-name").textContent = libLabel(lib);
    const dot = lib.online ? "🟢" : "⚪";
    $("#lib-sub").textContent = `${dot} ${(lib.n || 0).toLocaleString()} 文件 · ${fmtSize(lib.size)}`;
    btn.querySelector(".lib-icon").textContent = lib.online ? "💾" : "📀";
    btn.title = (lib.root_path || lib.root || "") + (lib.online ? "" : "\n（目录当前不可访问）");
  } else {
    $("#lib-name").textContent = state.libs.length ? "全部资料库（混合）" : "还没有资料库";
    $("#lib-sub").textContent = state.libs.length
      ? `${state.libs.length} 个库 · 建议选单个库浏览` : "先扫描一个文件夹";
    btn.querySelector(".lib-icon").textContent = "🗂";
  }

  const menu = $("#lib-menu");
  const rows = [];
  if (isAdmin() && state.libs.length > 1) {
    const tot = state.libs.reduce((a, l) => a + (l.n || 0), 0);
    rows.push(`<div class="libitem ${state.volId == null ? "on" : ""}" data-vol="">
      <span class="ic">🗂</span><span class="txt"><b>全部资料库（混合视图）</b>
      <small>${tot.toLocaleString()} 文件 · 只在找东西时用</small></span></div>`);
  }
  for (const l of visibleLibs()) {
    rows.push(`<div class="libitem ${l.id === state.volId ? "on" : ""}" data-vol="${l.id}">
      <span class="ic">${l.online ? "💾" : "📀"}</span>
      <span class="txt"><b>${esc(libLabel(l))}</b>
        <small>${esc(l.root_path || l.root || "")}</small>
        <small>${(l.n || 0).toLocaleString()} 文件 · ${fmtSize(l.size)} · ${((l.dir_count || l.dirs || 0)).toLocaleString()} 目录</small>
      </span></div>`);
  }
  if (isAdmin()) {
    rows.push(`<div class="libitem" data-addlib="1">
      <span class="ic">＋</span><span class="txt"><b>添加资料库…</b>
      <small>填一个文件夹路径，扫描进索引</small></span></div>`);
  }
  menu.innerHTML = rows.join("");
  $$(".libitem", menu).forEach(el => el.onclick = () => {
    $("#lib-menu").classList.add("hidden");
    if (el.dataset.addlib) return addLibrary();
    const v = el.dataset.vol;
    const target = v === "" ? null : +v;
    if (target === state.volId) return;
    state.volId = target;
    state.tree = null;
    state.openDirs.clear();
    resetFilters();
    try { localStorage.setItem("filelib.vol", target == null ? "" : String(target)); } catch {}
    pushHistory(false);
    markActive();
    Promise.all([loadStats(), loadTree(), loadTags()]).then(() => render())
      .catch(e => { toast(e.message || "加载失败", 2600); render().catch(() => {}); });
  });
}

/* ---------------- 统计 ---------------- */

let STATS = null;
let BOOT_GUIDED = false;   // loadStats 的"默认进第一个库"引导只在首次加载时执行

async function loadStats(silent) {
  if (state.volId == null && !silent) {
    let saved = null;
    try { saved = localStorage.getItem("filelib.vol"); } catch {}
    if (saved) state.volId = +saved;
  }
  let st;
  try {
    st = await api(withVol("/api/stats"));
  } catch (e) {
    // localStorage 里记的库可能已被管理员收回（换号/权限调整后残留）：
    // 退回全局视角重试（服务端会按用户权限过滤可见库），避免整页报"索引读取失败"。
    if (state.volId == null) throw e;
    state.volId = null;
    try { localStorage.setItem("filelib.vol", ""); } catch {}
    st = await api("/api/stats");
  }
  STATS = st;
  state.libs = st.volumes || [];

  const vis = visibleLibs();
  if (state.volId != null && !vis.some(l => l.id === state.volId)) {
    state.volId = vis[0] ? vis[0].id : null;
  }
  if (state.volId == null && vis.length) {
    // 普通用户没有"混合视图"入口（服务端对其 vol=None 只会返回第一个授权库，
    // 界面上会把某个库误标成"全部资料库"）→ 直接落到第一个可见库
    if (!isAdmin()) {
      const first = vis.find(l => l.online) || vis[0];
      state.volId = first.id;
      try { localStorage.setItem("filelib.vol", String(state.volId)); } catch {}
      // URL 上若带着深链参数（view/kind/q/lib…），绝不能在这里 pushHistory ——
      // 它会用"此刻的 state"（view=home）覆盖地址栏，boot 稍后就读不到深链了。
      // 仅在裸地址（无任何参数）时才把默认库写进 URL。
      if (!parseURLState()) pushHistory(true);
      return loadStats(true);
    }
    // 初始引导（仅管理员、仅页面加载时做一次）：混合视图默认建议进单个库
    if (!BOOT_GUIDED && state.libs.length > 1 && !location.search.includes("lib=")) {
      BOOT_GUIDED = true;
      const online = vis.find(l => l.online) || vis[0];
      state.volId = online.id;
      try { localStorage.setItem("filelib.vol", String(state.volId)); } catch {}
      pushHistory(true);
      return loadStats(true);
    }
  }
  renderKinds(st);
  renderLibBar();
  return st;
}

function renderKinds(st) {
  const ul = $("#nav-kinds");
  if (!ul) return;
  const rows = (st.by_kind || []).filter(k => k.n > 0);
  ul.innerHTML = rows.map(k =>
    `<li data-kind="${esc(k.kind)}"><span>${kindIcon(k.kind)} ${KIND_LABEL[k.kind] || esc(k.kind)}</span>
     <span class="n">${(k.n || 0).toLocaleString()}</span></li>`).join("")
    || `<li class="dim">此库暂无文件</li>`;
  $$("#nav-kinds li[data-kind]").forEach(li => li.onclick = () => {
    state.kind = state.kind === li.dataset.kind ? null : li.dataset.kind;
    Object.assign(state, { view: "list", dir: "", series: null, author: null, mark: null, tag: null, topic: null });
    refresh();
  });
}

/* ---------------- 标签 ---------------- */

let TAGS_VIEW = "all";   // 侧栏标签筛选：all | user | auto

/* 侧栏标签多选筛选：state.tag 存逗号分隔多值（如 "科幻,2020"），null=未筛选 */
const selTags = () => String(state.tag || "").split(",").filter(Boolean);

async function loadTags() {
  const el = $("#nav-tags");
  if (!el) return;
  try {
    const r = await api(withVol("/api/tags?limit=40"));
    const items = r.items || [];
    const f = TAGS_VIEW;
    const shown = items.filter(t => f === "all" || (f === "user" ? t.user : !t.user));
    const bar = `<li class="tagfilter">
      <span class="tf-item ${f === "all" ? "on" : ""}" data-tf="all">全部</span>
      <span class="tf-item ${f === "user" ? "on" : ""}" data-tf="user">人工</span>
      <span class="tf-item ${f === "auto" ? "on" : ""}" data-tf="auto">自动</span>
      ${isAdmin() && items.some(t => !t.user)
        ? `<a class="tf-clear" id="btn-autotag-clear" title="清空全部自动标签（人工标签不受影响）">清空自动</a>` : ""}
    </li>`;
    el.innerHTML = bar + (shown.length
      ? shown.map(t => `<li data-tag="${esc(t.name)}" class="${selTags().includes(t.name) ? "active" : ""}">
          <span>🏷 ${esc(String(t.name).replace(/^auto:/, ""))}${t.user ? "" : " <small>自动</small>"}</span>
          <span class="n">${(t.n || 0).toLocaleString()}</span></li>`).join("")
      : `<li class="dim">${items.length ? "该类型下暂无标签" : "暂无标签（详情里可添加）"}</li>`);
    $$("#nav-tags li[data-tag]").forEach(li => li.onclick = () => {
      const cur = selTags();                          // 多选：点已选的取消，未选的追加
      const i = cur.indexOf(li.dataset.tag);
      if (i >= 0) cur.splice(i, 1); else cur.push(li.dataset.tag);
      state.tag = cur.length ? cur.join(",") : null;
      Object.assign(state, { view: "list", dir: "", kind: null, topic: null, series: null, author: null, mark: null });
      refresh();
    });
    $$("#nav-tags [data-tf]").forEach(s => s.onclick = () => {
      TAGS_VIEW = s.dataset.tf;
      loadTags();
    });
    const clr = $("#btn-autotag-clear");
    if (clr) clr.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm("确定清空全部自动标签？\n\n人工标签不受影响；清空后可在「维护 → 🏷 自动打标签」重新生成。")) return;
      try {
        const rr = await api("/api/autotag_clear", post({}));
        toast(`已清除 ${rr.removed || 0} 条自动标签`);
        if (state.tag) state.tag = selTags().filter(t => !t.startsWith("auto:")).join(",") || null;
        loadTags();
        refresh();
      } catch (e) { toast(e.message, 2600); }
    };
  } catch {
    el.innerHTML = "";
  }
}

/* ---------------- 目录树 ---------------- */

async function loadTree() {
  const el = $("#nav-tree");
  if (!el) return;
  if (state.volId == null) {
    el.innerHTML = `<li class="dim">选择具体资料库后显示目录</li>`;
    state.tree = null;
    return;
  }
  if (state.tree && state.tree.volId === state.volId) { renderTree(); return; }
  el.innerHTML = `<li class="dim">加载目录…</li>`;
  try {
    const d = await api(`/api/dirtree?vol=${state.volId}`);
    const byRel = {}, kids = {};
    for (const n of (d.nodes || [])) {
      byRel[n.rel] = n;
      (kids[n.parent] = kids[n.parent] || []).push(n);
    }
    for (const k in kids) kids[k].sort((a, b) => a.name.localeCompare(b.name, "zh"));
    state.tree = { byRel, kids, volId: state.volId };
    renderTree();
  } catch (e) {
    el.innerHTML = `<li class="dim">目录加载失败：${esc(e.message)}</li>`;
  }
}

function renderTree(animRel) {
  const el = $("#nav-tree");
  if (!el || !state.tree) return;
  const out = [];
  const walk = (parent, depth) => {
    for (const n of (state.tree.kids[parent] || [])) {
      const hasKids = (state.tree.kids[n.rel] || []).length > 0;
      const open = state.openDirs.has(n.rel);
      // 箭头恒用 ▸，展开态靠 CSS rotate(90deg) 过渡成 ▾（字符切换无法做动画）
      out.push(`<li class="tnode ${open ? "open" : ""} ${state.view === "list" && state.dir === n.rel ? "active" : ""} ${animRel === parent ? "tin" : ""}"
        data-rel="${esc(n.rel)}" style="padding-left:${8 + depth * 12}px">
        <span class="tarrow">${hasKids ? "▸" : `<span class="leaf">·</span>`}</span>
        <span class="tname" title="${esc(n.rel)}">${esc(n.name)}</span>
        <span class="tn">${n.n > 0 ? n.n.toLocaleString() : ""}</span></li>`);
      if (open) walk(n.rel, depth + 1);
    }
  };
  walk("", 0);
  el.innerHTML = out.length ? out.join("") : `<li class="dim">此库没有子目录</li>`;
  $$("#nav-tree li.tnode").forEach(li => {
    li.onclick = e => {
      const rel = li.dataset.rel;
      if (e.target.closest(".tarrow")) {
        if (e.target.closest(".leaf")) return;
        const opening = !state.openDirs.has(rel);
        if (opening) state.openDirs.add(rel); else state.openDirs.delete(rel);
        renderTree(opening ? rel : undefined);
        return;
      }
      Object.assign(state, { view: "list", kind: null, topic: null, series: null,
        author: null, mark: null, tag: null, q: "", fulltext: false, dir: rel });
      const si = $("#search");
      if (si) si.value = "";
      $("#search-full")?.classList.remove("on");
      openTo(rel);
      refresh();
      if (isMobile()) setSidebar(false);
    };
  });
  // 进入目录后把高亮节点滚进视野（长目录树里否则找不到自己在哪）
  requestAnimationFrame(() => {
    const act = $("#nav-tree li.tnode.active");
    if (act) act.scrollIntoView({ block: "nearest" });
  });
}
function openTo(rel) {
  const parts = rel.split("/").filter(Boolean);
  let acc = "";
  for (const p of parts) { state.openDirs.add(acc ? acc.replace(/\/$/, "") : ""); acc += p + "/"; }
  if (parts.length) state.openDirs.add(parts.slice(0, -1).join("/"));
  if (state.tree) renderTree();
}

/* ---------------- 侧栏导航 ---------------- */

function markActive() {
  $$("#sec-main li[data-nav]").forEach(li => {
    let on = false;
    if (state.view === "list") {
      if (["star", "read", "todo"].includes(state.mark)) on = li.dataset.nav === state.mark;
    } else {
      on = li.dataset.nav === state.view;
    }
    li.classList.toggle("active", on);
  });
  $$("#nav-kinds li[data-kind]").forEach(li =>
    li.classList.toggle("active", state.kind === li.dataset.kind));
  $$("#nav-tags li[data-tag]").forEach(li =>
    li.classList.toggle("active", selTags().includes(li.dataset.tag)));
  $$("#nav-tree li.tnode").forEach(li =>
    li.classList.toggle("active", li.dataset.rel === state.dir && state.view === "list"));
  // 顶栏场景按钮：📦 打包下载只在文件列表里有意义；📁＋ 新建文件夹需要库上下文
  const bz = $("#btn-zip");
  if (bz) bz.classList.toggle("hidden", !(state.view === "list" && !state.fulltext));
  const bn = $("#btn-newdir");
  if (bn) bn.classList.toggle("hidden", !(isAdmin() && state.volId != null));
  // 当前视图落在某个折叠组里时自动展开它（否则高亮项被藏起来用户会懵）
  const act = $("#sec-main li[data-nav].active");
  if (act) {
    const g = act.closest(".navgrp");
    if (g && g.classList.contains("collapsed")) setGrpCollapsed(g, false);
  }
  applyNavPerms();
}

/** 当前用户对某导航项是否有权限（admin / nav==="*" 全放行）。 */
/** 当前用户在指定库（默认当前库）对某导航项是否有权限。 */
function navAllowed(n, vol) {
  if (isAdmin() || ME.nav === "*") return true;
  if (n === "home") {
    // 总览是进库的基础能力：对该库有任何权限即可
    const nav = ME.nav || {};
    const v = vol === undefined ? state.volId : vol;
    if (v != null) return nav[v] != null;
    return (Array.isArray(ME.vols) ? ME.vols : []).some(x => nav[x] != null);
  }
  const nav = ME.nav || {};
  const v = vol === undefined ? state.volId : vol;
  const check = vid => {
    const m = nav[vid];
    if (m === "*") return true;
    return Array.isArray(m) && m.includes(n);
  };
  if (v != null) return check(v);
  return (Array.isArray(ME.vols) ? ME.vols : []).some(check);
}
function canPlay() { return navAllowed("play"); }
function canDownload() { return navAllowed("download"); }

/* ---------------- 侧栏分组折叠（状态记忆 + 平滑动画） ---------------- */

const NAV_GRP_KEY = "filelib.navg";

function navGrpState() {
  try { return JSON.parse(localStorage.getItem(NAV_GRP_KEY) || "{}") || {}; }
  catch { return {}; }
}
function saveGrpState(st) {
  try { localStorage.setItem(NAV_GRP_KEY, JSON.stringify(st)); } catch {}
}
/** 折叠/展开一个分组（box = .navgrp 或 section）。同步持久化。 */
function setGrpCollapsed(box, collapsed) {
  box.classList.toggle("collapsed", !!collapsed);
  const head = box.querySelector(":scope > h3.grp-head");
  if (!head || !head.dataset.grp) return;
  const st = navGrpState();
  if (collapsed) st[head.dataset.grp] = 1; else delete st[head.dataset.grp];
  saveGrpState(st);
}
/** 页面加载时恢复上次的折叠状态（默认全展开）。 */
function applyGrpState() {
  const st = navGrpState();
  $$("#sidebar h3.grp-head").forEach(h => {
    if (h.dataset.grp && st[h.dataset.grp]) h.parentElement.classList.add("collapsed");
  });
}
function bindNavGroups() {
  $$("#sidebar h3.grp-head").forEach(h => {
    h.onclick = e => {
      // ⊞⊟ 展开收起树、⚙ 标签管理这些 h3 内的小工具不触发折叠
      if (e.target.closest("a")) return;
      setGrpCollapsed(h.parentElement, !h.parentElement.classList.contains("collapsed"));
    };
  });
}

const ALL_NAV = ["home", "tree", "topics", "authors", "series", "kinds", "tags",
  "star", "read", "todo", "continue", "stats", "random", "recent", "big", "search",
  "fulltext", "play", "dups", "junk", "download"];

function applyNavPerms() {
  if (isAdmin() || ME.nav === "*") {
    $$("[data-nav]").forEach(el => el.classList.remove("hidden"));
    $$("#sidebar .navgrp").forEach(el => el.classList.remove("hidden"));
    return;
  }
  // 按【当前选中库】计算可见导航项（而非所有授权库的并集）——
  // 否则一个库勾了"全部"，其他库未勾选的菜单也会漏出来。
  const nav = ME.nav || {};
  const vols = Array.isArray(ME.vols) ? ME.vols : [];
  const allowed = new Set(["home"]);
  const collect = mm => {
    if (mm === "*") { ALL_NAV.forEach(x => allowed.add(x)); return true; }
    if (Array.isArray(mm)) mm.forEach(x => allowed.add(x));
    return false;
  };
  let any = false;
  if (state.volId != null && nav[state.volId] != null) {
    any = true;
    collect(nav[state.volId]);
  } else {
    // 混合视图或当前库未单独配置 → 取所有授权库的并集
    for (const vid of vols) {
      if (nav[vid] == null) continue;
      any = true;
      if (collect(nav[vid])) break;
    }
  }
  if (!any) allowed.clear();  // 什么都没配 → 全隐藏
  // 有库但一个导航项都没配 → 空界面（只有总览骨架也是空的）
  const MAIN_NAV = ["home", "tree", "topics", "authors", "series", "kinds", "tags",
                    "star", "read", "todo", "continue", "stats", "random", "recent", "big",
                    "play", "download"];
  $$("#sec-main li[data-nav]").forEach(el => {
    const n = el.dataset.nav;
    if (n && MAIN_NAV.includes(n)) el.classList.toggle("hidden", !allowed.has(n));
  });
  // 空间分析跟「目录」权限走（服务端 dirtree 用 tree 导航位校验）
  $$("#nav-main li[data-nav='space']").forEach(el =>
    el.classList.toggle("hidden", !allowed.has("tree")));
  // 侧栏各分区（目录/类型/标签）跟随对应导航项
  $$("#sidebar section[data-nav]").forEach(el => {
    const n = el.dataset.nav;
    if (n) el.classList.toggle("hidden", !allowed.has(n));
  });
  // 组内所有项都被权限隐藏时，组标题一并隐藏（避免露出空组头）
  $$("#sidebar .navgrp").forEach(g => {
    const vis = $$("li[data-nav]", g).some(li => !li.classList.contains("hidden"));
    g.classList.toggle("hidden", !vis);
  });
}

function showEmptyPerm() {
  $("#grid").className = "";
  $("#grid").innerHTML = `<div class="empty"><div class="big">🔒</div>
    你的账号还没有被分配任何资料库<br>
    <small>请联系管理员在「管理后台 → 用户与权限」里授予访问权限</small></div>`;
  setCrumb(`<span class="libtag">👤 ${esc(ME.username)}</span>`);
}

/* ---------------- 搜索 ---------------- */

function bindSearch() {
  const si = $("#search");
  si.addEventListener("input", debounce(e => {
    state.q = e.target.value.trim();
    $("#search-clear").classList.toggle("hidden", !state.q);
    if (!state.q) return;
    Object.assign(state, { view: "list", dir: "", kind: null, topic: null,
      series: null, author: null, mark: null, tag: null });
    refresh();
  }, 280));
  si.addEventListener("keydown", e => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (state.fulltext && state.q) runFulltext();
      else si.blur();
    }
  });
  $("#search-clear").onclick = () => {
    si.value = "";
    state.q = "";
    state.fulltext = false;
    $("#search-clear").classList.add("hidden");
    $("#search-full").classList.remove("on");
    state.view = "home";
    refresh();
    si.focus();
  };
  $("#search-full").onclick = () => {
    state.fulltext = !state.fulltext;
    $("#search-full").classList.toggle("on", state.fulltext);
    if (state.fulltext) {
      si.placeholder = "搜索书里的内容（正文）… 回车开始";
      si.focus();
      if (state.q) runFulltext();
    } else {
      si.placeholder = "搜索文件名/目录… 支持 ext:mp4  tag:标签  author:作者  size:>100m";
      $("#ft-box").classList.add("hidden");
    }
  };
}

/* ---------------- 主渲染 ---------------- */

async function render() {
  closeDetail();
  if (!state.fulltext) $("#ft-box").classList.add("hidden");
  const grid = $("#grid");
  grid.className = "grid";
  animateView(grid);

  const vis = visibleLibs();
  if (!isAdmin() && !vis.length) return showEmptyPerm();

  const lb = $("#loadbar");
  if (lb) lb.classList.add("on");
  try {
    switch (state.view) {
      case "home": return await viewHome();
      case "topics": return await viewTopics();
      case "authors": return await viewAuthors();
      case "series": return await viewSeries();
      case "kinds": return await viewKinds();
      case "continue": return await viewContinue();
      case "random": return await viewRandom();
      case "stats": return await viewStats();
      case "space": return await viewSpace();
      case "recent": state.sort = "mtime_desc"; state.view = "list"; return await startList();
      case "big": state.sort = "size_desc"; state.view = "list"; return await startList();
      case "list": return await startList();
      default: return await viewHome();
    }
  } finally {
    if (lb) lb.classList.remove("on");
  }
}

/* 视图切换过渡：容器整体淡入上移。走 inline style，避免被 className 重置清掉 */
function animateView(el) {
  if (!el) return;
  el.style.animation = "none";
  void el.offsetHeight;
  el.style.animation = "viewin .2s ease";
  clearTimeout(el._animT);
  el._animT = setTimeout(() => { el.style.animation = ""; }, 260);
}

function libSuffix() {
  const l = currentLib();
  return l ? ` · 📚 ${esc(libLabel(l))}` : " · 🗂 混合视图";
}
function setCrumb(html) { const c = $("#crumbs"); if (c) c.innerHTML = html; }

async function viewHome() {
  const st = STATS || await loadStats();
  const lib = currentLib();
  const el = $("#grid");
  el.className = "";

  let home = { star: [], continue: [], recent: [] };
  try { home = await api(withVol("/api/home")); } catch {}
  // 轨道原始条目留档：从轨道卡片发起播放时，把同轨道内容作为连播队列
  state.homeRails = { star: home.star || [], recent: home.recent || [], continue: home.continue || [] };
  const noLibs = !(st.volumes || []).length;
  if (home.empty) {
    el.innerHTML = noLibs
      ? `<div class="empty"><div class="big">📚</div>还没有任何资料库<br>
         <button class="btn acc" id="empty-add" style="margin-top:12px">＋ 添加第一个资料库</button><br>
         <small style="color:var(--fg2)">填一个文件夹路径，扫描只读不改文件</small></div>`
      : `<div class="empty"><div class="big">📭</div>
         这个资料库还没有数据<br><small>${isAdmin() ? "点右上角 ⟳ 更新这个库" : "请联系管理员"}</small></div>`;
    const ea = $("#empty-add", el);
    if (ea) ea.onclick = addLibrary;
    setCrumb(`📚 ${esc(lib ? libLabel(lib) : "")}`);
    return;
  }

  const kinds = (st.by_kind || []).filter(k => k.n > 0);
  el.innerHTML = `
    <div class="home-hero">
      <div class="hh-main">
        <h1>${lib ? "📚 " + esc(libLabel(lib)) : "🗂 全部资料库"}</h1>
        <p>${lib ? esc(lib.root_path || lib.root || "") : "多个库混合显示，只建议找东西时使用"}</p>
      </div>
      <div class="stat">
        <div><b>${(st.total_files || 0).toLocaleString()}</b><span>文件</span></div>
        <div><b>${fmtSize(st.total_size || 0)}</b><span>占用</span></div>
        <div><b>${(st.total_dirs || 0).toLocaleString()}</b><span>目录</span></div>
        <div><b>${(st.thumbs_done || 0).toLocaleString()}</b><span>已出封面</span></div>
      </div>
    </div>
    ${kinds.length ? `<div class="kindbar">${kinds.map(k =>
      `<div class="kb" data-kind="${esc(k.kind)}">${kindIcon(k.kind)} ${KIND_LABEL[k.kind] || esc(k.kind)}
        <b>${(k.n || 0).toLocaleString()}</b></div>`).join("")}</div>` : ""}
    ${home.daily ? dailyHTML(home.daily) : ""}
    ${railHTML("⭐ 我的收藏", home.star, "star")}
    ${railHTML("🕒 最近添加", home.recent, "recent")}
    ${railHTML("▶ 继续观看", home.continue, "continue")}
    ${!kinds.length ? `<div class="empty"><div class="big">📭</div>
      这个资料库还没有可显示的文件<br><small>${isAdmin()
        ? "点右上角 ⟳ 更新这个库" : "请联系管理员"}</small></div>` : ""}
  `;
  setCrumb(`${lib ? "📚 " + esc(libLabel(lib)) : "🗂 全部资料库"} · ${(st.total_files || 0).toLocaleString()} 个文件 · ${fmtSize(st.total_size || 0)}`);
  $$(".kindbar .kb", el).forEach(b => b.onclick = () => {
    state.kind = b.dataset.kind;
    Object.assign(state, { view: "list", dir: "", mark: null, tag: null });
    refresh();
  });
  $$(".daily[data-id]", el).forEach(d => d.onclick = () => quickOpen(home.daily));
  bindCards(el);
}

/* 今日推荐卡片：按日期做种子，一天一换 */
function dailyHTML(it) {
  const ex = (it.excerpt || "").trim();
  return `<div class="daily" data-id="${it.id}" title="点击直接打开阅读">
    <div class="dl-badge">📖 今日推荐</div>
    <div class="dl-main">
      <div class="dl-name">${kindIcon(it.kind)} ${esc(it.name)}</div>
      ${ex ? `<div class="dl-ex">「${esc(ex.slice(0, 110))}${ex.length > 110 ? "…" : ""}」</div>` : ""}
      <div class="dl-sub">${fmtSize(it.size)} · 每天随机挑一本，点开就读</div>
    </div></div>`;
}

function railHTML(title, items, kind) {
  if (!items || !items.length) {
    if (kind === "star" || kind === "continue") return "";
    return `<div class="rail"><h2>${title}</h2><div class="rail-empty">暂无</div></div>`;
  }
  return `<div class="rail"><h2>${title}<span class="more">${items.length} 个</span></h2>
    <div class="grid" data-rail="${kind}">${items.map(cardHTML).join("")}</div></div>`;
}

async function viewKinds() {
  const st = STATS || await loadStats();
  setCrumb(`按类型浏览${libSuffix()}`);
  const el = $("#grid");
  const kinds = (st.by_kind || []).filter(k => k.n > 0);
  if (!kinds.length) {
    el.innerHTML = `<div class="empty"><div class="big">🗂</div>此库还没有文件</div>`;
    return;
  }
  el.innerHTML = kinds.map(k => `
    <div class="card" data-kind="${esc(k.kind)}">
      <div class="cover" style="font-size:40px">${kindIcon(k.kind)}</div>
      <div class="meta"><div class="name">${KIND_LABEL[k.kind] || esc(k.kind)}</div>
        <div class="sub"><span>${(k.n || 0).toLocaleString()} 个</span><span>${fmtSize(k.size)}</span></div>
      </div></div>`).join("");
  $$(".card[data-kind]", el).forEach(c => c.onclick = () => {
    state.kind = c.dataset.kind;
    Object.assign(state, { view: "list", dir: "" });
    refresh();
  });
}

async function viewTopics() {
  const { items } = await api(withVol("/api/topics"));
  setCrumb(`按主题浏览${libSuffix()} · ${items.length} 个主题`);
  const el = $("#grid");
  if (!items.length) {
    el.innerHTML = `<div class="empty"><div class="big">🌏</div>还没有主题数据<br>
      <small>主题从目录结构归纳，扫描完成后出现</small></div>`;
    return;
  }
  el.innerHTML = items.map(t => `
    <div class="card" data-topic="${esc(t.topic)}">
      <div class="cover">${t.cover ? `<img loading="lazy" src="/thumb/${t.cover}.jpg" alt="" data-ph="${esc(t.topic)}" onerror="phErr(this,'🌏')">` : "🌏"}</div>
      <div class="meta"><div class="name">${esc(t.topic)}</div>
        <div class="sub"><span>${(t.n || 0).toLocaleString()} 个</span><span>${fmtSize(t.size)}</span></div>
      </div></div>`).join("");
  $$(".card[data-topic]", el).forEach(c => c.onclick = () => {
    state.topic = c.dataset.topic;
    Object.assign(state, { view: "list", dir: "" });
    refresh();
  });
}

async function viewAuthors() {
  const qs = new URLSearchParams({ limit: "500" });
  if (state.volId != null) qs.set("vol", state.volId);
  const r = await api("/api/authors?" + qs);
  const items = r.items || [];
  setCrumb(`按作者浏览${libSuffix()} · ${(r.total || 0).toLocaleString()} 位作者`);
  const el = $("#grid");
  el.className = "grid authors-grid";
  el.innerHTML = items.length ? items.map(a => `
    <div class="card author" data-author="${esc(a.name)}">
      <div class="cover">👤</div>
      <div class="meta"><div class="name">${esc(a.name)}</div>
        <div class="sub"><span>${a.n} 部</span></div></div></div>`).join("")
    : `<div class="empty"><div class="big">👤</div>还没有作者信息<br>
        <small>作者来自 EPUB/MOBI 元数据和文件名，请先「更新索引」</small></div>`;
  $$(".card[data-author]", el).forEach(c => c.onclick = () => {
    state.author = c.dataset.author;
    Object.assign(state, { view: "list", dir: "" });
    refresh();
  });
}

async function viewSeries() {
  const qs = new URLSearchParams({ limit: "300" });
  if (state.volId != null) qs.set("vol", state.volId);
  if (state.kind) qs.set("kind", state.kind);
  const r = await api("/api/series?" + qs);
  const items = r.items || [];
  setCrumb(`按系列浏览${libSuffix()} · ${(r.total || 0).toLocaleString()} 个系列`);
  const el = $("#grid");
  el.innerHTML = items.length ? items.map(s => `
    <div class="card" data-series="${esc(s.series)}">
      <div class="cover">${s.cover ? `<img loading="lazy" src="/thumb/${s.cover}.jpg" alt="" data-ph="${esc(s.series)}" onerror="phErr(this,'🔖')">` : "🔖"}</div>
      <div class="meta"><div class="name">${esc(s.series)}</div>
        <div class="sub"><span>${s.n} 个</span><span>${fmtSize(s.size)}</span></div>
      </div></div>`).join("")
    : `<div class="empty"><div class="big">🔖</div>这个资料库里没有系列信息<br>
        <small>系列从"剧集/套装"命名里提取</small></div>`;
  $$(".card[data-series]", el).forEach(c => c.onclick = () => {
    state.series = c.dataset.series;
    Object.assign(state, { view: "list", dir: "" });
    refresh();
  });
}

/* ---------------- 继续看 / 随机看 ---------------- */

async function viewContinue() {
  const el = $("#grid");
  el.className = "";
  const lib = currentLib();
  setCrumb(`▶ 继续看 <span class="libtag">${lib ? "📚 " + esc(libLabel(lib)) : "🗂 混合视图"}</span>`);
  el.innerHTML = `<div class="empty"><div class="big">⏳</div>加载中…</div>`;
  let items = [];
  try {
    ({ items } = await api(withVol("/api/continue?limit=200")));
  } catch (e) {
    el.innerHTML = `<div class="empty"><div class="big">⚠️</div>加载失败：${esc(e.message)}</div>`;
    return;
  }
  state.items = items;
  const scope = lib ? `当前库「${esc(libLabel(lib))}」` : "全部资料库";
  if (!items.length) {
    el.innerHTML = `<div class="empty"><div class="big">🎈</div>还没有继续看记录<br>
      <small>播放视频 / 音频后自动记录进度，可在这里继续或清理</small></div>`;
    return;
  }
  el.innerHTML = `
    <div class="cwbar">
      <label style="display:inline-flex;align-items:center;gap:5px;cursor:pointer">
        <input type="checkbox" id="cw-all"> 全选</label>
      <button class="btn small danger" id="cw-del" disabled>🗑 删除所选</button>
      <button class="btn small danger" id="cw-clear">🧹 一键清空（${scope}）</button>
      <small style="color:var(--fg2);margin-left:auto">共 ${items.length} 条记录 · 记录只属于你自己，删除不影响文件</small>
    </div>
    <div class="cwlist">
    ${items.map(it => {
      const pct = Math.min(100, Math.max(0, Math.round(it.progress || 0)));
      const openable = it.kind === "video" || it.kind === "audio" || it.kind === "image"
        || READER_EXT.includes((it.ext || "").toLowerCase());
      const openLabel = (it.kind === "video" || it.kind === "audio") ? "▶ 继续"
        : it.kind === "image" ? "🔍 查看" : "📖 续读";
      return `
      <div class="cwrow" data-id="${it.id}">
        <input type="checkbox" class="cwck" data-id="${it.id}">
        ${it.thumb ? `<img class="cwthumb" loading="lazy" src="/thumb/${it.id}.jpg" alt="" data-ph="${esc(it.name || "")}" onerror="phErr(this,'${kindIcon(it.kind)}')">`
                   : `<span class="cwthumb">${kindIcon(it.kind)}</span>`}
        <div class="cwmain">
          <div class="cwname" title="${esc(it.name)}">${esc(it.name)}</div>
          <div class="cwprog"><div class="cwfill" style="width:${pct}%"></div></div>
          <small style="color:var(--fg2)">${fmtDur(it.pos)} / ${fmtDur(it.dur)} · ${pct}%</small>
        </div>
        ${openable && canPlay() ? `<button class="btn small acc" data-open="${it.id}">${openLabel}</button>` : ""}
        <button class="btn small" data-rm="${it.id}" title="删除这条记录">✕</button>
      </div>`;
    }).join("")}
    </div>`;

  const boxes = $$(".cwck", el);
  const delBtn = $("#cw-del", el);
  const allCk = $("#cw-all", el);
  const upd = () => {
    const n = boxes.filter(b => b.checked).length;
    delBtn.disabled = !n;
    delBtn.textContent = n ? `🗑 删除所选（${n}）` : "🗑 删除所选";
    allCk.checked = n > 0 && n === boxes.length;
  };
  allCk.onchange = () => { boxes.forEach(b => b.checked = allCk.checked); upd(); };
  boxes.forEach(b => b.onchange = upd);
  const rmRow = id => {
    $(`.cwrow[data-id="${id}"]`, el)?.remove();
    state.items = state.items.filter(x => x.id !== id);
  };
  const maybeEmpty = () => {
    if (!state.items.length) viewContinue();
  };
  delBtn.onclick = async () => {
    const ids = boxes.filter(b => b.checked).map(b => +b.dataset.id);
    if (!ids.length) return;
    if (!confirm(`把所选 ${ids.length} 条记录删掉？（不影响文件本身）`)) return;
    try {
      await api("/api/continue", post({ action: "delete", ids }));
      ids.forEach(rmRow);
      boxes.forEach(b => b.checked = false);
      upd();
      toast("已删除所选记录");
      maybeEmpty();
    } catch (e) { toast(e.message, 2600); }
  };
  $("#cw-clear", el).onclick = async () => {
    if (!confirm(`清空${scope}的全部继续看记录？（不影响文件本身）`)) return;
    try {
      await api("/api/continue", post({ action: "clear", vol: lib ? lib.id : null }));
      toast("已清空记录");
      viewContinue();
    } catch (e) { toast(e.message, 2600); }
  };
  $$("[data-open]", el).forEach(b => b.onclick = () => {
    const it = state.items.find(x => x.id === +b.dataset.open);
    if (it) quickOpen(it);
  });
  $$("[data-rm]", el).forEach(b => b.onclick = async () => {
    const id = +b.dataset.rm;
    try {
      await api("/api/continue", post({ action: "delete", ids: [id] }));
      rmRow(id);
      maybeEmpty();
    } catch (e) { toast(e.message, 2600); }
  });
  $$(".cwname", el).forEach(n => n.onclick = () => openDetail(+n.closest(".cwrow").dataset.id));
}

async function viewRandom() {
  const el = $("#grid");
  el.className = "";
  setCrumb(`🎲 随机看${libSuffix()}`);
  el.innerHTML = `<div class="empty"><div class="big">⏳</div>正在随机抽选…</div>`;
  let items = [];
  try {
    ({ items } = await api(withVol("/api/random?n=60")));
  } catch (e) {
    el.innerHTML = `<div class="empty"><div class="big">⚠️</div>加载失败：${esc(e.message)}</div>`;
    return;
  }
  state.items = items;
  el.innerHTML = `
    <div class="cwbar">
      <button class="btn small acc" id="rnd-again">🎲 换一批</button>
      <small style="color:var(--fg2);margin-left:auto">随机抽出 ${items.length} 个文件 ·
        单击看详情，双击直接打开</small>
    </div>
    <div class="grid" id="rnd-grid"></div>`;
  const g = $("#rnd-grid", el);
  if (items.length) {
    g.innerHTML = items.map(it => cardHTML(it)).join("");
    bindCards(g);
  } else {
    g.innerHTML = `<div class="empty"><div class="big">📭</div>这个资料库还没有文件</div>`;
  }
  $("#rnd-again", el).onclick = () => viewRandom();
}

/* ---------------- 阅读统计（打卡热力图） ---------------- */

function fmtMin(secs) {
  if (!secs || secs < 60) return Math.round(secs || 0) + " 秒";
  const m = Math.round(secs / 60);
  if (m < 60) return m + " 分钟";
  return (m % 60 ? (m / 60).toFixed(1) : Math.floor(m / 60)) + " 小时";
}

async function viewStats() {
  const el = $("#grid");
  el.className = "";
  setCrumb(`📈 阅读统计`);
  let s;
  try { s = await api("/api/readstats"); } catch (e) {
    el.innerHTML = `<div class="empty"><div class="big">📈</div>${esc(e.message)}</div>`;
    return;
  }
  const t = s.totals || {};
  if (!t.days) {
    el.innerHTML = `<div class="empty"><div class="big">📈</div>还没有阅读记录<br>
      <small>打开任意 TXT / EPUB / PDF 阅读一会儿，停留时长会自动记到这里</small></div>`;
    return;
  }
  const secs = {};
  (s.days || []).forEach(x => { secs[x.d] = x.secs; });
  const p2 = n => String(n).padStart(2, "0");
  const keyOf = d => `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const end = new Date(today);                       // 对齐到本周日（周一起头）
  end.setDate(end.getDate() + (6 - (end.getDay() + 6) % 7));
  const cells = [];
  for (let i = 370; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    cells.push({ key: keyOf(d), future: d > today, secs: secs[keyOf(d)] || 0 });
  }
  const lvl = v => v <= 0 ? 0 : v < 900 ? 1 : v < 2700 ? 2 : v < 5400 ? 3 : 4;
  el.innerHTML = `
    <div class="stats-wrap">
      <div class="stats-cards">
        <div class="statc acc"><b>${fmtMin(s.today)}</b><span>今日阅读</span></div>
        <div class="statc"><b>${s.streak} 天</b><span>连续打卡${s.streak >= 7 ? " 🔥" : ""}</span></div>
        <div class="statc"><b>${(t.days || 0).toLocaleString()} 天</b><span>累计打卡</span></div>
        <div class="statc"><b>${fmtMin(t.secs || 0)}</b><span>累计时长</span></div>
        <div class="statc"><b>${(t.books || 0).toLocaleString()} 本</b><span>读过</span></div>
      </div>
      <div class="hm-box">
        <div class="hm-title">近一年阅读热力图</div>
        <div class="hm">${cells.map(c => c.future
          ? `<i class="hm-cell off"></i>`
          : `<i class="hm-cell l${lvl(c.secs)}" title="${c.key}${c.secs ? " · " + fmtMin(c.secs) : ""}"></i>`).join("")}</div>
        <div class="hm-legend">少 <i class="hm-cell l0"></i><i class="hm-cell l1"></i><i class="hm-cell l2"></i><i class="hm-cell l3"></i><i class="hm-cell l4"></i> 多</div>
      </div>
      <small class="stats-note">在阅读器里停留时自动计时（每 30 秒记一次，页面切到后台不计）。</small>
    </div>`;
}

async function startList() {
  state.offset = 0; state.items = []; state.itemsBase = 0; state.done = false;
  $("#grid").className = listGridClass();
  $("#grid").innerHTML = "";
  markActive();
  await loadList(true);
}

/* 列表容器布局：收藏/待读的书架模式用 block，其他用网格 */
function listGridClass() {
  if (state.mark && shelfMode()) return "shelf-page";
  return "grid" + (state.masonry && state.kind === "image" ? " masonry" : "");
}

/* ---- 拟物书架（收藏 / 待读 / 已读列表的书脊视图） ---- */

const SHELF_COLORS = ["#8a5a44", "#39647e", "#5f7d4e", "#7d5a8a", "#a8763e",
  "#4e6e7d", "#8a4e5f", "#5d7357", "#6b5b95", "#7d6a4e"];

function shelfMode() {
  try { return localStorage.getItem("filelib.shelf") === "1"; } catch { return false; }
}
function setShelfMode(v) {
  try {
    if (v) localStorage.setItem("filelib.shelf", "1");
    else localStorage.removeItem("filelib.shelf");
  } catch {}
}
function hashIdx(s) {
  let h = 0;
  for (const ch of String(s || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % SHELF_COLORS.length;
}
function shelfBarHTML() {
  const on = shelfMode();
  return `<div class="shelf-bar" id="shelf-bar">
    <span class="shelf-title">📚 书架</span>
    <span class="shelf-seg">
      <button class="btn small ${on ? "acc" : "ghost"}" id="shelf-on">书架</button>
      <button class="btn small ${on ? "ghost" : "acc"}" id="shelf-off">卡片</button>
    </span></div>`;
}
function spineHTML(it) {
  const h = 118 + Math.min(74, Math.log10(Math.max(1, it.size || 1)) * 13);
  const title = esc(String(it.name || "").replace(/\.[^.]+$/, ""));
  return `<div class="spine" data-id="${it.id}" title="${esc(it.name)}"
    style="background:${SHELF_COLORS[hashIdx(it.name)]};height:${h.toFixed(0)}px">
    <span class="spine-t">${title}</span></div>`;
}
function appendShelf(items) {
  const grid = $("#grid");
  if (!$("#shelf-bar", grid)) {
    grid.insertAdjacentHTML("afterbegin", shelfBarHTML() +
      (state.items.some(x => x.kind === "book") ? `<div class="shelf" id="shelf-books"></div>` : "") +
      `<div class="grid shelf-others" id="shelf-others"></div>`);
    $("#shelf-on", grid).onclick = () => toggleShelf(true);
    $("#shelf-off", grid).onclick = () => toggleShelf(false);
  }
  const books = items.filter(x => x.kind === "book");
  const others = items.filter(x => x.kind !== "book");
  const bk = $("#shelf-books", grid), ot = $("#shelf-others", grid);
  if (bk) bk.insertAdjacentHTML("beforeend", books.map(spineHTML).join(""));
  if (ot && others.length) ot.insertAdjacentHTML("beforeend", others.map(it => cardHTML(it)).join(""));
  if (bk) $$(".spine", bk).forEach(s => {
    if (s._b) return;
    s._b = true;
    s.onclick = () => {
      const it = state.items.find(x => x.id === +s.dataset.id);
      if (it) quickOpen(it);
    };
  });
  if (ot) bindCards(ot);
}
function toggleShelf(on) {
  setShelfMode(on);
  const grid = $("#grid");
  grid.className = listGridClass();
  grid.innerHTML = "";
  if (on) {
    appendShelf(state.items);
  } else {
    grid.insertAdjacentHTML("afterbegin", shelfBarHTML());
    $("#shelf-on", grid).onclick = () => toggleShelf(true);
    $("#shelf-off", grid).onclick = () => toggleShelf(false);
    grid.insertAdjacentHTML("beforeend", state.items.map(it => cardHTML(it)).join(""));
    bindCards(grid);
  }
}

async function loadList(first) {
  if (state.loading) return;
  state.loading = true;
  const sentinel = $("#sentinel");
  if (first) sentinel.textContent = "加载中…";
  try {
    const qs = new URLSearchParams({
      q: state.q, sort: state.sort, limit: state.limit, offset: state.offset,
    });
    if (state.volId != null) qs.set("vol", state.volId);
    if (state.dir) { qs.set("dir", state.dir); qs.set("recursive", "1"); }
    if (state.kind) qs.set("kind", state.kind);
    if (state.topic) qs.set("topic", state.topic);
    if (state.series) qs.set("series", state.series);
    if (state.author) qs.set("author", state.author);
    if (state.mark) qs.set("mark", state.mark);
    if (state.tag) qs.set("tag", state.tag);
    const res = await api("/api/list?" + qs);
    state.total = res.total || 0;
    const items = res.items || [];
    state.items.push(...items);
    state.offset += items.length;
    if (!items.length || state.offset >= state.total) state.done = true;

    setCrumbDir();
    const grid = $("#grid");
    if (first) grid.innerHTML = "";
    if (items.length) {
      // 全局页号（offset 本身就是全局偏移）；restoreChunk 用 itemsBase 换算局部下标
      const pageIdx = Math.floor(
        (state.offset - items.length) / Math.max(1, state.limit));
      if (state.mark && shelfMode()) {
        appendShelf(items);
      } else {
        if (state.mark && first && !$("#shelf-bar", grid)) {
          grid.insertAdjacentHTML("afterbegin", shelfBarHTML());
          $("#shelf-on", grid).onclick = () => toggleShelf(true);
          $("#shelf-off", grid).onclick = () => toggleShelf(false);
        }
        grid.insertAdjacentHTML("beforeend",
          items.map(it => cardHTML(it, pageIdx)).join(""));
        bindCards(grid);
        scheduleTrim();
      }
    }
    if (!state.items.length) {
      state.masonry = false;
      grid.className = "grid";
      grid.innerHTML = `<div class="empty"><div class="big">🔍</div>没有匹配的文件<br>
        <small>${state.q ? "搜索词：" + esc(state.q) : "换个分类或目录看看"}</small></div>`;
      sentinel.textContent = "";
    } else {
      // 结果集较大时始终提供跳页——尤其是已全部加载后，用户更需要直接跳转
      const loaded = state.itemsBase + state.items.length;
      const info = state.done
        ? `共 ${state.total.toLocaleString()} 个文件 · 已全部加载`
        : `已加载 ${loaded.toLocaleString()} / ${state.total.toLocaleString()} · 继续滚动`;
      sentinel.innerHTML = info + (state.total > state.limit * 3 ? `
        <span class="jump"><input id="jump-page" type="number" min="1"
              max="${Math.ceil(state.total / state.limit)}" placeholder="页码">
        <button id="jump-go" class="btn small">跳转</button></span>` : "");
      bindJump();
    }
  } catch (e) {
    sentinel.textContent = "加载失败：" + e.message;
  } finally {
    state.loading = false;
    // 追加后哨兵可能仍留在视口内（IO 只在交叉状态"变化"时通知）——
    // 此时主动补载一页，直到填出缓冲区，避免"停在最底部不再续载"
    requestAnimationFrame(() => {
      if (!state.done && !state.loading && state.items.length &&
          state.itemsBase + state.items.length < state.total) {
        const sr = $("#sentinel").getBoundingClientRect();
        if (sr.top < window.innerHeight + 700 && sr.bottom > -700) loadList(false);
      }
    });
  }
}

/* ---- 分块回收（虚拟滚动的保守版） ----
   列表滚到很深时，把离视口很远的整块卡片替换成等高占位，滚回来再原位恢复。
   收益：10 万级目录里持续下滚，DOM 卡片数恒定在 ~1000 以内，不掉帧。
   回收阈值(3200) > 恢复阈值(2400)，两者留出滞回区间避免反复摘装。 */
const CHUNK_TRIM_PX = 3200;
const CHUNK_KEEP_PX = 2400;
let _trimQueued = false;

function scheduleTrim() {
  if (_trimQueued) return;
  _trimQueued = true;
  requestAnimationFrame(() => { _trimQueued = false; trimChunks(); });
}

function trimChunks() {
  if (state.view !== "list" || state.masonry) return;   // 瀑布流是 CSS 多列布局，回收会引发整列重排
  if (state.mark && shelfMode()) return;                // 书架视图不用分块回收
  const grid = $("#grid");
  if (!grid || !grid.childElementCount) return;
  const map = new Map();
  grid.querySelectorAll("[data-chunk]").forEach(el => {
    const k = +el.dataset.chunk;
    let arr = map.get(k);
    if (!arr) map.set(k, arr = []);
    arr.push(el);
  });
  for (const [page, els] of map) {
    const r1 = els[0].getBoundingClientRect();
    const r2 = els[els.length - 1].getBoundingClientRect();
    const top = Math.min(r1.top, r2.top), bottom = Math.max(r1.bottom, r2.bottom);
    if (bottom >= -CHUNK_TRIM_PX) continue;              // 还在视口上方缓冲带内，保留
    const h = bottom - top;
    if (h <= 0) continue;
    const sp = document.createElement("div");
    sp.className = "vspacer";
    sp.dataset.chunk = page;
    sp.style.height = h + "px";
    els[0].replaceWith(sp);                              // 占位顶到块首，保持总高度与滚动位置
    for (const el of els) el.remove();
    spacerIO.observe(sp);
  }
}

const spacerIO = ("IntersectionObserver" in window)
  ? new IntersectionObserver(entries => {
      for (const en of entries) {
        if (!en.isIntersecting) continue;
        spacerIO.unobserve(en.target);
        restoreChunk(en.target);
      }
    }, { rootMargin: `${CHUNK_KEEP_PX}px 0px` })
  : null;

function restoreChunk(sp) {
  const page = +sp.dataset.chunk;
  const local = page * state.limit - state.itemsBase;   // 全局页号 → items 内局部下标
  const items = local >= 0 ? state.items.slice(local, local + state.limit) : [];
  if (!items.length) { sp.remove(); return; }
  const tmp = document.createElement("div");
  tmp.innerHTML = items.map(it => cardHTML(it, page)).join("");
  const frag = document.createDocumentFragment();
  while (tmp.firstChild) frag.appendChild(tmp.firstChild);
  sp.replaceWith(frag);
  bindCards($("#grid"));
  scheduleTrim();
}

/* ---- 跳页：超大结果集不必一路滚过去 ---- */
function bindJump() {
  const inp = $("#jump-page"), go = $("#jump-go");
  if (!inp || !go) return;
  const jump = () => {
    const p = parseInt(inp.value, 10);
    if (p >= 1) jumpToPage(p - 1);
  };
  go.onclick = jump;
  inp.onkeydown = e => { if (e.key === "Enter") jump(); };
}

function jumpToPage(pageIdx) {
  const maxPage = Math.max(0, Math.ceil(state.total / state.limit) - 1);
  pageIdx = Math.min(Math.max(0, pageIdx), maxPage);
  state.offset = pageIdx * state.limit;
  state.itemsBase = state.offset;
  state.items = [];
  state.done = false;
  state.loading = false;
  $("#grid").innerHTML = "";
  $("#main").scrollTop = 0;
  loadList(true);
}

function setCrumbDir() {
  const lib = currentLib();
  let html = lib
    ? `<span class="libtag">📚 ${esc(libLabel(lib))}</span>`
    : `<span class="libtag mixed">🗂 混合视图</span>`;
  const bits = [];
  if (state.mark) bits.push(MARK_LABEL[state.mark]);
  if (state.series) bits.push(`系列：${esc(state.series)}`);
  if (state.author) bits.push(`作者：${esc(state.author)}`);
  if (state.tag) bits.push(`标签：${esc(selTags().join(" ＋ "))}`);
  if (state.topic) bits.push(`主题：${esc(state.topic)}`);
  if (state.kind) bits.push(`类型：${KIND_LABEL[state.kind] || esc(state.kind)}`);

  if (state.dir) {
    html += ` <a data-go="">📁 根目录</a>`;
    let acc = "";
    for (const p of state.dir.split("/").filter(Boolean)) {
      acc += p + "/";
      html += ` <span class="sep">›</span> <a data-go="${esc(acc)}">${esc(p)}</a>`;
    }
  } else if (bits.length) {
    html += ` <span class="sep">›</span> ${bits.join(" · ")}`;
  } else {
    html += ` <span class="sep">›</span> 全部文件`;
  }
  if (state.q) html += ` · 搜索"${esc(state.q)}"`;
  html += ` <span class="cnt">${state.total.toLocaleString()} 个文件</span>
    <span style="margin-left:8px">${sortSelect()}</span>`;
  setCrumb(html);
  $$("#crumbs a[data-go]").forEach(a => a.onclick = () => {
    state.dir = a.dataset.go || "";
    refresh();
  });
  const sel = $("#sort-sel");
  if (sel) sel.onchange = () => { state.sort = sel.value; startList(); };
}
function sortSelect() {
  const opts = [["mtime_desc", "最新优先"], ["mtime_asc", "最早优先"],
    ["size_desc", "大文件优先"], ["size_asc", "小文件优先"],
    ["name_asc", "名称 A→Z"], ["name_desc", "名称 Z→A"]];
  return `<select id="sort-sel" class="btn small" style="padding:2px 6px">${
    opts.map(([v, t]) => `<option value="${v}" ${state.sort === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
}

/* ---- 卡片 ----
   chunk：所属分页序号（仅列表视图传入），供"分块回收"按块摘除/恢复 DOM */
function cardHTML(it, chunk) {
  const isVideo = it.kind === "video" || it.kind === "audio";
  const isImage = it.kind === "image";
  const isBook = it.kind === "book";
  const cover = it.thumb
    ? `<img loading="lazy" src="/thumb/${it.id}.jpg" alt="" data-ph="${esc(it.name || "")}" onerror="phErr(this,'${kindIcon(it.kind)}')">`
    : `<span>${kindIcon(it.kind)}</span>`;
  const badges = [];
  if (it.code) badges.push(`<span class="badge acc">${esc(it.code)}</span>`);
  if (it.quality && it.quality.length) badges.push(`<span class="badge">${esc([].concat(it.quality).join(" "))}</span>`);
  else if (isVideo && it.width) badges.push(`<span class="badge">${it.width}×${it.height || "?"}</span>`);
  if (it.duration) badges.push(`<span class="badge">${fmtDur(it.duration)}</span>`);
  const markBadge = it.mark
    ? `<span class="markbadge" data-mark="${esc(it.mark)}">${MARK_ICON[it.mark] || ""}</span>` : "";
  const canRead = READER_EXT.includes((it.ext || "").toLowerCase());
  const action = isVideo ? `<div class="pl">▶</div>`
    : isImage ? `<div class="pl read">🔍</div>`
    : canRead ? `<div class="pl read">📖</div>` : "";
  const sub = (isBook && it.author ? `<span>${esc(it.author)}</span>` : "") +
              `<span>${fmtSize(it.size)}</span>`;
  // 文档卡摘要行（全文索引时写入 meta.summary，重跑全文索引即有）
  const sumline = (it.kind === "doc" && it.summary)
    ? `<div class="sumline" title="${esc(it.summary)}">${esc(it.summary)}</div>` : "";
  return `
  <div class="card ${esc(it.kind)}" data-id="${it.id}" data-name="${esc(it.name)}" data-kind="${esc(it.kind)}"
       data-ext="${esc(it.ext || "")}"${chunk != null ? ` data-chunk="${chunk}"` : ""}>
    <div class="cover">${cover}${action}${markBadge}</div>
    <div class="meta">
      <div class="name" title="${esc(it.name)}">${esc(it.name)}</div>
      <div class="sub">${badges.join("")}${sub}</div>${sumline}
    </div>
  </div>`;
}

function itemOf(el) {
  const id = +el.dataset.id;
  return state.items.find(x => x.id === id)
    || { id, name: el.dataset.name, kind: el.dataset.kind, ext: el.dataset.ext };
}

// 卡片若在首页轨道里，返回该轨道的条目作为连播队列
function railQueueOf(el) {
  const rail = el && el.closest && el.closest("[data-rail]");
  if (!rail) return null;
  const items = (state.homeRails || {})[rail.dataset.rail];
  return items && items.length ? items : null;
}

function bindCards(root) {
  $$(".card[data-id]", root || document).forEach(c => {
    if (c._b) return;
    c._b = true;
    c.onclick = e => {
      if (LP_FIRED) { LP_FIRED = false; e.preventDefault(); e.stopPropagation(); return; }
      if (e.target.closest(".markbadge")) return;
      openDetail(+c.dataset.id);
    };
    c.ondblclick = e => { e.preventDefault(); quickOpen(itemOf(c), railQueueOf(c)); };
    c.oncontextmenu = e => { e.preventDefault(); showCtxMenu(e, itemOf(c)); };
    // 触屏长按 ≈ 电脑右键：500ms 触发同一套菜单
    let lpT = null, lpX = 0, lpY = 0;
    c.addEventListener("pointerdown", e => {
      if (e.pointerType !== "touch") return;
      lpX = e.clientX; lpY = e.clientY;
      clearTimeout(lpT);
      lpT = setTimeout(() => {
        lpT = null; LP_FIRED = true;
        showCtxMenu(e, itemOf(c));
      }, 500);
    });
    ["pointerup", "pointercancel"].forEach(ev =>
      c.addEventListener(ev, () => { clearTimeout(lpT); lpT = null; }));
    c.addEventListener("pointermove", e => {
      if (lpT && Math.hypot(e.clientX - lpX, e.clientY - lpY) > 12) {
        clearTimeout(lpT); lpT = null;   // 手指移动 = 滚动/滑动，不算长按
      }
    });
    const pl = c.querySelector(".pl");
    if (pl) pl.onclick = e => { e.stopPropagation(); quickOpen(itemOf(c), railQueueOf(c)); };
    const mb = c.querySelector(".markbadge");
    if (mb) mb.onclick = async e => {
      e.stopPropagation();
      await toggleMark(+c.dataset.id, mb.dataset.mark, false);
    };
  });
}

function quickOpen(it, queue) {
  if (!it) return;
  // 「播放/阅读」是独立权限项：被收回时降级为只看详情
  const wantsPlay = it.kind === "video" || it.kind === "audio" || it.kind === "image"
    || READER_EXT.includes((it.ext || "").toLowerCase());
  if (wantsPlay && !canPlay()) {
    openDetail(it.id);
    toast("没有播放/阅读权限，仅可查看信息");
    return;
  }
  if (it.kind === "video" || it.kind === "audio") return openPlayer(it.id, it.name, queue);
  if (it.kind === "image") return openLightbox(it.id);
  if (READER_EXT.includes((it.ext || "").toLowerCase())) return openReader(it.id, it.name);
  openDetail(it.id);
}

/* ---------------- 右键菜单 ---------------- */

function showCtxMenu(e, it) {
  const menu = $("#ctxmenu");
  const rows = [];
  if (it.kind === "video" || it.kind === "audio") rows.push(`<div data-act="play">▶ 播放</div>`);
  if (it.kind === "image") rows.push(`<div data-act="view">🔍 看大图</div>`);
  if (READER_EXT.includes((it.ext || "").toLowerCase()) && it.kind !== "video") {
    rows.push(`<div data-act="read">📖 阅读</div>`);
  }
  rows.push(`<div data-act="detail">ℹ 查看详情</div>`);
  if (canDownload()) rows.push(`<div data-act="download">⬇ 下载</div>`);
  rows.push(`<hr>`);
  if (isAdmin()) {
    rows.push(`<div data-act="star">⭐ 收藏 / 取消</div>`);
    rows.push(`<div data-act="todo">📌 待读 / 取消</div>`);
    rows.push(`<div data-act="read-mark">✅ 已读 / 未读</div>`);
    rows.push(`<hr>`);
  }
  // 本机打开/定位/复制路径：open/reveal 仅 admin（服务端仅允许 admin 触发
  // 本机外部程序），普通用户保留"复制完整路径"
  if (isAdmin()) {
    rows.push(`<div data-act="open">↗ 用本机程序打开</div>`);
    rows.push(`<div data-act="reveal">📍 在资源管理器中定位</div>`);
  }
  rows.push(`<div data-act="copy">📋 复制完整路径</div>`);
  if (isAdmin()) {
    rows.push(`<div data-act="delete" class="danger">🗑 移入回收站</div>`);
  }
  menu.innerHTML = rows.join("");
  menu.classList.remove("hidden");
  const w = menu.offsetWidth || 190, h = menu.offsetHeight || 200;
  menu.style.left = Math.max(4, Math.min(e.clientX, window.innerWidth - w - 8)) + "px";
  menu.style.top = Math.max(4, Math.min(e.clientY, window.innerHeight - h - 8)) + "px";

  menu.onclick = async ev => {
    const act = ev.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    menu.classList.add("hidden");
    try {
      if (act === "play") openPlayer(it.id, it.name, railQueueOf(e.target));
      else if (act === "view") openLightbox(it.id);
      else if (act === "read") openReader(it.id, it.name);
      else if (act === "detail") openDetail(it.id);
      else if (act === "star") await toggleMark(it.id, "star", !(it.marks || []).includes("star"));
      else if (act === "todo") await toggleMark(it.id, "todo", !(it.marks || []).includes("todo"));
      else if (act === "read-mark") await toggleMark(it.id, "read", !(it.marks || []).includes("read"));
      else if (act === "open") await api("/api/open", post({ id: it.id }));
      else if (act === "reveal") await api("/api/reveal", post({ id: it.id }));
      else if (act === "download") location.href = "/download/" + it.id + "." + (it.ext || "bin");
      else if (act === "copy") {
        const d = await api("/api/file?id=" + it.id);
        await navigator.clipboard.writeText(d.path);
        toast("路径已复制");
      } else if (act === "delete") {
        if (!confirm(`把「${it.name}」移入回收站？\n（可从回收站恢复）`)) return;
        const r = await api("/api/delete", post({ ids: [it.id] }));
        if (r.results[0].ok) {
          toast("已移入回收站");
          state.items = state.items.filter(x => x.id !== it.id);
          $$(`.card[data-id="${it.id}"]`).forEach(c => c.remove());
          loadStats(true).catch(() => {});
        } else alert(r.results[0].msg);
      }
    } catch (ex) { toast(ex.message, 2600); }
  };
}

/* ---------------- 标记 ---------------- */

function findItemAnywhere(id) {
  /* 列表与首页横滑轨（最近添加/继续看等）里的同一文件可能是不同对象，都要能找到 */
  return (state.items || []).find(x => x.id === id)
    || Object.values(state.homeRails || {}).flat().find(x => x && x.id === id) || null;
}

function paintCardMark(id) {
  /* 依据条目里的 marks 重画卡片角标（it.mark 为优先级单值：star > read > todo）。
     同一文件可能同时出现在列表和首页轨道（甚至两条轨道），每张同 id 卡片都要画 */
  const cards = $$(`.card[data-id="${id}"]`);
  if (!cards.length) return;
  const it = findItemAnywhere(id);
  const bm = it ? it.mark : null;
  cards.forEach(card => {
    card.querySelector(".markbadge")?.remove();
    if (!bm) return;
    card.querySelector(".cover").insertAdjacentHTML("beforeend",
      `<span class="markbadge" data-mark="${bm}">${MARK_ICON[bm]}</span>`);
    card.querySelector(".markbadge").onclick = async e => {
      e.stopPropagation();
      await toggleMark(id, bm, false);   // 点角标 = 取消这个标记（不是清空全部）
    };
  });
}

function syncItemMarks(id, marks) {
  const set = it => {
    it.marks = marks.slice();
    it.mark = marks.includes("star") ? "star" : marks.includes("read") ? "read"
      : marks.includes("todo") ? "todo" : null;
  };
  let hit = false;
  (state.items || []).forEach(x => { if (x.id === id) { set(x); hit = true; } });
  Object.values(state.homeRails || {}).forEach(arr =>
    (arr || []).forEach(x => { if (x && x.id === id) { set(x); hit = true; } }));
  paintCardMark(id);
  return hit;
}

async function toggleMark(id, mk, on) {
  /* 多标记勾选：star 可与 read/todo 并存，read/todo 互斥（服务端保证） */
  let marks;
  try {
    const r = await api("/api/mark", post({ id, mark: mk, on }));
    marks = r.marks || [];
  } catch (e) {
    if (e.status === 403) return toast("没有权限修改标记");
    throw e;
  }
  syncItemMarks(id, marks);
  if (on) toast("已标记" + MARK_LABEL[mk].slice(2));
  return marks;
}

/* ---------------- 详情 ---------------- */

let detailId = null;

async function openDetail(id) {
  detailId = id;
  $("#detail").classList.remove("hidden");
  const b = $("#detail-body");
  b.innerHTML = `<div class="empty">加载中…</div>`;
  let it;
  try { it = await api("/api/file?id=" + id); }
  catch (e) { b.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }

  const m = it.meta || {};
  const kv = (k, v) => (v || v === 0) ? `<div class="kv"><b>${k}</b><span>${esc(v)}</span></div>` : "";
  const cover = it.thumb ? `<img class="cover" src="/thumb/${it.id}.jpg" alt="" data-ph="${esc(it.name || "")}" onerror="phErr(this,'${kindIcon(it.kind)}')">` : "";
  const readable = READER_EXT.includes((it.ext || "").toLowerCase());
  const isMedia = it.kind === "video" || it.kind === "audio";
  const marks = it.marks || (it.mark ? [it.mark] : []);
  const MARK_BTNS = ["star", "todo", "read"].map(mk =>
    `<button class="btn small ${marks.includes(mk) ? "acc" : ""}" data-mk="${mk}">${MARK_LABEL[mk]}</button>`
  ).join("");
  const utags = (it.tags || []).filter(t => t.source === "user");
  const atags = (it.tags || []).filter(t => t.source !== "user");
  const stripAuto = s => String(s).replace(/^auto:/, "");
  const chips = utags.map(t =>
    isAdmin()
      ? `<span class="tagchip" data-rm="${esc(t.name)}">${esc(t.name)}<a class="x" title="移除">✕</a></span>`
      : `<span class="tagchip">${esc(t.name)}</span>`
  ).join("") || `<small style="color:var(--fg2)">还没有标签</small>`;
  const autoChips = atags.map(t => `<span class="tagchip auto">${esc(stripAuto(t.name))}</span>`).join("");
  const autoBlock = isAdmin()
    ? `<details class="autotags" id="autotags-wrap"${atags.length ? "" : " hidden"}>
         <summary>${atags.length} 个自动标签</summary>
         <div class="tagchips" id="autotags-box">${autoChips}</div></details>`
    : (atags.length
        ? `<details class="autotags"><summary>${atags.length} 个自动标签</summary>
             <div class="tagchips">${autoChips}</div></details>`
        : "");
  const lib = state.libs.find(l => l.id === it.vol_id);

  b.innerHTML = `
    ${cover}
    <h2>${esc(it.name)}</h2>
    ${kv("类型", (it.kind_label || KIND_LABEL[it.kind] || it.kind) + (it.ext ? " · ." + it.ext : ""))}
    ${kv("大小", fmtSize(it.size))}
    ${kv("修改时间", fmtTime(it.mtime))}
    ${it.duration ? kv("时长", fmtDur(it.duration)) : ""}
    ${it.width && it.height ? kv("分辨率", it.width + "×" + it.height) : ""}
    ${m.title && m.title !== it.name ? kv("书名", m.title) : ""}
    ${m.author ? kv("作者", m.author) : ""}
    ${m.publisher ? kv("出版社", m.publisher) : ""}
    ${m.year ? kv("年份", m.year) : ""}
    ${it.code ? kv("编号", it.code) : ""}
    ${it.series ? kv("系列", it.series) : ""}
    ${it.topic ? kv("主题", it.topic) : ""}
    ${it.quality && it.quality.length ? kv("版本", [].concat(it.quality).join(" · ")) : ""}
    ${kv("所属资料库", lib ? libLabel(lib) : (it.vol_id != null ? "库 #" + it.vol_id : ""))}
    ${it.status === "missing" ? `<div class="sec-note" style="margin:10px 0">⚠ 文件当前不在磁盘上（外接盘未连接或已移动）</div>` : ""}
    <div class="row marks">${MARK_BTNS}</div>
    ${isAdmin() ? `<div class="kv" style="display:block"><b>标签</b>
      <div class="tagchips" id="tagchips">${chips}</div>
      ${autoBlock}
      <input id="taginput" class="taginput" list="tag-suggest" placeholder="输入标签后回车，可连续添加">
      <datalist id="tag-suggest"></datalist></div>`
      : ((it.tags || []).length ? `<div class="kv" style="display:block"><b>标签</b>
         <div class="tagchips">${chips}</div>${autoBlock}</div>` : "")}
    ${it.media_progress && it.media_progress.pos > 5
      ? kv("上次看到", fmtDur(it.media_progress.pos) + " / " + fmtDur(it.media_progress.dur)) : ""}
    ${it.read_progress && it.read_progress.total
      ? kv("上次读到", "第 " + ((it.read_progress.page || 0) + 1) + " / " + it.read_progress.total) : ""}
    <div class="path">${esc(it.path)}</div>
    <div class="row">
      ${isMedia ? `<button class="btn acc" data-act="play">▶ 播放</button>` : ""}
      ${it.kind === "image" ? `<button class="btn acc" data-act="view">🔍 看大图</button>` : ""}
      ${readable && !isMedia ? `<button class="btn" data-act="read">📖 阅读</button>` : ""}
      <button class="btn" data-act="download">⬇ 下载</button>
      ${isAdmin() ? `<button class="btn" data-act="open">↗ 打开</button>
      <button class="btn" data-act="reveal">📍 定位</button>` : ""}
    </div>
    ${isMedia && NO_PLAY.has((it.ext || "").toLowerCase())
      ? `<div class="dim" style="font-size:11px;line-height:1.6">${TC_OK
          ? `⚠ 该格式（.${esc(it.ext)}）浏览器无法直接播放，点「播放」将自动启用实时转码（需服务端 CPU）`
          : `⚠ 该格式（.${esc(it.ext)}）浏览器通常无法在线播放，点「播放」若黑屏请用「下载」或管理员「打开」交给本地播放器`}</div>` : ""}
    ${isAdmin() && it.kind === "book"
      ? `<div class="row"><button class="btn small ghost" data-act="coverup">🖼 上传封面</button></div>` : ""}
  `;

  $$("[data-mk]", b).forEach(btn => btn.onclick = async () => {
    const mk = btn.dataset.mk;
    btn.disabled = true;
    try {
      const now = await toggleMark(it.id, mk, !marks.includes(mk));
      marks.length = 0;
      now.forEach(x => marks.push(x));
      $$("[data-mk]", b).forEach(x => x.classList.toggle("acc", marks.includes(x.dataset.mk)));
    } catch (e) {
      toast(e.message || "操作失败");
    } finally {
      btn.disabled = false;
    }
  });

  $$("[data-act]", b).forEach(btn => btn.onclick = async () => {
    const a = btn.dataset.act;
    try {
      if (a === "play") openPlayer(it.id, it.name);
      else if (a === "view") openLightbox(it.id);
      else if (a === "read") openReader(it.id, it.name);
      else if (a === "download") location.href = "/download/" + it.id + "." + (it.ext || "bin");
      else if (a === "open") await api("/api/open", post({ id: it.id }));
      else if (a === "reveal") await api("/api/reveal", post({ id: it.id }));
      else if (a === "coverup") pickCover(it.id);
    } catch (e) { toast(e.message, 2600); }
  });

  const inp = b.querySelector("#taginput");
  if (inp) {
    api(withVol("/api/tags?limit=80")).then(r => {
      const dl = b.querySelector("#tag-suggest");
      if (dl) dl.innerHTML = (r.items || []).map(t => `<option value="${esc(t.name)}">`).join("");
    }).catch(() => {});
    const userNames = () => (it.tags || []).filter(t => t.source === "user").map(t => t.name);
    const save = async () => {
      const r = await api("/api/set_tags", post({ id: it.id, tags: userNames() }));
      it.tags = r.tags;
      loadTags();
      renderChips();
    };
    const renderChips = () => {
      const box = $("#tagchips");
      if (!box) return;
      const ut = (it.tags || []).filter(t => t.source === "user");
      const at = (it.tags || []).filter(t => t.source !== "user");
      box.innerHTML = ut.map(t =>
        `<span class="tagchip" data-rm="${esc(t.name)}">${esc(t.name)}<a class="x">✕</a></span>`).join("")
        || `<small style="color:var(--fg2)">还没有标签</small>`;
      const wrap = $("#autotags-wrap");
      if (wrap) {
        wrap.hidden = !at.length;
        const sm = wrap.querySelector("summary");
        if (sm) sm.textContent = `${at.length} 个自动标签`;
        const ab = $("#autotags-box");
        if (ab) ab.innerHTML = at.map(t =>
          `<span class="tagchip auto">${esc(stripAuto(t.name))}</span>`).join("");
      }
      $$(".tagchip[data-rm]", box).forEach(c => c.querySelector(".x").onclick = async () => {
        it.tags = it.tags.filter(t => !(t.source === "user" && t.name === c.dataset.rm));
        await save();
      });
    };
    $$(".tagchip[data-rm]", b).forEach(c => c.querySelector(".x").onclick = async () => {
      it.tags = it.tags.filter(t => !(t.source === "user" && t.name === c.dataset.rm));
      await save();
    });
    inp.addEventListener("keydown", async e => {
      if (e.key !== "Enter" && e.key !== ",") return;
      e.preventDefault();
      const v = e.target.value.trim().replace(/[,，]+$/, "");
      e.target.value = "";
      if (!v || userNames().includes(v)) return;
      it.tags = [...(it.tags || []), { name: v, grp: "user", source: "user" }];
      await save();
    });
  }
}
function closeDetail() { $("#detail").classList.add("hidden"); }

function pickCover(id) {
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/*";
  inp.onchange = () => {
    const f = inp.files[0];
    if (!f) return;
    const fr = new FileReader();
    fr.onload = async () => {
      try {
        await api("/api/cover_upload", post({ id, data: fr.result }));
        toast("封面已更新");
        $$(`.card[data-id="${id}"] img, #detail-body img.cover`)
          .forEach(im => { im.src = "/thumb/" + id + ".jpg?t=" + Date.now(); });
        openDetail(id);
      } catch (e) { alert(e.message); }
    };
    fr.readAsDataURL(f);
  };
  inp.click();
}

/* ---------------- 播放器 ---------------- */

const PL = { id: null, name: "", ext: "", list: [], idx: -1, hideTimer: null,
             tc: false, tcBase: 0, tcDur: 0, tcPending: null, triedTC: false };

/* 浏览器原生几乎不可能播放的格式：详情/卡片里预先提示，播放失败再兜底 */
const NO_PLAY = new Set(("avi wmv rmvb rm flv f4v vob mpg mpeg mpe m2ts mts asf divx xvid "
  + "wma ape alac aiff aif dsf dff mka amr mid midi dat iso").split(" "));
/* 纯音频格式：播放器切换到音乐模式（不显示黑屏视频区） */
const AUD_EXT = new Set(("mp3 flac wav aac m4a ogg oga opus wma ape alac aiff aif "
  + "dsf dff mka amr mid midi").split(" "));

/* 实时转码（可选增强）：/api/me 返回 transcode=true（服务端探测到 ffmpeg）时启用。
 * TC_AUTO —— 浏览器基本必挂的老格式，打开播放器就直接走 /transcode/ 边转边播；
 * TC_RETRY —— 直连失败后降级再试转码的集合（多含 mkv/mov/webm 这类"看编码"的容器）。 */
let TC_OK = false;
const TC_AUTO = new Set(("avi wmv rmvb rm flv f4v vob mpg mpeg mpe m2ts mts ts asf ogv 3gp 3g2 divx xvid dat "
  + "wma ape alac aiff aif amr mka dsf dff").split(" "));
const TC_RETRY = new Set([...TC_AUTO, "mkv", "mov", "webm"]);

function showFallback() {
  const f = $("#pl-fallback");
  if (!f || !PL.id) return;
  $("#plf-title").textContent = PL.name || "";
  $("#plf-open").classList.toggle("hidden", !isAdmin());
  const tip = $("#plf-tip");
  if (tip) tip.textContent = TC_OK
    ? "MP4 (H.264)、WebM、MP3、FLAC、M4A、OGG 等主流格式可直接在线播放"
    : "提示：在程序目录 bin\\ 下放置 ffmpeg.exe（或安装到 PATH / filelib.json 配置 ffmpeg 路径）并重启服务，AVI/WMV 等老格式即可实时转码在线播放";
  f.classList.remove("hidden");
  $("#pl-play").textContent = "▶";
  setTcLoad(false);
}
function hideFallback() {
  const f = $("#pl-fallback");
  if (f) f.classList.add("hidden");
}

/* 转码流期间的实际时长/进度：fMP4 管道流拿不到总时长，用索引元数据补 */
function plDuration() {
  const v = video();
  if (!PL.tc) return v.duration || 0;
  return PL.tcDur || (isFinite(v.duration) ? v.duration : 0);
}
function plCurrent() {
  const v = video();
  /* 转码拖动/重启期间锁在目标位置：旧流的 timeupdate 若照常叠加会让进度条乱跳 */
  if (PL.tc && PL.tcPending != null) return PL.tcPending;
  return PL.tc ? PL.tcBase + (v.currentTime || 0) : (v.currentTime || 0);
}

let tcSeekTimer = null;
function tcSeek(sec) {
  /* 转码流没有字节范围，浏览器无法直接拖动：
   * 带着 -ss 重启 ffmpeg 从目标位置重新出流（350ms 防抖，连续拖动只发最后一次）。
   * tcPending 把进度条锁在目标位置，直到新流真的出帧（loadeddata）才恢复跟随。 */
  if (!PL.id) return;
  PL.tcBase = Math.max(0, sec);
  PL.tcPending = PL.tcBase;
  clearTimeout(tcSeekTimer);
  tcSeekTimer = setTimeout(() => {
    if (!PL.id) return;
    const v = video();
    const keepPlay = !v.paused;
    v.src = `/transcode/${PL.id}.${PL.ext || "bin"}?seek=${Math.floor(PL.tcBase)}&r=${Date.now()}`;
    v.load();
    const buf = $("#pl-buf");
    if (buf) buf.style.width = "0%";   // 旧流的缓冲区已作废，别再误导
    if (keepPlay) v.play().catch(() => {});
    paintSeek();
  }, 350);
}

/* 转码缓冲指示层：ffmpeg 启动/追帧期间显示（延迟 250ms 再出现，短暂停顿不闪烁） */
let tcLoadTimer = null;
function setTcLoad(on) {
  clearTimeout(tcLoadTimer);
  const el = $("#pl-tcload");
  if (!el) return;
  if (on) tcLoadTimer = setTimeout(() => el.classList.remove("hidden"), 250);
  else el.classList.add("hidden");
}

function openPlayer(id, name, queue) {
  PL.id = id;
  PL.name = name || "";
  hideFallback();
  // 连播队列来源：显式传入（首页轨道）> 已有队列且包含本片（上/下一集切换时不能丢）
  // > 当前视图条目（列表页播放）。首页轨道视图 state.items 为空，靠队列参数兜住。
  const src = (queue && queue.length) ? queue
    : (PL.list.some(x => x.id === id) ? PL.list : state.items);
  PL.list = src.filter(x => x.kind === "video" || x.kind === "audio");
  PL.idx = PL.list.findIndex(x => x.id === id);
  const cur = PL.list[PL.idx];
  const ext = (cur && cur.ext) || (String(name).split(".").pop() || "mp4");
  const extL = String(ext).toLowerCase();
  const isAud = AUD_EXT.has(extL);
  $("#player").classList.toggle("audio-mode", isAud);
  $("#pl-music").classList.toggle("hidden", !isAud);
  $("#pl-title").textContent = PL.name;
  const v = $("#pl-video");
  // 老格式且服务端有 ffmpeg → 直接走实时转码；其余直连（播放失败再降级转码）
  PL.tc = TC_OK && TC_AUTO.has(extL);
  PL.ext = extL;
  PL.triedTC = PL.tc;
  PL.tcBase = 0;
  PL.tcDur = 0;
  PL.tcPending = null;
  clearTimeout(tcSeekTimer);
  $("#pl-tcbadge").classList.toggle("hidden", !PL.tc);
  v.src = PL.tc ? `/transcode/${id}.${ext}?r=${Date.now()}` : `/media/${id}.${ext}`;
  $("#player").classList.remove("hidden");
  $("#player").classList.remove("mini");
  $("#pl-mini").classList.add("hidden");
  $("#pl-play").textContent = "▶";
  paintSeek();
  v.play().then(() => { $("#pl-play").textContent = "⏸"; }).catch(() => {
    hint("点击播放");
  });
  if (PL.tc) toast("老格式已启用实时转码播放", 2400);
  loadProgress(id);
  showCtrl();
  $("#pl-vol").value = v.volume;
}

let plLoadedFor = null;
async function loadProgress(id) {
  if (plLoadedFor === id) return;
  plLoadedFor = id;
  try {
    const d = await api("/api/file?id=" + id);
    if (PL.id !== id) return;   // 等待期间用户已切换/关闭
    const p = d.media_progress;
    /* 转码流拿不到总时长：优先服务端探测/索引的真实时长（meta），其次历史进度里的 dur */
    const realDur = (d.duration && d.duration > 0) ? d.duration
      : (p && p.dur > 0 ? p.dur : 0);
    if (realDur > 0 && PL.tc) { PL.tcDur = realDur; paintSeek(); }
    const v = $("#pl-video");
    if (p && p.pos > 5 && p.dur > 0 && p.pos < p.dur - 8) {
      if (PL.tc) tcSeek(p.pos);        // 转码流：直接从上次位置重启转码
      else v.currentTime = p.pos;
      toast("已从上次位置 " + fmtDur(p.pos) + " 继续播放", 2600);
    }
  } catch {}
}

function showCtrl() {
  const pl = $("#player");
  pl.classList.remove("idle");
  clearTimeout(PL.hideTimer);
  PL.hideTimer = setTimeout(() => {
    if (!$("#pl-video").paused) pl.classList.add("idle");
  }, 2800);
}
function hint(txt) {
  const h = $("#pl-hint");
  if (!h) return;
  h.textContent = txt;
  h.classList.add("show");
  clearTimeout(h._t);
  h._t = setTimeout(() => h.classList.remove("show"), 700);
}
function closePlayer() {
  saveProgress();
  PL.id = null;            // 先清 id：避免清 src 时误触 error → 兜底层
  clearTimeout(tcSeekTimer);
  PL.tc = false; PL.tcBase = 0; PL.tcDur = 0; PL.tcPending = null; PL.triedTC = false;
  setTcLoad(false);
  $("#pl-tcbadge").classList.add("hidden");
  video().pause();
  video().removeAttribute("src");
  video().load();
  hideFallback();
  $("#player").classList.remove("audio-mode", "mini");
  $("#pl-music").classList.add("hidden");
  $("#pl-mini").classList.add("hidden");
  $("#player").classList.add("hidden");
  plLoadedFor = null;
}
/* 收成底部迷你条：播放不中断（video 元素保留，只是视觉上收起）。
   音频挂机听歌、视频临时切走看资料都用它。 */
function minimizePlayer() {
  if (!PL.id) return closePlayer();
  saveProgress();
  $("#plm-title").textContent = PL.name;
  $("#plm-time").textContent = fmtDur(plCurrent());
  $("#plm-play").textContent = video().paused ? "▶" : "⏸";
  $("#player").classList.add("mini");
  $("#pl-mini").classList.remove("hidden");
  $("#player").classList.remove("idle");
}
/* ✕ / Esc 的行为：全屏且还在播 → 收进迷你条；已在迷你条或已暂停 → 直接关 */
function closeOrMinimize() {
  const mini = $("#player").classList.contains("mini");
  if (!mini && PL.id && !video().paused) minimizePlayer();
  else closePlayer();
}
function saveProgress() {
  if (!PL.id) return;
  const dur = plDuration();
  if (!dur || !isFinite(dur)) return;
  api("/api/progress", post({ id: PL.id, pos: plCurrent(), dur })).catch(() => {});
}

const video = () => $("#pl-video");
function togglePlay() {
  const v = video();
  if (v.paused) v.play().catch(() => {});
  else v.pause();
}
function seekBy(sec) {
  const dur = plDuration();
  if (!dur) return;
  const t = Math.max(0, Math.min(dur - 0.15, plCurrent() + sec));
  if (PL.tc) { tcSeek(t); paintSeek(); }
  else video().currentTime = t;
  hint((sec > 0 ? "+" : "") + Math.round(sec) + "s");
  showCtrl();
}
function plStep(d) {
  if (PL.list.length < 2) return toast("同目录没有更多视频");
  PL.idx = (PL.idx + d + PL.list.length) % PL.list.length;
  const it = PL.list[PL.idx];
  plLoadedFor = null;
  openPlayer(it.id, it.name);
}
function toggleFullscreen() {
  const el = $("#player");
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else (el.requestFullscreen || el.webkitRequestFullscreen || (() => {})).call(el);
  setTimeout(showCtrl, 200);
}

function paintSeek() {
  const v = video();
  const dur = plDuration();
  const cur = plCurrent();
  const pct = dur ? Math.min(100, (cur / dur) * 100) : 0;
  const fill = $("#pl-seek");
  if (fill) fill.style.width = pct + "%";
  const knob = $("#pl-knob");
  if (knob) knob.style.left = pct + "%";
  const t = $("#pl-time");
  if (t) t.textContent = `${fmtDur(cur)} / ${dur ? fmtDur(dur) : "--:--"}`;
  /* 转码重启期间旧流缓冲已作废，不刷新缓冲条 */
  if (!(PL.tc && PL.tcPending != null)) try {
    if (v.buffered.length && dur) {
      const b = v.buffered.end(v.buffered.length - 1);
      const buf = $("#pl-buf");
      if (buf) buf.style.width = Math.min(100, ((PL.tc ? PL.tcBase : 0) + b) / dur * 100) + "%";
    }
  } catch {}
}

/* ---------------- 图片大图 ---------------- */

let lbList = [], lbIndex = -1;
const LB_SLIDE = { on: false, t: null, ms: 3500 };

function openLightbox(id) {
  lbList = state.items.filter(x => x.kind === "image");
  if (!lbList.length) {
    const it = state.items.find(x => x.id === id);
    lbList = it ? [it] : [{ id, name: "", ext: "jpg", size: 0 }];
  }
  lbIndex = lbList.findIndex(x => x.id === id);
  if (lbIndex < 0) lbIndex = 0;
  $("#lightbox").classList.remove("hidden");
  showLb();
}
function lbSlideNext() {
  if (!LB_SLIDE.on) return;
  lbIndex = (lbIndex + 1) % lbList.length;
  showLb();
}
function lbSlideArm() {
  clearTimeout(LB_SLIDE.t);
  if (LB_SLIDE.on) LB_SLIDE.t = setTimeout(lbSlideNext, LB_SLIDE.ms);
}
function lbSlideToggle() {
  LB_SLIDE.on = !LB_SLIDE.on;
  const b = $("#lb-slide");
  b.textContent = LB_SLIDE.on ? "⏸ 轮播" : "▶ 轮播";
  b.classList.toggle("on", LB_SLIDE.on);
  if (LB_SLIDE.on) lbSlideNext();   // 开启立即切到下一张，用户能感知生效
  else clearTimeout(LB_SLIDE.t);
}
function showLb() {
  if (!lbList.length) return closeLightbox();
  lbIndex = (lbIndex + lbList.length) % lbList.length;
  const it = lbList[lbIndex];
  const img = $("#lb-img");
  img.classList.remove("zoom");
  img.src = `/media/${it.id}.${it.ext || "jpg"}`;
  $("#lb-title").textContent = `${it.name} · ${lbIndex + 1}/${lbList.length} · ${fmtSize(it.size)}`;
  $("#lb-dl").onclick = () => { location.href = "/download/" + it.id + "." + (it.ext || "jpg"); };
  // 预加载下一张，轮播切换不闪白
  const nxt = lbList[(lbIndex + 1) % lbList.length];
  if (nxt && nxt.id) { const im = new Image(); im.src = `/media/${nxt.id}.${nxt.ext || "jpg"}`; }
  lbSlideArm();
}
function closeLightbox() {
  LB_SLIDE.on = false;
  clearTimeout(LB_SLIDE.t);
  const b = $("#lb-slide");
  if (b) { b.textContent = "▶ 轮播"; b.classList.remove("on"); }
  $("#lightbox").classList.add("hidden");
  $("#lb-img").removeAttribute("src");
  lbList = []; lbIndex = -1;
}

/* ---------------- 阅读器 ---------------- */

const RD = { id: null, name: "", kind: null, pages: [], page: 0, chapters: [], ch: 0, fontSize: 17 };

async function openReader(id, name, jumpQ) {
  Object.assign(RD, { id, name: name || "", kind: null, pages: [], page: 0, chapters: [], ch: 0 });
  ttsStop();                                  // 换书/重开先停掉上一本的朗读
  $("#rd-tts").classList.add("hidden");
  $("#rd-tts-rate").classList.add("hidden");
  RD.lastBeat = Date.now();
  $("#rd-title").textContent = RD.name;
  $("#rd-body").innerHTML = `<div class="empty">加载中…</div>`;
  $("#rd-list").innerHTML = "";
  $("#rd-side").classList.add("hidden");
  $("#rd-progress-fill").style.width = "0";
  $("#reader").classList.remove("hidden");

  try {
    const info = await api("/api/read?id=" + id);
    if (info.kind === "text") {
      RD.kind = "text";
      splitPages(info.text);
      $("#rd-tts").classList.remove("hidden");
      $("#rd-tts-rate").classList.remove("hidden");
      let start = 0, jumped = false;
      if (jumpQ) {
        // 全文搜索进入：定位到命中页并高亮，优先于"上次阅读位置"
        const needle = jumpQ.trim();
        start = RD.pages.findIndex(p => p.includes(needle));
        if (start < 0) start = RD.pages.findIndex(p => p.includes(needle.replace(/\s+/g, "")));
        jumped = start >= 0;
        if (start < 0) start = 0;
      }
      if (!jumped) {
        try {
          const d = await api("/api/file?id=" + id);
          if (d.read_progress && d.read_progress.page) start = d.read_progress.page;
        } catch {}
      }
      RD.page = Math.min(start, Math.max(0, RD.pages.length - 1));
      renderPage(jumped ? jumpQ.trim() : null);
      if (jumped) toast("已定位到搜索命中的位置", 2400);
      else if (start > 0) toast("已跳到上次读到的位置", 2400);
      return;
    }
    if (info.kind === "pdf") {
      RD.kind = "pdf";
      $("#rd-body").innerHTML = `<iframe class="pdf-frame" src="${info.url}#view=FitH"></iframe>`;
      $("#rd-page").textContent = "PDF";
      $("#rd-progress-fill").style.width = "100%";
      if (jumpQ) toast("PDF 暂不支持自动跳转，可用阅读器的查找功能搜索", 3200);
      return;
    }
    const eb = await api("/api/ebook?id=" + id);
    if (eb.kind === "chapters" && eb.chapters.length) {
      RD.kind = "chapters";
      RD.chapters = eb.chapters;
      $("#rd-tts").classList.remove("hidden");
      $("#rd-tts-rate").classList.remove("hidden");
      $("#rd-title").textContent = eb.title || RD.name;
      $("#rd-list").innerHTML = eb.chapters.map((c, i) =>
        `<li data-ch="${i}" class="${i === 0 ? "on" : ""}">${esc(c.title || ("第 " + (i + 1) + " 节"))}</li>`).join("");
      $$("#rd-list li").forEach(li => li.onclick = () => {
        RD.ch = +li.dataset.ch;
        renderChapter();
        if (isMobile()) $("#rd-side").classList.add("hidden");
      });
      let jumped = false;
      if (jumpQ) {
        const needle = jumpQ.trim();
        RD.ch = RD.chapters.findIndex(c => (c.text || "").includes(needle));
        if (RD.ch < 0) RD.ch = RD.chapters.findIndex(c => (c.text || "").includes(needle.replace(/\s+/g, "")));
        jumped = RD.ch >= 0;
        if (RD.ch < 0) RD.ch = 0;
      }
      if (!jumped) {
        // 恢复上次读到的章节
        try {
          const d = await api("/api/file?id=" + id);
          if (d.read_progress && d.read_progress.page) {
            RD.ch = Math.min(d.read_progress.page, RD.chapters.length - 1);
            toast("已跳到上次读到的章节", 2400);
          }
        } catch {}
      }
      renderChapter(jumped ? jumpQ.trim() : null);
      if (jumped) toast("已定位到搜索命中的章节", 2400);
      return;
    }
    $("#rd-body").innerHTML = `<div class="empty"><div class="big">📖</div>
      这个格式暂时不能内嵌阅读<br><br>
      <button class="btn acc" id="rd-dl2">⬇ 下载 / 用本机阅读器打开</button></div>`;
    $("#rd-dl2").onclick = () => {
      const ext = RD.name.split(".").pop() || "bin";
      if (isAdmin()) {
        api("/api/open", post({ id })).catch(() => { location.href = "/download/" + id + "." + ext; });
      } else location.href = "/download/" + id + "." + ext;
    };
  } catch (e) {
    $("#rd-body").innerHTML = `<div class="empty"><div class="big">⚠</div>${esc(e.message)}<br>
      <small>${e.status === 424 ? "安装 Calibre 后重启程序即可在线阅读 MOBI/AZW3" : ""}</small></div>`;
  }
}

function splitPages(text) {
  const CHUNK = 2600;
  const pages = [];
  let buf = "";
  for (const p of String(text).split(/\n/)) {
    buf += p + "\n";
    if (buf.length >= CHUNK) { pages.push(buf); buf = ""; }
  }
  if (buf.trim()) pages.push(buf);
  RD.pages = pages.length ? pages : ["（空文件）"];
}
function hiText(raw, q) {
  const escd = esc(raw);
  if (!q) return escd;
  // 先转义再高亮：对转义后的文本做子串替换（q 也转义对齐）
  const eq = esc(q);
  if (!eq) return escd;
  return escd.split(eq).join("<mark>" + eq + "</mark>");
}
function scrollToMark() {
  const m = $("#rd-body mark");
  if (m) m.scrollIntoView({ block: "center" });
}
function renderPage(q) {
  if (!RD.pages.length) return;
  RD.page = Math.max(0, Math.min(RD.page, RD.pages.length - 1));
  $("#rd-body").innerHTML =
    `<div class="reader-text" style="font-size:${RD.fontSize}px">${hiText(RD.pages[RD.page], q)}</div>`;
  $("#rd-body").scrollTop = 0;
  if (q) scrollToMark();
  $("#rd-page").textContent = `${RD.page + 1} / ${RD.pages.length}`;
  $("#rd-progress-fill").style.width = ((RD.page + 1) / RD.pages.length * 100) + "%";
  saveReadProgress();
  if (TTS.on) ttsSpeakFromCurrent();   // 朗读中翻页：跟着读新页
}
function renderChapter(q) {
  const c = RD.chapters[RD.ch];
  if (!c) return;
  $("#rd-body").innerHTML =
    `<div class="reader-text" style="font-size:${RD.fontSize}px">${hiText(c.text || "", q)}</div>`;
  $("#rd-body").scrollTop = 0;
  if (q) scrollToMark();
  $("#rd-title").textContent = c.title || RD.name;
  $("#rd-page").textContent = `${RD.ch + 1} / ${RD.chapters.length}`;
  $("#rd-progress-fill").style.width = ((RD.ch + 1) / RD.chapters.length * 100) + "%";
  $$("#rd-list li").forEach((li, i) => li.classList.toggle("on", i === RD.ch));
  saveReadProgress();
  if (TTS.on) ttsSpeakFromCurrent();   // 朗读中切章：跟着读新章
}
const saveReadProgress = debounce(() => {
  if (!RD.id) return;
  if (RD.kind === "text") {
    api("/api/progress", post({ id: RD.id, page: RD.page, total: RD.pages.length })).catch(() => {});
  } else if (RD.kind === "chapters") {
    api("/api/progress", post({ id: RD.id, page: RD.ch, total: RD.chapters.length })).catch(() => {});
  }
}, 1200);

function rdNext() {
  if (RD.kind === "text") { RD.page++; renderPage(); }
  else if (RD.kind === "chapters") { RD.ch = Math.min(RD.ch + 1, RD.chapters.length - 1); renderChapter(); }
}
function rdPrev() {
  if (RD.kind === "text") { RD.page--; renderPage(); }
  else if (RD.kind === "chapters") { RD.ch = Math.max(0, RD.ch - 1); renderChapter(); }
}
function closeReader() {
  ttsStop();
  beatRead();                              // 把最后一段不足 30 秒的余量补进统计
  $("#reader").classList.add("hidden");
  $("#rd-body").innerHTML = "";
  Object.assign(RD, { id: null, kind: null, pages: [], chapters: [] });
}

/* ---------------- TTS 听书（浏览器原生语音，零依赖） ---------------- */

const TTS = { on: false, rate: 1, queue: [] };
const TTS_RATES = [1, 1.25, 1.5, 2, 0.75];

function ttsText() {
  if (RD.kind === "text") return RD.pages[RD.page] || "";
  if (RD.kind === "chapters") return (RD.chapters[RD.ch] || {}).text || "";
  return "";
}
function ttsToggle() { TTS.on ? ttsStop() : ttsStart(); }
function ttsStart() {
  if (!("speechSynthesis" in window)) return toast("此浏览器不支持语音朗读");
  TTS.on = true;
  $("#rd-tts").textContent = "⏸";
  ttsSpeakFromCurrent();
}
/* 把当前页/章文本切成短句队列（过长的 utterance 在部分浏览器会卡住） */
function ttsSpeakFromCurrent() {
  const txt = ttsText().trim();
  $("#rd-tts").textContent = TTS.on ? "⏸" : "🔊";
  if (!TTS.on || !txt) { if (!txt) ttsStop(); return; }
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  TTS.queue = [];
  txt.split(/(?<=[。！？!?；;])/).forEach(seg => {
    seg = seg.trim();
    if (!seg) return;
    while (seg.length > 240) {
      let cut = seg.lastIndexOf("，", 240);
      if (cut < 120) cut = 240;
      TTS.queue.push(seg.slice(0, cut));
      seg = seg.slice(cut);
    }
    TTS.queue.push(seg);
  });
  ttsNext();
}
function ttsNext() {
  if (!TTS.on) return;
  const seg = TTS.queue.shift();
  if (seg == null) {
    // 本页/章读完 → 自动翻页接着读；到头了就停
    const more = RD.kind === "text"
      ? RD.page < RD.pages.length - 1
      : RD.kind === "chapters" && RD.ch < RD.chapters.length - 1;
    if (more) { rdNext(); return; }        // rdNext → renderPage/Chapter → 重新入队
    ttsStop();
    return;
  }
  const u = new SpeechSynthesisUtterance(seg);
  u.rate = TTS.rate;
  u.lang = "zh-CN";
  u.onend = () => ttsNext();
  u.onerror = () => ttsNext();
  speechSynthesis.speak(u);
}
function ttsStop() {
  const was = TTS.on;
  TTS.on = false;
  TTS.queue = [];
  if ("speechSynthesis" in window) try { speechSynthesis.cancel(); } catch {}
  const b = $("#rd-tts");
  if (b) b.textContent = "🔊";
  return was;
}
function ttsCycleRate() {
  const i = TTS_RATES.indexOf(TTS.rate);
  TTS.rate = TTS_RATES[(i + 1) % TTS_RATES.length];
  $("#rd-tts-rate").textContent = TTS.rate + "×";
  toast("朗读语速 " + TTS.rate + "×");
  // 正在朗读时重读当前句，让新语速立即生效
  if (TTS.on) ttsSpeakFromCurrent();
}

/* ---------------- 阅读时长记录（打卡热力图数据源） ----------------
   阅读器开着 + 页面可见时每 30 秒打一拍；关阅读器时把零头补上。 */
function beatRead() {
  if (!RD.id || !RD.lastBeat) return;
  const el = (Date.now() - RD.lastBeat) / 1000;
  if (el >= 8) api("/api/readlog", post({ id: RD.id, secs: Math.min(120, Math.round(el)) })).catch(() => {});
  RD.lastBeat = Date.now();
}
setInterval(() => {
  if (!RD.id) return;
  if ($("#reader").classList.contains("hidden")) return;
  if (document.visibilityState !== "visible") return;   // 挂后台不计时
  api("/api/readlog", post({ id: RD.id, secs: 30 })).catch(() => {});
  RD.lastBeat = Date.now();
}, 30000);

/* ---------------- 老板键（摸鱼伪装层） ----------------
   任意界面按 ~ 键瞬间变成一个假的代码编辑器；
   正在看的书会以「代码注释」的形式藏在里面。Esc 或再按 ~ 返回。 */

let BOSS_BUILT = false, BOSS_TITLE = "";

function toggleBoss(on) {
  const el = $("#boss");
  if (!el) return;
  const show = on === undefined ? el.classList.contains("hidden") : !!on;
  if (show && !BOSS_BUILT) { buildBoss(); BOSS_BUILT = true; }
  el.classList.toggle("hidden", !show);
  if (show) {
    if (TTS.on) ttsStop();
    BOSS_TITLE = document.title;
    document.title = "quarterly_report.py - mywork - Visual Studio Code";
  } else if (BOSS_TITLE) {
    document.title = BOSS_TITLE;
    BOSS_TITLE = "";
  }
}
function buildBoss() {
  const d = new Date();
  const p = n => String(n).padStart(2, "0");
  const ts = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  // 藏私货：正在读的内容以注释形式出现在"代码"里，回来还在原处
  const hidden = (RD.id ? (RD.kind === "text" ? RD.pages[RD.page]
    : (RD.chapters[RD.ch] || {}).text) : "") || "";
  const body = (hidden.trim() || "def sync_index(root):\n    for path in walk(root):\n        yield path\n").slice(0, 1600);
  const code = body.split("\n").slice(0, 40).map(l =>
    "    # " + l.replace(/\*\//g, "*_/").slice(0, 110)).join("\n");
  const tree = [
    ["▾ src", 0], ["  core", 1], ["  indexer.py", 1], ["  report.py", 1],
    ["  sync.py", 1], ["  utils.py", 1], ["▾ tests", 0], ["  test_main.py", 1],
    ["  conftest.py", 1], ["README.md", 0], ["requirements.txt", 0],
  ].map(([t, lv], i) =>
    `<div class="bs-f${i === 3 ? " on" : ""}" style="padding-left:${8 + lv * 14}px">${esc(t)}</div>`).join("");
  $("#boss").innerHTML = `
  <div class="bs-title">
    <span class="bs-dot r"></span><span class="bs-dot y"></span><span class="bs-dot g"></span>
    <span class="bs-file">report.py — mywork</span>
    <span class="bs-clock">${ts}</span>
  </div>
  <div class="bs-body">
    <aside class="bs-side">${tree}</aside>
    <div class="bs-main">
      <div class="bs-tabs"><span class="bs-tab on">report.py</span><span class="bs-tab">sync.py</span><span class="bs-tab">utils.py</span></div>
      <pre class="bs-code"><span class="bs-ln">${code.split("\n").map((_, i) => i + 1).join("\n")}</span>${esc(code)}
<span class="bs-cursor"></span></pre>
      <div class="bs-status">⎇ main*   UTF-8   Python   Ln 42, Col 17   Spaces: 4</div>
    </div>
  </div>
  <div class="bs-hint">按 ~ 返回 FileLib</div>`;
}

/* ---------------- 全文搜索 ---------------- */

async function runFulltext() {
  const box = $("#ft-box");
  const q = state.q.trim();
  if (!q) return;
  box.classList.remove("hidden");
  box.innerHTML = `<div class="ft-head">🔎 正在搜索「${esc(q)}」的正文…</div>`;
  try {
    const qs = new URLSearchParams({ q, limit: "40" });
    if (state.volId != null) qs.set("vol", state.volId);
    const r = await api("/api/fulltext?" + qs);
    const items = r.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="ft-head">🔎 正文里没有找到「${esc(q)}」
        <button class="btn small ghost" style="float:right" id="ft-close">收起</button></div>
        <div class="ft-item"><div class="ft-snip">可能还没建全文索引。${isAdmin()
          ? "去左侧「维护 → 建全文索引」跑一次（TXT/PDF/EPUB/MOBI 都能提取）。"
          : "请联系管理员建立全文索引。"}</div></div>`;
      $("#ft-close").onclick = () => box.classList.add("hidden");
      return;
    }
    box.innerHTML = `<div class="ft-head">📖 正文命中 ${items.length} 处
        <small>（文件内容与「${esc(q)}」有关）</small>
        <button class="btn small ghost" style="float:right" id="ft-close">收起</button></div>` +
      items.map(it => `<div class="ft-item" data-id="${it.id}">
        <div class="ft-name">${kindIcon(it.kind)} ${esc(it.name)}</div>
        <div class="ft-snip">${it.snippet || ""}</div></div>`).join("");
    $$(".ft-item[data-id]", box).forEach(el => el.onclick = () => {
      const id = +el.dataset.id;
      const it = state.items.find(x => x.id === id);
      openReader(id, it ? it.name : el.querySelector(".ft-name").textContent.replace(/^\S+\s/, ""), q);
    });
    $("#ft-close").onclick = () => box.classList.add("hidden");
  } catch (e) {
    box.innerHTML = `<div class="ft-head">搜索失败：${esc(e.message)}</div>`;
  }
}

/* ---------------- 后台任务 ---------------- */

let pollTimer = null;
function pollTask() {
  clearInterval(pollTimer);
  const tick = async () => {
    if (!ME.logged) return;
    let t;
    try { t = await api("/api/task"); } catch { return; }
    const bar = $("#taskbar");
    if (t.state === "running") {
      bar.dataset.was = "1";
      bar.classList.remove("hidden");
      const names = { scan: "⟳ 更新索引", thumbs: "🖼 生成封面", fulltext: "🔎 建全文索引", dedup: "⧉ 查重复", organize: "⚡ 一键整理", cleanup: "🧹 清理数据", rethumb: "🖼 重做缩略图", autotag: "🏷 自动打标签" };
      $("#tb-title").textContent = names[t.name] || t.name || "任务";
      $("#tb-msg").textContent = t.message || "";
      const p = t.progress || {};
      let pct = 0;
      if (p.total) pct = Math.min(100, ((p.i ?? p.done ?? 0) / p.total) * 100);
      else if (p.scanned) pct = 45;
      $("#tb-fill").style.width = pct + "%";
      const pend = Object.entries(t.pending || {})
        .filter(([, v]) => v.pending > 0).map(([k, v]) => `${k} ${v.pending}`).join(" · ");
      $("#tb-count").textContent = pend;
    } else {
      if (bar.dataset.was === "1") {
        bar.dataset.was = "";
        if (t.state === "done") {
          toast("任务完成：" + (t.message || ""), 3000);
          if (t.name === "scan" || t.name === "thumbs" || t.name === "organize") {
            state.tree = null;
            loadStats(true).then(() => {
              loadTree();
              if (state.view === "home") render();
            });
            if (t.name === "organize") showDups(false).catch(() => {}); // 顺手展示去重结果
          } else if (t.name === "dedup") {
            showDups(false).catch(() => {});   // 去重分析完成后自动展示结果
          } else if (t.name === "autotag") {
            loadTags();                        // 打标签完成后侧栏标签云立即出现
          }
        } else if (t.state === "error") {
          toast("任务失败：" + (t.message || ""), 6000);
        }
      }
      bar.classList.add("hidden");
    }
  };
  tick();                        // 启动任务后立刻查一次，进度条马上可见
  pollTimer = setInterval(tick, 1500);
}

/* ---------------- 维护动作 ---------------- */

// 兜底：维护任务可能几秒内就跑完（甚至启动前就无事可做），
// 任务条来不及出现。启动后延迟 1.2s 回查一次结果并弹提示。
function watchTaskOnce() {
  setTimeout(async () => {
    try {
      const t = await api("/api/task");
      if (t.state === "done") {
        toast("任务完成：" + (t.message || ""), 4000);
        if (t.name === "dedup") showDups(false).catch(() => {});  // 秒完成时进度条来不及出现，直接弹结果
      }
      else if (t.state === "error") toast("任务失败：" + (t.message || ""), 6000);
      /* 还在 running 就交给 pollTask 的进度条 */
    } catch {}
  }, 1200);
}

async function doScan(root) {
  const lib = currentLib();
  if (!root) {
    if (!lib) {
      const vols = (STATS && STATS.volumes) || [];
      if (!vols.length) return addLibrary();  // 还没有任何库：⟳ 直接引导添加
      return toast("先选一个资料库");
    }
    if (!lib.online) return alert("目录当前不可访问（外接盘没插？）\n" + (lib.root_path || lib.root));
    if (!confirm(`重新扫描这个资料库？\n\n${lib.root_path || lib.root}\n\n已有索引不会丢，只更新变化的部分。`)) return;
    root = lib.root_path || lib.root;
  }
  try {
    await api("/api/scan", post({ root }));
    toast("开始扫描…");
    watchTaskOnce();
  } catch (e) { alert(e.message); }
}
async function doOrganize(root) {
  const scope = root ? `目录：${root}` : "所有已登记的资料库";
  if (!confirm(`一键整理（推荐）\n\n作用范围：${scope}\n\n依次执行：\n  1. ⟳ 增量扫描（只更新变化）\n  2. 🖼 补齐缺失封面\n  3. 🔎 建全文索引\n  4. ⧉ 去重分析\n\n大库需要一些时间，可随时停止。继续？`)) return;
  try {
    await api("/api/organize", post(root ? { root } : {}));
    toast("一键整理已开始…");
    watchTaskOnce();
  } catch (e) { alert(e.message); }
}
async function addLibrary() {
  const last = (() => { try { return localStorage.getItem("filelib.lastlib") || ""; } catch { return ""; } })();
  const root = await uiPrompt("添加资料库", {
    hint: "填一个文件夹的完整路径，例如 E:\\0000 书\\我的书库。扫描只读取文件信息，不会改动它们。",
    value: last,
    placeholder: "E:\\0000 书\\我的书库",
  });
  if (!root) return;
  const p = root.trim().replace(/^"|"$/g, "");
  if (!p) return;
  try { localStorage.setItem("filelib.lastlib", p); } catch {}
  await doScan(p);
}
/* 缩略图兜底：失败/未生成时显示书脊色块占位（首字 + 类型图标），替代光秃秃的 emoji */
function phErr(im, icon) {
  const t = (im.dataset.ph || "").trim();
  const ch = t ? [...t][0] : icon;
  im.outerHTML = `<span class="ph"><b>${esc(ch)}</b><i>${icon}</i></span>`;
}
async function doThumbs(fix) {
  try {
    if (fix) await api("/api/cover_fix", post({ retry_failed: true }));
    else {
      const w = Math.min(8, Math.max(1, +(prompt(
        "生成封面 / 缩略图\n\n并发线程数 1~8：越大越快、CPU 占用越高（回车默认 6）：", "6") || 6)));
      await api("/api/thumbs", post({ workers: w }));
    }
    toast("已开始生成封面 / 缩略图");
    watchTaskOnce();
  } catch (e) { alert(e.message); }
}
async function doRethumb() {
  if (!confirm("缩略图重压缩？\n\n把已生成的缩略图按新参数（更小尺寸 / 更低质量系数）重编码，通常可省 40% 左右空间。\n直接读旧缩略图处理、不重新扫原文件，速度很快；处理中的文件不会损坏，可随时停止。")) return;
  try {
    await api("/api/rethumb", post({}));
    toast("缩略图重压缩已开始…");
    watchTaskOnce();
  } catch (e) { alert(e.message); }
}
async function doFulltext() {
  if (!confirm("为书库建立全文索引？\n\n会把 TXT/PDF/EPUB/MOBI 的正文提取进索引库，之后搜索能命中书里的内容。\n大库要跑较久，可随时停止。")) return;
  try {
    await api("/api/fulltext_build", post({}));
    toast("开始建全文索引…");
    watchTaskOnce();
  } catch (e) { alert(e.message); }
}
async function doAutotag() {
  if (!confirm("自动打标签？\n\n按文件名/目录规则 + 内嵌元数据（音频 ID3、EPUB、图片 EXIF）+ 正文关键词，自动生成标签。\n标签带「自动」标记，人工标签不受影响；大库要跑较久，可随时停止。")) return;
  try {
    await api("/api/autotag", post({}));
    toast("自动打标签已开始…");
    watchTaskOnce();
  } catch (e) { alert(e.message); }
}
async function doBackup() {
  try {
    const r = await api("/api/backup", post({}));
    if (r.ok) alert("备份完成：\n" + r.path);
    else alert("备份失败（索引库不存在？）");
  } catch (e) { alert(e.message); }
}
async function openDataDir() {
  try {
    const d = await api("/api/admin/overview");
    alert(`索引与缓存都在这里：\n${d.data_dir}\n\n（删掉这个目录 = 彻底清除所有数据，磁盘上的原始文件不受影响）`);
  } catch {}
}

function openToolsMenu(e) {
  e.stopPropagation();
  const m = $("#toolsmenu");
  if (!m.classList.contains("hidden")) return m.classList.add("hidden");
  m.innerHTML = `
    <div class="ti acc" data-t="organize">⚡ 一键整理<small>扫描→封面→全文→去重</small></div>
    <div class="ti" data-t="scan">⟳ 更新索引<small>增量</small></div>
    <div class="ti" data-t="thumbs">🖼 补封面 / 缩略图</div>
    <div class="ti" data-t="coverfix">📕 重试失败的书封</div>
    <div class="ti" data-t="rethumb">🗜 缩略图重压缩<small>省约 40% 空间</small></div>
    <div class="ti" data-t="fulltext">🔎 建全文索引</div>
    <div class="ti" data-t="autotag">🏷 自动打标签</div>
    <div class="ti" data-t="dups">⧉ 查重复<small>先分析</small></div>
    <div class="ti" data-t="crossdup">🔁 跨库重复对比</div>
    <div class="ti" data-t="junk">🧹 清理建议</div>
    <div class="ti" data-t="backup">💾 备份索引</div>
    <div class="ti" data-t="library">📚 资料库管理</div>
    <div class="ti" data-t="datadir">📂 数据目录</div>`;
  m.classList.remove("hidden");
  m.onclick = ev => {
    const t = ev.target.closest("[data-t]")?.dataset.t;
    m.classList.add("hidden");
    if (!t) return;
    if (t === "organize") doOrganize();
    if (t === "scan") doScan();
    else if (t === "thumbs") doThumbs(false);
    else if (t === "coverfix") doThumbs(true);
    else if (t === "rethumb") doRethumb();
    else if (t === "fulltext") doFulltext();
    else if (t === "autotag") doAutotag();
    else if (t === "dups") showDups(true);
    else if (t === "crossdup") showCrossDups();
    else if (t === "junk") showJunk();
    else if (t === "backup") doBackup();
    else if (t === "library") openAdmin("libs");
    else if (t === "datadir") openDataDir();
  };
}

/* ---------------- 重复 / 垃圾 ---------------- */

async function showDups(launch) {
  if (launch) {
    try {
      await api("/api/dups_full", post({}));
      toast("开始去重分析，可随时停止；完成后自动显示结果", 3600);
      watchTaskOnce();
      return;
    } catch (e) { return alert(e.message); }
  }
  const r = await api(withVol("/api/dups"));
  renderDups(r.groups || [], `⧉ 重复文件${libSuffix()}`, false, r.total);
}
/* ---------------- 空间分析 ---------------- */

async function viewSpace() {
  const el = $("#grid");
  el.className = "";
  const lib = currentLib();
  if (!lib) {
    setCrumb(`📊 空间分析`);
    el.innerHTML = `<div class="empty"><div class="big">📊</div>空间分析需要先选择一个资料库<br>
      <small>点击顶部的库切换器选择一个库</small></div>`;
    return;
  }
  setCrumb(`📊 空间分析 <span class="libtag">📚 ${esc(libLabel(lib))}</span>`);
  el.innerHTML = `<div class="empty"><div class="big">⏳</div>正在统计目录体积…</div>`;
  let tr, topFiles;
  try {
    [tr, topFiles] = await Promise.all([
      api(`/api/dirtree?vol=${lib.id}`),
      api(`/api/list?vol=${lib.id}&sort=size_desc&limit=15`).catch(() => ({ items: [] })),
    ]);
  } catch (e) {
    el.innerHTML = `<div class="empty"><div class="big">⚠️</div>统计失败：${esc(e.message)}</div>`;
    return;
  }
  const nodes = (tr.nodes || []).filter(n => (n.n || 0) > 0);
  // tsize = 含子树的累计体积；老索引没有 tsize 时退回直属 size
  const tsz = n => (n.tsize != null ? n.tsize : (n.size || 0));
  const top = [...nodes].sort((a, b) => tsz(b) - tsz(a)).slice(0, 12);
  const roots = nodes.filter(n => !n.parent);
  const maxSz = top.length ? tsz(top[0]) : 0;
  const maxRoot = roots.length ? Math.max(...roots.map(tsz)) : 0;

  const bars = top.map(n => `
    <div class="sbar" data-go="${esc(n.rel)}" title="${esc(n.rel)}">
      <span class="sbar-name">📁 ${esc(n.name)}</span>
      <span class="sbar-track"><i style="width:${maxSz ? Math.max(2, tsz(n) / maxSz * 100).toFixed(1) : 0}%"></i></span>
      <span class="sbar-size">${fmtSize(tsz(n))} · ${(n.n || 0).toLocaleString()} 文件</span>
    </div>`).join("") || `<div class="empty">这个库还没有文件</div>`;

  const cells = treemap(roots.map(n => ({ ...n, v: tsz(n) })), 1000, 420);
  const svg = cells.length ? `<svg viewBox="0 0 1000 420" class="treemap">${cells.map(r => {
    const o = maxRoot ? 0.45 + 0.5 * Math.cbrt(tsz(r) / maxRoot) : 0.6;
    return `<g data-go="${esc(r.rel)}" class="tmcell">
      <rect x="${r.x.toFixed(1)}" y="${r.y.toFixed(1)}" width="${r.w.toFixed(1)}" height="${r.h.toFixed(1)}" rx="6" style="opacity:${o.toFixed(2)}"/>
      ${r.w > 100 && r.h > 46 ? `<text x="${(r.x + 10).toFixed(1)}" y="${(r.y + 26).toFixed(1)}">${esc(r.name.slice(0, Math.floor(r.w / 15)))}</text>
      <text x="${(r.x + 10).toFixed(1)}" y="${(r.y + 48).toFixed(1)}" class="dim">${fmtSize(tsz(r))}</text>` : (r.w > 52 && r.h > 24 ? `<text x="${(r.x + 8).toFixed(1)}" y="${(r.y + 20).toFixed(1)}">${esc(r.name.slice(0, Math.max(1, Math.floor(r.w / 13))))}</text>` : "")}
    </g>`;
  }).join("")}</svg>` : "";

  const big = (topFiles.items || []).slice(0, 15).map(it2 => `
    <div class="sbar" data-id="${it2.id}">
      <span class="sbar-name">${KIND_ICON[it2.kind] || "📄"} ${esc(it2.name)}</span>
      <span class="sbar-size">${fmtSize(it2.size)}</span>
    </div>`).join("");

  el.innerHTML = `
    <div class="space-wrap">
      <section class="space-sec"><h3>🔋 目录占用 Top ${top.length}<small style="color:var(--fg2);font-weight:400">（含子目录）</small></h3>${bars}</section>
      ${svg ? `<section class="space-sec"><h3>🗺 顶层空间分布<small style="color:var(--fg2);font-weight:400">（面积≈占用，点击进入浏览）</small></h3>${svg}</section>` : ""}
      ${big ? `<section class="space-sec"><h3>🐘 最大文件 Top ${(topFiles.items || []).length}</h3>${big}</section>` : ""}
    </div>`;
  $$(".sbar[data-go], .tmcell", el).forEach(n => n.onclick = () => {
    Object.assign(state, { view: "list", dir: n.dataset.go, q: "", kind: null,
      topic: null, series: null, author: null, mark: null, tag: null, offset: 0 });
    refresh();
  });
  $$(".sbar[data-id]", el).forEach(n => n.onclick = () => openDetail(+n.dataset.id));
}

/** 简化 treemap：按面积对半递归切分，面积比例准确、无重叠、实现稳。 */
function treemap(items, W, H) {
  const out = [];
  function lay(list, x, y, w, h) {
    if (!list.length || w < 12 || h < 12) return;
    if (list.length === 1) { out.push({ ...list[0], x, y, w, h }); return; }
    const total = list.reduce((a, x2) => a + x2.area, 0);
    let acc = 0, i = 0;
    while (i < list.length - 1 && acc + list[i].area < total / 2) { acc += list[i].area; i++; }
    if (i === 0) i = 1;   // 最大项独占过半：也必须切出非空两半，否则无限递归
    const a = list.slice(0, i), b = list.slice(i);
    const fa = a.reduce((s, x2) => s + x2.area, 0) / total;
    if (w >= h) { lay(a, x, y, w * fa, h); lay(b, x + w * fa, y, w * (1 - fa), h); }
    else { lay(a, x, y, w, h * fa); lay(b, x, y + h * fa, w, h * (1 - fa)); }
  }
  const list = items.filter(x => x.v > 0).sort((a, b) => b.v - a.v)
    .map(x => ({ ...x, area: x.v }));
  lay(list, 0, 0, W, H);
  return out.filter(r => r.w >= 24 && r.h >= 24);
}

async function showCrossDups() {
  try {
    const r = await api("/api/dups?cross=1");
    const g = r.groups || [];
    const wasted = g.reduce((a, x) => a + (x.wasted || 0), 0);
    renderDups(g, `🔁 跨库重复（${g.length} 组 · 可省 ${fmtSize(wasted)}）
      <small style="color:var(--fg2);font-weight:400">同一份内容在多个库里都有</small>`, true, r.total);
  } catch (e) { alert(e.message); }
}
function renderDups(groups, title, cross, total) {
  const p = document.createElement("div");
  p.className = "panel";
  document.body.appendChild(p);
  total = total == null ? groups.length : total;
  const libName = vid => (state.libs || []).find(l => l.id === vid)?.label || `库 #${vid}`;
  const PER = 10;                       // 弹窗每页展示的重复组数
  const keepMap = {};                   // 各组选中的保留项（跨页记忆，不丢）
  groups.forEach(g => { keepMap[g.hash] = (g.items[0] || {}).id; });
  let page = 0;

  const stratFns = {
    first: its => its[0],
    newest: its => its.reduce((a, b) => ((+b.mtime || 0) >= (+a.mtime || 0) ? b : a)),
    oldest: its => its.reduce((a, b) => ((+b.mtime || 0) <= (+a.mtime || 0) ? b : a)),
    biggest: its => its.reduce((a, b) => ((+b.size || 0) >= (+a.size || 0) ? b : a)),
  };
  const pages = () => Math.max(1, Math.ceil(groups.length / PER));
  const keepOf = g => {
    const k = keepMap[g.hash];
    return g.items.some(m => m.id === k) ? k : (g.items[0] || {}).id;
  };
  const wastedAll = () => groups.reduce((a, x) => a + (x.wasted || 0), 0);
  const extraAll = () => groups.reduce((a, g) => a + Math.max(0, g.items.length - 1), 0);

  function groupHTML(g) {
    const keep = keepOf(g);
    return `<div class="dupgroup" data-hash="${esc(g.hash)}">
      <div class="gh"><span>${g.n} 个相同 · 浪费 ${fmtSize(g.wasted)}</span>
        <button class="btn small danger" data-keepone="1">保留所选，其余入回收站</button></div>
      ${g.items.map(m => `
      <div class="dupitem" data-id="${m.id}" data-mtime="${m.mtime || 0}" data-size="${m.size || 0}">
        ${m.thumb ? `<img src="/thumb/${m.id}.jpg" style="width:56px;border-radius:5px" alt="">` : ""}
        <div class="nm">${esc(m.dir)}<br><b>${esc(m.name)}</b><br>
          <small>${fmtSize(m.size)} · ${fmtDay(m.mtime)} · ${esc(libName(m.vol_id))}</small></div>
        <label class="keepchoice" style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;white-space:nowrap">
          <input type="radio" name="k-${esc(g.hash)}" value="${m.id}"
            ${m.id === keep ? "checked" : ""}> 保留这份</label>
        <button class="btn small" data-del="${m.id}">删除</button>
      </div>`).join("")}
    </div>`;
  }

  async function delChunked(all, btn, label) {
    const CH = 50, okIds = new Set();
    let failN = 0;
    for (let i = 0; i < all.length; i += CH) {
      const chunk = all.slice(i, i + CH);
      try {
        const r = await api("/api/delete", post({ ids: chunk }));
        r.results.forEach(x => { if (x.ok) okIds.add(+x.id); else failN++; });
      } catch { chunk.forEach(id => failN++); }
      if (btn) btn.textContent = `${label} ${Math.min(i + CH, all.length)}/${all.length}…`;
    }
    return { okIds, failN };
  }

  async function refresh() {
    /* 删除后重查一遍：组数/总数始终真实，避免"删完了还显示旧数据" */
    try {
      const r = await api(withVol("/api/dups" + (cross ? "?cross=1" : "")));
      groups = r.groups || [];
      total = r.total == null ? groups.length : r.total;
    } catch { /* 拉取失败时保留当前数据继续展示 */ }
    render();
  }

  function planIds(list) {
    const plan = [];
    list.forEach(g => {
      const keep = keepOf(g);
      const ids = g.items.map(m => m.id).filter(id => id !== keep);
      if (ids.length) plan.push({ g, ids });
    });
    return plan;
  }

  async function cleanPlan(plan, btn, scopeDesc) {
    const all = plan.flatMap(x => x.ids);
    if (!all.length) return toast("没有可清理的副本：每组都只剩一份了");
    if (!confirm(`将把${scopeDesc} ${all.length} 个重复副本移入回收站（每组保留所选的一份，可从回收站恢复）？`)) return;
    btn.disabled = true;
    const { okIds, failN } = await delChunked(all, btn, "🧹 清理中");
    toast(failN ? `清理完成：成功 ${okIds.size}，失败 ${failN}（失败项可重试）`
                : `已清理 ${okIds.size} 个副本，每组保留一份`, 4000);
    btn.disabled = false;
    await refresh();
  }

  function render() {
    const ps = pages();
    if (page >= ps) page = ps - 1;
    const shown = groups.slice(page * PER, (page + 1) * PER);
    const pageExtra = shown.reduce((a, g) => a + Math.max(0, g.items.length - 1), 0);
    const truncated = groups.length < total;
    p.innerHTML = `
      <button class="btn small close-x" data-close>✕ 关闭</button>
      <h2>${title}</h2>
      <div class="dupmeta">共 <b>${(+total || 0).toLocaleString()}</b> 组重复
        ${truncated ? `（数量过大，本次列出前 ${groups.length} 组，清理后可重新查询）`
                    : (groups.length ? "· <b>已全部列出</b>" : "")}
        ${groups.length ? `· 可回收约 ${fmtSize(wastedAll())}` : ""}</div>
      ${groups.length ? `
      <div class="dupbatch">
        <label style="display:inline-flex;align-items:center;gap:6px;white-space:nowrap">每组保留
          <select id="dup-strategy" class="inp" style="padding:5px 8px">
            <option value="first">第一个</option>
            <option value="newest">最新的</option>
            <option value="oldest">最旧的</option>
            <option value="biggest">最大的</option>
          </select></label>
        <button class="btn small danger" id="dup-batch">🧹 一键清理全部（${extraAll()} 个副本）</button>
        <span class="dupnav">
          <button class="btn small" id="dup-prev" ${page <= 0 ? "disabled" : ""}>‹ 上一页</button>
          <span id="dup-pageinfo">第 ${page + 1} / ${ps} 页</span>
          <button class="btn small" id="dup-next" ${page >= ps - 1 ? "disabled" : ""}>下一页 ›</button>
        </span>
        <small style="color:var(--fg2);margin-left:auto">副本移入回收站，可恢复；保留选择翻页不丢</small>
      </div>
      <div id="dup-list">${shown.map(groupHTML).join("")}</div>
      ${pageExtra ? `<div class="duppagebar">
        <button class="btn small danger" id="dup-pagebatch">🧹 清理本页（${pageExtra} 个副本，每组保留所选）</button>
      </div>` : ""}` : `
      <div class="empty"><div class="big">✨</div>
        ${cross ? "各库之间没有重复内容" : "没有发现重复文件"}<br>
        <small>如果刚点了「查重复」，请等分析跑完（大文件要读盘）。</small></div>`}`;
    $("[data-close]", p).onclick = () => p.remove();

    /* 保留单选 → 记入 keepMap，并禁用被保留项的删除按钮 */
    $$("#dup-list .dupgroup", p).forEach(grp => {
      const h = grp.dataset.hash;
      const g = groups.find(x => x.hash === h) || { items: [] };
      $$("input[type=radio]", grp).forEach(rd => rd.onchange = () => {
        keepMap[h] = +rd.value;
        $$("[data-del]", grp).forEach(b => b.disabled = (+b.dataset.del === +rd.value));
      });
      const k = keepOf(g);
      $$("[data-del]", grp).forEach(b => b.disabled = (+b.dataset.del === +k));
    });

    $$("[data-del]", p).forEach(b => b.onclick = async () => {
      if (!confirm("移入回收站？可从回收站恢复。")) return;
      b.disabled = true;
      try {
        const r = await api("/api/delete", post({ ids: [+b.dataset.del] }));
        if (!r.results[0].ok) { alert(r.results[0].msg); b.disabled = false; return; }
        await refresh();
      } catch (e) { toast(e.message, 2600); b.disabled = false; }
    });

    $$("[data-keepone]", p).forEach(b => b.onclick = async () => {
      const grp = b.closest(".dupgroup");
      const g = groups.find(x => x.hash === grp.dataset.hash);
      if (!g) return;
      const ids = g.items.map(m => m.id).filter(id => id !== keepOf(g));
      if (!ids.length) return toast("没有需要删除的其他副本");
      if (!confirm(`保留选中的那份，把其余 ${ids.length} 个副本移入回收站？可从回收站恢复。`)) return;
      b.disabled = true;
      const { okIds, failN } = await delChunked(ids, b, "🧹 清理中");
      toast(failN ? `成功 ${okIds.size}，失败 ${failN}` : `已清理 ${okIds.size} 个副本`);
      await refresh();
    });

    const strat = $("#dup-strategy", p);
    if (strat) strat.onchange = () => {
      const fn = stratFns[strat.value] || stratFns.first;
      groups.forEach(g => { if (g.items.length) keepMap[g.hash] = fn(g.items).id; });
      render();
    };

    $("#dup-prev", p)?.addEventListener("click", () => { if (page > 0) { page--; render(); } });
    $("#dup-next", p)?.addEventListener("click", () => { if (page < pages() - 1) { page++; render(); } });

    const batchBtn = $("#dup-batch", p);
    if (batchBtn) batchBtn.onclick = () => cleanPlan(planIds(groups), batchBtn, "全部");

    const pageBtn = $("#dup-pagebatch", p);
    if (pageBtn) pageBtn.onclick = () =>
      cleanPlan(planIds(groups.slice(page * PER, (page + 1) * PER)), pageBtn, "本页");
  }

  render();
}

async function showJunk() {
  const j = await api(withVol("/api/junk"));
  const p = document.createElement("div");
  p.className = "panel";
  const rows = Object.values(j).filter(r => r && r.n > 0);
  p.innerHTML = `<button class="btn small close-x" data-close>✕ 关闭</button>
    <h2>🧹 清理建议${libSuffix()}</h2>
    ${rows.length ? rows.map(r => `<div class="junkrow">
      <span>${esc(r.label)}</span><span>${r.n.toLocaleString()} 个 · ${fmtSize(r.size)}</span>
      </div>`).join("") : `<div class="empty">没有发现可清理的垃圾文件 👍</div>`}
    <p style="margin-top:16px;color:var(--fg2);font-size:12.5px;line-height:1.8">
      这里只是统计提示，程序不会自动删任何东西。<br>
      确认不要的可以去对应目录手动处理，或用右键「移入回收站」。</p>`;
  document.body.appendChild(p);
  $("[data-close]", p).onclick = () => p.remove();
}

/* ---------------- 弹窗 ---------------- */

function openModal(html) {
  const m = $("#modal");
  $("#modal-box").innerHTML = html;
  m.classList.remove("closing");
  m.classList.remove("hidden");
  $$("[data-close]", m).forEach(b => b.onclick = closeModal);
  m.onclick = e => { if (e.target.id === "modal") closeModal(); };
  return $("#modal-box");
}
function closeModal() {
  const m = $("#modal");
  if (m.classList.contains("hidden") || m.classList.contains("closing")) return;
  m.classList.add("closing");
  clearTimeout(m._ct);
  m._ct = setTimeout(() => {
    m.classList.add("hidden");
    m.classList.remove("closing");
  }, 130);
}

/* 统一的输入弹窗（替代原生 prompt：样式一致、移动端友好、可自动化测试）
   返回 Promise<string|null>，取消/关闭 → null */
function uiPrompt(title, opts = {}) {
  return new Promise(resolve => {
    let settled = false;
    const done = v => {
      if (settled) return;
      settled = true;
      obs.disconnect();
      closeModal();
      resolve(v);
    };
    const box = openModal(`<h2>${esc(title)}</h2>
      ${opts.hint ? `<div class="modal-note">${esc(opts.hint)}</div>` : ""}
      <input type="text" id="uip-in" autocomplete="off" spellcheck="false"
        ${opts.value != null ? `value="${esc(opts.value)}"` : ""}
        ${opts.placeholder ? `placeholder="${esc(opts.placeholder)}"` : ""}>
      <div class="modal-btns">
        <button class="btn" data-close>取消</button>
        <button class="btn acc" id="uip-ok">确定</button>
      </div>`);
    // 任何关闭路径（遮罩点击 / 取消按钮 / 全局 Esc）都解析为 null
    const obs = new MutationObserver(() => {
      if ($("#modal").classList.contains("hidden")) done(null);
    });
    obs.observe($("#modal"), { attributes: true, attributeFilter: ["class"] });
    const inp = $("#uip-in", box);
    $("#uip-ok", box).onclick = () => done(inp.value);
    inp.onkeydown = e => {
      if (e.key === "Enter") done(inp.value);
    };
    setTimeout(() => { try { inp.focus(); } catch {} }, 30);
  });
}

/* ---------------- 休息提醒 ---------------- */

const REST = { on: false, interval: 45, idlePause: true, lastActive: Date.now(),
  shown: false, startAt: Date.now(), offToday: false };

function startRest() {
  try {
    REST.offToday = localStorage.getItem("filelib.rest.offday") === new Date().toISOString().slice(0, 10);
  } catch {}
  ["mousemove", "keydown", "click", "scroll", "touchstart"].forEach(ev =>
    document.addEventListener(ev, () => { REST.lastActive = Date.now(); }, { passive: true }));
  setInterval(() => {
    if (!REST.on || REST.offToday || REST.shown) return;
    if (REST.idlePause && Date.now() - REST.lastActive > 3 * 60 * 1000) {
      REST.startAt = Date.now();
      return;
    }
    if (Date.now() - REST.startAt >= REST.interval * 60 * 1000) {
      REST.shown = true;
      $("#rest-min").textContent = REST.interval;
      $("#rest").classList.remove("hidden");
    }
  }, 5000);
}
function loadRest() {
  let cfg = null;
  try { cfg = JSON.parse(localStorage.getItem("filelib.rest") || "null"); } catch {}
  if (cfg) {
    REST.on = !!cfg.on;
    REST.interval = cfg.interval || 45;
    REST.idlePause = cfg.idle !== false;
  }
  const b = $("#btn-rest");
  if (b) b.title = REST.on ? `休息提醒：每 ${REST.interval} 分钟` : "休息提醒（未开启）";
  startRest();
}

/* ---------------- 快捷键帮助 ---------------- */

const SHORTCUTS = [
  ["Ctrl + B", "收起 / 展开左侧导航栏"],
  ["/", "聚焦搜索框"],
  ["Esc", "关闭当前浮层 / 逐层退出"],
  ["T", "切换主题"],
  ["~", "老板键：瞬间伪装成代码编辑器，再按返回"],
  ["?", "打开这个帮助"],
  ["G 然后 H", "回总览"],
  ["G 然后 F", "回全部文件"],
  ["↑ / ↓", "列表里上下选文件，回车打开"],
  ["空格", "视频：播放 / 暂停"],
  ["← / →", "视频：后退 / 前进 10 秒"],
  ["J / L", "视频：后退 / 前进 30 秒"],
  ["↑ / ↓", "视频：音量 +10% / -10%"],
  ["F", "视频：全屏"],
  ["M", "视频：静音"],
  ["P / N", "视频：上一个 / 下一个"],
  ["[ / ]", "视频：减速 / 加速"],
  ["0–9", "视频：跳到 0%–90%"],
  ["← / →", "图片：上一张 / 下一张"],
  ["← / →", "阅读器：上一页 / 下一页"],
  ["+ / -", "阅读器：字号放大 / 缩小"],
  ["T（阅读器）", "打开 / 收起章节目录"],
];

/* ---------------- 键盘 ---------------- */

let gPressed = false;

document.addEventListener("keydown", e => {
  const tag = (e.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable;

  // 老板键优先级最高：伪装层开着时吞掉一切按键，只认 ~ / Esc 退出
  const _bossEl = $("#boss");
  if (_bossEl && !_bossEl.classList.contains("hidden")) {
    e.preventDefault();
    if (e.key === "`" || e.key === "Escape" || e.key === "~") toggleBoss(false);
    return;
  }
  if (e.key === "`" && !typing) { e.preventDefault(); toggleBoss(true); return; }

  if (e.key === "Escape") {
    const layers = [
      ["#help", () => $("#help").classList.add("hidden")],
      ["#modal", closeModal],
      ["#lightbox", closeLightbox],
      ["#player", closeOrMinimize],
      ["#reader", closeReader],
      ["#detail", closeDetail],
      ["#adminpanel", () => $("#adminpanel").classList.add("hidden")],
      ["#restset", () => $("#restset").classList.add("hidden")],
      ["#lib-menu", () => $("#lib-menu").classList.add("hidden")],
      ["#toolsmenu", () => $("#toolsmenu").classList.add("hidden")],
      ["#ctxmenu", () => $("#ctxmenu").classList.add("hidden")],
      ["#rest", () => { $("#rest").classList.add("hidden"); REST.shown = false; REST.startAt = Date.now(); }],
    ];
    for (const [sel, fn] of layers) {
      const el = $(sel);
      if (el && !el.classList.contains("hidden")) {
        fn();
        e.preventDefault();
        return;
      }
    }
    return;
  }

  if (typing) return;

  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
    e.preventDefault();
    setSidebar(!sidebarOpen());
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  // 播放器优先
  if (!$("#player").classList.contains("hidden")) {
    const v = video();
    switch (e.key.toLowerCase()) {
      case " ": e.preventDefault(); togglePlay(); return;
      case "arrowleft": e.preventDefault(); seekBy(-10); return;
      case "arrowright": e.preventDefault(); seekBy(10); return;
      case "j": seekBy(-30); return;
      case "l": seekBy(30); return;
      case "arrowup":
        e.preventDefault();
        v.volume = Math.min(1, v.volume + 0.1);
        $("#pl-vol").value = v.volume;
        hint(Math.round(v.volume * 100) + "%"); return;
      case "arrowdown":
        e.preventDefault();
        v.volume = Math.max(0, v.volume - 0.1);
        $("#pl-vol").value = v.volume;
        hint(Math.round(v.volume * 100) + "%"); return;
      case "f": toggleFullscreen(); return;
      case "m":
        v.muted = !v.muted;
        $("#pl-mute").textContent = v.muted ? "🔇" : "🔊";
        hint(v.muted ? "静音" : "有声"); return;
      case "p": plStep(-1); return;
      case "n": plStep(1); return;
      case "[": v.playbackRate = Math.max(0.25, v.playbackRate - 0.25);
        $("#pl-rate").textContent = v.playbackRate + "×"; hint(v.playbackRate + "×"); return;
      case "]": v.playbackRate = Math.min(4, v.playbackRate + 0.25);
        $("#pl-rate").textContent = v.playbackRate + "×"; hint(v.playbackRate + "×"); return;
      default:
        if (/^[0-9]$/.test(e.key) && v.duration) v.currentTime = v.duration * (+e.key / 10);
        return;
    }
  }

  if (!$("#lightbox").classList.contains("hidden")) {
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); lbIndex--; showLb(); }
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); lbIndex++; showLb(); }
    return;
  }

  if (!$("#reader").classList.contains("hidden")) {
    if (!RD.kind) return;
    if (e.key === "ArrowRight" || e.key === "PageDown") { e.preventDefault(); rdNext(); }
    else if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); rdPrev(); }
    else if (e.key === " ") {
      e.preventDefault();
      if (RD.kind === "text") return;   // TXT 用滚轮/箭头
      if (RD._sp) { clearTimeout(RD._sp); RD._sp = null; rdNext(); }
      else RD._sp = setTimeout(() => { RD._sp = null; rdNext(); }, 260);
    }
    else if (e.key.toLowerCase() === "t") $("#rd-toc").click();
    else if (e.key === "+" || e.key === "=") $("#rd-font+").click();
    else if (e.key === "-") $("#rd-font-").click();
    return;
  }

  if (!$("#detail").classList.contains("hidden")) {
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      const i = state.items.findIndex(x => x.id === detailId);
      if (i >= 0) {
        const ni = i + (e.key === "ArrowDown" ? 1 : -1);
        if (state.items[ni]) { openDetail(state.items[ni].id); e.preventDefault(); }
      }
    }
    if (e.key === "Enter" && detailId) {
      const it = state.items.find(x => x.id === detailId);
      if (it) quickOpen(it);
    }
    return;
  }

  if (state.view === "list") {
    const step = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
    if (step && state.items.length) {
      e.preventDefault();
      let idx = state.items.findIndex(x => x.id === state.hl);
      idx = Math.max(0, Math.min(state.items.length - 1, idx + step));
      state.hl = state.items[idx].id;
      highlightCard();
    }
    if (e.key === "Enter" && state.hl) {
      const it = state.items.find(x => x.id === state.hl);
      if (it) quickOpen(it);
    }
  }

  if (e.key === "/") { e.preventDefault(); $("#search").focus(); $("#search").select(); return; }
  if (e.key === "\\") {
    // 反斜杠：切到全文搜索并聚焦（已在全文模式时切回普通搜索）
    e.preventDefault();
    state.fulltext = !state.fulltext;
    $("#search-full")?.classList.toggle("on", state.fulltext);
    $("#search").focus();
    $("#search").select();
    if (state.fulltext && state.q) runFulltext();
    return;
  }
  if (e.key === "?") { e.preventDefault(); $("#help").classList.remove("hidden"); return; }
  if (e.key.toLowerCase() === "t") { cycleTheme(); return; }
  if (e.key.toLowerCase() === "g") { gPressed = true; setTimeout(() => (gPressed = false), 900); return; }
  if (gPressed) {
    gPressed = false;
    if (e.key.toLowerCase() === "h") { state.view = "home"; refresh(); }
    if (e.key.toLowerCase() === "f") {
      Object.assign(state, { view: "list", dir: "", kind: null, mark: null, tag: null, q: "" });
      refresh();
    }
  }
});

function highlightCard() {
  $$(".card").forEach(c => { c.style.outline = ""; });
  const c = $(`.card[data-id="${state.hl}"]`);
  if (c) {
    c.style.outline = "2px solid var(--acc)";
    c.scrollIntoView({ block: "nearest" });
  }
}
function cycleTheme() {
  const i = THEMES.findIndex(t => t.id === document.body.dataset.theme);
  applyTheme(THEMES[(i + 1) % THEMES.length].id);
}

/* ---------------- 登录 / 用户 ---------------- */

function showLogin(mustSetup) {
  $("#login").classList.remove("hidden");
  $("#login-sub").textContent = mustSetup
    ? "首次使用：创建管理员账号（拥有全部权限）"
    : "请输入账号密码进入资料库";
  $("#login-go").textContent = mustSetup ? "创建管理员并进入" : "登 录";
  $("#login-pass2-wrap").style.display = mustSetup ? "" : "none";
  $("#login-go").dataset.setup = mustSetup ? "1" : "";
  $("#login-ver").textContent = ME.version || "";
  // 记住用户名：预填 + 勾选态恢复（只记用户名，绝不存密码）
  try {
    const saved = localStorage.getItem("filelib.user") || "";
    $("#login-user").value = saved;
    $("#login-remember").checked = saved ? true : localStorage.getItem("filelib.remember") !== "0";
  } catch {}
  setTimeout(() => $("#login-user").focus(), 80);
}

async function doLogin() {
  const err = $("#login-err");
  err.textContent = "";
  const setup = $("#login-go").dataset.setup === "1";
  const username = $("#login-user").value.trim();
  const password = $("#login-pass").value;
  if (!username || !password) { err.textContent = "用户名和密码都要填"; return; }
  if (setup) {
    if ($("#login-pass2").value !== password) { err.textContent = "两次输入的密码不一致"; return; }
    if (password.length < 6) { err.textContent = "密码至少 6 位"; return; }
    if (!/^[\w\-.]{2,20}$/.test(username)) { err.textContent = "用户名 2-20 位，字母数字下划线"; return; }
  }
  const btn = $("#login-go");
  btn.disabled = true;
  let keepDisabled = false;   // 锁定倒计时期间保持按钮禁用
  try {
    const r = await api(setup ? "/api/setup" : "/api/login", post({ username, password }));
    Object.assign(ME, r);
    ME.logged = true;
    // 记住用户名 / 清除
    try {
      if (setup) localStorage.removeItem("filelib.user");
      else if ($("#login-remember").checked) localStorage.setItem("filelib.user", username);
      else localStorage.removeItem("filelib.user");
      localStorage.setItem("filelib.remember", $("#login-remember").checked ? "1" : "0");
    } catch {}
    $("#login").classList.add("hidden");
    $("#login-err").textContent = "";
    await boot();
  } catch (e) {
    const ra = e.data && +e.data.retry_after || 0;
    if (ra > 0) {
      // 锁定倒计时：期间按钮禁用，秒数实时刷新，不用反复试
      keepDisabled = true;
      let left = ra;
      err.textContent = `失败次数过多，已临时锁定，${left} 秒后可重试`;
      const h = setInterval(() => {
        left -= 1;
        if (left <= 0) {
          clearInterval(h);
          btn.disabled = false;
          err.textContent = "锁定已解除，可以重试";
          $("#login-pass").value = "";
          $("#login-pass").focus();
        } else {
          err.textContent = `失败次数过多，已临时锁定，${left} 秒后可重试`;
        }
      }, 1000);
    } else {
      err.textContent = e.message;
      $("#login-pass").value = "";
      $("#login-pass").focus();
    }
  } finally {
    if (!keepDisabled) btn.disabled = false;
  }
}

function applyRoleUI() {
  const admin = isAdmin();
  document.body.classList.toggle("not-admin", !admin);
  // HTML 里管理员按钮带静态 hidden，登录成功后必须摘掉，否则管理员自己也看不到
  $$(".admin-only").forEach(el => el.classList.toggle("hidden", !admin));
  const who = $("#whoami");
  who.classList.remove("hidden");
  who.innerHTML = `${isAdmin() ? "🛡" : "👤"} ${esc(ME.username)}`;
  who.title = `${isAdmin() ? "管理员" : "普通用户"} · 点击打开菜单`;
}

function userMenu(e) {
  e.stopPropagation();
  const m = $("#ctxmenu");
  m.innerHTML = `
    <div style="padding:8px 12px;font-size:12px;color:var(--fg2)">
      ${esc(ME.username)} · ${isAdmin() ? "管理员" : "普通用户"}</div>
    <hr>
    ${isAdmin() ? `<div data-u="admin">🛡 管理后台</div>` : ""}
    <div data-u="theme">🎨 切换主题</div>
    <div data-u="help">⌨ 快捷键</div>
    <div data-u="rest">🧘 休息提醒设置</div>
    <hr><div data-u="logout" class="danger">↩ 退出登录</div>`;
  m.classList.remove("hidden");
  const r = $("#whoami").getBoundingClientRect();
  m.style.left = Math.max(4, Math.min(r.right - 190, window.innerWidth - 200)) + "px";
  m.style.top = (r.bottom + 6) + "px";
  m.onclick = async ev => {
    const a = ev.target.closest("[data-u]")?.dataset.u;
    m.classList.add("hidden");
    if (!a) return;
    if (a === "admin") openAdmin("users");
    else if (a === "help") $("#help").classList.remove("hidden");
    else if (a === "theme") cycleTheme();
    else if (a === "rest") $("#btn-rest").click();
    else if (a === "logout") {
      if (!confirm("退出登录？")) return;
      // 登出要点：不管请求成功与否，都必须真的退出。
      // 曾经出现过 POST /api/logout 因 CSRF 缺失拿 403、紧接着跳转的
      // GET / 又被同一条 keep-alive 连接上残留的 "{}" 污染成 501 的情况，
      // 用户看到的就是一屏 "Unsupported method ('{}GET')"。
      try {
        await api("/api/logout", post({}));
      } catch (e) {
        // 403/429 也好，网络中断也好，本地状态一律清掉
        ME.logged = false;
        ME.id = null;
        ME.username = "";
        ME.role = "user";
        ME.vols = [];
        ME.nav = null;
      }
      location.replace("/");
    }
  };
}

/* ---------------- 管理后台 ---------------- */

let ADMIN_TAB = "users";

function openAdmin(tab) {
  if (!isAdmin()) return;
  ADMIN_TAB = tab || ADMIN_TAB;
  $("#adminpanel").classList.remove("hidden");
  $$("#admin-tabs b").forEach(b => b.classList.toggle("on", b.dataset.tab === ADMIN_TAB));
  renderAdmin();
}

async function renderAdmin() {
  const el = $("#admin-body");
  el.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    if (ADMIN_TAB === "users") await adminUsers(el);
    else if (ADMIN_TAB === "libs") await adminLibs(el);
    else if (ADMIN_TAB === "set") await adminSettings(el);
    else if (ADMIN_TAB === "data") await adminData(el);
    else if (ADMIN_TAB === "logs") await adminLogs(el);
    else if (ADMIN_TAB === "sec") await adminSecurity(el);
  } catch (e) {
    el.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

/* --- 用户与权限 --- */
async function adminUsers(el) {
  const r = await api("/api/admin/users");
  const users = r.users || [];
  const navItems = (r.nav_items || []).filter(n => n && n.k);
  const logins = r.logins || [];
  const nameCount = {};
  for (const l of state.libs) {
    const k = l.label || libShortName(l);
    nameCount[k] = (nameCount[k] || 0) + 1;
  }

  el.innerHTML = `
    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center">
      <button class="btn acc" id="u-add">＋ 新建用户</button>
      <span style="font-size:12px;color:var(--fg2)">
        共 ${users.length} 个账号 · 权限修改即时生效 · 普通用户看不到未授权的资料库与导航项</span>
    </div>
    <table class="admtable">
      <thead><tr><th style="width:150px">用户</th><th style="width:110px">角色</th>
        <th style="width:130px">密码</th><th>可查看的资料库 / 导航项</th>
        <th style="width:130px">最近登录</th><th style="width:110px">IP</th>
        <th style="width:60px">启用</th><th style="width:60px"></th></tr></thead>
      <tbody>
      ${users.map(u => {
        const nav = u.nav || {};
        const extra = state.libs.filter(l => !(u.perms || []).includes(l.id) && state.libs.length && !isAdmin());
        return `<tr data-uid="${u.id}" class="${u.disabled ? "dis" : ""}">
          <td><b>${esc(u.username)}</b>${u.id === ME.id
            ? ' <span style="background:var(--acc);color:var(--acc-fg);border-radius:4px;padding:0 5px;font-size:10px">我</span>' : ""}
            ${u.note ? `<br><small style="color:var(--fg2)">${esc(u.note)}</small>` : ""}</td>
          <td><select data-role="${u.id}">
            <option value="user" ${u.role === "user" ? "selected" : ""}>普通用户</option>
            <option value="admin" ${u.role === "admin" ? "selected" : ""}>管理员</option>
          </select></td>
          <td><span class="pw" data-pw="${esc(u.password || "")}">••••••</span>
            <a class="pw-eye" data-pw-eye="${u.id}" title="显示 / 隐藏">👁</a></td>
          <td class="perms">${u.role === "admin"
            ? `<small style="color:var(--fg2)">管理员拥有全部资料库与全部功能</small>`
            : (state.libs.length ? state.libs.map(l => {
                const dup = nameCount[l.label || libShortName(l)] > 1;
                const on = (u.perms || []).includes(l.id);
                const m = nav[l.id];
                const all = m === "*";
                return `<div class="admlib ${dup ? "isdup" : ""}">
                  <input type="checkbox" data-perm="${l.id}" data-uid="${u.id}" ${on ? "checked" : ""}>
                  <span><b class="${dup ? "dupname" : ""}">${l.online ? "💾" : "📀"} ${esc(libLabel(l))}</b>
                    <small>#${l.id} · ${esc(l.root_path || l.root || "")} · ${(l.n || 0).toLocaleString()} 文件</small>
                    ${on ? (() => {
                      const checked = all ? navItems.length : (Array.isArray(m) ? m.filter(k => k !== "home").length : 0);
                      const summary = all ? "全部功能" : `已选 ${checked} 项`;
                      return `<details class="navdetail" style="margin-top:4px">
                      <summary title="点击展开 / 收起功能明细">
                        <label class="${all ? "on" : ""}" title="该库开放全部功能" data-stop="1">
                          <input type="checkbox" data-navall="${l.id}" data-uid="${u.id}" ${all ? "checked" : ""}>全部</label>
                        <small style="color:var(--fg2);margin-left:6px">${summary} ▾</small>
                      </summary>
                      <span class="navchips" style="margin-top:4px">
                      ${navItems.filter(n => ["tree", "kinds", "recent",
                          "star", "read", "todo", "continue", "stats", "random",
                          "play", "download"].includes(n.k)).map(n => {
                        const chk = all || (Array.isArray(m) && m.includes(n.k));
                        return `<label class="${chk ? "on" : ""}" style="${all ? "opacity:.55" : ""}">
                          <input type="checkbox" data-nav="${n.k}" data-vol="${l.id}" data-uid="${u.id}"
                            ${chk ? "checked" : ""} ${all ? "disabled" : ""}>${esc(n.label)}</label>`;
                      }).join("")}
                      </span>
                    </details>`;
                    })() : ""}
                  </span>
                </div>`;
              }).join("") : `<small style="color:var(--fg2)">还没有资料库</small>`)}</td>
          <td><small>${u.last_login ? fmtTime(u.last_login) : "从未"}</small></td>
          <td><small>${esc(u.last_ip || "-")}</small></td>
          <td><input type="checkbox" data-dis="${u.id}" ${u.disabled ? "" : "checked"}></td>
          <td style="white-space:nowrap">
            <button class="btn small" data-pw="${u.id}" data-name="${esc(u.username)}" title="重设密码">🔑</button>
            <button class="btn small danger" data-del="${u.id}">删除</button>
          </td>
        </tr>`;
      }).join("")}
      </tbody>
    </table>
    <h3 class="admlog">最近登录记录</h3>
    <div class="admlogs">
      ${logins.map(l => `<div class="admlogrow ${l.ok ? "" : "bad"}">
        <span>${fmtTime(l.ts)}</span><b>${esc(l.username || "-")}</b>
        <span>${esc(l.ip || "-")}</span><span>${l.ok ? "✓ 成功" : "✗ " + esc(l.reason || "失败")}</span>
      </div>`).join("") || `<small style="color:var(--fg2)">暂无记录</small>`}
    </div>`;

  $("#u-add").onclick = async () => {
    const username = (await uiPrompt("新建用户", {
      hint: "用户名 2-20 位，字母 / 数字 / 中文 / _-. 都行",
      placeholder: "例如 mom",
    }))?.trim();
    if (!username) return;
    const password = await uiPrompt(`新建用户「${username}」`, {
      hint: "初始密码至少 6 位。创建后记得给TA分配可看的资料库。",
      placeholder: "初始密码",
    });
    if (!password) return;
    try {
      await api("/api/admin/users", post({ action: "add", username, password, role: "user", perms: [] }));
      toast("已创建（记得给它分配资料库）");
      renderAdmin();
    } catch (e) { alert(e.message); }
  };
  $$(".pw-eye", el).forEach(a => a.onclick = () => {
    const s = a.parentElement.querySelector(".pw");
    const shown = s.dataset.show === "1";
    s.textContent = shown ? "••••••" : (s.dataset.pw || "(空)");
    s.dataset.show = shown ? "" : "1";
  });
  $$("select[data-role]", el).forEach(sel => sel.onchange = async () => {
    const uid = +sel.dataset.role;
    if (uid === ME.id && sel.value !== "admin" &&
        !confirm("把自己改成普通用户后将失去管理权限，继续？")) { sel.value = "admin"; return; }
    try {
      await api("/api/admin/users", post({ action: "role", id: uid, role: sel.value }));
      renderAdmin();
    } catch (e) { alert(e.message); renderAdmin(); }
  });
  $$("input[data-perm]", el).forEach(cb => cb.onchange = async () => {
    const uid = +cb.dataset.uid;
    const perms = $$(`input[data-perm][data-uid="${uid}"]`, el).filter(x => x.checked).map(x => +x.dataset.perm);
    try {
      await api("/api/admin/users", post({ action: "perms", id: uid, perms }));
      renderAdmin();
    } catch (e) { alert(e.message); }
  });
  // summary 内的"全部"勾选框：阻止冒泡，避免点击时展开/收起
  $$("details.navdetail > summary label", el).forEach(lb =>
    lb.addEventListener("click", ev => ev.stopPropagation()));
  // 更新某个"权限折叠区"的摘要文字与勾选样式（原地更新，不重渲染 → details 保持展开）
  const paintNavDetail = (detail, all) => {
    const chips = detail.querySelector(".navchips");
    const boxes = chips ? $$("input[data-nav]", chips) : [];
    boxes.forEach(x => {
      if (all) { x.checked = true; x.disabled = true; }
      x.closest("label").classList.toggle("on", x.checked);
      x.closest("label").style.opacity = all ? ".55" : "";
    });
    const small = detail.querySelector("summary small");
    if (small) small.textContent = all ? "全部功能" : `已选 ${boxes.filter(x => x.checked).length} 项 ▾`;
  };

  $$("input[data-navall]", el).forEach(cb => cb.onchange = async () => {
    const uid = +cb.dataset.uid, vid = +cb.dataset.navall;
    try {
      await api("/api/admin/users", post({ action: "nav", id: uid, nav: { [vid]: cb.checked ? "*" : [] } }));
      const detail = cb.closest("details.navdetail");
      if (detail) {
        const chips = detail.querySelector(".navchips");
        if (chips) $$("input[data-nav]", chips).forEach(x => {
          x.checked = cb.checked; x.disabled = cb.checked;
          x.closest("label").classList.toggle("on", cb.checked);
          x.closest("label").style.opacity = cb.checked ? ".55" : "";
        });
        const small = detail.querySelector("summary small");
        if (small) small.textContent = cb.checked ? "全部功能" : "已选 0 项 ▾";
      }
    } catch (e) { alert(e.message); }
  });
  $$("input[data-nav]", el).forEach(cb => cb.onchange = async () => {
    const uid = +cb.dataset.uid, vid = +cb.dataset.vol;
    const keys = $$(`input[data-nav][data-uid="${uid}"][data-vol="${vid}"]`, el)
      .filter(x => x.checked).map(x => x.dataset.nav);
    try {
      await api("/api/admin/users", post({ action: "nav", id: uid, nav: { [vid]: keys } }));
      cb.closest("label").classList.toggle("on", cb.checked);
      const detail = cb.closest("details.navdetail");
      if (detail) paintNavDetail(detail, false);
    } catch (e) { alert(e.message); cb.closest("label").classList.toggle("on", !cb.checked); }
  });
  $$("input[data-dis]", el).forEach(cb => cb.onchange = async () => {
    try {
      await api("/api/admin/users", post({ action: "disable", id: +cb.dataset.dis, disabled: !cb.checked }));
      toast(cb.checked ? "已启用" : "已停用");
    } catch (e) { alert(e.message); renderAdmin(); }
  });
  $$("button[data-pw]", el).forEach(b => b.onclick = async () => {
    const pw = prompt(`为「${b.dataset.name}」设置新密码（至少 4 位）：\n保存后该用户的所有登录会话立即失效，需用新密码重新登录。`);
    if (pw === null) return;
    if (pw.trim().length < 4) return alert("密码至少 4 位");
    try {
      await api("/api/admin/users", post({ action: "pass", id: +b.dataset.pw, password: pw.trim() }));
      toast("密码已更新" + (+b.dataset.pw === ME.id ? "" : "，该用户旧会话已全部失效"));
    } catch (e) { alert(e.message); }
  });
  $$("button[data-del]", el).forEach(b => b.onclick = async () => {
    if (!confirm("确定删除这个用户？其标记与阅读进度会一并删除。")) return;
    try {
      await api("/api/admin/users", post({ action: "del", id: +b.dataset.del }));
      renderAdmin();
    } catch (e) { alert(e.message); }
  });
}

/* --- 资料库 --- */
async function adminLibs(el) {
  const [o, settings] = await Promise.all([
    api("/api/admin/overview"),
    api("/api/admin/settings"),
  ]);
  const vols = o.volumes || [];
  el.innerHTML = `
    <div class="sec-note">
      <b>索引与数据全部在这台电脑上。</b>数据目录：<code>${esc(o.data_dir || "")}</code><br>
      索引库 ${fmtSize(o.db_size)} · ffmpeg：${o.ffmpeg ? "✅ " + esc(o.ffmpeg) : "❌ 未找到"} ·
      Calibre：${o.calibre ? "✅ 已识别" : "❌ 未找到（AZW3/MOBI 封面与在线阅读受限）"}
    </div>
    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
      <button class="btn acc" id="lib-add">＋ 添加资料库（扫描文件夹）</button>
      <button class="btn" id="lib-backup">💾 立即备份索引</button>
      <button class="btn" id="lib-thumbs">🖼 生成封面</button>
    </div>
    <table class="admtable">
      <thead><tr><th style="width:44px">#</th><th style="width:150px">名称</th><th>根目录</th>
        <th style="width:80px">文件</th><th style="width:80px">大小</th>
        <th style="width:70px">封面</th><th style="width:70px">全文</th>
        <th style="width:70px">状态</th><th style="width:190px"></th></tr></thead>
      <tbody>
      ${vols.map(v => `<tr>
        <td>${v.id}</td>
        <td><b>${esc(v.label || libShortName(v))}</b></td>
        <td><small style="word-break:break-all">${esc(v.root_path || "")}</small></td>
        <td>${(v.n || 0).toLocaleString()}</td>
        <td>${fmtSize(v.size)}</td>
        <td>${(v.thumbs || 0).toLocaleString()}</td>
        <td>${(v.texts || 0).toLocaleString()}</td>
        <td>${v.online ? "🟢" : "⚪ 离线"}</td>
        <td style="white-space:nowrap">
          <button class="btn small" data-scan="${esc(v.root_path || "")}">⟳ 重扫</button>
          <button class="btn small" data-rename="${v.id}" data-name="${esc(v.label || "")}">改名</button>
          <button class="btn small danger" data-drop="${v.id}">删索引</button>
        </td>
      </tr>`).join("") || `<tr><td colspan="9"><small style="color:var(--fg2)">
        还没有资料库，点上面「添加资料库」</small></td></tr>`}
      </tbody>
    </table>
    <h3 class="admlog">自动备份（保留最近 ${settings.backup_keep || 3} 份）</h3>
    <div class="admlogs">${(settings.backups || []).map(b =>
      `<div class="admlogrow" style="grid-template-columns:220px 1fr 150px">
        <span>${esc(b.name || b.path || "")}</span><span>${fmtSize(b.size || 0)}</span>
        <span>${b.mtime ? fmtTime(b.mtime) : ""}</span></div>`).join("")
      || `<small style="color:var(--fg2)">暂无备份</small>`}</div>`;

  $("#lib-add").onclick = addLibrary;
  $("#lib-backup").onclick = doBackup;
  $("#lib-thumbs").onclick = () => doThumbs(false);
  $$("[data-scan]", el).forEach(b => b.onclick = () => doScan(b.dataset.scan));
  $$("[data-rename]", el).forEach(b => b.onclick = async () => {
    const name = await uiPrompt("资料库显示名", {
      hint: "只改显示名，不影响磁盘文件",
      value: b.dataset.name,
    });
    if (name == null) return;
    try {
      await api("/api/admin/library", post({ action: "rename", id: +b.dataset.rename, label: name }));
      await loadStats(true);
      renderAdmin();
    } catch (e) { alert(e.message); }
  });
  $$("[data-drop]", el).forEach(b => b.onclick = async () => {
    if (!confirm("只删除这个库的索引记录，磁盘上的文件一个都不动。\n确定继续？")) return;
    try {
      await api("/api/admin/library", post({ action: "drop", id: +b.dataset.drop }));
      state.tree = null;
      await loadStats(true);
      toast("索引已删除");
      renderAdmin();
    } catch (e) { alert(e.message); }
  });
}

/* --- 设置 --- */
async function adminSettings(el) {
  const s = await api("/api/admin/settings");
  const chk = v => (v ? "checked" : "");
  el.innerHTML = `
    <div class="setgrid">
      <div class="setcard">
        <h3>🔎 索引与扫描</h3>
        <div class="setrow"><label>每天定时扫描</label>
          <input type="checkbox" id="s-scan-daily" ${chk(s.scan_daily_enabled)}></div>
        <div class="setrow"><label>每天几点</label>
          <input type="time" id="s-scan-time" value="${esc(s.scan_daily_time || "03:00")}"></div>
        <div class="setrow"><label>盘插上时自动扫</label>
          <input type="checkbox" id="s-scan-temp" ${chk(s.scan_temp_enabled)}></div>
        <div class="setrow"><label>间隔（分钟）</label>
          <input type="number" id="s-scan-int" min="5" max="1440" value="${s.scan_temp_interval_min || 30}"></div>
        <div class="set-note">定时扫描只在程序运行时生效，且不会和手动任务打架。</div>
      </div>
      <div class="setcard">
        <h3>💾 备份</h3>
        <div class="setrow"><label>每天自动备份索引</label>
          <input type="checkbox" id="s-backup" ${chk(s.auto_backup)}></div>
        <div class="setrow"><label>保留份数</label>
          <input type="number" id="s-keep" min="1" max="20" value="${s.backup_keep || 3}"></div>
        <div class="set-note">备份放在 <code>${esc(s.data_dir || "")}\\backups\\</code>，
          超过份数的最旧文件自动删除。</div>
      </div>
      <div class="setcard">
        <h3>🧘 休息提醒</h3>
        <div class="setrow"><label>默认开启</label>
          <input type="checkbox" id="s-rest" ${chk(s.rest_remind)}></div>
        <div class="setrow"><label>间隔（分钟）</label>
          <input type="number" id="s-rest-int" min="5" max="240" step="5" value="${s.rest_interval_min || 45}"></div>
        <div class="set-note">每个用户在右上角 🧘 里也能单独调。</div>
      </div>
      <div class="setcard">
        <h3>⚙ 界面</h3>
        <div class="setrow"><label>站点名称</label>
          <input type="text" id="s-site" value="${esc(s.site_name || "FileLib")}"></div>
        <div class="setrow"><label>默认主题</label>
          <select id="s-theme">${THEMES.map(t =>
            `<option value="${t.id}" ${s.theme === t.id ? "selected" : ""}>${t.icon} ${t.name}</option>`).join("")}</select></div>
      </div>
      <div class="setcard">
        <h3>🔒 限流</h3>
        <div class="setrow"><label>普通用户每分钟请求数</label>
          <input type="number" id="s-rate" min="30" max="6000" value="${s.rate_limit_user || 600}"></div>
        <div class="set-note">管理员不受限。防止普通账号疯狂刷新拖垮程序。</div>
      </div>
      <div class="setcard">
        <h3>🧩 环境</h3>
        <div class="setrow"><label>ffmpeg</label>
          <span style="font-size:12px">${s.ffmpeg ? "✅ " + esc(s.ffmpeg) : "❌ 未找到（视频封面/时长拿不到）"}</span></div>
        <div class="setrow"><label>Calibre</label>
          <span style="font-size:12px">${s.calibre ? "✅ " + esc(s.calibre) : "❌ 未找到（AZW3/MOBI 在线阅读受限）"}</span></div>
        <div class="setrow"><label>补封面用 Calibre 兜底</label>
          <input type="checkbox" id="s-cover-calibre" ${chk(s.cover_calibre)}></div>
        <div class="set-note">开启后，提取不到内嵌封面的 AZW3/MOBI 会调用 Calibre 转换兜底（每本可达数十秒，明显拖慢补封面）；默认关。按需生成封面始终不用兜底。</div>
        <div class="setrow"><label>便携模式</label>
          <span style="font-size:12px">${s.portable ? "✅ 数据在程序目录" : "使用系统数据目录"}</span></div>
        <div class="setrow"><label>版本</label><span style="font-size:12px">${esc(s.version || "")}</span></div>
        <div class="set-note">数据目录：<code>${esc(s.data_dir || "")}</code></div>
      </div>
    </div>
    <div style="margin-top:16px;display:flex;gap:8px">
      <button class="btn acc" id="set-save">保存设置</button>
      <button class="btn" id="set-reload">重新读取</button>
    </div>`;

  $("#set-save").onclick = async () => {
    const body = {
      scan_daily_enabled: $("#s-scan-daily").checked,
      scan_daily_time: $("#s-scan-time").value || "03:00",
      scan_temp_enabled: $("#s-scan-temp").checked,
      scan_temp_interval_min: +$("#s-scan-int").value || 30,
      auto_backup: $("#s-backup").checked,
      backup_keep: +$("#s-keep").value || 3,
      rest_remind: $("#s-rest").checked,
      rest_interval_min: +$("#s-rest-int").value || 45,
      site_name: $("#s-site").value.trim() || "FileLib",
      theme: $("#s-theme").value,
      rate_limit_user: +$("#s-rate").value || 600,
      cover_calibre: $("#s-cover-calibre").checked,
    };
    try {
      await api("/api/admin/settings", post(body));
      $("#brand-name").textContent = body.site_name;
      document.title = body.site_name + " · 本地资料库";
      toast("设置已保存");
    } catch (e) { alert(e.message); }
  };
  $("#set-reload").onclick = renderAdmin;
}

/* --- 日志 --- */
async function adminLogs(el) {
  const r = await api("/api/admin/logs");
  el.innerHTML = `
    <div style="display:flex;gap:6px;margin-bottom:12px">
      <button class="btn small acc" data-lt="logins">登录记录</button>
      <button class="btn small" data-lt="audit">操作审计</button>
      <button class="btn small" data-lt="backups">备份</button>
    </div>
    <div id="log-body"></div>`;
  const render1 = k => {
    const box = $("#log-body");
    if (k === "logins") {
      box.innerHTML = `<table class="admtable"><thead><tr><th style="width:150px">时间</th>
        <th style="width:150px">用户</th><th style="width:150px">IP</th><th>结果</th></tr></thead><tbody>
        ${(r.logins || []).map(l => `<tr class="${l.ok ? "" : "dis"}">
          <td>${fmtTime(l.ts)}</td><td>${esc(l.username || "-")}</td><td>${esc(l.ip || "-")}</td>
          <td>${l.ok ? "✓ 成功" : "✗ " + esc(l.reason || "失败")}</td></tr>`).join("")
          || `<tr><td colspan="4"><small style="color:var(--fg2)">暂无记录</small></td></tr>`}
        </tbody></table>`;
    } else if (k === "audit") {
      box.innerHTML = `<table class="admtable"><thead><tr><th style="width:150px">时间</th>
        <th style="width:120px">用户</th><th style="width:150px">动作</th><th>详情</th>
        <th style="width:130px">IP</th></tr></thead><tbody>
        ${(r.audit || []).map(a => `<tr><td>${fmtTime(a.ts)}</td><td>${esc(a.username || "-")}</td>
          <td>${esc(a.action || "")}</td><td><small>${esc((a.detail || "").slice(0, 160))}</small></td>
          <td><small>${esc(a.ip || "")}</small></td></tr>`).join("")
          || `<tr><td colspan="5"><small style="color:var(--fg2)">暂无记录</small></td></tr>`}
        </tbody></table>`;
    } else {
      box.innerHTML = (r.backups || []).length
        ? `<table class="admtable"><thead><tr><th>文件</th><th style="width:120px">大小</th>
           <th style="width:180px">时间</th></tr></thead><tbody>
           ${r.backups.map(b => `<tr><td>${esc(b.name || b.path || "")}</td>
             <td>${fmtSize(b.size || 0)}</td><td>${b.mtime ? fmtTime(b.mtime) : ""}</td></tr>`).join("")}
           </tbody></table>`
        : `<div class="empty">暂无备份</div>`;
    }
  };
  $$("[data-lt]", el).forEach(b => b.onclick = () => {
    $$("[data-lt]", el).forEach(x => x.classList.toggle("acc", x === b));
    render1(b.dataset.lt);
  });
  render1("logins");
}

/* --- 安全 --- */
async function adminSecurity(el) {
  const o = await api("/api/admin/overview");
  el.innerHTML = `
    <div class="sec-note">
      <b>已默认开启的防护</b><br>
      · 密码 PBKDF2-SHA256（12 万轮 + 每用户随机盐），库被拷走也难以反推<br>
      · 会话存库 + HttpOnly Cookie（SameSite=Lax）：关浏览器不掉线，JS 偷不到<br>
      · 所有写操作校验 <code>X-Requested-With</code> 自定义头与 Origin，防 CSRF<br>
      · 校验 Host，防 DNS Rebinding 攻击<br>
      · 登录失败指数退避锁定（IP + 账号双维度）<br>
      · 普通用户接口限流 + 并发播放上限<br>
      · 静态文件路径穿越防护、URL 与请求体长度上限、CSP 等安全响应头<br>
      · 「打开 / 定位文件」仅允许服务器本机触发；删除一律进回收站
    </div>
    <div class="setgrid">
      <div class="setcard">
        <h3>📡 局域网访问</h3>
        <div class="set-note">
          本机：<code>http://127.0.0.1:${location.port || "8000"}</code><br>
          手机/平板：<code>http://服务器IP:${location.port || "8000"}</code><br>
          只在信任的家庭/内网开启，公网请勿直接暴露。
        </div>
      </div>
      <div class="setcard">
        <h3>🧾 数据都在本机</h3>
        <div class="set-note">
          索引库：<code>${esc(o.data_dir || "")}\\index.db</code>（${fmtSize(o.db_size)}）<br>
          封面缓存：<code>thumbs\\</code> · 书封：<code>covers\\</code><br>
          正文缓存：<code>booktext\\</code> · 备份：<code>backups\\</code><br>
          密钥：<code>secret.key</code>（删掉后需重设密码）
        </div>
      </div>
      <div class="setcard">
        <h3>🧹 一键清缓存</h3>
        <div class="set-note">打开 <b>⚙ 设置</b> 旁的 <b>🧹 数据</b> 页，
          可以在线清理缩略图 / 书封 / 正文缓存 / 自动备份，并查看各目录占用。<br>
          索引库压缩与完全初始化请停服后运行 <code>python cleanup.py</code>。</div>
      </div>
    </div>`;
}

/* --- 数据管理 --- */
async function adminData(el) {
  const d = await api("/api/admin/data");
  const total = d.total_size || 1;
  const colors = { thumbs: "#8e7cc3", covers: "#e69138", booktext: "#6fa8dc", backups: "#e06666" };
  const share = d.parts.filter(p => p.target && p.size > 0).map(p =>
    `<i style="width:${Math.max(0.5, (p.size / total) * 100).toFixed(2)}%;background:${colors[p.target]}"></i>`).join("");
  const tips = {
    thumbs: "删了会按需重新生成，只是头一次浏览变慢",
    covers: "删了下次打开书会重新抓取",
    booktext: "删了下次在线阅读会重新解析",
    backups: "最占空间，优先清它；下次扫描会自动重建",
  };
  el.innerHTML = `
    <div class="sec-note">
      <b>数据目录：</b><code>${esc(d.data_dir)}</code> · 合计 <b>${esc(d.total_human)}</b><br>
      这里全是「索引 + 缓存」。下面的清理操作不会碰到磁盘上的原始文件。
    </div>
    <div class="datashare" title="各项占用占比（彩色=可在线清理）">${share || '<i style="width:100%;background:var(--line)"></i>'}</div>
    <table class="admtable">
      <thead><tr><th>组成</th><th style="width:90px">大小</th><th style="width:80px">文件数</th>
        <th>说明</th><th style="width:90px"></th></tr></thead>
      <tbody>
      ${d.parts.map(p => {
        const isDb = p.path.endsWith("index.db");
        const note = p.target ? tips[p.target]
          : isDb ? "唯一真正存数据的文件：索引/账号/标记/进度/统计全在这，网页端不提供删除"
          : p.path.endsWith("secret.key") ? "本机密钥，删掉后已存密码无法解密，需重设密码"
          : (p.path.endsWith("-wal") || p.path.endsWith("-shm")) ? "运行临时文件，关服务自动归零，不用管" : "";
        return `<tr>
          <td><b>${esc(p.name)}</b></td>
          <td>${esc(p.human)}</td>
          <td>${p.files ? p.files.toLocaleString() : "—"}</td>
          <td><small style="color:var(--fg2)">${esc(note)}</small></td>
          <td>${p.target ? `<button class="btn small danger" data-clean="${p.target}">清理</button>` : ""}</td>
        </tr>`;
      }).join("")}
      </tbody>
    </table>
    <h3 class="admlog">已登记的资料库索引</h3>
    <div class="admlogs">
      ${(d.libraries || []).map(l => `<div class="admlogrow">
        <span>#${l.vol_id}</span><b>${esc(l.label || l.root)}</b>
        <span>${(l.n || 0).toLocaleString()} 条 · ${fmtSize(l.size || 0)}</span>
        <span>${l.online ? "✓ 在线" : "✗ 离线（盘没插？）"}</span>
      </div>`).join("") || `<small style="color:var(--fg2)">还没有登记任何资料库</small>`}
      <small style="color:var(--fg2)">要删除某个资料库的索引：切到「📚 资料库」页操作，原文件不受影响。</small>
    </div>
    <div class="setgrid">
      <div class="setcard">
        <h3>💾 备份瘦身（推荐先做）</h3>
        <div class="set-note">
          当前 <b>${(d.backups || []).length}</b> 份备份，合计
          <b>${fmtSize((d.backups || []).reduce((s, b) => s + (b.size || 0), 0))}</b>，
          设置保留 <b>${d.backup_keep || 3}</b> 份。<br>
          每份备份都是完整的 index.db 拷贝，几十万文件时单份可达数 GB。
        </div>
        <button class="btn small" id="btn-prune-backups">只保留最新 ${d.backup_keep || 3} 份</button>
      </div>
      <div class="setcard">
        <h3>🧯 清理会怎样</h3>
        <div class="set-note">
          缩略图 / 书封 / 正文缓存：删完自动重建，数据零损失，只是第一次浏览变慢。<br>
          自动备份：删掉后等下次扫描重建。<br>
          清理是后台任务，大目录（几万个文件）要跑一会儿，右下角有进度。
        </div>
      </div>
      <div class="setcard">
        <h3>🏭 压缩 / 初始化（需停服）</h3>
        <div class="set-note">
          索引库本体与密钥不在网页端提供清理（服务运行中自删有风险）。
          关闭 FileLib 后，在程序目录运行以下命令（点「复制」再粘贴到 PowerShell）：${d.flib_env ? `<br>本服务使用了自定义数据目录，命令已带上 <code>FILELIB_DATA</code>` : ""}
        </div>
        ${[["压缩索引库 · 回收磁盘空间（" + (d.db_media_human || "") + " 里可能有大量可回收空间）", "python cleanup.py --vacuum"],
           ["只清索引重新建库（重扫一次即恢复）", "python cleanup.py --db"],
           ["完全回到初始状态（慎用）", "python cleanup.py --all --yes"]]
          .map(([desc, cmd]) => {
            const full = (d.flib_env ? `$env:FILELIB_DATA='${d.flib_env}'\n` : "") + cmd;
            return `<div class="set-note">${esc(desc)}</div>
              <div class="cmdline"><code>${esc(full)}</code>
                <button class="btn small" data-cmd="${esc(full)}">复制</button></div>`;
          }).join("")}
      </div>
    </div>`;

  $$("#admin-body [data-clean]").forEach(b => b.onclick = async () => {
    const t = b.dataset.clean;
    const name = { thumbs: "缩略图缓存", covers: "书封缓存", booktext: "正文缓存", backups: "自动备份" }[t];
    if (!confirm(`清理${name}？\n\n删除后可自动重建，原始文件不受影响。`)) return;
    try {
      await api("/api/admin/cleanup", post({ target: t }));
      toast("清理已开始…");
      watchTaskOnce();
      const iv = setInterval(async () => {
        try {
          const tk = await api("/api/task");
          if (tk.state !== "running") { clearInterval(iv); adminData(el).catch(() => {}); }
        } catch { clearInterval(iv); }
      }, 1500);
    } catch (e) { alert(e.message); }
  });

  const pb = $("#btn-prune-backups");
  if (pb) pb.onclick = async () => {
    const n = d.backup_keep || 3;
    if (!confirm(`只保留最新 ${n} 份备份，删除更旧的？\n\n最新 ${n} 份不动，随时可用来恢复。`)) return;
    try {
      const r = await api("/api/admin/cleanup", post({ target: "backups", keep: n }));
      if (r.deleted === 0) { toast(r.message || "备份不多，无需清理"); return; }
      toast("备份清理已开始…");
      watchTaskOnce();
      const iv = setInterval(async () => {
        try {
          const tk = await api("/api/task");
          if (tk.state !== "running") { clearInterval(iv); adminData(el).catch(() => {}); }
        } catch { clearInterval(iv); }
      }, 1500);
    } catch (e) { alert(e.message); }
  };

  $$("#admin-body [data-cmd]").forEach(b => b.onclick = async () => {
    try {
      await navigator.clipboard.writeText(b.dataset.cmd);
      toast("已复制，停服后粘贴到 PowerShell 运行");
    } catch {
      prompt("自动复制失败，请手动复制以下命令：", b.dataset.cmd);
    }
  });
}

/* ---------------- 启动 ---------------- */

/* PWA：注册 Service Worker（仅 https 或本机；失败静默，零影响）
   版本号走注册 URL 的 ?v=，升级后 SW 缓存名自动轮换 */
function registerSW() {
  try {
    if (!("serviceWorker" in navigator)) return;
    if (location.protocol !== "https:"
        && !["127.0.0.1", "localhost"].includes(location.hostname)) return;
    navigator.serviceWorker
      .register("/sw.js?v=" + encodeURIComponent(ME.version || "1"))
      .catch(() => {});
  } catch {}
}

function bindUI() {
  $("#btn-sidebar").onclick = () => setSidebar(!sidebarOpen());
  $("#sb-backdrop").onclick = () => setSidebar(false);
  $("#btn-theme").onclick = cycleTheme;
  $("#btn-theme").oncontextmenu = e => {
    e.preventDefault();
    openModal(`<h2>🎨 选择主题</h2>
      <div class="setgrid" style="grid-template-columns:1fr 1fr">
        ${THEMES.map(t => `<button class="btn" data-t="${t.id}" style="justify-content:flex-start">
          ${t.icon} ${t.name}${document.body.dataset.theme === t.id ? " ✓" : ""}</button>`).join("")}
      </div>
      <div class="modal-btns"><button class="btn acc" data-close>关闭</button></div>`);
    $$("#modal-box [data-t]").forEach(b => b.onclick = () => { applyTheme(b.dataset.t); closeModal(); });
  };
  $("#btn-help").onclick = () => {
    $("#help-grid").innerHTML = SHORTCUTS.map(([k, d]) =>
      `<div class="hk"><kbd>${esc(k)}</kbd><span>${esc(d)}</span></div>`).join("");
    $("#help").classList.remove("hidden");
  };
  $("#help-close").onclick = () => $("#help").classList.add("hidden");
  $("#help").onclick = e => { if (e.target.id === "help") $("#help").classList.add("hidden"); };

  $("#btn-rescan").onclick = () => doScan();
  $("#btn-tools").onclick = openToolsMenu;
  $("#btn-zip").onclick = () => {
    if (state.view !== "list" || state.fulltext) return toast("请先进入一个文件列表");
    if (state.volId == null && !isAdmin()) return toast("请先选择一个资料库");
    const qs = new URLSearchParams();
    if (state.volId != null) qs.set("vol", state.volId);
    if (state.dir) { qs.set("dir", state.dir); qs.set("recursive", "1"); }
    if (state.q) qs.set("q", state.q);
    if (state.kind) qs.set("kind", state.kind);
    if (state.mark) qs.set("mark", state.mark);
    if (state.tag) qs.set("tag", state.tag);
    qs.set("sort", state.sort === "mtime_desc" ? "name_asc" : (state.sort || "name_asc"));
    qs.set("limit", "100");
    toast("开始打包（≤100 个 / 2GB），完成后浏览器自动保存", 2800);
    location.href = "/download_zip?" + qs.toString();
  };
  $("#btn-newdir").onclick = async () => {
    if (state.volId == null) return toast("请先选择一个资料库");
    const name = await uiPrompt("新建文件夹",
      { hint: `位置：${state.dir || "资料库根目录"}`, placeholder: "文件夹名称" });
    if (!name || !name.trim()) return;
    try {
      const r = await api("/api/fm_mkdir", post({ vol: state.volId, rel: state.dir || "", name: name.trim() }));
      toast(`已创建文件夹「${r.name}」`);
      loadTree().catch(() => {});
    } catch (e) { toast(e.message, 2600); }
  };
  $("#toolsmenu").onclick = e => e.stopPropagation();
  $("#lib-current").onclick = e => {
    e.stopPropagation();
    $("#lib-menu").classList.toggle("hidden");
  };
  $("#lib-menu").onclick = e => e.stopPropagation();
  document.addEventListener("click", () => {
    LP_FIRED = false;
    $("#lib-menu").classList.add("hidden");
    $("#toolsmenu").classList.add("hidden");
    $("#ctxmenu").classList.add("hidden");
  });

  $("#detail-close").onclick = closeDetail;
  $("#rd-next").onclick = rdNext;
  $("#rd-prev").onclick = rdPrev;
  $("#rd-close").onclick = closeReader;
  $("#rd-tts").onclick = ttsToggle;
  $("#rd-tts-rate").onclick = ttsCycleRate;
  $("#rd-toc").onclick = () => {
    if (RD.kind !== "chapters") return toast("这个格式没有章节目录");
    $("#rd-side").classList.toggle("hidden");
  };
  $("#rd-font+").onclick = () => {
    RD.fontSize = Math.min(30, RD.fontSize + 1);
    const t = $("#rd-body .reader-text");
    if (t) t.style.fontSize = RD.fontSize + "px";
    try { localStorage.setItem("filelib.font", RD.fontSize); } catch {}
  };
  $("#rd-font-").onclick = () => {
    RD.fontSize = Math.max(12, RD.fontSize - 1);
    const t = $("#rd-body .reader-text");
    if (t) t.style.fontSize = RD.fontSize + "px";
    try { localStorage.setItem("filelib.font", RD.fontSize); } catch {}
  };
  $("#rd-mark").onclick = async () => {
    if (!RD.id) return;
    await toggleMark(RD.id, "read", true);
    $("#rd-mark").textContent = "✅ 已标为已读";
    setTimeout(() => { $("#rd-mark").textContent = "✅ 已读"; }, 1500);
  };
  // 触屏点屏幕左/右边缘翻页（仅分页文本阅读；中缝 30% 不响应，
  // 点到链接/按钮/搜索命中标记也不翻页；桌面鼠标用户不受影响）
  $("#rd-body").addEventListener("click", e => {
    if (RD.kind !== "text") return;
    if (window.matchMedia && !window.matchMedia("(pointer: coarse)").matches) return;
    if (e.target.closest("a, button, input, mark, select, textarea")) return;
    const x = e.clientX / Math.max(1, window.innerWidth);
    if (x < 0.3) rdPrev();
    else if (x > 0.7) rdNext();
  });
  $("#rd-dl").onclick = () => {
    if (!RD.id) return;
    location.href = "/download/" + RD.id + "." + (RD.name.split(".").pop() || "bin");
  };
  $("#rd-jump").addEventListener("keydown", e => {
    if (e.key !== "Enter") return;
    const n = parseInt(e.target.value, 10);
    e.target.value = "";
    if (!n) return;
    if (RD.kind === "text") { RD.page = n - 1; renderPage(); }
    else if (RD.kind === "chapters") { RD.ch = Math.min(n - 1, RD.chapters.length - 1); renderChapter(); }
  });
  $("#rd-body").addEventListener("scroll", debounce(e => {
    const el = e.target;
    if (RD.kind !== "text") return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 60 && RD.page < RD.pages.length - 1) {
      const at = el.scrollTop;
      RD.page++;
      renderPage();
      el.scrollTop = at;   // 连续滚动阅读，不跳回顶部
    }
  }, 150));

  // 播放器
  $("#pl-close").onclick = closePlayer;
  $("#pl-play").onclick = togglePlay;
  $("#pl-back").onclick = () => seekBy(-10);
  $("#pl-fwd").onclick = () => seekBy(10);
  $("#pl-prev").onclick = () => plStep(-1);
  $("#pl-next").onclick = () => plStep(1);
  $("#pl-mute").onclick = () => {
    const v = video();
    v.muted = !v.muted;
    $("#pl-mute").textContent = v.muted ? "🔇" : "🔊";
  };
  $("#pl-vol").oninput = e => {
    const v = video();
    v.volume = +e.target.value;
    v.muted = v.volume === 0;
    $("#pl-mute").textContent = v.muted ? "🔇" : "🔊";
  };
  const RATES = [1, 1.25, 1.5, 1.75, 2, 0.75];
  $("#pl-rate").onclick = () => {
    const v = video();
    const i = RATES.indexOf(v.playbackRate);
    const r = RATES[(i + 1) % RATES.length];
    v.playbackRate = r;
    $("#pl-rate").textContent = r + "×";
    hint(r + "×");
  };
  $("#pl-pip").onclick = async () => {
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else await video().requestPictureInPicture();
    } catch { toast("当前浏览器不支持画中画"); }
  };
  $("#pl-fs").onclick = toggleFullscreen;

  // 播放失败兜底：浏览器解不了码（avi/rmvb/wma/ape…）或文件缺失时给出明确出路
  // （error 的完整降级链在下方 bindUI 的 error 监听里：直连失败 → 转码重试 → 兜底层）
  const V = $("#pl-video");
  V.addEventListener("loadedmetadata", hideFallback);
  $("#plf-close").onclick = closePlayer;
  $("#plf-dl").onclick = () => {
    const cur = PL.list[PL.idx];
    if (cur) location.href = "/download/" + cur.id + "." + (cur.ext || "bin");
  };
  $("#plf-open").onclick = async () => {
    try {
      await api("/api/open", post({ id: PL.id }));
      toast("已交给系统播放器");
    } catch (e) { toast(e.message || "仅管理员可在本机使用"); }
  };

  const sw = $("#pl-seekwrap");
  let seeking = false;
  // 悬停时间气泡（动态创建，避免改 HTML）
  const tip = document.createElement("div");
  tip.className = "pl-seek-tip";
  tip.textContent = "0:00";
  sw.appendChild(tip);
  const seekAt = (e, preview) => {
    const r = sw.getBoundingClientRect();
    const x = (e.clientX !== undefined ? e.clientX : 0) - r.left;
    const pct = Math.max(0, Math.min(1, x / r.width));
    const dur = plDuration();
    if (preview || !dur) {
      // 悬停/拖动预览：气泡显示目标时间；转码流拖动中旋钮跟手（松手才真正重启转码）
      if (dur) {
        tip.textContent = fmtDur(pct * dur);
        if (PL.tc && seeking) { PL.tcPending = pct * dur; paintSeek(); }
      }
      return;
    }
    const t = pct * dur;
    if (PL.tc) { tcSeek(t); paintSeek(); }   // 转码流：重启 ffmpeg 从目标点出流
    else video().currentTime = t;
    paintSeek();
  };
  sw.addEventListener("pointerenter", () => sw.classList.add("hovering"));
  sw.addEventListener("pointerleave", () => sw.classList.remove("hovering"));
  sw.addEventListener("pointermove", e => {
    // 气泡跟随鼠标
    const r = sw.getBoundingClientRect();
    const x = Math.max(24, Math.min(r.width - 24, e.clientX - r.left));
    tip.style.left = x + "px";
    seekAt(e, true);   // 悬停与拖动都刷新气泡；拖动中转码流同时让旋钮跟手
  });
  sw.addEventListener("pointerdown", e => {
    seeking = true;
    sw.classList.add("dragging");
    $("#player").classList.add("dragging");   // 拖动期间控制条不自动隐藏
    try { sw.setPointerCapture(e.pointerId); } catch {}
    seekAt(e, PL.tc);   // 转码流按下只预览（重启转码代价高），松手才真正跳转
  });
  const endSeek = e => {
    if (!seeking) return;
    seeking = false;
    sw.classList.remove("dragging");
    $("#player").classList.remove("dragging");
    try { sw.releasePointerCapture(e.pointerId); } catch {}
    if (e && e.clientX !== undefined) seekAt(e);   // 松手落到准确位置
    else if (PL.tc && PL.tcPending != null) tcSeek(PL.tcPending);   // pointercancel：落在最后预览位置，别让进度条悬空
  };
  sw.addEventListener("pointerup", endSeek);
  sw.addEventListener("pointercancel", endSeek);

  const v = $("#pl-video");
  v.addEventListener("timeupdate", paintSeek);
  v.addEventListener("durationchange", paintSeek);
  v.addEventListener("progress", paintSeek);
  v.addEventListener("loadstart", () => { if (PL.tc) setTcLoad(true); });   // 转码流（含 seek 重启）出帧前显示缓冲层
  v.addEventListener("loadeddata", () => { PL.tcPending = null; setTcLoad(false); paintSeek(); });   // 新流首帧就绪，恢复跟随
  v.addEventListener("playing", () => { PL.tcPending = null; setTcLoad(false); });
  v.addEventListener("play", () => {
    $("#pl-play").textContent = "⏸";
    $("#plm-play").textContent = "⏸";
    showCtrl();
  });
  v.addEventListener("pause", () => {
    $("#pl-play").textContent = "▶";
    $("#plm-play").textContent = "▶";
    $("#player").classList.remove("idle");
    saveProgress();
  });
  v.addEventListener("timeupdate", () => {
    if ($("#player").classList.contains("mini"))
      $("#plm-time").textContent = fmtDur(plCurrent());
  });
  v.addEventListener("ended", () => {
    saveProgress();
    if (PL.id) api("/api/mark", post({ id: PL.id, mark: "read" })).catch(() => {});
    if (PL.list.length > 1) setTimeout(() => plStep(1), 600);
  });
  v.addEventListener("error", () => {
    if (!PL.id) return;
    // 降级链：直连失败且服务端有 ffmpeg → 老格式自动切实时转码再试一次
    if (!PL.triedTC && TC_OK && PL.ext && TC_RETRY.has(PL.ext)) {
      PL.triedTC = true;
      PL.tc = true; PL.tcBase = 0; PL.tcDur = 0; PL.tcPending = null;
      $("#pl-tcbadge").classList.remove("hidden");
      v.src = `/transcode/${PL.id}.${PL.ext}?r=${Date.now()}`;
      v.play().catch(() => {});
      toast("直连失败，已切换为实时转码播放", 3000);
      return;
    }
    const code = v.error ? v.error.code : 0;
    if (code === 2) toast("读取文件失败，可能外接盘断开", 5000);
    else if (PL.triedTC) toast("直连与转码均失败：文件可能已损坏或编码不受支持", 5000);
    else toast("这个格式浏览器解不了码（MKV/HEVC 常见），可右键「用本机程序打开」", 6000);
    showFallback();
  });
  v.addEventListener("waiting", () => { if (PL.tc) { hint("转码缓冲中…"); setTcLoad(true); } });
  v.addEventListener("timeupdate", debounce(() => { if (PL.id && !v.paused) saveProgress(); }, 15000));
  v.addEventListener("click", () => { togglePlay(); showCtrl(); });
  v.addEventListener("dblclick", toggleFullscreen);
  $("#player").addEventListener("mousemove", showCtrl);
  window.addEventListener("beforeunload", () => { if (PL.id) saveProgress(); });

  // 大图
  $("#lb-prev").onclick = e => { e.stopPropagation(); lbIndex--; showLb(); };
  $("#lb-next").onclick = e => { e.stopPropagation(); lbIndex++; showLb(); };
  $("#lb-close").onclick = closeLightbox;
  $("#lb-slide").onclick = e => { e.stopPropagation(); lbSlideToggle(); };
  $("#lb-zoom").onclick = () => $("#lb-img").classList.toggle("zoom");

  // 迷你播放条
  $("#pl-close").onclick = closeOrMinimize;
  $("#plm-play").onclick = () => togglePlay();
  $("#plm-expand").onclick = () => {
    $("#player").classList.remove("mini");
    $("#pl-mini").classList.add("hidden");
    showCtrl();
  };
  $("#plm-close").onclick = closePlayer;
  $("#lb-img").onclick = () => $("#lb-img").classList.toggle("zoom");
  $("#lightbox").onclick = e => { if (e.target.id === "lightbox") closeLightbox(); };

  // 任务浮窗
  $("#tb-stop").onclick = async () => {
    await api("/api/stop", post({}));
    toast("已请求停止，正在收尾…");
  };
  $("#tb-min").onclick = () => $("#taskbar").classList.toggle("min");

  // 登录
  $("#login-go").onclick = doLogin;
  $("#login-pass").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
  $("#login-pass2").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
  $("#login-user").addEventListener("keydown", e => { if (e.key === "Enter") $("#login-pass").focus(); });
  $("#whoami").onclick = userMenu;

  // 管理后台
  $("#btn-admin").onclick = () => openAdmin("users");
  $("#admin-close").onclick = () => $("#adminpanel").classList.add("hidden");
  $$("#admin-tabs b").forEach(b => b.onclick = () => {
    ADMIN_TAB = b.dataset.tab;
    $$("#admin-tabs b").forEach(x => x.classList.toggle("on", x === b));
    renderAdmin();
  });

  // 侧栏维护工具
  $$("#sec-admin li[data-tool]").forEach(li => li.onclick = () => {
    const t = li.dataset.tool;
    if (t === "organize") doOrganize();
    else if (t === "scan") doScan();
    else if (t === "thumbs") doThumbs(false);
    else if (t === "fulltext") doFulltext();
    else if (t === "autotag") doAutotag();
    else if (t === "dups") showDups(true);
    else if (t === "crossdup") showCrossDups();
    else if (t === "junk") showJunk();
    else if (t === "backup") doBackup();
    else if (t === "library") openAdmin("libs");
  });

  // 侧栏分组折叠（浏览 / 我的 / 按类型 / 目录 / 标签 / 维护）
  bindNavGroups();
  applyGrpState();

  // 侧栏主菜单
  $$("#sec-main li[data-nav]").forEach(li => li.onclick = () => {
    const nav = li.dataset.nav;
    Object.assign(state, {
      kind: null, topic: null, series: null, author: null, tag: null,
      q: "", dir: "", fulltext: false, mark: null,
    });
    const si = $("#search");
    if (si) si.value = "";
    $("#search-full")?.classList.remove("on");
    if (["star", "read", "todo"].includes(nav)) {
      state.view = "list";
      state.mark = nav;
    } else {
      state.view = nav;
    }
    if (isMobile()) setSidebar(false);
    refresh();
  });

  // 目录树工具
  $("#tree-expand").onclick = e => {
    e.stopPropagation();
    if (!state.tree) return;
    for (const n of Object.values(state.tree.byRel)) {
      if (n.rel.split("/").filter(Boolean).length <= 1) state.openDirs.add(n.rel);
    }
    renderTree();
  };
  $("#tree-collapse").onclick = e => {
    e.stopPropagation();
    state.openDirs.clear();
    renderTree();
  };
  $("#tag-manage").onclick = e => {
    e.stopPropagation();
    openTagAdmin();
  };

  // 休息提醒
  $("#btn-rest").onclick = () => {
    $("#rs-on").checked = REST.on;
    $("#rs-int").value = REST.interval;
    $("#rs-idle").checked = REST.idlePause;
    $("#restset").classList.remove("hidden");
  };
  $("#rs-cancel").onclick = () => $("#restset").classList.add("hidden");
  $("#rs-ok").onclick = () => {
    REST.on = $("#rs-on").checked;
    REST.interval = Math.max(5, Math.min(240, parseInt($("#rs-int").value, 10) || 45));
    REST.idlePause = $("#rs-idle").checked;
    REST.shown = false;
    REST.startAt = Date.now();
    try {
      localStorage.setItem("filelib.rest", JSON.stringify({
        on: REST.on, interval: REST.interval, idle: REST.idlePause,
      }));
    } catch {}
    $("#restset").classList.add("hidden");
    $("#btn-rest").title = REST.on ? `休息提醒：每 ${REST.interval} 分钟` : "休息提醒（未开启）";
    toast(REST.on ? `已开启：每 ${REST.interval} 分钟提醒休息` : "已关闭休息提醒");
  };
  $("#rest-ok").onclick = () => {
    $("#rest").classList.add("hidden");
    REST.shown = false;
    REST.startAt = Date.now();
  };
  $("#rest-snooze").onclick = () => {
    $("#rest").classList.add("hidden");
    REST.shown = false;
    REST.startAt = Date.now() - Math.max(0, REST.interval - 5) * 60 * 1000;
  };
  $("#rest-off").onclick = () => {
    $("#rest").classList.add("hidden");
    REST.shown = false;
    REST.offToday = true;
    try { localStorage.setItem("filelib.rest.offday", new Date().toISOString().slice(0, 10)); } catch {}
    toast("今天不再提醒，明天自动恢复");
  };

  bindSearch();
  window.addEventListener("resize", debounce(() => {
    if (!isMobile()) document.body.classList.remove("sb-open");
  }, 200));
  window.addEventListener("popstate", e => applyState(e.state || {}, false));

  const io = new IntersectionObserver(entries => {
    if (entries[0].isIntersecting && !state.done && !state.loading &&
        state.items.length && state.view === "list") {
      loadList(false);
    }
  }, { rootMargin: "700px" });
  io.observe($("#sentinel"));

  // 主滚动区：分块回收 + 回到顶部按钮显隐（合并到一个 passive 监听）
  const mainEl = $("#main");
  mainEl.addEventListener("scroll", () => {
    scheduleTrim();
    const t = $("#btn-top");
    // 阈值随视口高度走：手机两三屏就出现，桌面沿用约 2.4 屏
    if (t) t.classList.toggle("hidden",
      mainEl.scrollTop < Math.max(1200, window.innerHeight * 1.6));
  }, { passive: true });
  $("#btn-top").onclick = () => mainEl.scrollTo({ top: 0, behavior: "smooth" });
}

/* 标签管理器（admin）
   搜索 / 排序 / 来源筛选（含隐藏） / 多选批量（合并·隐藏·删除） / 单个改名。
   行点击 = 用该标签筛选文件（与侧栏点标签一致，保留基本功能）。
   隐藏 ≠ 删除：隐藏的标签不出现在侧栏与聚合里，文件筛选仍可用。 */
async function openTagAdmin() {
  const M = { view: "all", sort: "n", q: "", sel: new Set(), all: [] };
  const strip = s => String(s).replace(/^auto:/, "");
  openModal(`<h2>🏷 标签管理器</h2>
    <div class="tg-top">
      <input type="text" id="tg-q" placeholder="搜索标签…" autocomplete="off" spellcheck="false">
      <select id="tg-sort">
        <option value="n">按使用数</option>
        <option value="name">按名称</option>
      </select>
    </div>
    <div class="tg-tabs">
      <span class="tg-tab on" data-v="all">全部</span>
      <span class="tg-tab" data-v="user">人工</span>
      <span class="tg-tab" data-v="auto">自动</span>
      <span class="tg-tab" data-v="hidden">隐藏</span>
      <span id="tg-count" class="tg-count"></span>
    </div>
    <div id="tg-list" class="tg-list"></div>
    <div id="tg-bulk" class="tg-bulk hidden">
      <span id="tg-selcnt"></span>
      <button class="btn small acc" id="tg-merge" title="把其余选中标签的文件关联并入保留的那个">⧉ 合并…</button>
      <button class="btn small" id="tg-hidebtn">🚫 隐藏</button>
      <button class="btn small" id="tg-unhide">👁 取消隐藏</button>
      <button class="btn small danger" id="tg-del">🗑 删除</button>
      <button class="btn small ghost" id="tg-clearsel">取消选择</button>
    </div>
    <div class="modal-btns">
      <button class="btn" id="tag-new">＋ 新建标签</button>
      <button class="btn acc" data-close>关闭</button>
    </div>`);

  const syncBulk = () => {
    $("#tg-bulk").classList.toggle("hidden", M.sel.size === 0);
    $("#tg-selcnt").textContent = `已选 ${M.sel.size} 个`;
  };

  const render = () => {
    let list = M.all.filter(t =>
      M.view === "all" ? true :
      M.view === "hidden" ? t.hidden :
      M.view === "user" ? t.user : !t.user);
    if (M.q) {
      const k = M.q.toLowerCase();
      list = list.filter(t => strip(t.name).toLowerCase().includes(k));
    }
    list = list.slice().sort(M.sort === "name"
      ? (a, b) => strip(a.name).localeCompare(strip(b.name), "zh")
      : (a, b) => (b.n - a.n) || strip(a.name).localeCompare(strip(b.name), "zh"));
    const nU = M.all.filter(t => t.user).length;
    const nH = M.all.filter(t => t.hidden).length;
    $("#tg-count").textContent =
      `共 ${M.all.length} · 人工 ${nU} · 自动 ${M.all.length - nU}` + (nH ? ` · 隐藏 ${nH}` : "");
    $("#tg-list").innerHTML = list.length ? list.map(t => {
      const nm = esc(t.name), disp = esc(strip(t.name));
      const badge = t.hidden ? `<span class="tg-badge hid">隐藏</span>`
        : t.user ? "" : `<span class="tg-badge">自动</span>`;
      const flag = t.generic ? `<span class="tg-flag" title="出现在超过 30% 的文件上，区分度低">泛</span>`
        : (t.rare ? `<span class="tg-flag" title="少于 3 个文件在用，多为噪声">微</span>` : "");
      return `<div class="tg-row ${t.hidden ? "ishid" : ""}" data-nm="${nm}">
        <input type="checkbox" class="tg-ck" data-nm="${nm}" ${M.sel.has(t.name) ? "checked" : ""}>
        <span class="tg-name" title="${nm}（点击筛选该标签的文件）">${disp}</span>${badge}${flag}
        <span class="tg-n">${(t.n || 0).toLocaleString()}</span>
        <span class="tg-ops">
          ${t.user ? `<button class="btn small ghost" data-rn="${nm}">改名</button>` : ""}
          <button class="btn small ghost" data-hd="${nm}" data-on="${t.hidden ? "0" : "1"}">${t.hidden ? "取消隐藏" : "隐藏"}</button>
          <button class="btn small danger" data-rmt="${nm}">删除</button>
        </span></div>`;
    }).join("") : `<div class="empty">没有匹配的标签</div>`;
    syncBulk();
    bindRows();
  };

  const reload = async () => {
    const r = await api(withVol("/api/tags?limit=2000&show_hidden=1"));
    M.all = r.items || [];
    render();
    loadTags();                    // 侧栏同步
  };

  const bulkSetHidden = async on => {
    for (const n of [...M.sel]) {
      try { await api("/api/tag_admin", post({ action: "hide", name: n, on })); }
      catch (err) { toast(err.message, 2600); break; }
    }
    M.sel.clear();
    await reload();
  };

  const bindRows = () => {
    $$("#tg-list .tg-ck").forEach(ck => ck.onchange = () => {
      if (ck.checked) M.sel.add(ck.dataset.nm); else M.sel.delete(ck.dataset.nm);
      syncBulk();
    });
    $$("#tg-list .tg-row").forEach(row => row.onclick = e => {
      if (e.target.closest("input,button")) return;
      // 行点击 = 用该标签筛选文件（与侧栏点标签一致）
      state.tag = row.dataset.nm;
      Object.assign(state, { view: "list", dir: "", kind: null, topic: null,
        series: null, author: null, mark: null });
      closeModal();
      refresh();
    });
    $$("#tg-list [data-hd]").forEach(b => b.onclick = async e => {
      e.stopPropagation();
      try { await api("/api/tag_admin", post({ action: "hide", name: b.dataset.hd, on: b.dataset.on === "1" })); }
      catch (err) { toast(err.message, 2600); return; }
      await reload();
    });
    $$("#tg-list [data-rmt]").forEach(b => b.onclick = async e => {
      e.stopPropagation();
      if (!confirm(`删除标签「${strip(b.dataset.rmt)}」？（只删标签，不删文件）`)) return;
      try { await api("/api/tag_admin", post({ action: "delete", name: b.dataset.rmt })); }
      catch (err) { toast(err.message, 2600); return; }
      M.sel.delete(b.dataset.rmt);
      await reload();
    });
    $$("#tg-list [data-rn]").forEach(b => b.onclick = async e => {
      e.stopPropagation();
      const old = b.dataset.rn;
      const nn = (await uiPrompt(`重命名标签「${strip(old)}」`, { value: strip(old) }))?.trim();
      if (!nn || nn === old) return;
      try { await api("/api/tag_admin", post({ action: "rename", old, new: nn })); }
      catch (err) { toast(err.message, 2600); }
      openTagAdmin();              // uiPrompt 关过浮层，重建管理器
    });
  };

  $$(".tg-tabs .tg-tab").forEach(s => s.onclick = () => {
    M.view = s.dataset.v;
    $$(".tg-tabs .tg-tab").forEach(x => x.classList.toggle("on", x === s));
    render();
  });
  $("#tg-q").oninput = debounce(e => { M.q = e.target.value.trim(); render(); }, 150);
  $("#tg-sort").onchange = e => { M.sort = e.target.value; render(); };
  $("#tg-clearsel").onclick = () => { M.sel.clear(); render(); };
  $("#tg-hidebtn").onclick = () => bulkSetHidden(true);
  $("#tg-unhide").onclick = () => bulkSetHidden(false);
  $("#tg-del").onclick = async () => {
    if (!confirm(`删除选中的 ${M.sel.size} 个标签？（只删标签，不删文件）`)) return;
    for (const n of [...M.sel]) {
      try { await api("/api/tag_admin", post({ action: "delete", name: n })); }
      catch (err) { toast(err.message, 2600); break; }
    }
    M.sel.clear();
    await reload();
  };
  $("#tg-merge").onclick = () => {
    const names = [...M.sel];
    if (names.length < 2) { toast("至少选择 2 个标签才能合并"); return; }
    openModal(`<h2>⧉ 合并标签</h2>
      <div class="modal-note">保留一个目标标签，其余标签的文件关联全部并入它（不删任何文件）。
        合并到人工标签后，相关关联会转为人工标签，不受「清空自动」影响——这也是把
        自动标签固定下来的办法。</div>
      <div class="tg-radio">${names.map((n, i) =>
        `<label><input type="radio" name="tg-dst" value="${esc(n)}" ${i === 0 ? "checked" : ""}>
          <span>${esc(strip(n))}</span></label>`).join("")}</div>
      <div class="modal-btns">
        <button class="btn" data-close>取消</button>
        <button class="btn acc" id="tg-merge-ok">合并</button>
      </div>`);
    $("#tg-merge-ok").onclick = async () => {
      const dst = ($("input[name='tg-dst']:checked") || {}).value;
      if (!dst) return;
      const srcs = names.filter(n => n !== dst);
      let okc = 0;
      for (const s of srcs) {
        try {
          const rr = await api("/api/tag_admin", post({ action: "merge", src: s, dst }));
          if (rr && rr.ok) okc++;
        } catch (err) { toast(err.message, 3000); break; }
      }
      toast(`已合并 ${okc} 个标签到「${strip(dst)}」`);
      openTagAdmin();
    };
  };
  $("#tag-new").onclick = async () => {
    const name = (await uiPrompt("新建标签", { placeholder: "标签名" }))?.trim();
    if (!name) return;
    try { await api("/api/tag_admin", post({ action: "create", name })); }
    catch (err) { toast(err.message, 2600); }
    loadTags();
    openTagAdmin();
  };

  await reload();
}

async function boot() {
  loadRest();
  applyTheme(document.body.dataset.theme || "light");
  try { RD.fontSize = +(localStorage.getItem("filelib.font") || 17) || 17; } catch {}

  try {
    const me = await api("/api/me");
    Object.assign(ME, me);
    TC_OK = !!ME.transcode;   // 服务端探测到 ffmpeg 才启用实时转码
  } catch (e) {
    $("#lib-name").textContent = "无法连接服务";
    $("#lib-sub").textContent = e.message;
    return;
  }
  registerSW();
  if (!ME.logged) {
    showLogin(!!ME.must_setup);
    return;
  }
  $("#login").classList.add("hidden");
  applyRoleUI();
  pollTask();

  // 权限：一个库都没有 → 空界面
  // 注意：必须先静默加载一次统计拿到 state.libs，再判断 —— 否则页面刚打开时
  // libs 为空，普通用户会被误判成"没有分配任何资料库"（刷新/深链必现）。
  if (!isAdmin()) {
    await loadStats(true).catch(() => {});
    if (!visibleLibs().length) {
      renderLibBar();
      markActive();
      showEmptyPerm();
      return;
    }
  }

  try {
    await loadStats();
    BOOT_GUIDED = true;
  } catch (e) {
    $("#lib-name").textContent = "索引读取失败";
    $("#lib-sub").textContent = e.message;
    return;
  }

  const us = parseURLState();
  if (us) {
    // 深链里的库可能已被删除 / 无权访问（分享出去的旧链接、权限调整后）：
    // 不校验就 applyState 会让后续所有请求 403，整页卡在错误态。
    // 非管理员保留不了"混合视图"（服务端限制），同样回退到引导已选好的库。
    let volFixed = false;
    if (us.vol !== undefined) {
      const vis = visibleLibs();
      if (us.vol == null && !isAdmin()) { delete us.vol; volFixed = true; }
      else if (us.vol != null && !vis.some(l => l.id === us.vol)) { delete us.vol; volFixed = true; }
    }
    await applyState({ ...us, vol: us.vol !== undefined ? us.vol : state.volId }, false);
    // 回退发生时用 replaceState 把真实状态写回地址栏（不新增历史记录，后退不循环）
    if (volFixed) pushHistory(true);
    return;
  }
  await Promise.all([loadTree(), loadTags()]);
  markActive();
  await render();
  pushHistory(true);
}

/* ---------------- 入口 ---------------- */

// bindUI 必须容错：哪怕某个按钮绑定失败，也要保证 boot() 能跑起来，
// 否则整个界面会“看起来能登录但什么都用不了”
try {
  bindUI();
} catch (e) {
  console.error("bindUI 初始化失败：", e);
  try { toast("界面初始化出错：" + (e && e.message ? e.message : e)); } catch (_) {}
}
boot();
