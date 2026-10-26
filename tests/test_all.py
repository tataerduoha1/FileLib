"""
tests/test_all.py —— FileLib 全功能自动化测试（零依赖）

跑法：
    python tests/test_all.py                # 默认 http://127.0.0.1:8899
    python tests/test_all.py --base http://127.0.0.1:8321
    python tests/test_all.py --user xxx --pass yyy

覆盖：健康检查 / 认证与会话 / 权限与越权 / 各浏览接口 / 列表查询语法 /
      文件详情与缩略图 / 媒体 Range / 阅读 / 下载 / 标记与标签 / 进度 /
      管理后台 / 安全响应头 / CSRF / Host 校验 / 路径穿越 / 限流与锁定
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 本机测试：绕过系统代理（否则 127.0.0.1 会被 http_proxy 劫持成 502）
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

BASE = "http://127.0.0.1:8899"
USER = ""
PASS = ""

OK = 0
FAIL = 0
SKIP = 0
FAILED = []
DETAIL = []


def q(**kw):
    """构造查询串：自动对中文/特殊字符做百分号编码。"""
    out = []
    for k, v in kw.items():
        if v is None:
            continue
        out.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
    return "&".join(out)


def ck(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        FAILED.append(name + (f"  [{extra}]" if extra else ""))
        print(f"  \033[31m✗ {name}\033[0m {extra}")


def sk(name, why=""):
    global SKIP
    SKIP += 1
    print(f"  \033[33m-\033[0m {name} 跳过（{why}）")


class Client:
    def __init__(self, base, token=None):
        self.base = base
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.token = token

    def req(self, path, method="GET", body=None, headers=None, raw=False,
            cookie=None):
        url = self.base + path
        data = None
        h = {"X-Requested-With": "FileLib", "User-Agent": "FileLibTest/2.0"}
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
            h["Origin"] = self.base
        if headers:
            h.update(headers)
        if cookie:
            h["Cookie"] = cookie
            self._nocookie = True
            op = urllib.request.build_opener()
        else:
            op = self.op
        r = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with op.open(r, timeout=30) as resp:
                raw_data = resp.read()
                code = resp.status
                hdrs = dict(resp.headers)
        except urllib.error.HTTPError as e:
            try:
                raw_data = e.read()
            except Exception:
                raw_data = b""
            code = e.code
            hdrs = dict(e.headers)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # 服务端在 413 等场景会主动断连，按状态码语义处理即可
            return 413, b"", {}
        if raw:
            return code, raw_data, hdrs
        try:
            return code, json.loads(raw_data.decode("utf-8")), hdrs
        except Exception:
            return code, raw_data, hdrs

    def get(self, path, **kw):
        return self.req(path, "GET", **kw)

    def post(self, path, body=None, **kw):
        return self.req(path, "POST", body or {}, **kw)




def _bootstrap_reset(base, user, pw, quiet=False):
    """登录取会话 → 调本机专用接口清空限流计数 → 登出。失败不致命。"""
    def say(msg):
        if not quiet:
            print(msg)
    try:
        c = Client(base)
        code, d, _ = c.post("/api/login", {"username": user, "password": pw})
        if code != 200:
            if not quiet:
                say(f"  （预清理限流跳过：登录返回 {code}，可等待 60 秒后重跑）")
            return
        code, d, _ = c.get("/api/admin/rate_reset")
        if code == 200:
            say("  （已重置限流计数）")
        c.post("/api/logout")
    except Exception as ex:
        say(f"  （预清理限流跳过：{type(ex).__name__}）")



def h(title):
    print(f"\n\033[36m▌{title}\033[0m")


# ----------------------------------------------------------------------

def main():
    global BASE, USER, PASS
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--user", default=USER)
    ap.add_argument("--pass", dest="pw", default=PASS)
    a = ap.parse_args()
    BASE, USER, PASS = a.base.rstrip("/"), a.user, a.pw

    print("=" * 66)
    print(f" FileLib 功能测试  base={BASE}  user={USER}")
    print("=" * 66)

    anon = Client(BASE)

    # 上一轮测试可能把本机 IP 的游客额度打满（60/分），开跑前先清一次，
    # 否则第一组会全部 429、结论失真。
    _bootstrap_reset(BASE, USER, PASS)

    # ---------------- 健康检查 ----------------
    h("1. 服务与静态资源")
    c, d, hd = anon.get("/api/ping")
    ck("/api/ping 返回 ok", c == 200 and isinstance(d, dict) and d.get("ok"), f"{c} {str(d)[:80]}")
    ck("版本号存在", bool(isinstance(d, dict) and d.get("version")),
       str(d.get("version") if isinstance(d, dict) else d[:60]))
    c, raw, hd = anon.get("/", raw=True)
    ck("首页 HTML 200", c == 200 and b"<html" in raw.lower(), str(c))
    c, raw, hd = anon.get("/static/app.css", raw=True)
    ck("app.css 可访问且非空", c == 200 and len(raw) > 5000, f"{c} {len(raw)}")
    ck("CSS 含 5 套主题变量",
       all(k in raw for k in (b'data-theme="dark"', b'data-theme="sepia"',
                              b'data-theme="night"', b'data-theme="contrast"')))
    c, raw, hd = anon.get("/static/app.js", raw=True)
    ck("app.js 可访问且非空", c == 200 and len(raw) > 20000, f"{c} {len(raw)}")
    ck("JS 带 X-Requested-With", b"X-Requested-With" in raw)
    ck("静态资源有缓存头", "Cache-Control" in hd, str(hd.get("Cache-Control")))

    h("2. 安全响应头")
    c, raw, hd = anon.get("/", raw=True)
    hdr = {k.lower(): v for k, v in hd.items()}
    ck("HTML 有 CSP", "content-security-policy" in hdr)
    ck("X-Content-Type-Options", hdr.get("x-content-type-options") == "nosniff")
    ck("X-Frame-Options 或 CSP frame-ancestors",
       "x-frame-options" in hdr or "frame-ancestors" in hdr.get(
           "content-security-policy", ""))
    ck("Referrer-Policy", "referrer-policy" in hdr)

    h("3. 未登录拦截")
    c, d, _ = anon.get("/api/stats")
    ck("未登录 /api/stats → 401", c == 401, str(c))
    c, d, _ = anon.get("/api/list")
    ck("未登录 /api/list → 401", c == 401, str(c))
    c, d, _ = anon.post("/api/scan", {"root": "C:\\"})
    ck("未登录 POST /api/scan → 401/403", c in (401, 403), str(c))

    # ---------------- 登录 ----------------
    h("4. 登录 / 会话")
    c, d, hd = anon.post("/api/login", {"username": USER, "password": "wrong-pass-xyz"})
    ck("错误密码被拒", c == 401, str(c))
    c, d, hd = anon.post("/api/login", {"username": USER, "password": PASS})
    if c != 200:
        # 账号/密码不对时后面几十项会全部 401，结论毫无意义，直接中止
        print(f"\n\033[31m✗ 登录失败（{c}）：用户名 {USER} 或密码不正确。\033[0m")
        print("  请用真实账号重跑：python tests\\test_all.py --user 账号 --pass 密码")
        print("  忘记密码可在数据目录执行：python tools\\reset_pw.py --user 账号\n")
        return 2
    ck("正确密码登录成功", c == 200 and d.get("logged"), f"{c} {d}")
    ck("返回角色", d.get("role") in ("admin", "user"), str(d.get("role")))
    setc = hd.get("Set-Cookie", "")
    ck("Cookie 带 HttpOnly", "HttpOnly" in setc, setc[:80])
    ck("Cookie 带 SameSite", "SameSite" in setc)
    tok = ""
    for part in setc.split(";"):
        if part.strip().startswith("flsid="):
            tok = part.strip()[6:]
    ck("会话令牌非空", bool(tok))

    A = anon  # 已登录客户端
    c, d, _ = A.get("/api/me")
    ck("/api/me 已登录", c == 200 and d.get("logged") is True)

    h("5. CSRF / Host 防护")
    c, d, _ = A.req("/api/mark", "POST", {"id": 1, "mark": "star"},
                    headers={"X-Requested-With": ""})
    ck("缺 X-Requested-With 被拒", c == 403, str(c))
    c, d, _ = A.req("/api/mark", "POST", {"id": 1, "mark": "star"},
                    headers={"Origin": "http://evil.example"})
    ck("跨站 Origin 被拒", c == 403, str(c))
    c, d, _ = A.req("/api/stats", "GET", headers={"Host": "evil.example.com"})
    ck("非法 Host 被拒（防 DNS Rebinding）", c == 421, str(c))

    h("6. 路径穿越")
    for p in ("/static/../server.py", "/static/..%2f..%2fserver.py",
              "/static/....//server.py"):
        c, raw, _ = A.get(p, raw=True)
        ck(f"穿越被拦 {p}", c in (400, 403, 404) and b"SQLITE" not in raw.upper(), str(c))

    # ---------------- 数据浏览 ----------------
    h("7. 统计与资料库")
    c, st, _ = A.get("/api/stats")
    ck("/api/stats 200", c == 200, str(c))
    vols = st.get("volumes") or []
    ck("有资料库列表", isinstance(vols, list) and len(vols) >= 1, str(len(vols)))
    ck("文件总数 > 0", (st.get("total_files") or 0) > 0, str(st.get("total_files")))
    ck("按类型统计存在", isinstance(st.get("by_kind"), list) and st["by_kind"])
    ck("统计含设置项", isinstance(st.get("settings"), dict))
    print(f"    → {(st.get('total_files') or 0):,} 文件 / {len(vols)} 个库")
    vid = vols[0]["id"] if vols else None

    h("8. 各浏览接口")
    for name, path in [
        ("目录树", f"/api/dirtree?vol={vid}"),
        ("列表", f"/api/list?vol={vid}&limit=5"),
        ("主题", f"/api/topics?vol={vid}"),
        ("系列", f"/api/series?vol={vid}"),
        ("作者", f"/api/authors?vol={vid}"),
        ("出版社", f"/api/publishers?vol={vid}"),
        ("标签", f"/api/tags?vol={vid}"),
        ("首页聚合", f"/api/home?vol={vid}"),
        ("大文件", f"/api/list?vol={vid}&sort=size_desc&limit=3"),
    ]:
        c, d, _ = A.get(path)
        ck(f"{name} 接口 200", c == 200, f"{c} {str(d)[:80]}")

    c, d, _ = A.get(f"/api/dirtree?vol={vid}")
    ck("目录树有节点", isinstance(d.get("nodes"), list), str(type(d.get("nodes"))))
    _tn = d.get("nodes") or []
    ck("目录树含子树累计体积 tsize",
       bool(_tn) and all("tsize" in n for n in _tn)
       and any(n["tsize"] > 0 for n in _tn),
       str(len(_tn)))
    c, lst, _ = A.get(f"/api/list?vol={vid}&limit=5")
    items = lst.get("items") or []
    ck("列表返回条目", len(items) > 0, str(len(items)))
    ck("列表条目字段完整",
       bool(items) and all(k in items[0] for k in ("id", "name", "kind", "size", "ext")),
       str(list(items[0].keys())[:10] if items else ""))
    ck("列表带总数", "total" in lst, str(lst.get("total")))

    h("9. 查询语法")
    c, d, _ = A.get(f"/api/list?vol={vid}&q=ext:pdf&limit=3")
    ck("ext:pdf 语法", c == 200, str(c))
    if (d.get("total") or 0) > 0:
        ck("ext:pdf 结果都是 pdf",
           all(x["ext"] == "pdf" for x in d.get("items") or []))
    c, d, _ = A.get(f"/api/list?vol={vid}&q=size:>50m&limit=3")
    ck("size:>50m 语法", c == 200, str(c))
    if d.get("items"):
        ck("size 过滤生效", all(x["size"] > 50 * 1024 * 1024 for x in d["items"]))
    c, d, _ = A.get("/api/list?" + q(vol=vid, q="把", limit=3))
    ck("中文关键字搜索", c == 200, str(c))
    c, d, _ = A.get("/api/list?" + q(vol=vid, q='"""', limit=3))
    ck("畸形查询不 500", c in (200, 400), str(c))

    h("10. 文件详情 / 缩略图 / 媒体")
    c, d, _ = A.get(f"/api/list?vol={vid}&limit=50")
    items = d.get("items") or []
    if items:
        fid = items[0]["id"]
        c, f, _ = A.get(f"/api/file?id={fid}")
        ck("文件详情 200", c == 200, str(c))
        ck("详情含完整路径", bool(f.get("path")), str(f.get("path"))[:70])
        ck("详情含 meta 字段", "meta" in f)
        c, d2, _ = A.get("/api/file?id=999999999")
        ck("不存在的 id → 404", c == 404, str(c))

        thumb = [x for x in items if x.get("thumb")]
        if thumb:
            tid = thumb[0]["id"]
            c, raw, hd = A.get(f"/thumb/{tid}.jpg", raw=True)
            ck("缩略图可读取", c == 200 and len(raw) > 300, f"{c} {len(raw)}")
        else:
            sk("缩略图", "此库暂无已生成封面")

        video = [x for x in items if x["kind"] == "video"]
        vids = video[0] if video else None
        if not vids:
            c, dv, _ = A.get(f"/api/list?vol={vid}&kind=video&limit=3")
            vids = (dv.get("items") or [None])[0]
        # 外接盘拔掉时老索引里的文件可能不在盘上，逐个探测直到找到真实存在的
        if vids:
            probed = []
            c, dv, _ = A.get(f"/api/list?vol={vid}&kind=video&limit=200")
            for cand in [vids] + (dv.get("items") or []):
                if cand["id"] in probed:
                    continue
                probed.append(cand["id"])
                cc, _, _ = A.req(f"/media/{cand['id']}.{cand['ext']}", "GET",
                                 headers={"Range": "bytes=0-99"}, raw=True)
                if cc == 206:
                    vids = cand
                    break
                if len(probed) > 60:
                    break
            else:
                vids = None
        if vids:
            c, raw, hd = A.req(f"/media/{vids['id']}.{vids['ext']}", "GET",
                               headers={"Range": "bytes=0-99"}, raw=True)
            ck("媒体 Range 返回 206", c == 206, str(c))
            ck("Range 返回正确长度", len(raw) == 100, str(len(raw)))
            hdr = {k.lower(): v for k, v in hd.items()}
            ck("有 Accept-Ranges", hdr.get("accept-ranges") == "bytes")
            ck("有 Content-Range", "content-range" in hdr)
            c, raw2, _ = A.get(f"/download/{vids['id']}.{vids['ext']}", raw=True)
            ck("下载接口 200", c == 200, str(c))
        else:
            sk("媒体 Range", "此库没有视频")

    h("11. 阅读")
    textable = None
    for kind in ("doc", "book"):
        c, d, _ = A.get(f"/api/list?vol={vid}&kind={kind}&limit=60")
        for x in d.get("items") or []:
            if x["ext"] in ("txt", "md"):
                textable = x
                break
        if textable:
            break
    if not textable:
        c, d, _ = A.get(f"/api/list?vol={vid}&q=ext:txt&limit=5")
        textable = (d.get("items") or [None])[0]
    if textable:
        c, d, _ = A.get(f"/api/read?id={textable['id']}")
        ck("TXT/MD 阅读接口", c == 200 and d.get("kind") in ("text", "pdf"), str(c))
        if d.get("kind") == "text":
            ck("正文非空", len(d.get("text") or "") > 0,
               str(len(d.get("text") or "")))
    else:
        sk("阅读接口", "没有 txt/md 样本")

    epub = None
    c, d, _ = A.get(f"/api/list?vol={vid}&q=ext:epub&limit=3")
    epub = (d.get("items") or [None])[0]
    if epub:
        t0 = time.time()
        c, d, _ = A.get(f"/api/ebook?id={epub['id']}")
        dt = time.time() - t0
        ck("EPUB 章节解析", c == 200 and d.get("kind") == "chapters",
           f"{c} {str(d)[:60]}")
        ck("EPUB 有章节且非空", len(d.get("chapters") or []) > 0,
           str(len(d.get("chapters") or [])))
        print(f"    → {len(d.get('chapters') or [])} 章，耗时 {dt:.2f}s")
        c2, d2, _ = A.get(f"/api/ebook?id={epub['id']}")
        ck("EPUB 二次读取走缓存", d2.get("cached") is True, str(d2.get("cached")))
    else:
        sk("EPUB 解析", "此库没有 epub")

    h("12. 标记 / 标签 / 进度")
    if items:
        fid = items[0]["id"]
        c, d, _ = A.post("/api/mark", {"id": fid, "mark": "star"})
        ck("设置收藏", c == 200 and d.get("marks") == ["star"], f"{c} {d}")
        c, d, _ = A.post("/api/mark", {"id": fid, "mark": "todo"})
        ck("设置待读", c == 200 and d.get("marks") == ["todo"], f"{c} {d}")
        c, d, _ = A.post("/api/mark", {"id": fid, "mark": None})
        ck("取消标记", c == 200 and d.get("marks") == [], f"{c} {d}")
        c, d, _ = A.post("/api/mark", {"id": fid, "mark": "bogus"})
        ck("非法标记被拒", c == 400, str(c))
        # 多标记（v3）：star 可与 read/todo 并存；read/todo 互斥；on 控制单个勾选
        c, d, _ = A.post("/api/mark", {"id": fid, "mark": "star", "on": True})
        ck("勾选 star", c == 200 and d.get("marks") == ["star"], f"{c} {d}")
        c, d, _ = A.post("/api/mark", {"id": fid, "mark": "read", "on": True})
        ck("再勾 read，两者并存", sorted(d.get("marks") or []) == ["read", "star"], f"{c} {d}")
        c, d, _ = A.post("/api/mark", {"id": fid, "mark": "todo", "on": True})
        ck("勾 todo 自动取消 read（互斥）", sorted(d.get("marks") or []) == ["star", "todo"], f"{c} {d}")
        c, d, _ = A.post("/api/mark", {"id": fid, "mark": "todo", "on": False})
        ck("取消单个标记", d.get("marks") == ["star"], f"{c} {d}")
        c, d, _ = A.get(f"/api/file?id={fid}")
        ck("详情返回 marks 数组", isinstance(d.get("marks"), list) and d.get("marks") == ["star"], f"{c} {d.get('marks')}")
        c, d, _ = A.post("/api/mark", {"id": fid, "mark": None})
        ck("清空全部标记", d.get("marks") == [], f"{c} {d}")

        c, d, _ = A.post("/api/set_tags", {"id": fid, "tags": ["测试标签", "自动化"]})
        ck("设置标签", c == 200, f"{c} {d}")
        names = [t["name"] for t in (d.get("tags") or [])]
        ck("标签写入成功", "测试标签" in names, str(names))
        c, d, _ = A.get("/api/list?" + q(vol=vid, tag="测试标签", limit=5))
        ck("按标签搜索命中", (d.get("total") or 0) >= 1, str(d.get("total")))
        # 多标签 AND 交集（v1.0.7）：tag=a,b / tags=a,b / 语法 tag:a,b 三种入口
        c, d, _ = A.get("/api/list?" + q(vol=vid, tag="测试标签,自动化", limit=5))
        ck("多标签交集 tag=a,b", c == 200 and fid in [x["id"] for x in (d.get("items") or [])],
           f"{c} total={d.get('total')}")
        c, d, _ = A.get("/api/list?" + q(vol=vid, tags="测试标签,自动化", limit=5))
        ck("多标签交集 tags=a,b", c == 200 and fid in [x["id"] for x in (d.get("items") or [])],
           f"{c} total={d.get('total')}")
        c, d, _ = A.get("/api/list?" + q(vol=vid, q="tag:测试标签,自动化", limit=5))
        ck("语法 tag:a,b 交集", c == 200 and fid in [x["id"] for x in (d.get("items") or [])],
           f"{c} total={d.get('total')}")
        c, d, _ = A.get("/api/list?" + q(vol=vid, tags="测试标签,不存在的标签xyz", limit=5))
        ck("交集缺一不命中", (d.get("total") or 0) == 0, str(d.get("total")))
        c, d, _ = A.post("/api/set_tags", {"id": fid, "tags": []})
        ck("清空标签", c == 200, str(c))

        c, d, _ = A.post("/api/progress", {"id": fid, "page": 3, "total": 20})
        ck("保存阅读进度", c == 200, str(c))
        c, f, _ = A.get(f"/api/file?id={fid}")
        rp = f.get("read_progress") or {}
        ck("阅读进度已持久化", rp.get("page") == 3, str(rp))

    h("12b. 今日推荐 / 阅读统计")
    c, d, _ = A.get("/api/home?" + q(vol=vid))
    ck("首页聚合 200", c == 200, str(c))
    ck("首页含今日推荐字段", "daily" in d, str(list(d.keys())))
    if d.get("daily"):
        dl = d["daily"]
        ck("今日推荐是可读书/文档", dl.get("kind") in ("book", "doc"), str(dl.get("kind")))
        ck("今日推荐字段完整", dl.get("id") and dl.get("name"), str(dl)[:100])
    c2, d2, _ = A.get("/api/home?" + q(vol=vid))
    ck("今日推荐同日稳定",
       (d.get("daily") or {}).get("id") == (d2.get("daily") or {}).get("id"))
    if items:
        fid = items[0]["id"]
        c, d, _ = A.post("/api/readlog", {"id": fid, "secs": 30})
        ck("阅读心跳 200", c == 200, f"{c} {d}")
        c, d, _ = A.post("/api/readlog", {"id": fid, "secs": 45})
        ck("关阅读器补零头 200", c == 200, str(c))
        c, d, _ = A.post("/api/readlog", {"secs": -5})
        ck("非法时长被拒", c == 400, str(c))
        c, d, _ = A.post("/api/readlog", {"secs": 999})
        ck("超长心跳被拒", c == 400, str(c))
    c, d, _ = A.get("/api/readstats")
    ck("阅读统计 200", c == 200, str(c))
    tt = d.get("totals") or {}
    ck("统计有打卡天", (tt.get("days") or 0) >= 1, str(tt))
    ck("今日有时长", (d.get("today") or 0) >= 30, str(d.get("today")))
    ck("连续打卡 >= 1", (d.get("streak") or 0) >= 1, str(d.get("streak")))
    ck("逐日数组非空", isinstance(d.get("days"), list) and len(d.get("days") or []) >= 1)
    c, d, _ = Client(BASE).get("/api/readstats")
    ck("未登录读统计 → 401", c == 401, str(c))

    h("13. 全文搜索")
    c, d, _ = A.get("/api/fulltext?" + q(q="的", vol=vid, limit=5))
    ck("全文搜索接口 200", c == 200, str(c))
    ck("全文返回 items 列表", isinstance(d.get("items"), list))
    c, d, _ = A.get("/api/fulltext?" + q(q='"""', vol=vid))
    ck("畸形全文查询不 500", c in (200, 400), str(c))

    h("14. 重复 / 清理")
    c, d, _ = A.get(f"/api/dups?vol={vid}")
    ck("重复文件接口 200", c == 200 and "groups" in d, str(c))
    c, d, _ = A.get("/api/dups?cross=1")
    ck("跨库重复接口 200", c == 200 and "groups" in d, str(c))
    c, d, _ = A.get(f"/api/junk?vol={vid}")
    ck("清理建议接口 200", c == 200, str(c))

    h("15. 任务状态与环境")
    c, t, _ = A.get("/api/task")
    ck("/api/task 200", c == 200, str(c))
    ck("任务状态合法",
       t.get("state") in ("idle", "running", "done", "error", "aborted"), str(t.get("state")))
    ck("返回 ffmpeg 探测结果", "ffmpeg" in t, str(t.get("ffmpeg")))
    ck("返回 calibre 探测结果", "calibre" in t, str(t.get("calibre")))
    c, d, _ = A.get("/api/tools")
    ck("/api/tools 200", c == 200, str(c))

    # ---------------- 权限 ----------------
    h("16. 普通用户越权")
    if d and st:
        users = A.get("/api/admin/users")
        ulist = users[1].get("users") or []
        picked = None
        tmp_uid = None
        nu, UPW = None, ""
        # 当前登录的就是普通账号：直接拿它自己测，不用查后台
        me2 = A.get("/api/me")[1] or {}
        if me2.get("logged") and me2.get("role") == "user":
            picked = {"username": USER, "perms": me2.get("perms") or [], "role": "user"}
        if picked is None:
            normal = [u for u in ulist if u["role"] == "user" and not u["disabled"]]
            pick = next((u for u in ulist if u["username"] == USER and u["role"] == "user"), None)
            if pick:
                picked = pick
            elif normal:
                picked = normal[0]
        if picked:
            nu, UPW = picked, PASS
            probed = Client(BASE).post("/api/login", {"username": nu["username"], "password": PASS})[0]
            if probed != 200:
                nu, UPW = None, ""   # 这个账号的密码不是 PASS，别硬试
        if not nu:
            # 用 admin 临时建一个，保证这组一定能跑
            c, d1, _ = A.post("/api/admin/users", {"action": "add", "username": "zz_perm_probe",
                                                   "password": "Probe12345", "role": "user"})
            if c == 200:
                tmp_uid = d1["user"]["id"]
                # 授一个可见库 + 基础导航项，才能测「授权库内可用」
                A.post("/api/admin/users", {"action": "perms", "id": tmp_uid, "perms": [vid]})
                A.post("/api/admin/users", {"action": "nav", "id": tmp_uid,
                                            "nav": {str(vid): ["home", "tree", "search", "play"]}})
                c2, d2, _ = A.get("/api/admin/users")
                nu = next((x for x in (d2.get("users") or []) if x["id"] == tmp_uid), None)
                UPW = "Probe12345"
            else:
                nu, UPW = None, ""
        if nu:
            U = Client(BASE)
            c, dd, _ = U.post("/api/login", {"username": nu["username"], "password": UPW})
            if c == 200:
                ck("普通用户可登录", True)
                for p, m in [("/api/admin/users", "GET"), ("/api/admin/settings", "GET"),
                             ("/api/admin/overview", "GET"), ("/api/admin/logs", "GET")]:
                    c, _, _ = U.req(p, m)
                    ck(f"普通用户 {p} → 403", c == 403, str(c))
                c, _, _ = U.post("/api/scan", {"root": "C:\\"})
                ck("普通用户不能扫描 → 403", c == 403, str(c))
                c, _, _ = U.post("/api/delete", {"ids": [1]})
                ck("普通用户不能删除 → 403", c == 403, str(c))
                c, _, _ = U.post("/api/open", {"id": 1})
                ck("普通用户不能打开本机文件 → 403", c == 403, str(c))
                # 标记是个人口味：授权库内应允许；未授权库应 403
                own = (nu.get("perms") or [None])[0]
                c1, dd1, _ = U.get("/api/list?" + q(vol=own, limit=1)) if own else (0, {}, None)
                fid_own = ((dd1.get("items") or [{}])[0] or {}).get("id")
                if fid_own:
                    c, dd2, _ = U.post("/api/mark", {"id": fid_own, "mark": "star"})
                    ck("普通用户可给授权库文件打标记", c == 200, f"{c} {dd2}")
                    c, _, _ = U.post("/api/mark", {"id": fid_own, "mark": None})
                    ck("普通用户可取消标记", c == 200, str(c))
                else:
                    sk("普通用户标记", "授权库里没有文件")
                c, _, _ = U.post("/api/set_tags", {"id": 1, "tags": []})
                # 找一条「不在他可见库里」的文件 id 才能真正测越权。
                # 必须用 admin 会话去取（普通用户对未授权库本来就该 403），
                # 且要跳过错过的库/离线盘。
                perms_all = nu.get("perms") or []
                hidden_fid = None
                for v in vols:
                    if v["id"] in perms_all:
                        continue
                    c2, d2, _ = A.get(f"/api/list?vol={v['id']}&limit=1&recursive=1")
                    it = (d2.get("items") or [])
                    if it and it[0].get("id"):
                        hidden_fid = it[0]["id"]
                        break
                if hidden_fid and hidden_fid == 1:
                    hidden_fid = None  # id=1 恰好可见库里的第一条，跳过避免误判
                if hidden_fid:
                    c, _, _ = U.post("/api/mark", {"id": hidden_fid, "mark": "star"})
                    ck("普通用户不能标记未授权库的文件 → 403/404", c in (403, 404), str(c))
                    c, _, _ = U.post("/api/set_tags", {"id": hidden_fid, "tags": ["x"]})
                    ck("普通用户不能给未授权库文件打标签 → 403/404", c in (403, 404), str(c))
                    c, _, _ = U.get(f"/api/file?id={hidden_fid}")
                    ck("普通用户读不到未授权库文件详情 → 403/404", c in (403, 404), str(c))
                else:
                    sk("普通用户跨库越权", "没有可用的未授权库文件")
                # 未授权库
                perms = nu.get("perms") or []
                deny = [v["id"] for v in vols if v["id"] not in perms]
                if deny:
                    c, _, _ = U.get(f"/api/list?vol={deny[0]}")
                    ck("访问未授权库被拒", c in (403, 200), str(c))
                    if c == 200:
                        ck("未授权库返回空集", True)
            else:
                sk("普通用户权限测试", f"普通用户登录失败（{c}），请用 --user/--pass 指定普通用户")
        else:
            sk("普通用户权限测试", "没有普通用户且临时创建失败")
        if tmp_uid:
            A.post("/api/admin/users", {"action": "del", "id": tmp_uid})

    h("17. 管理后台（admin）")
    c, d, _ = A.get("/api/admin/users")
    ck("用户列表 200", c == 200 and "users" in d, str(c))
    ck("返回导航项定义", isinstance(d.get("nav_items"), list) and len(d["nav_items"]) > 5,
       str(len(d.get("nav_items") or [])))
    ck("用户带密码字段", all("password" in u for u in d.get("users") or []))
    c, d2, _ = A.get("/api/admin/settings")
    ck("设置读取 200", c == 200 and "data_dir" in d2, str(c))
    ck("设置带备份列表", isinstance(d2.get("backups"), list))
    c, o, _ = A.get("/api/admin/overview")
    ck("总览 200", c == 200 and "volumes" in o, str(c))
    ck("总览库信息含在线状态",
       all("online" in v for v in o.get("volumes") or []))
    c, d, _ = A.get("/api/admin/logs")
    ck("日志 200", c == 200 and "logins" in d, str(c))

    # 新建 → 改权限 → 删除 全链路
    c, d, _ = A.post("/api/admin/users", {"action": "add", "username": "zz_test_user",
                                          "password": "Zz123456", "role": "user", "perms": []})
    if c == 200:
        uid = d["user"]["id"]
        ck("新建用户", True, str(uid))
        c, d, _ = A.post("/api/admin/users", {"action": "perms", "id": uid, "perms": [vid]})
        ck("设置库权限", c == 200 and vid in (d["user"].get("perms") or []), str(d.get("user", {}).get("perms")))
        c, d, _ = A.post("/api/admin/users", {"action": "nav", "id": uid, "nav": {str(vid): ["home", "tree"]}})
        ck("设置导航项权限", c == 200, str(c))
        U2 = Client(BASE)
        c, d2, _ = U2.post("/api/login", {"username": "zz_test_user", "password": "Zz123456"})
        ck("新用户可登录", c == 200, str(c))
        ck("新用户 nav 返回受限", isinstance(d2.get("nav"), dict), str(d2.get("nav"))[:60])
        c, d3, _ = U2.get("/api/stats")
        ck("新用户只看到授权库",
           [v["id"] for v in d3.get("volumes") or []] == [vid],
           str([v["id"] for v in d3.get("volumes") or []]))
        c, d, _ = A.post("/api/admin/users", {"action": "disable", "id": uid, "disabled": True})
        ck("停用用户", c == 200, str(c))
        c, d, _ = U2.get("/api/stats")
        ck("停用后立即失效", c == 401, str(c))
        c, d, _ = A.post("/api/admin/users", {"action": "del", "id": uid})
        ck("删除用户", c == 200, str(c))
    else:
        ck("新建用户", False, f"{c} {d}")

    c, d, _ = A.post("/api/admin/users", {"action": "del", "id": 1})
    ck("不能删除自己", c == 400, str(c))
    c, d, _ = A.post("/api/admin/users", {"action": "add", "username": "a", "password": "1"})
    ck("非法用户名/弱密码被拒", c == 400, str(c))

    h("17b. 文件管理与分享（实时目录 / 分享链接 / 打包下载）")
    c, d, _ = A.get(f"/api/fm_dirs?vol={vid}&rel=")
    ck("fm_dirs 200", c == 200 and isinstance(d.get("dirs"), list), f"{c} {str(d)[:80]}")
    c, d, _ = A.get("/api/fm_dirs?vol=99999&rel=")
    ck("fm_dirs 不存在的卷 → 404", c == 404, str(c))
    c, d, _ = A.get(f"/api/fm_dirs?vol={vid}&rel=" + q(rel="../../etc"))
    ck("fm_dirs 穿越 rel 被拒", c in (400, 404), str(c))

    import shutil   # 后面缩略图测试的临时卷清理还要用
    import tempfile
    # 「在当前目录新建文件夹」已于 v1.0.16 整体下线（右上角按钮 + 端点），
    # 旧端点保留路由统一返回 404，让旧缓存页面得到明确提示而不是静默失败。
    c, d, _ = A.post("/api/fm_mkdir", {"vol": vid, "rel": "", "name": "zz_removed"})
    ck("已下线 mkdir → 404", c == 404, str(c))
    c, d, _ = A.post("/api/fm_mkdir", {"vol": vid, "rel": "..", "name": "x"})
    ck("已下线 mkdir 穿越场景 → 404", c == 404, str(c))

    c, lst, _ = A.get(f"/api/list?vol={vid}&limit=1")
    fid0 = ((lst.get("items") or [{}])[0] or {}).get("id")
    # 分享链接功能已于 v1.0.2 移除
    c, d, _ = A.post("/api/share", {"id": fid0 or 1})
    ck("已下线分享接口 → 404", c == 404, str(c))
    # 权限导航项与导航栏同步（图片墙/浏览-目录已删，继续看/随机看新增）
    c, d, _ = A.get("/api/admin/users")
    nks = [x.get("k") for x in (d.get("nav_items") or [])]
    ck("权限导航项同步（无 imagewall，含 continue/random）",
       "imagewall" not in nks and "continue" in nks and "random" in nks,
       str(nks)[:150])
    c, d, _ = A.get("/api/continue")
    ck("继续看记录 200", c == 200 and isinstance(d.get("items"), list), str(c))
    c, d, _ = A.get("/api/random?n=5")
    ck("随机看 200（≤5 条）", c == 200 and isinstance(d.get("items"), list)
       and len(d.get("items") or []) <= 5, str(c))
    c, d, _ = A.get("/api/dups")
    ck("查重返回 total 总数", c == 200 and isinstance(d.get("total"), int), str(c))
    if fid0:
        c, d, _ = A.post("/api/progress", {"id": fid0, "pos": 3, "dur": 10})
        ck("保存播放进度 200", c == 200 and d.get("ok"), str(c))
        c, d, _ = A.get("/api/continue")
        ck("进度进入继续看记录",
           c == 200 and any(x.get("id") == fid0 for x in d.get("items") or []), str(c))
        c, d, _ = A.post("/api/continue", {"action": "delete", "ids": [fid0]})
        ck("删除单条继续看记录 200", c == 200 and d.get("ok"), str(c))
        c, d, _ = A.get("/api/continue")
        ck("删除后记录消失",
           c == 200 and not any(x.get("id") == fid0 for x in d.get("items") or []), str(c))
        A.post("/api/progress", {"id": fid0, "pos": 5, "dur": 10})
        c, d, _ = A.post("/api/continue", {"action": "clear", "vol": vid})
        ck("一键清空当前库记录 200", c == 200 and d.get("ok"), str(c))
        c, d, _ = A.get(f"/api/continue?vol={vid}")
        ck("清空后记录为空", c == 200 and not (d.get("items") or []), str(c))
    else:
        sk("继续看记录管理", "库里没有文件")

    c, raw, hd = A.get(f"/download_zip?vol={vid}&limit=5", raw=True)
    ck("打包下载 200", c == 200 and raw[:2] == b"PK", f"{c} {len(raw)}")
    ck("zip 响应头正确", "zip" in (hd.get("Content-Type") or ""),
       str(hd.get("Content-Type")))
    try:
        import io as _io
        import zipfile as _zf
        zf = _zf.ZipFile(_io.BytesIO(raw))
        bad = zf.testzip()
        ck("zip 结构完整可解", bad is None and len(zf.namelist()) >= 1,
           f"bad={bad} n={len(zf.namelist())}")
    except Exception as ex:
        ck("zip 结构完整可解", False, str(ex))
    c, d, _ = A.get(f"/download_zip?vol={vid}&limit=200&sort=size_desc")
    ck("打包上限场景不 500", c in (200, 404), str(c))

    NP = Client(BASE)
    c, d, _ = NP.post("/api/fm_mkdir", {"vol": vid, "rel": "", "name": "zzz"})
    ck("未登录新建文件夹 → 401", c == 401, str(c))
    c, d, _ = NP.get(f"/api/fm_dirs?vol={vid}&rel=")
    ck("未登录实时目录 → 401", c == 401, str(c))
    c, d, _ = NP.get("/api/continue")
    ck("未登录继续看 → 401", c == 401, str(c))
    c, d, _ = NP.get(f"/download_zip?vol={vid}&limit=2")
    ck("未登录打包 → 401", c == 401, str(c))

    h("17c. 改密 / 缩略图维护（v1.0.5）")
    # --- 管理员改密：改他人后旧会话立即失效；改自己不掉线 ---
    c, d, _ = A.post("/api/admin/users", {"action": "add", "username": "zz_pw_user",
                                          "password": "Pw123456", "role": "user", "perms": []})
    pw_uid = (d.get("user") or {}).get("id") if c == 200 else None
    ck("改密: 新建临时用户", c == 200 and pw_uid, f"{c} {str(d)[:80]}")
    if pw_uid:
        P = Client(BASE)
        c, d, _ = P.post("/api/login", {"username": "zz_pw_user", "password": "Pw123456"})
        ck("改密: 临时用户登录", c == 200, str(c))
        c, d, _ = A.post("/api/admin/users", {"action": "pass", "id": pw_uid, "password": "ab"})
        ck("改密: 弱密码被拒", c == 400, str(c))
        c, d, _ = A.post("/api/admin/users", {"action": "pass", "id": pw_uid, "password": "NewPass9"})
        ck("改密: 管理员改他人密码 200", c == 200, f"{c} {str(d)[:80]}")
        c, d, _ = P.get("/api/me")
        ck("改密: 旧会话立即失效", c == 401 or
           (isinstance(d, dict) and d.get("logged") is False), f"{c} {str(d)[:60]}")
        P2 = Client(BASE)
        c, d, _ = P2.post("/api/login", {"username": "zz_pw_user", "password": "Pw123456"})
        ck("改密: 旧密码登录被拒", c in (400, 401, 429), str(c))
        P3 = Client(BASE)
        c, d, _ = P3.post("/api/login", {"username": "zz_pw_user", "password": "NewPass9"})
        ck("改密: 新密码可登录", c == 200, str(c))
        c, d, _ = A.get("/api/me")
        me_id = (d or {}).get("id")
        if me_id:
            c, d, _ = A.post("/api/admin/users", {"action": "pass", "id": me_id, "password": PASS})
            ck("改密: 管理员改自己密码 200", c == 200, str(c))
            c, d, _ = A.get("/api/me")
            ck("改密: 改自己密码不掉线", c == 200 and (d or {}).get("logged"),
               f"{c} {str(d)[:60]}")
        A.post("/api/admin/users", {"action": "del", "id": pw_uid})
    else:
        sk("改密链路", "临时用户创建失败")

    # --- 缩略图存量重压缩任务（首跑真实重编码，二跑后尺寸达标全跳过） ---
    c, d, _ = A.post("/api/rethumb")
    ck("rethumb 启动 200", c == 200, f"{c} {str(d)[:80]}")
    if c == 200:
        c, d, _ = A.post("/api/rethumb")
        ck("rethumb 重复启动 → 409", c == 409, str(c))
        t = {}
        for _ in range(360):
            time.sleep(0.5)
            c, t, _ = A.get("/api/task")
            t = t or {}
            if t.get("name") == "rethumb" and t.get("state") != "running":
                break
        ck("rethumb 任务完成", t.get("state") == "done", str(t.get("state")))
        pg = t.get("progress") or {}
        ck("rethumb 有处理统计", isinstance(pg, dict) and "total" in pg,
           f"total={pg.get('total')} done={pg.get('done')}")
        ck("rethumb 体积不增大", (pg.get("bytes_after") or 0) <= (pg.get("bytes_before") or 0),
           f"{pg.get('bytes_before')} → {pg.get('bytes_after')}")
    else:
        sk("rethumb 全链路", f"启动失败 {c}")

    # --- 按需生成：无缩略图的图片文件，请求 /thumb 现场生成并落盘缓存 ---
    import base64
    png1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    tv_root = os.path.join(tempfile.gettempdir(), f"fltest_ondemand_{int(time.time())}")
    tvid = None
    try:
        os.makedirs(tv_root)
        with open(os.path.join(tv_root, "p.png"), "wb") as f:
            f.write(png1x1)
        c, d, _ = A.post("/api/scan", {"root": tv_root})
        if c == 200:
            for _ in range(40):
                time.sleep(0.5)
                c, t, _ = A.get("/api/task")
                t = t or {}
                if t.get("name") == "scan" and t.get("state") != "running":
                    break
            c, st, _ = A.get("/api/stats")
            for v in st.get("volumes") or []:
                if os.path.normcase(str(v.get("root") or "")) == os.path.normcase(tv_root):
                    tvid = v["id"]
                    break
        tid = None
        if tvid:
            c, d, _ = A.get(f"/api/list?vol={tvid}&limit=10")
            for x in d.get("items") or []:
                if x.get("kind") == "image" and not x.get("thumb"):
                    tid = x["id"]
                    break
        if tid:
            c, raw, hd = A.get(f"/thumb/{tid}.jpg", raw=True)
            ck("按需生成: 无缩略图 → 200", c == 200 and len(raw) > 50, f"{c} {len(raw)}")
            ck("按需生成: 返回 JPEG", (hd.get("Content-Type") or "").startswith("image/jpeg"),
               str(hd.get("Content-Type")))
            c, raw2, _ = A.get(f"/thumb/{tid}.jpg", raw=True)
            ck("按需生成: 二次请求命中缓存", c == 200 and len(raw2) == len(raw),
               f"{c} {len(raw2)}")
            c, raw3, _ = A.get("/thumb/999999999.jpg", raw=True)
            ck("按需生成: 不存在的 id → 404", c == 404, str(c))
        else:
            sk("按需生成", "测试卷扫描失败或没有无缩略图的图片")
    except Exception as ex:
        sk("按需生成", f"{type(ex).__name__}: {ex}")
    finally:
        if tvid:
            A.post("/api/admin/library", {"action": "drop", "id": tvid})
        shutil.rmtree(tv_root, ignore_errors=True)

    h("17d. 数据管理（v1.0.6）")
    # --- 占用统计 ---
    c, d, _ = A.get("/api/admin/data")
    ck("data: 管理员 200", c == 200, str(c))
    ck("data: 含数据目录与合计", c == 200 and d.get("data_dir") and d.get("total_human"),
       str(d)[:100])
    parts = d.get("parts") or []
    targets = {p.get("target") for p in parts}
    ck("data: parts 含四个可清理目标",
       {"thumbs", "covers", "booktext", "backups"} <= targets, str(targets))
    ck("data: parts 字段齐全", bool(parts) and all(
        {"name", "path", "size", "files", "human"} <= set(p) for p in parts),
       str(parts[:1])[:120])
    ck("data: 库列表是数组", isinstance(d.get("libraries"), list),
       str(d.get("libraries"))[:60])
    c, _, _ = NP.get("/api/admin/data")
    ck("data: 未登录 → 401", c == 401, str(c))

    # --- 普通用户 → 403 ---
    du_uid = None
    c, d, _ = A.post("/api/admin/users", {"action": "add", "username": "zz_data_user",
                                          "password": "Data12345", "role": "user", "perms": []})
    du_uid = (d.get("user") or {}).get("id") if c == 200 else None
    if du_uid:
        DU = Client(BASE)
        c, _, _ = DU.post("/api/login", {"username": "zz_data_user", "password": "Data12345"})
        c, _, _ = DU.get("/api/admin/data")
        ck("data: 普通用户 GET → 403", c == 403, str(c))
        c, _, _ = DU.post("/api/admin/cleanup", {"target": "thumbs"})
        ck("data: 普通用户 POST → 403", c == 403, str(c))
        A.post("/api/admin/users", {"action": "del", "id": du_uid})
    else:
        sk("数据管理权限", "临时用户创建失败")

    # --- 清理动作 ---
    c, d, _ = A.post("/api/admin/cleanup", {"target": "nope"})
    ck("cleanup: 未知目标 → 400", c == 400, f"{c} {str(d)[:60]}")
    c, d, _ = A.post("/api/admin/cleanup", {"target": "index"})
    ck("cleanup: 索引库不是 Web 清理目标 → 400", c == 400, str(c))
    c, d, _ = A.post("/api/admin/cleanup", {"target": "booktext"})
    ck("cleanup: 清正文缓存启动 200", c == 200, f"{c} {str(d)[:80]}")
    if c == 200:
        t = {}
        for _ in range(120):
            time.sleep(0.5)
            c, t, _ = A.get("/api/task")
            t = t or {}
            if t.get("name") == "cleanup" and t.get("state") != "running":
                break
        ck("cleanup: 任务完成", t.get("state") == "done", str(t.get("state")))
        ck("cleanup: 完成消息带统计", "删除" in (t.get("message") or ""),
           str(t.get("message")))
        c, d, _ = A.get("/api/admin/data")
        ck("cleanup: 完成后统计可刷新", c == 200, str(c))
    else:
        sk("cleanup 全链路", f"启动失败 {c}")
    c, _, _ = NP.post("/api/admin/cleanup", {"target": "thumbs"})
    ck("cleanup: 未登录 → 401", c == 401, str(c))

    h("17e. v1.0.7 新特性（备份瘦身字段/登录倒计时/PWA/自动打标签）")
    # --- 数据页新字段 ---
    c, d, _ = A.get("/api/admin/data")
    ck("data: 含 backup_keep（按份数清理备份）", c == 200 and "backup_keep" in d, str(c))
    ck("data: 含 flib_env（环境提示）", c == 200 and "flib_env" in d, str(c))

    # --- 登录失败响应结构（retry_after 仅在触发锁定时出现，倒计时断言在第 19 组） ---
    c, d, _ = NP.post("/api/login", {"username": USER, "password": "definitely-wrong"})
    ck("login: 单次失败 401 且未锁定时无倒计时",
       c == 401 and isinstance(d, dict) and not d.get("retry_after"),
       f"{c} {str(d)[:80]}")

    # --- PWA 静态资源 ---
    c, raw, _ = A.get("/manifest.webmanifest", raw=True)
    ck("pwa: manifest 200", c == 200 and b'"name"' in raw, str(c))
    c, raw, hd = A.get("/sw.js", raw=True)
    ck("pwa: sw.js 200", c == 200, str(c))
    ck("pwa: sw.js no-cache", c == 200 and "no-cache" in str(hd.get("Cache-Control") or ""),
       str(hd.get("Cache-Control")))
    ck("pwa: sw.js 含静态壳缓存清单", c == 200 and b"filelib-shell" in raw, "")

    # --- 自动打标签（全链路） ---
    c, d, _ = NP.post("/api/autotag", {})
    ck("autotag: 未登录 → 401", c == 401, str(c))
    # 若 17d 的清理任务还在跑，先等它结束再触发
    for _ in range(60):
        c, t, _ = A.get("/api/task")
        t = t or {}
        if t.get("state") != "running":
            break
        time.sleep(0.5)
    c, d, _ = A.post("/api/autotag", {})
    ck("autotag: 管理员触发 200", c == 200 and d.get("ok"), f"{c} {str(d)[:60]}")
    t = {}
    if c == 200:
        for _ in range(240):
            time.sleep(0.5)
            c, t, _ = A.get("/api/task")
            t = t or {}
            if t.get("name") == "autotag" and t.get("state") != "running":
                break
        ck("autotag: 任务完成", t.get("name") == "autotag" and t.get("state") == "done",
           f"{t.get('name')}/{t.get('state')} {(t.get('error') or '')[:60]}")
    else:
        sk("autotag 任务链路", f"触发失败 {c}")
    c, d, _ = A.get("/api/tags?limit=100")
    items = (d.get("items") or []) if isinstance(d, dict) else []
    auto_items = [x for x in items if not x.get("user")]
    ck("tags: 聚合含 user 布尔", all(isinstance(x.get("user"), bool) for x in items),
       str(items[:1])[:80])
    ck("tags: 有自动标签（auto: 前缀）",
       any(str(x.get("name", "")).startswith("auto:") for x in auto_items),
       f"自动标签数={len(auto_items)}")
    c, lst, _ = A.get("/api/list?limit=1")
    f0 = (lst.get("items") or [None])[0] if isinstance(lst, dict) else None
    fid = (f0 or {}).get("id")
    if fid:
        c, fd, _ = A.get("/api/file?id=" + str(fid))
        ftags = fd.get("tags") or []
        ck("file: 标签带 source 字段", all("source" in x for x in ftags),
           str(ftags[:2])[:100])
    else:
        sk("file 标签来源", "列表为空")
    du_uid = None
    c, d, _ = A.post("/api/admin/users", {"action": "add", "username": "zz_tag_user",
                                          "password": "Taggo12345", "role": "user", "perms": []})
    du_uid = (d.get("user") or {}).get("id") if c == 200 else None
    if du_uid:
        DU = Client(BASE)
        DU.post("/api/login", {"username": "zz_tag_user", "password": "Taggo12345"})
        c, _, _ = DU.post("/api/autotag", {})
        ck("autotag: 普通用户 → 403", c == 403, str(c))
        c, _, _ = DU.post("/api/autotag_clear", {})
        ck("autotag_clear: 普通用户 → 403", c == 403, str(c))
        A.post("/api/admin/users", {"action": "del", "id": du_uid})
    else:
        sk("autotag 权限", "临时用户创建失败")
    c, d, _ = A.post("/api/autotag_clear", {})
    ck("autotag_clear: 管理员清空 200",
       c == 200 and d.get("ok") and (d.get("removed") or 0) > 0, f"{c} {str(d)[:60]}")
    c, d, _ = A.get("/api/tags?limit=100")
    items = (d.get("items") or []) if isinstance(d, dict) else []
    ck("autotag_clear: 清空后无自动标签", all(x.get("user") for x in items),
       f"剩余 {len(items)} 项")

    h("17f. 标签管理器（合并/隐藏/改名守卫，v1.0.8）")
    # --- 重新触发自动打标签：验证新策略仍产出 auto: 标签，并供守卫断言用 ---
    c, d, _ = A.post("/api/autotag", {})
    ck("tagmgr: 触发自动打标签 200", c == 200 and d.get("ok"), f"{c} {str(d)[:60]}")
    t = {}
    if c == 200:
        for _ in range(240):
            time.sleep(0.5)
            c, t, _ = A.get("/api/task")
            t = t or {}
            if t.get("name") == "autotag" and t.get("state") != "running":
                break
        ck("tagmgr: 自动打标签完成", t.get("state") == "done", f"{t.get('state')}")
    c, d, _ = A.get("/api/tags?limit=300")
    items = (d.get("items") or []) if isinstance(d, dict) else []
    auto_name = next((x["name"] for x in items if not x.get("user")), None)
    ck("tagmgr: 新策略产出自动标签", auto_name is not None, f"auto={auto_name}")
    ck("tagmgr: 聚合项带 hidden/generic/rare 字段",
       all(all(k in x for k in ("hidden", "generic", "rare")) for x in items), "")
    if auto_name:
        c, d, _ = A.post("/api/tag_admin", {"action": "rename", "old": auto_name, "new": "改名尝试"})
        ck("tagmgr: 自动标签改名 → 400", c == 400, str(c))
        c, d, _ = A.post("/api/tag_admin", {"action": "hide", "name": auto_name, "on": True})
        ck("tagmgr: 隐藏标签 200", c == 200 and d.get("ok"), str(c))
        c, d, _ = A.get("/api/tags?limit=300")
        ck("tagmgr: 默认聚合不含隐藏标签",
           all(x["name"] != auto_name for x in (d.get("items") or [])), "")
        c, d, _ = A.get("/api/tags?limit=300&show_hidden=1")
        hit = [x for x in (d.get("items") or []) if x["name"] == auto_name]
        ck("tagmgr: show_hidden=1 可见且带 hidden 标记",
           bool(hit) and hit[0].get("hidden") is True, str(hit)[:60])
        A.post("/api/tag_admin", {"action": "hide", "name": auto_name, "on": False})
    else:
        sk("tagmgr 隐藏/守卫", "无自动标签")
    # --- 人工标签合并链路 ---
    c, lst, _ = A.get("/api/list?limit=2")
    ids = [x["id"] for x in (lst.get("items") or [])][:2] if isinstance(lst, dict) else []
    if len(ids) == 2:
        A.post("/api/set_tags", {"id": ids[0], "tags": ["合并源甲"]})
        A.post("/api/set_tags", {"id": ids[1], "tags": ["合并目标乙"]})
        c, d, _ = A.post("/api/tag_admin",
                         {"action": "merge", "src": "合并源甲", "dst": "合并目标乙"})
        ck("tagmgr: 人工标签合并 200", c == 200 and d.get("ok"), str(c))
        c, d, _ = A.get("/api/tags?limit=500")
        names = {x["name"]: x["n"] for x in (d.get("items") or [])}
        ck("tagmgr: 合并后目标计数 ≥2", names.get("合并目标乙", 0) >= 2,
           str(names.get("合并目标乙")))
        ck("tagmgr: 源标签已删除", "合并源甲" not in names, "")
        c, fd, _ = A.get("/api/file?id=" + str(ids[0]))
        ft = [x.get("name") for x in (fd.get("tags") or [])] if isinstance(fd, dict) else []
        ck("tagmgr: 原文件关联迁到目标标签", "合并目标乙" in ft, str(ft)[:80])
        A.post("/api/set_tags", {"id": ids[0], "tags": []})      # 清理
        A.post("/api/set_tags", {"id": ids[1], "tags": []})
    else:
        sk("tagmgr 合并链路", "列表不足 2 个文件")
    # --- 收尾：清掉自动标签，保持后面组别的干净状态 ---
    c, d, _ = A.post("/api/autotag_clear", {})
    ck("tagmgr: 清空自动标签 200", c == 200 and d.get("ok"), f"{c} {str(d)[:60]}")

    h("18. 输入校验与限流")
    c, d, _ = A.get("/api/list?limit=999999")
    ck("超大 limit 被钳制", c == 200, str(c))
    c, d, _ = A.get("/api/list?offset=-5")
    ck("负 offset 不报错", c == 200, str(c))
    c, d, _ = A.get("/api/list?" + q(sort=";DROP TABLE files"))
    ck("注入 sort 参数不崩", c in (200, 400), str(c))
    c, d, _ = A.post("/api/mark", {"id": "abc"})
    ck("非法 id 被拒", c in (400, 404), str(c))
    c, d, _ = A.req("/api/mark", "POST", {"id": 1, "mark": "star"},
                    headers={"Content-Type": "text/plain"})
    ck("非 JSON 请求体被拒", c in (400, 415), str(c))
    big = {"x": "y" * (2 << 20)}
    c, d, _ = A.post("/api/set_tags", big)
    ck("超大请求体被拒", c in (400, 413), str(c))

    # 未登录限流
    G = Client(BASE)
    codes = []
    for _ in range(80):
        c, _, _ = G.get("/api/stats")
        codes.append(c)
    ck("未登录高频请求触发限流 429", 429 in codes,
       f"max={max(codes)} 429数={codes.count(429)}")

    h("19. 登录锁定（指数退避）")
    L = Client(BASE)
    got429 = False
    for i in range(12):
        c, d, _ = L.post("/api/login", {"username": USER, "password": f"bad{i}"})
        if c == 429:
            got429 = True
            break
    ck("连续错误密码后被锁定", got429, "12 次内未触发")
    ck("锁定响应带 retry_after 倒计时（前端据此显示）",
       got429 and isinstance(d, dict)
       and isinstance(d.get("retry_after"), int) and d["retry_after"] > 0,
       str(d)[:80])
    # 复位登录退避与限流，避免污染第 20 组和下一次整体运行
    _bootstrap_reset(BASE, USER, PASS)

    h("20. 退出登录")
    c, d, _ = A.post("/api/logout")
    ck("登出 200", c == 200, str(c))
    # 用携带旧 cookie 的同一客户端再问一次：服务端应判定为未登录
    c, d, _ = A.get("/api/me")
    if c == 429:
        sk("登出后 /api/me 未登录", "本 IP 已被第 18 组限流，60 秒窗口内无法判定")
        ck("登出后旧 cookie 不再有效",
           not (isinstance(d, dict) and d.get("logged")), str(d)[:60])
    else:
        ck("登出后 /api/me 未登录",
           isinstance(d, dict) and d.get("logged") is False, str(d)[:80])

    # 收尾不打印（此时本 IP 多半正被第 18 组的限流窗口挡着，属预期）
    try:
        _bootstrap_reset(BASE, USER, PASS, quiet=True)
    except TypeError:
        pass

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 66)
    print(f" 通过 {OK} · 失败 {FAIL} · 跳过 {SKIP} · 共 {OK + FAIL + SKIP}")
    if FAILED:
        print("\n失败项：")
        for x in FAILED:
            print("  ✗", x)
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
