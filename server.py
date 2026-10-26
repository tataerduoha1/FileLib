"""
server.py —— FileLib 本地资料库 Web 服务（标准库实现，零框架依赖）

启动：
    python server.py                      # 本机访问 http://127.0.0.1:8321
    python server.py --host 0.0.0.0       # 允许手机/局域网访问
    python server.py --port 8321 --open

安全设计（逐项对应"防护措施"）：
  1. 会话 Cookie：HttpOnly + SameSite=Lax（+ HTTPS 时自动 Secure）
  2. CSRF：写请求必须带 X-Requested-With: FileLib，且 Origin/Referer 必须同源
  3. Host 校验：防 DNS 重绑定
  4. 登录：PBKDF2 12 万轮 + 每用户随机盐；失败 5 次指数退避锁定
  5. 限流：普通用户按分钟配额；媒体流按会话数限并发；admin 不限制
  6. 路径穿越：静态文件 resolve 后必须在 UI_DIR 内；媒体只按索引 id 取
  7. 权限：资料库级 + 导航项级双层校验，所有接口统一走 _guard()
  8. 安全响应头：CSP / nosniff / Referrer-Policy / frame-ancestors
  9. 请求体大小上限、URL 长度上限、参数类型校验
 10. 审计日志：配置变更、删除、权限调整全部落库
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from filelib.core import db as dbmod
from filelib.core import users as users_mod
from filelib.core import bookcover as bc
from filelib.core import security as sec
from filelib.core import config as cfg
from filelib.core import cache as cachemod
from filelib.core.db import connect, init_db, thumb_dir, backup_db, list_backups
from filelib.core.indexer import (find_ffmpeg, find_ffprobe, enqueue_tasks,
                                  build_meta, run_thumb_worker, extract_text)
from filelib.core.scanner import Scanner, ScanStats, long_path
from filelib.core.store import Store
from filelib.core.kinds import KIND_LABEL, get_ext, kind_of
from filelib.core import jobs
from filelib.core.jobs import (JOB, SCHEDULER, do_scan, do_cover_job,
                               do_fulltext_job, do_organize_job, do_autotag_job)

UI_DIR = ROOT / "filelib" / "ui"
APP_VERSION = cfg.APP_VERSION
RANGE_CHUNK = 1 << 20
MAX_BODY = 1 << 20            # 1MB 请求体上限
MAX_URL = 4096

LOGIN_GUARD = sec.LoginGuard(max_fail=5, window=600, lock_base=60)
# 限流额度：此处读 filelib.json 只是出厂默认；make_server 启动时会用
# settings 表（管理后台保存的值）校准，后台改完立即热更，无需重启。
RL_USER = sec.RateLimiter(int(cfg.cfg("rate_limit_user", 300)))
RL_GUEST = sec.RateLimiter(int(cfg.cfg("rate_limit_guest", 60)))
# 媒体流专用额度：一次播放会连发多次 Range 请求，普通接口的 60/分 明显不够
RL_MEDIA = sec.RateLimiter(int(cfg.cfg("rate_limit_media", 600)))
MEDIA_SESSIONS: dict[int, set[str]] = {}
MEDIA_LOCK = threading.Lock()
MEDIA_MAX = int(cfg.cfg("max_media_sessions", 3))   # 同上：settings 表校准 + 热更
TLS_ON = False    # --tls-cert/--tls-key 启动时置 True：会话 Cookie 加 Secure 标记

# ---------- 实时转码（可选增强，需要外部 ffmpeg） ----------
# 覆盖浏览器原生解不了的"老格式"。探测到 ffmpeg 才启用；否则前端保持直连+兜底。
TC_VIDEO_EXTS = {
    "avi", "wmv", "flv", "f4v", "rmvb", "rm", "vob", "mpg", "mpeg", "mpe",
    "m2ts", "mts", "ts", "asf", "ogv", "3gp", "3g2", "divx", "xvid",
    "dat", "mkv", "mov", "webm",
}
TC_AUDIO_EXTS = {"wma", "ape", "alac", "aiff", "aif", "amr", "mka", "dsf", "dff"}
TC_SEM = threading.BoundedSemaphore(int(cfg.cfg("max_transcode_sessions", 2)))
_FFMPEG_PATH: str | None | bool = False   # False=未探测, None=探测过但不可用
_FFMPEG_LOCK = threading.Lock()


def transcode_ffmpeg() -> str | None:
    """探测一次并缓存 ffmpeg 路径（探测链复用索引器的 find_ffmpeg）。

    结果缓存到进程退出：装好 ffmpeg 后需重启服务生效，避免每次请求都扫盘。
    """
    global _FFMPEG_PATH
    if _FFMPEG_PATH is not False:
        return _FFMPEG_PATH or None
    with _FFMPEG_LOCK:
        if _FFMPEG_PATH is False:
            p = find_ffmpeg()
            _FFMPEG_PATH = p if p else None
    return _FFMPEG_PATH


def human_size(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{u}" if u != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}PB"


def _cleanup_tree(p: Path, j=None) -> dict:
    """容错删除目录内容：逐文件 unlink，被占用的跳过并计数，最后清空目录壳。

    服务运行中清理缓存必然遇到"正好有请求在用某个缩略图"的情况，
    shutil.rmtree 会整趟失败，所以这里逐个删、失败了不算致命。
    目录本身也删掉（thumbs/covers/booktext/backups 都会按需重建）。
    """
    if not p.exists():
        return {"files": 0, "bytes": 0, "failed": 0}
    all_files: list[str] = []
    for root, _, fns in os.walk(p):
        for fn in fns:
            all_files.append(os.path.join(root, fn))
    total = len(all_files)
    files = freed = failed = 0
    for i, fp in enumerate(all_files):
        try:
            sz = os.path.getsize(fp)
        except OSError:
            sz = 0
        try:
            os.unlink(long_path(fp))
            files += 1
            freed += sz
        except OSError:
            failed += 1
        if j is not None and i % 200 == 0:
            j.progress = {"phase": "清理中", "done": i, "total": total}
    # 清掉空目录壳（删不掉的说明里面还有占位文件，无妨）
    for root, dirs, _fns in os.walk(p, topdown=False):
        for d in dirs:
            try:
                (Path(root) / d).rmdir()
            except OSError:
                pass
    try:
        p.rmdir()
    except OSError:
        pass
    if j is not None:
        j.progress = {"phase": "完成", "done": total, "total": total}
    return {"files": files, "bytes": freed, "failed": failed}


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


class Handler(BaseHTTPRequestHandler):
    server_version = f"FileLib/{APP_VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    store: Store = None
    allow_any_host = False

    # ---------- 基础 ----------

    def log_message(self, fmt, *args):
        if os.environ.get("FILELIB_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _headers_common(self, ctype: str, extra: dict | None = None, page: bool = False):
        self.send_header("Content-Type", ctype)
        # 调用方显式给了 Cache-Control 就不再叠加默认 no-store，
        # 避免出现两个矛盾的头（no-store 会压过 no-cache/max-age）
        if not (extra or {}).get("Cache-Control"):
            self.send_header("Cache-Control", "no-store")
        for k, v in (sec.PAGE_HEADERS if page else sec.BASE_HEADERS).items():
            self.send_header(k, v)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
              page: bool = False):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self._headers_common(ctype, extra, page)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _json(self, obj, code: int = 200, extra: dict | None = None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _err(self, code: int, msg: str, extra: dict | None = None):
        d = {"error": msg}
        if extra:
            d.update(extra)
        self._json(d, code)

    # ---------- 请求体收尾 ----------

    def _discard_body(self) -> None:
        """把请求体读掉丢掉，保持长连接上的字节流对齐。

        为什么必须有这一步：HTTP/1.1 长连接里，请求体也是流的一部分。
        如果服务端在读完 body 之前就回了响应（403/421/414 等提前返回），
        这堆字节还留在 socket 里，下一次读请求行就会读到
        「{}GET /api/me ...」，BaseHTTPRequestHandler 解析出的 method
        变成 {}GET，直接吐 501 Unsupported method ('{}GET')。

        真实触发场景：退出登录时前端 location.href="/" 立刻跳转，
        浏览器在同一 keep-alive 连接上紧接着发 GET /，于是首页变成 501。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = -1
        # 长度非法或超过上限：不尝试读（读不完反而会卡住），直接断开
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            return
        remain = n
        while remain > 0:
            try:
                chunk = self.rfile.read(min(remain, 65536))
            except (OSError, ValueError):
                self.close_connection = True
                return
            if not chunk:
                self.close_connection = True
                return
            remain -= len(chunk)

    def _err_and_close(self, code: int, msg: str):
        """提前返回错误时必须断掉连接：body 没读完时复用它必然错位。

        宁可让客户端重连一次，也不能让它下一条请求被解析成 501。
        """
        self.close_connection = True
        self._err(code, msg)

    # ---------- 请求解析 ----------

    def _req_path(self) -> str:
        """安全解析请求路径（防路径穿越 / 编码绕过）。

        先百分号解码两次（防止 %252e%252e 这类双重编码），
        再统一分隔符、消解 . 与 ..，最后确认没有残留的 ..
        """
        raw = urllib.parse.unquote(urllib.parse.unquote(self.path or "/"))
        raw = raw.split("?")[0].split("#")[0].replace("\\", "/")
        if "\x00" in raw:
            raise ApiError(400, "非法路径")
        parts: list[str] = []
        for seg in raw.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not parts:
                    raise ApiError(400, "非法路径")
                parts.pop()
                continue
            parts.append(seg)
        return "/" + "/".join(parts)

    def _query(self) -> dict[str, list[str]]:
        q = urllib.parse.urlparse(self.path).query
        if len(q) > MAX_URL:
            raise ApiError(414, "URL 过长")
        return urllib.parse.parse_qs(q, max_num_fields=60)

    def _q1(self, key: str, default: str | None = None) -> str | None:
        v = self._query().get(key)
        return v[0] if v else default

    def _body(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            raise ApiError(415, "请求体必须是 application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            # 不读超大请求体，直接拒绝并关闭连接（否则客户端会卡在写数据）
            self.close_connection = True
            raise ApiError(413, "请求体过大")
        raw = self.rfile.read(n)
        try:
            d = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "请求格式错误")
        if not isinstance(d, dict):
            raise ApiError(400, "请求格式错误")
        return d

    # ---------- 会话 / 身份 ----------

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "flsid":
                return v
        return None

    def _user(self) -> dict | None:
        tok = self._cookie_token()
        if not tok:
            return None
        conn = self.store.rw
        s = users_mod.get_session(conn, tok)
        if not s:
            return None
        try:
            u = users_mod.get_by_id(conn, s["uid"])
        except Exception:
            return None
        if not u or u["disabled"]:
            return None
        u["_token"] = tok
        return u

    def _set_cookie(self, token: str) -> dict:
        secure = ("; Secure" if (TLS_ON or self.headers.get("X-Forwarded-Proto") == "https")
                  else "")
        return {"Set-Cookie":
                f"flsid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={7*86400}{secure}"}

    def _public_me(self, u: dict | None) -> dict:
        if not u:
            return {"logged": False,
                    "must_setup": users_mod.count_users(self.store.rw) == 0}
        is_admin = u["role"] == "admin"
        return {"logged": True, "id": u["id"], "username": u["username"],
                "role": u["role"], "note": u.get("note") or "",
                "vols": "all" if is_admin else u["perms"],
                "nav": "*" if is_admin else u["nav"],
                "version": APP_VERSION,
                "transcode": bool(transcode_ffmpeg()),
                "themes": ["light", "dark", "sepia", "night", "contrast"]}

    def _is_admin(self) -> bool:
        u = self._user()
        return bool(u and u["role"] == "admin")

    def _admin(self) -> dict:
        u = self._user()
        if not u or u["role"] != "admin":
            raise ApiError(403, "需要管理员权限")
        return u

    def _vol_for(self, nav: str = "home"):
        """
        把请求的 vol 换算成实际允许的范围。
        admin → 请求什么就是什么（None = 全部库）
        普通用户 → 必须命中其有权限的库，且该库下允许该 nav；否则 403 / 空集
        """
        vol = None
        v = self._q1("vol")
        if v not in (None, "", "0"):
            try:
                vol = int(v)
            except ValueError:
                raise ApiError(400, "vol 参数错误")
        u = self._user()
        if not u:
            raise ApiError(401, "需要登录")
        if u["role"] == "admin":
            return vol
        if vol is None:
            allowed = [x for x in u["perms"] if users_mod.can_see(u, x, nav)]
            return allowed[0] if allowed else -1
        if not users_mod.can_see(u, vol, nav):
            raise ApiError(403, "无权访问")
        return vol

    def _require_nav(self, nav: str) -> None:
        """当前库是否允许该导航项（普通用户）。"""
        u = self._user()
        if not u:
            raise ApiError(401, "需要登录")
        if u["role"] == "admin":
            return
        vol = self._vol_for(nav)
        if vol == -1:
            raise ApiError(403, "无权访问")

    def _file_row(self, fid: int) -> dict | None:
        r = self.store.conn.execute(
            "SELECT f.*, v.root_path FROM files f JOIN volumes v ON v.id=f.vol_id "
            "WHERE f.id=?", (fid,)).fetchone()
        return dict(r) if r else None

    def _file_guard(self, fid: int, nav: str = "play") -> dict:
        u = self._user()
        if not u:
            raise ApiError(401, "需要登录")
        row = self._file_row(fid)
        if not row:
            raise ApiError(404, "记录不存在")
        if u["role"] != "admin" and not users_mod.can_see(u, row["vol_id"], nav):
            raise ApiError(403, "无权访问该资料库")
        return row

    def _abs_path(self, row: dict) -> str:
        return os.path.join(row["root_path"], row["rel_dir"].replace("/", os.sep), row["name"])

    # ---------- 限流 ----------

    def _rate(self, u: dict | None) -> None:
        ip = sec.client_ip(self)
        if u and u["role"] == "admin":
            return
        if u:
            ok, left = RL_USER.hit(f"u{u['id']}")
        else:
            ok, left = RL_GUEST.hit(f"ip{ip}")
        if not ok:
            raise ApiError(429, "请求过于频繁，请稍后再试")

    def _reset_rate_touch(self) -> None:
        """登录成功后清掉该 IP 的游客限流计数。

        否则一旦触发过限流，连登录成功本身都要等满 60 秒窗口，
        用户看到的就是「登录成功但整页 429」。
        """
        try:
            RL_GUEST.reset(f"ip{sec.client_ip(self)}")
        except Exception:
            pass

    # ---------- 写请求防护 ----------

    def _csrf_ok(self) -> bool:
        # 自定义头：浏览器跨站表单无法伪造（跨站 fetch 带它必须先过 CORS 预检，
        # 而本服务不返回任何 Access-Control-Allow-Origin，预检必然失败）
        if (self.headers.get("X-Requested-With") or "") != "FileLib":
            return False
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        if sec.origin_ok(origin, referer, self.headers.get("Host") or ""):
            return True
        # 兜底：Origin 与 Referer 都缺失时（部分手机浏览器 / 微信内置 /
        # 隐私模式下会剥掉这两个头），退化为「Sec-Fetch-Site 同源」判定。
        # 这不是降低安全性——判定依据仍然是浏览器自己标注的跨站属性，
        # 而跨站页面无法伪造 Sec-Fetch-* 系列头。
        if origin or referer:
            return False  # 有值但不匹配 → 明确跨站，拒绝
        return (self.headers.get("Sec-Fetch-Site") or "").strip().lower() in ("same-origin", "none")

    # ================= 路由 =================

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            if len(self.path) > MAX_URL:
                return self._err(414, "URL 过长")
            host = self.headers.get("Host") or ""
            if not sec.is_allowed_host(host, self.allow_any_host):
                return self._err(421, "Host 不被允许（请用本机 IP 或 localhost 访问）")
            path = self._req_path()
            u = self._user()
            if path.startswith("/static/") or path in (
                    "/", "/index.html", "/favicon.ico", "/manifest.webmanifest", "/sw.js"):
                if not u and path.startswith("/static/"):
                    pass
                return self._route_static(path)
            # 媒体/下载/转码不占普通接口的分钟额度（拖进度条会连发几十次 Range 请求），
            # 单独用一套更宽松的限流器，仍能挡住刷带宽的脚本。
            if (path.startswith("/media/") or path.startswith("/download/")
                    or path.startswith("/transcode/")):
                key = f"m{u['id']}" if u else "m0"
                if not RL_MEDIA.hit(key)[0]:
                    return self._err(429, "请求过于频繁，请稍后再试")
                if not u:
                    return self._json({"error": "需要登录", "auth": False}, 401)
                return self._route_api(path, u)
            if path.startswith("/download_zip"):
                key = f"m{u['id']}" if u else "m0"
                if not RL_MEDIA.hit(key)[0]:
                    return self._err(429, "请求过于频繁，请稍后再试")
                if not u:
                    return self._json({"error": "需要登录", "auth": False}, 401)
                return self._download_zip(u)
            self._rate(u)
            if path == "/api/me":
                return self._json(self._public_me(u))
            if path == "/api/ping":
                return self._json({"ok": True, "version": APP_VERSION})
            if not u:
                return self._json({"error": "需要登录", "auth": False}, 401)
            return self._route_api(path, u)
        except ApiError as ex:
            self._safe_err(ex.code, ex.msg)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ConnectionAbortedError, OSError) as ex:
            # 客户端中途断开（播放器拖动进度条、反复刷新）是正常现象，不刷错误
            if getattr(ex, "winerror", None) not in (10053, 10054, 10058):
                self._trace(ex)
                self._safe_err(500, f"{type(ex).__name__}: {ex}")
        except Exception as ex:
            self._trace(ex)
            self._safe_err(500, f"{type(ex).__name__}: {ex}")

    def do_POST(self):
        try:
            if len(self.path) > MAX_URL:
                self._discard_body()
                return self._err(414, "URL 过长")
            host = self.headers.get("Host") or ""
            if not sec.is_allowed_host(host, self.allow_any_host):
                self._discard_body()
                return self._err(421, "Host 不被允许")
            if not self._csrf_ok():
                # 读完 body 再回 403：否则 "{}" 这类 2 字节体会残留，
                # 被同一条 keep-alive 连接上的下一个请求（如退出登录后
                # 立刻跳转的 GET /）读成 "{}GET"，变成 501。
                self._discard_body()
                return self._err(403, "请从页面内操作（CSRF 校验未通过）")
            path = self._req_path()
            u = self._user()
            if path == "/api/logout":
                # 登出永远放行（不能被限流卡住，否则用户"被锁在应用里"）
                self._discard_body()
                return self._api_logout()
            if path in ("/api/login", "/api/setup"):
                # 登录/初始化不计入游客分钟额度：它们本身已有
                # 账号级 + IP 级的指数退避锁定来防爆破，
                # 再加一层分钟限流只会让用户"登录成功却整页 429"。
                return self._api_login() if path == "/api/login" else self._api_setup()
            self._rate(u)
            if not u:
                self._discard_body()
                return self._json({"error": "需要登录", "auth": False}, 401)
            return self._route_post(path, u)
        except ApiError as ex:
            # ApiError 可能抛在 _body() 之前（_rate/_run 里），也可能之后。
            # 统一兜底：没读干净的连接不复用，避免下一条请求错位。
            self.close_connection = True
            self._safe_err(ex.code, ex.msg)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as ex:
            self.close_connection = True
            self._trace(ex)
            self._safe_err(500, f"{type(ex).__name__}: {ex}")

    def _trace(self, ex):
        import traceback
        traceback.print_exc()

    def _safe_err(self, code, msg):
        try:
            self._err(code, msg)
        except Exception:
            pass

    # ---------- 静态 ----------

    def _route_static(self, path: str):
        if path == "/favicon.ico":
            p = UI_DIR / "favicon.svg"
            if p.is_file():
                return self._send(200, p.read_bytes(), "image/svg+xml",
                                  {"Cache-Control": "public, max-age=86400"})
            return self._send(204, b"", "image/x-icon")
        if path in ("/", "/index.html"):
            return self._send(200, (UI_DIR / "index.html").read_bytes(),
                              "text/html; charset=utf-8", page=True)
        if path == "/manifest.webmanifest":
            p = UI_DIR / "manifest.webmanifest"
            if p.is_file():
                return self._send(200, p.read_bytes(),
                                  "application/manifest+json; charset=utf-8",
                                  {"Cache-Control": "public, max-age=300"})
            return self._err(404, "not found")
        if path == "/sw.js":
            # Service Worker 必须挂在根作用域。no-cache 让浏览器每次都能
            # 拿到最新脚本（字节变了自动激活新版 SW，缓存名带版本号整体轮换）
            p = UI_DIR / "sw.js"
            if p.is_file():
                return self._send(200, p.read_bytes(), "text/javascript; charset=utf-8",
                                  {"Cache-Control": "no-cache"})
            return self._err(404, "not found")
        rel = path[8:]
        target = (UI_DIR / rel).resolve()
        base = UI_DIR.resolve()
        if not str(target).startswith(str(base) + os.sep) or not target.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix in (".js", ".css", ".svg"):
            ctype += "; charset=utf-8"
        # 用 ETag 协商缓存，而不是 max-age 强缓存。
        # 强缓存的问题：改完 app.js 用户那边最多 5 分钟还在跑旧代码，
        # 表现就是「明明修了还是报同样的错」。ETag 每次询问一句 304，
        # 既省流量又保证改完立刻生效。
        raw = target.read_bytes()
        etag = '"%x-%x"' % (len(raw), int(target.stat().st_mtime))
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            for k, v in sec.BASE_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            return
        self._send(200, raw, ctype,
                   {"Cache-Control": "no-cache", "ETag": etag})

    # ---------- GET API ----------

    def _route_api(self, path: str, u: dict):
        store = self.store

        if path == "/api/stats":
            vol = self._vol_for("home")
            st = store.stats(vol)
            st = self._filter_stats(st, u)
            st["settings"] = self._public_settings()
            return self._json(st)

        if path == "/api/tree":
            vol = self._vol_for("tree")
            rel = self._q1("rel", "") or ""
            return self._json({"rel": rel, "vol_id": vol,
                               "children": store.dir_children(rel, vol)})

        if path == "/api/dirtree":
            vol = self._vol_for("tree")
            if vol is None:
                return self._err(400, "需要 vol 参数")
            if vol == -1:
                return self._json({"vol_id": -1, "nodes": [], "root_files": {"n": 0, "size": 0}})
            return self._json(store.dir_subtree(vol))

        if path == "/api/fm_dirs":
            return self._api_fm_dirs()

        if path == "/api/list":
            return self._api_list(u)

        if path == "/api/topics":
            return self._json({"items": store.topics(self._vol_for("topics"))})

        if path == "/api/series":
            return self._api_series()

        if path == "/api/authors":
            q = self._query()
            vol = self._vol_for("authors")
            if vol == -1:
                return self._json({"total": 0, "items": []})
            return self._json(store.authors(
                vol_id=vol, q=self._q1("q", "") or "",
                limit=sec.clamp_int(self._q1("limit", "300"), 1, 1000, 300),
                offset=sec.clamp_int(self._q1("offset", "0"), 0, 10 ** 8, 0)))

        if path == "/api/author_books":
            name = self._q1("name", "") or ""
            vol = self._vol_for("authors")
            if vol == -1:
                return self._json({"items": []})
            return self._json({"items": store.author_books(name, vol)})

        if path == "/api/publishers":
            vol = self._vol_for("authors")
            if vol == -1:
                return self._json({"items": []})
            return self._json(store.publishers(vol, q=self._q1("q", "") or ""))

        if path == "/api/file":
            fid = sec.clamp_int(self._q1("id", "0"), 0, 10 ** 12, 0)
            self._file_guard(fid, "home")
            item = store.get_file(fid)
            if not item:
                return self._err(404, "not found")
            uu = u
            item["read_progress"] = store.get_read_progress(uu["id"], fid)
            item["media_progress"] = store.get_media_progress(uu["id"], fid)
            store.ensure_media_duration(item)   # 转码流进度条需要真实总时长：缺则惰性探测并持久化
            return self._json(item)

        if path == "/api/tags":
            return self._api_tags()

        if path == "/api/fulltext":
            return self._api_fulltext()

        if path == "/api/dups":
            self._require_nav("dups")
            cross = "cross" in self._query()
            vol = self._vol_for("dups")
            if vol == -1:
                return self._json({"groups": [], "total": 0})
            return self._json({"groups": store.duplicates(vol, cross=cross),
                               "total": store.duplicates_total(vol, cross=cross)})

        if path == "/api/junk":
            self._require_nav("junk")
            vol = self._vol_for("junk")
            if vol == -1:
                return self._json({})
            return self._json(store.junk_candidates(vol))

        if path == "/api/home":
            """首页聚合：收藏、继续看、最近添加、统计。"""
            vol = self._vol_for("home")
            if vol == -1:
                return self._json({"empty": True})
            return self._json({
                "star": store.query(mark="star", vol_id=vol, limit=12)["items"],
                "continue": store.continue_watching(u["id"], vol, 12),
                "recent": store.recent_files(vol, 12),
                "daily": store.daily_pick(vol),
            })

        if path == "/api/continue":
            """继续看页：当前用户最近的播放/阅读记录（含进度）。"""
            self._require_nav("continue")
            vol = self._vol_for("continue")
            if vol == -1:
                return self._json({"items": []})
            limit = sec.clamp_int(self._q1("limit", "200"), 1, 500, 200)
            return self._json({"items": store.continue_watching(u["id"], vol, limit)})

        if path == "/api/random":
            """随机看：随机挑一批文件。"""
            self._require_nav("random")
            vol = self._vol_for("random")
            if vol == -1:
                return self._json({"items": []})
            n = sec.clamp_int(self._q1("n", "60"), 1, 200, 60)
            return self._json({"items": store.random_files(vol, n)})

        if path == "/api/readstats":
            """阅读统计：逐日时长 / 汇总 / 连续打卡（当前登录用户）。"""
            self._require_nav("stats")
            return self._json(store.read_stats(u["id"]))

        if path == "/api/task":
            snap = JOB.snapshot()
            snap["ffmpeg"] = bool(find_ffmpeg())
            snap["calibre"] = bool(bc.find_calibre())
            snap["pending"] = self._pending_counts()
            return self._json(snap)

        if path == "/api/read":
            return self._read_doc(u)

        if path == "/api/ebook":
            return self._ebook(u)

        if path.startswith("/thumb/") or path.startswith("/cover/"):
            return self._thumb(path)

        if path.startswith("/media/"):
            return self._media(path)

        if path.startswith("/transcode/"):
            return self._transcode(path)

        if path.startswith("/download/"):
            return self._download(path, u)

        if path.startswith("/fonts/"):
            return self._err(404, "not found")

        if path == "/api/admin/users":
            self._admin()
            return self._admin_users()

        if path == "/api/admin/settings":
            self._admin()
            return self._json(self._admin_settings())

        if path == "/api/admin/logs":
            self._admin()
            conn = self.store.rw
            return self._json({"logins": users_mod.recent_logins(conn, 300),
                               "audit": users_mod.recent_audit(conn, 300),
                               "backups": list_backups()})

        if path == "/api/admin/overview":
            self._admin()
            return self._json(self._admin_overview())

        if path == "/api/admin/data":
            self._admin()
            return self._json(self._admin_data())

        if path == "/api/admin/rate_reset":
            # 只允许本机回环调用：用于自检/压测前重置限流计数，不对外网开放
            me = self._admin()
            if not sec.is_loopback(sec.client_ip(self)):
                return self._err(403, "仅限本机操作")
            RL_GUEST.clear()
            RL_USER.clear()
            RL_MEDIA.clear()
            LOGIN_GUARD.clear()
            users_mod.audit(self.store.rw, me, sec.client_ip(self), "rate.reset", "重置限流计数")
            return self._json({"ok": True})

        if path == "/api/tools":
            return self._json({"ffmpeg": find_ffmpeg(), "ffprobe": find_ffprobe(),
                               "calibre": bc.find_calibre()})

        if path == "/api/errors":
            self._admin()
            rows = self.store.conn.execute(
                "SELECT path, reason, ts FROM scan_errors ORDER BY id DESC LIMIT 200").fetchall()
            return self._json({"items": [dict(r) for r in rows]})

        return self._err(404, "no route")

    # ---------- POST API ----------

    def _route_post(self, path: str, u: dict):
        admin_only = {
            "/api/scan", "/api/thumbs", "/api/tag_bulk",
            "/api/tag_admin", "/api/fulltext_build", "/api/backup", "/api/stop",
            "/api/delete", "/api/dups_full", "/api/organize",
            "/api/admin/users", "/api/admin/settings", "/api/admin/library",
            "/api/admin/cleanup",
            "/api/cover_fix", "/api/cover_upload",
            "/api/fm_mkdir",
            "/api/autotag", "/api/autotag_clear",
        }
        if path in admin_only:
            self._admin()
        if path.endswith("_public"):
            return self._err(404, "no route")

        if path == "/api/scan":
            return self._api_scan()
        if path == "/api/organize":
            return self._api_organize()
        if path == "/api/thumbs":
            return self._api_thumbs()
        if path == "/api/mark":
            return self._api_mark(u)
        if path == "/api/set_tags":
            return self._api_set_tags()
        if path == "/api/tag_bulk":
            return self._api_tag_bulk()
        if path == "/api/tag_admin":
            return self._api_tag_admin()
        if path == "/api/fulltext_build":
            return self._api_fulltext_build()
        if path == "/api/autotag":
            return self._api_autotag()
        if path == "/api/autotag_clear":
            return self._api_autotag_clear()
        if path == "/api/backup":
            return self._api_backup()
        if path == "/api/stop":
            JOB.stop()
            return self._json({"ok": True})
        if path == "/api/open":
            return self._api_open(select=False)
        if path == "/api/reveal":
            return self._api_open(select=True)
        if path == "/api/delete":
            return self._api_delete(u)
        if path == "/api/fm_mkdir":
            return self._api_fm_mkdir()
        if path == "/api/continue":
            return self._api_continue(u)
        if path == "/api/dups_full":
            return self._api_dups_full()
        if path == "/api/admin/users":
            return self._admin_users(post=True)
        if path == "/api/admin/settings":
            return self._admin_settings(post=True)
        if path == "/api/admin/library":
            return self._api_library_admin(u)
        if path == "/api/admin/cleanup":
            return self._api_cleanup(u)
        if path == "/api/progress":
            return self._api_progress(u)
        if path == "/api/readlog":
            return self._api_readlog(u)
        if path == "/api/cover_fix":
            return self._api_cover_fix()
        if path == "/api/cover_upload":
            return self._api_cover_upload()
        if path == "/api/rethumb":
            return self._api_rethumb()
        return self._err(404, "no route")

    # ---------- 权限过滤 ----------

    def _filter_stats(self, st: dict, u: dict | None) -> dict:
        if u and u["role"] != "admin":
            st["volumes"] = [v for v in st.get("volumes", []) if v["id"] in u["perms"]]
        # 给每个库附上它的短名与可读标识（重名时前端用 id + 路径区分）
        return st

    def _public_settings(self) -> dict:
        conn = self.store.conn
        return {
            "rest_remind": dbmod.get_setting(conn, "rest_remind", False),
            "rest_interval_min": dbmod.get_setting(conn, "rest_interval_min", 45),
            "idle_pause": dbmod.get_setting(conn, "idle_pause", True),
            "theme": dbmod.get_setting(conn, "theme", "light"),
            "site_name": dbmod.get_setting(conn, "site_name", "FileLib"),
        }

    def _pending_counts(self) -> dict:
        try:
            rows = self.store.conn.execute(
                "SELECT type, SUM(state='pending') p, SUM(state='failed') f "
                "FROM tasks GROUP BY type").fetchall()
            return {r["type"]: {"pending": r["p"], "failed": r["f"]} for r in rows}
        except Exception:
            return {}

    # ---------- 列表 ----------

    def _api_list(self, u: dict):
        vol = self._vol_for("home")
        if vol == -1:
            return self._json({"total": 0, "items": [], "dirs": []})
        q = self._query()

        def one(k, d=None):
            v = q.get(k)
            return v[0] if v else d

        mark = one("mark")
        if mark:
            self._require_nav(mark if mark in ("star", "read", "todo") else "home")
        if one("tag") or one("tags"):
            self._require_nav("tags")
        if one("author"):
            self._require_nav("authors")

        res = self.store.query(
            q=one("q", "") or "",
            kind=one("kind"), rel_dir=one("dir"),
            recursive=(one("recursive", "1") != "0"),
            ext=one("ext"), topic=one("topic"), series=one("series"),
            author=one("author"), mark=mark,
            tag=one("tag"),
            tags=[t for t in (one("tags", "") or "").split(",") if t],
            not_tags=[t for t in (one("ntags", "") or "").split(",") if t],
            min_size=int(one("min_size", "0") or 0) or None,
            sort=one("sort", "mtime_desc"),
            limit=sec.clamp_int(one("limit", "120"), 1, 500, 120),
            offset=sec.clamp_int(one("offset", "0"), 0, 10 ** 8, 0),
            vol_id=vol,
        )
        ids = [i["id"] for i in res["items"]]
        res["dirs"] = []
        res["tagmap"] = self.store.tag_map(ids)
        return self._json(res)

    def _api_series(self):
        vol = self._vol_for("series")
        if vol == -1:
            return self._json({"total": 0, "items": []})
        res = self.store.series_list(
            kind=self._q1("kind"), topic=self._q1("topic"), q=self._q1("q", "") or "",
            limit=sec.clamp_int(self._q1("limit", "200"), 1, 500, 200),
            offset=sec.clamp_int(self._q1("offset", "0"), 0, 10 ** 8, 0),
            vol_id=vol)
        return self._json(res)

    def _api_tags(self):
        vol = self._vol_for("tags")
        if vol == -1:
            return self._json({"total": 0, "items": []})
        # user=1/0：只返回人工/自动标签（侧栏 tab 过滤下推，避免 LIMIT 截断
        # 后前端再过滤把人工标签挤没）
        uv = self._q1("user", "")
        user_only = True if uv == "1" else False if uv == "0" else None
        return self._json(self.store.all_tags(
            vol_id=vol, q=(self._q1("q", "") or "").strip(),
            limit=sec.clamp_int(self._q1("limit", "200"), 1, 2000, 200),
            show_hidden=self._q1("show_hidden", "") == "1",
            include_empty=self._q1("empty", "") == "1",
            user_only=user_only))

    # ---------- 全文搜索 ----------

    def _api_fulltext(self):
        self._require_nav("fulltext")
        q = (self._q1("q", "") or "").strip()
        if not q:
            return self._json({"items": [], "total": 0})
        vol = self._vol_for("fulltext")
        if vol == -1:
            return self._json({"items": [], "total": 0})
        limit = sec.clamp_int(self._q1("limit", "30"), 1, 200, 30)
        # FTS5 语法防护：把用户输入整体当成短语，避免语法错误 500
        safe = q.replace('"', " ").strip()
        match = f'"{safe}"' if safe else ""
        try:
            rows = self.store.conn.execute(
                """
                SELECT f.id, f.vol_id, f.rel_dir, f.name, f.ext, f.kind, f.size,
                       snippet(fts, 1, '<mark>', '</mark>', '…', 14) AS snip
                FROM fts JOIN files f ON f.id = fts.rowid
                WHERE fts MATCH ? AND f.status='present'
                ORDER BY rank LIMIT ?
                """, (match, limit * 4)).fetchall()
        except Exception:
            # 退化为逐词 OR
            try:
                match = " OR ".join(f'"{w}"' for w in safe.split() if w)
                rows = self.store.conn.execute(
                    """
                    SELECT f.id, f.vol_id, f.rel_dir, f.name, f.ext, f.kind, f.size,
                           snippet(fts, 1, '<mark>', '</mark>', '…', 14) AS snip
                    FROM fts JOIN files f ON f.id = fts.rowid
                    WHERE fts MATCH ? AND f.status='present'
                    ORDER BY rank LIMIT ?
                    """, (match, limit * 4)).fetchall()
            except Exception:
                return self._json({"items": [], "total": 0, "error": "搜索词格式不支持"})
        if not rows:
            # 兜底：trigram 需要 ≥3 字才能命中；1~2 字或无结果时退化为 LIKE 子串匹配
            like = f"%{safe}%"
            try:
                rows = self.store.conn.execute(
                    """
                    SELECT f.id, f.vol_id, f.rel_dir, f.name, f.ext, f.kind, f.size,
                           substr(t.body, max(1, instr(t.body, ?) - 40), 120) AS snip
                    FROM file_text t JOIN files f ON f.id = t.file_id
                    WHERE t.body LIKE ? AND f.status='present'
                    LIMIT ?
                    """, (safe, like, limit * 4)).fetchall()
            except Exception:
                rows = []
        items = []
        for r in rows:
            if vol is not None and r["vol_id"] != vol:
                continue
            items.append({"id": r["id"], "name": r["name"], "ext": r["ext"],
                          "kind": r["kind"], "size": r["size"], "dir": r["rel_dir"],
                          "snippet": r["snip"]})
            if len(items) >= limit:
                break
        return self._json({"items": items, "total": len(items)})

    # ---------- 缩略图 / 封面 ----------

    def _thumb(self, path: str):
        try:
            # path 形如 /thumb/123.jpg、/cover/45-s.jpg；[1] 是 "thumb"/"cover"，ID 在最后一段
            name = path.split("/")[-1]
            fid = int(name.split(".")[0])
        except (ValueError, IndexError):
            return self._err(400, "bad id")
        self._file_guard(fid, "home")
        is_cover = path.startswith("/cover/")
        size = "s" if "-s" in name else ""
        if is_cover:
            p = bc.cover_path(fid, size)
            if not p.is_file():
                p = thumb_dir() / f"{fid % 1000:03d}" / f"{fid}.jpg"
        else:
            p = thumb_dir() / f"{fid % 1000:03d}" / f"{fid}.jpg"
        if not p.is_file():
            # v1.0.5 按需生成：缓存不存在时现场生成一张（限并发），滚动到哪生成到哪
            if self._gen_thumb_now(fid, is_cover):
                for cand in ((bc.cover_path(fid, size),) if is_cover else ()) + \
                            (thumb_dir() / f"{fid % 1000:03d}" / f"{fid}.jpg",):
                    if cand.is_file():
                        p = cand
                        break
            if not p.is_file():
                return self._send(404, b"", "image/jpeg")
        try:
            data = p.read_bytes()
        except OSError:
            return self._send(404, b"", "image/jpeg")
        self._send(200, data, "image/jpeg", {"Cache-Control": "public, max-age=604800"})

    # 最多 3 路并发现场生成（滚动图片墙时浏览器会一次发几十个 img 请求，
    # 不限会瞬间压满 CPU / 磁盘；其余请求在此排队，先到先得）
    _GEN_SEM = threading.BoundedSemaphore(3)

    def _gen_thumb_now(self, fid: int, is_cover: bool = False) -> bool:
        """按需现场生成缩略图并落盘缓存（与后台任务同一套生成函数，结果一致）。"""
        try:
            r = self.store.conn.execute(
                "SELECT f.kind, f.ext, f.rel_dir, f.name, v.root_path "
                "FROM files f JOIN volumes v ON v.id = f.vol_id WHERE f.id=?",
                (fid,)).fetchone()
            if not r or not r["root_path"]:
                return False
            src = os.path.join(r["root_path"], r["rel_dir"].replace("/", os.sep), r["name"])
            if not os.path.isfile(src):
                return False
            from filelib.core.indexer import (_image_thumb, _book_thumb,
                                              _videogen_thumb, thumb_path)
            dst = thumb_path(fid)
            kind, ext = r["kind"], (r["ext"] or "").lower()
            with Handler._GEN_SEM:
                if kind == "image":
                    return _image_thumb(src, dst)
                if kind == "video":
                    ff = find_ffmpeg()
                    return bool(ff) and _videogen_thumb(ff, src, str(dst))
                if kind == "book" or ext in ("pdf", "epub", "mobi", "azw3", "azw",
                                             "azw4", "prc", "pdb", "fb2"):
                    # 现场路径禁用 Calibre 兜底（每本可达数十秒，会把请求线程挂死）
                    ok, _m = _book_thumb(src, ext, dst, fid, allow_calibre=False)
                    return ok
        except Exception:
            return False
        return False

    # ---------- 媒体流（Range，可拖进度） ----------

    # 显式 MIME 表：mimetypes 在部分 Windows 上依赖注册表，缺项会退化成
    # application/octet-stream，导致本来能播的格式也无法内联播放。
    _MEDIA_MIME = {
        "mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav",
        "aac": "audio/aac", "m4a": "audio/mp4", "ogg": "audio/ogg",
        "oga": "audio/ogg", "opus": "audio/ogg", "wma": "audio/x-ms-wma",
        "ape": "audio/x-ape", "alac": "audio/mp4", "aiff": "audio/aiff",
        "aif": "audio/aiff", "mka": "audio/x-matroska", "amr": "audio/amr",
        "mid": "audio/midi", "midi": "audio/midi",
        "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm",
        "mov": "video/quicktime", "avi": "video/x-msvideo",
        "wmv": "video/x-ms-wmv", "flv": "video/x-flv", "f4v": "video/mp4",
        "mkv": "video/x-matroska", "ts": "video/mp2t", "m2ts": "video/mp2t",
        "mts": "video/mp2t", "ogv": "video/ogg", "3gp": "video/3gpp",
        "3g2": "video/3gpp2", "mpg": "video/mpeg", "mpeg": "video/mpeg",
        "mpe": "video/mpeg", "vob": "video/mpeg", "asf": "video/x-ms-asf",
        "rmvb": "application/vnd.rn-realmedia-vbr",
        "rm": "application/vnd.rn-realmedia",
    }

    def _media(self, path: str):
        try:
            # path 形如 /media/12345.mp4（可能带 ?v=1 查询串，先剥掉）
            seg = path.split("?")[0].rstrip("/").split("/")[-1]
            fid = int(seg.split(".")[0])
        except (ValueError, IndexError):
            return self._err(400, "bad id")
        u = self._user()
        row = self._file_guard(fid, "play")
        # 普通用户：限制同时播放的会话数，避免多路大带宽拖垮机器
        sid = u.get("_token", "")
        if u["role"] != "admin":
            with MEDIA_LOCK:
                active = MEDIA_SESSIONS.setdefault(u["id"], set())
                if sid not in active and len(active) >= MEDIA_MAX:
                    return self._err(429, "同时播放数已达上限，请先关闭其他播放窗口")
                active.add(sid)
        src = long_path(self._abs_path(row))
        if not os.path.isfile(src):
            return self._err(404, "文件不存在（可能未插入外接盘）")

        size = os.path.getsize(src)
        ctype = (self._MEDIA_MIME.get(row["ext"])
                 or mimetypes.guess_type(row["name"])[0]
                 or "application/octet-stream")
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                elif b:
                    start = max(0, size - int(b))
                partial = True
            except ValueError:
                partial = False
        if start >= size:
            return self._err(416, "range")
        length = end - start + 1
        if length <= 0:
            return self._err(416, "range")

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        for k, v in sec.BASE_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(src, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(RANGE_CHUNK, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---------- 实时转码流（AVI/WMV/RMVB 等老格式 → fMP4/MP3） ----------

    def _transcode(self, path: str):
        ff = transcode_ffmpeg()
        if not ff:
            return self._err(
                503, "未检测到 ffmpeg，无法转码。请将 ffmpeg.exe 放入程序目录 "
                     "bin\\ 下（或安装到 PATH / 在 filelib.json 配置 ffmpeg 路径）后重启服务")
        try:
            # path 形如 /transcode/12345.avi?seek=90
            seg = path.split("?")[0].rstrip("/").split("/")[-1]
            fid = int(seg.split(".")[0])
        except (ValueError, IndexError):
            return self._err(400, "bad id")
        u = self._user()
        row = self._file_guard(fid, "play")
        # 与 /media 相同的普通用户会话数限制：转码更耗 CPU，更不能放任多开
        sid = u.get("_token", "")
        if u["role"] != "admin":
            with MEDIA_LOCK:
                active = MEDIA_SESSIONS.setdefault(u["id"], set())
                if sid not in active and len(active) >= MEDIA_MAX:
                    return self._err(429, "同时播放数已达上限，请先关闭其他播放窗口")
                active.add(sid)
        src = long_path(self._abs_path(row))
        if not os.path.isfile(src):
            return self._err(404, "文件不存在（可能未插入外接盘）")

        ext = row["ext"]
        is_audio = ext in TC_AUDIO_EXTS
        if ext not in TC_VIDEO_EXTS and not is_audio:
            return self._err(415, "该格式无需或暂不支持转码")
        seek = sec.clamp_int(self._q1("seek", "0"), 0, 48 * 3600, 0)
        if not TC_SEM.acquire(blocking=False):
            return self._err(429, "转码通道忙（同时路数已达上限），请稍后再试")

        args = [ff, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if seek > 0:
            args += ["-ss", str(seek)]     # 输入端快进：秒级出流，从拖动点重新转码
        args += ["-i", src]
        if is_audio:
            # 纯音频老格式（wma/ape/aiff…）→ MP3，兼容一切浏览器
            args += ["-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"]
        else:
            # 视频 → 分片 fMP4：边转边播，无需等待整文件转完
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                     "-c:a", "aac", "-b:a", "128k",
                     "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                     "-f", "mp4"]
        args += ["pipe:1"]

        # CREATE_NO_WINDOW：Windows 下不弹黑框
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            p = subprocess.Popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError:
            TC_SEM.release()
            return self._err(500, "ffmpeg 启动失败")

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg" if is_audio else "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for k, v in sec.BASE_HEADERS.items():
            self.send_header(k, v)
        # 管道流没有 Content-Length：靠断开连接标记响应结束
        self.close_connection = True
        self.end_headers()
        if self.command == "HEAD":
            if p.poll() is None:
                p.kill()
            p.wait()
            TC_SEM.release()
            return
        try:
            while True:
                # read1：有多少读多少，不凑满缓冲，保证首帧尽快到达
                chunk = p.stdout.read1(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass   # 客户端断开（关播放器 / 拖动进度条重开转码）是正常现象
        finally:
            # 无论哪边先结束，都必须回收 ffmpeg 进程并释放转码通道
            if p.poll() is None:
                p.kill()
            try:
                p.stdout.close()
            except OSError:
                pass
            p.wait()
            TC_SEM.release()

    def _download(self, path: str, u: dict):
        try:
            fid = int(path.split("?")[0].rstrip("/").split("/")[-1].split(".")[0])
        except (ValueError, IndexError):
            return self._err(400, "bad id")
        row = self._file_guard(fid, "download")
        src = long_path(self._abs_path(row))
        if not os.path.isfile(src):
            return self._err(404, "文件不存在")
        size = os.path.getsize(src)
        fname = urllib.parse.quote(row["name"])
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{fname}")
        for k, v in sec.BASE_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            with open(src, "rb") as f:
                while True:
                    chunk = f.read(1 << 18)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---------- 阅读（TXT / PDF 直出） ----------

    def _read_doc(self, u: dict):
        fid = sec.clamp_int(self._q1("id", "0"), 0, 10 ** 12, 0)
        row = self._file_guard(fid, "play")
        src = long_path(self._abs_path(row))
        if not os.path.isfile(src):
            return self._err(404, "文件不存在（可能未插入外接盘）")
        ext = (row["ext"] or "").lower()
        if ext in ("txt", "md", "markdown"):
            try:
                with open(src, "rb") as f:
                    raw = f.read(24 << 20)
            except OSError as ex:
                return self._err(500, str(ex))
            text = None
            for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "big5"):
                try:
                    text = raw.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if text is None:
                text = raw.decode("utf-8", "replace")
            return self._json({"kind": "text", "name": row["name"], "text": text,
                               "truncated": len(raw) >= (24 << 20)})
        if ext == "pdf":
            # PDF 交给浏览器内置阅读器（iframe + Range 请求）
            return self._json({"kind": "pdf", "name": row["name"],
                               "url": f"/media/{fid}.pdf"})
        return self._json({"kind": "none", "name": row["name"]})

    def _ebook(self, u: dict):
        """
        EPUB → 章节 JSON（内置阅读器渲染）
        MOBI/AZW3 → Calibre 转文本（装了就转，没装给出提示）
        """
        fid = sec.clamp_int(self._q1("id", "0"), 0, 10 ** 12, 0)
        row = self._file_guard(fid, "play")
        src = long_path(self._abs_path(row))
        ext = (row["ext"] or "").lower()
        if not os.path.isfile(src):
            return self._err(404, "文件不存在")
        cache = dbmod.data_dir() / "booktext"
        cache.mkdir(parents=True, exist_ok=True)
        cf = cache / f"{fid}.json"
        if cf.is_file():
            try:
                data = json.loads(cf.read_text("utf-8"))
                data["cached"] = True
                return self._json(data)
            except Exception:
                pass
        if ext == "epub":
            title, chs = bc.epub_chapters(src)
        elif ext in ("mobi", "azw", "azw3", "prc", "pdb"):
            title, chs = bc.mobi_pages(src)
            if not chs:
                return self._err(424, "解析 %s 需要安装 Calibre（ebook-convert）。"
                                      "装上后重试即可在线阅读。" % ext.upper())
        else:
            return self._json({"kind": "none", "name": row["name"]})
        if not chs:
            return self._err(422, "这本书没有可提取的正文（可能是图片版）")
        # 单章过长时按 6000 字切分，避免一次渲染几十万字
        out = []
        for i, (h, body) in enumerate(chs):
            body = body.strip()
            if len(body) <= 8000:
                out.append({"i": len(out), "title": h, "text": body})
            else:
                for j in range(0, len(body), 8000):
                    out.append({"i": len(out),
                                "title": h + ("" if j == 0 else "（续）"),
                                "text": body[j:j + 8000]})
            if len(out) > 1200:
                break
        data = {"kind": "chapters", "name": row["name"], "title": title or row["name"],
                "chapters": out}
        try:
            if sum(len(c["text"]) for c in out) < 8_000_000:
                cf.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        except Exception:
            pass
        return self._json(data)

    # ---------- 登录 / 注册 / 会话 ----------

    def _api_login(self):
        body = self._body()
        username = str(body.get("username") or "").strip()[:40]
        password = str(body.get("password") or "")[:128]
        ip = sec.client_ip(self)
        ua = self.headers.get("User-Agent") or ""
        wait = LOGIN_GUARD.blocked_for(ip, username)
        if wait > 0:
            users_mod.log_login(self.store.rw, username, ip, False, ua, "locked")
            # retry_after 随响应透传，登录页据此显示倒计时
            return self._err(429, f"尝试次数过多，请 {wait} 秒后再试", {"retry_after": wait})
        u = users_mod.verify(self.store.rw, username, password)
        if not u:
            LOGIN_GUARD.fail(ip, username)
            users_mod.log_login(self.store.rw, username, ip, False, ua, "bad")
            time.sleep(0.5)
            wait2 = LOGIN_GUARD.blocked_for(ip, username)
            return self._err(401, "用户名或密码错误，或账号已停用",
                             {"retry_after": wait2} if wait2 > 0 else None)
        LOGIN_GUARD.success(ip, username)
        self._reset_rate_touch()
        tok = users_mod.create_session(self.store.rw, u["id"], ip, ua)
        users_mod.log_login(self.store.rw, username, ip, True, ua, "ok")
        return self._json(self._public_me(u), extra=self._set_cookie(tok))

    def _api_setup(self):
        if users_mod.count_users(self.store.rw) > 0:
            # 先读掉 body 再回 409，否则残留字节会污染同连接的下一请求
            self._discard_body()
            return self._err(409, "管理员已存在，请直接登录")
        body = self._body()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        try:
            u = users_mod.create_user(self.store.rw, username, password, role="admin")
        except ValueError as ex:
            return self._err(400, str(ex))
        ip = sec.client_ip(self)
        self._reset_rate_touch()
        tok = users_mod.create_session(self.store.rw, u["id"], ip,
                                       self.headers.get("User-Agent") or "")
        users_mod.audit(self.store.rw, u, ip, "setup", "创建初始管理员")
        return self._json(self._public_me(u), extra=self._set_cookie(tok))

    def _api_logout(self):
        tok = self._cookie_token()
        if tok:
            with MEDIA_LOCK:
                for s in MEDIA_SESSIONS.values():
                    s.discard(tok)
            users_mod.drop_session(self.store.rw, tok)
        return self._json({"ok": True},
                          extra={"Set-Cookie": "flsid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})

    # ---------- 用户管理（admin） ----------

    def _admin_users(self, post: bool = False):
        self._admin()
        if not post:
            c = self.store.rw
            return self._json({"users": users_mod.list_users(c),
                               "nav_items": [{"k": k, "label": lbl} for k, lbl in users_mod.NAV_ITEMS],
                               "logins": users_mod.recent_logins(c, 200)})
        body = self._body()
        act = str(body.get("action") or "")
        c = self.store.rw
        me = self._admin()
        ip = sec.client_ip(self)
        if act == "add":
            try:
                u = users_mod.create_user(
                    c, str(body.get("username") or ""), str(body.get("password") or ""),
                    role=body.get("role") if body.get("role") in ("admin", "user") else "user",
                    note=str(body.get("note") or ""))
            except ValueError as ex:
                return self._err(400, str(ex))
            if body.get("nav"):
                users_mod.set_nav_map(c, u["id"], body["nav"])
            users_mod.audit(c, me, ip, "user.add", u["username"])
            return self._json({"ok": True, "user": users_mod.get_by_id(c, u["id"])})
        uid = sec.clamp_int(body.get("id"), 0, 10 ** 9, 0)
        if not uid:
            return self._err(400, "未指定用户")
        target = users_mod.get_by_id(c, uid)
        if not target:
            return self._err(404, "用户不存在")
        try:
            if act == "perms":
                users_mod.update(c, uid, perms=body.get("perms") or [])
            elif act == "nav":
                users_mod.set_nav_map(c, uid, body.get("nav") or {})
            elif act == "pass":
                pw = str(body.get("password") or "")
                users_mod.update(c, uid, password=pw)
                if uid != me["id"]:
                    # 改密后立即使该用户已有会话全部失效；操作者自己除外（否则把自己踢下线）
                    c.execute("DELETE FROM sessions WHERE uid=?", (uid,))
                users_mod.audit(c, me, ip, "user.pass", target["username"])
            elif act == "role":
                newrole = body.get("role")
                if newrole not in ("admin", "user"):
                    return self._err(400, "角色无效")
                if target["role"] == "admin" and newrole == "user" \
                        and users_mod.count_admins(c) <= 1:
                    return self._err(400, "至少要保留一个管理员")
                users_mod.update(c, uid, role=newrole)
            elif act == "disable":
                dis = bool(body.get("disabled"))
                if dis and target["role"] == "admin" and users_mod.count_admins(c) <= 1:
                    return self._err(400, "不能停用唯一的管理员")
                users_mod.update(c, uid, disabled=dis)
            elif act == "note":
                users_mod.update(c, uid, note=str(body.get("note") or ""))
            elif act == "del":
                if uid == me["id"]:
                    return self._err(400, "不能删除自己")
                if target["role"] == "admin" and users_mod.count_admins(c) <= 1:
                    return self._err(400, "不能删除唯一的管理员")
                users_mod.delete_user(c, uid)
                users_mod.audit(c, me, ip, "user.del", target["username"])
                return self._json({"ok": True})
            else:
                return self._err(400, "未知操作")
        except ValueError as ex:
            return self._err(400, str(ex))
        users_mod.audit(c, me, ip, f"user.{act}", f"{target['username']}")
        return self._json({"ok": True, "user": users_mod.get_by_id(c, uid)})

    # ---------- 设置（admin） ----------

    def _admin_settings(self, post: bool = False):
        conn = self.store.rw
        if not post:
            s = dbmod.all_settings(conn)
            out = dict(cfg.DEFAULTS)
            out.update(s)
            out["ffmpeg"] = find_ffmpeg()
            out["calibre"] = bc.find_calibre()
            out["data_dir"] = str(dbmod.data_dir())
            out["portable"] = cfg.is_portable()
            out["version"] = APP_VERSION
            out["backups"] = list_backups()
            return out
        body = self._body()
        me = self._admin()
        ip = sec.client_ip(self)
        allowed = {"rest_remind", "rest_interval_min", "idle_pause", "theme", "site_name",
                   "scan_temp_enabled", "scan_temp_interval_min", "scan_daily_enabled",
                   "scan_daily_time", "auto_backup", "backup_keep", "rate_limit_user",
                   "rate_limit_guest", "rate_limit_media", "max_media_sessions",
                   "cover_calibre"}
        for k, v in body.items():
            if k in allowed:
                dbmod.put_setting(conn, k, v)
        # 热更：限流额度与媒体并发数立即生效，无需重启
        for key, rl in (("rate_limit_user", RL_USER), ("rate_limit_guest", RL_GUEST),
                        ("rate_limit_media", RL_MEDIA)):
            if key in body:
                try:
                    rl.per_min = max(10, int(body[key]))
                except (TypeError, ValueError):
                    pass
        if "max_media_sessions" in body:
            try:
                global MEDIA_MAX
                MEDIA_MAX = max(1, int(body["max_media_sessions"]))
            except (TypeError, ValueError):
                pass
        users_mod.audit(conn, me, ip, "settings.save",
                        json.dumps({k: v for k, v in body.items() if k in allowed},
                                   ensure_ascii=False)[:300])
        return self._json({"ok": True, "settings": self._admin_settings()})

    def _admin_overview(self) -> dict:
        conn = self.store.conn
        vols = []
        for r in conn.execute(
                """
                SELECT v.*, (SELECT COUNT(*) FROM files WHERE vol_id=v.id) n,
                       (SELECT COALESCE(SUM(size),0) FROM files WHERE vol_id=v.id) size,
                       (SELECT COUNT(*) FROM dirs WHERE vol_id=v.id) dirs,
                       (SELECT COUNT(*) FROM files WHERE vol_id=v.id AND thumb_state=2) thumbs,
                       (SELECT COUNT(*) FROM files WHERE vol_id=v.id AND text_state=2) texts
                FROM volumes v ORDER BY v.id"""):
            d = dict(r)
            d["online"] = bool(d["root_path"] and os.path.isdir(d["root_path"]))
            vols.append(d)
        task = self._pending_counts()
        return {"volumes": vols, "tasks": task,
                "ffmpeg": find_ffmpeg(), "calibre": bc.find_calibre(),
                "data_dir": str(dbmod.data_dir()),
                "db_size": (dbmod.db_path().stat().st_size if dbmod.db_path().is_file() else 0),
                "backups": list_backups(), "job": JOB.snapshot()}

    def _api_library_admin(self, u: dict):
        """资料库管理：改名、删除索引、（重）扫描。"""
        self._admin()
        body = self._body()
        act = body.get("action")
        vid = sec.clamp_int(body.get("id"), 0, 10 ** 9, 0)
        conn = self.store.rw
        ip = sec.client_ip(self)
        if act == "rename":
            name = sec.safe_name(str(body.get("label") or ""), 60)
            conn.execute("UPDATE volumes SET label=? WHERE id=?", (name, vid))
            conn.commit()
            users_mod.audit(conn, u, ip, "library.rename", f"#{vid} → {name}")
        elif act == "drop":
            n = conn.execute("SELECT COUNT(*) n FROM files WHERE vol_id=?", (vid,)).fetchone()["n"]
            for sql in (
                "DELETE FROM tasks WHERE file_id IN (SELECT id FROM files WHERE vol_id=?)",
                "DELETE FROM file_text WHERE file_id IN (SELECT id FROM files WHERE vol_id=?)",
                "DELETE FROM file_tags WHERE file_id IN (SELECT id FROM files WHERE vol_id=?)",
                "DELETE FROM user_marks WHERE file_id IN (SELECT id FROM files WHERE vol_id=?)",
                "DELETE FROM meta WHERE file_id IN (SELECT id FROM files WHERE vol_id=?)",
                "DELETE FROM files WHERE vol_id=?",
                "DELETE FROM dirs WHERE vol_id=?",
                "DELETE FROM user_perms WHERE vol_id=?",
            ):
                conn.execute(sql, (vid,))
            conn.execute("DELETE FROM volumes WHERE id=?", (vid,))
            conn.commit()
            self.store.invalidate_roots()
            users_mod.audit(conn, u, ip, "library.drop", f"#{vid} 共 {n} 条索引")
        elif act == "exclude":
            ex = body.get("exclude") or []
            conn.execute("UPDATE volumes SET scan_cursor=? WHERE id=?",
                         (json.dumps({"exclude": [str(x) for x in ex][:200]}), vid))
            conn.commit()
        else:
            return self._err(400, "未知操作")
        return self._json({"ok": True})

    # ---------- 数据管理（占用统计 / 一键清缓存） ----------

    # Web 端可清理的目标：都是"删了能自动重建"的缓存/备份，绝不碰索引库本体。
    # 索引库的清空/压缩不在 Web 端提供——服务运行中自删库或 VACUUM 有风险
    # （实测运行中 VACUUM 会让 WAL 膨胀到占用翻倍），引导用户停服后用命令行。
    _CLEANUP_TARGETS = {
        "thumbs": ("缩略图缓存 thumbs/", "下次浏览会按需重新生成"),
        "covers": ("书封缓存 covers/", "下次打开书会重新抓取"),
        "booktext": ("电子书正文缓存 booktext/", "下次阅读会重新解析"),
        "backups": ("自动备份 backups/", "删除后要等下次扫描才会重建备份"),
    }

    def _admin_data(self) -> dict:
        """数据占用统计：复用 cleanup.survey() 的口径，前端「数据」页直接渲染。"""
        import cleanup as cln
        s = cln.survey()
        parts = []
        for p in s["parts"]:
            key = Path(p["path"]).name
            target = key if key in self._CLEANUP_TARGETS else None
            parts.append({**p, "human": cln.human(p["size"]), "target": target})
        return {
            "data_dir": s["base_dir"],
            "total_size": s["total_size"],
            "total_human": s["total_human"],
            "parts": parts,
            "libraries": s.get("libraries") or [],
            "db_files": s.get("db_files", 0),
            "db_media_size": s.get("db_media_size", 0),
            "db_media_human": cln.human(s.get("db_media_size", 0)),
            "backups": list_backups(),
            "backup_keep": int(dbmod.get_setting(self.store.conn, "backup_keep",
                                                 cfg.cfg("backup_keep", 3))),
            "flib_env": os.environ.get("FILELIB_DATA") or "",
            "job": JOB.snapshot(),
        }

    def _api_cleanup(self, u: dict):
        """一键清缓存：后台任务逐文件删除（跳过被占用的），完成后写审计。

        target=backups 且带 keep 时走「按保留份数清理」：只删旧备份、
        留最新 keep 份（keep 来自设置页的自动备份份数），不会全删。
        """
        body = self._body()
        target = str(body.get("target") or "")
        if target not in self._CLEANUP_TARGETS:
            return self._err(400, "未知清理目标")
        if target == "backups" and body.get("keep") is not None:
            return self._cleanup_backups_keep(body, u)
        if JOB.busy():
            return self._err(409, "已有任务在运行，请等它结束")
        base = dbmod.data_dir()
        p = base / target
        label = self._CLEANUP_TARGETS[target][0]
        me = self._user()
        ip = sec.client_ip(self)

        def job(j):
            j.message = f"清理 {label}"
            st = _cleanup_tree(p, j)
            note = (f"删除 {st['files']:,} 个文件，释放 {human_size(st['bytes'])}"
                    + (f"，{st['failed']:,} 个被占用跳过" if st["failed"] else ""))
            j.message = note
            users_mod.audit(self.store.rw, me, ip, "cleanup", f"{target}：{note}")

        if not JOB.start("cleanup", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True})

    def _cleanup_backups_keep(self, body: dict, u: dict):
        """备份瘦身：只留最新 keep 份，其余删除（list_backups 按时间倒序）。"""
        try:
            keep = max(0, int(body.get("keep")))
        except (TypeError, ValueError):
            return self._err(400, "keep 必须是数字")
        if JOB.busy():
            return self._err(409, "已有任务在运行，请等它结束")
        victims = [b["path"] for b in list_backups()[keep:]]
        if not victims:
            return self._json({"ok": True, "deleted": 0,
                               "message": f"备份不超过 {keep} 份，无需清理"})
        me = self._user()
        ip = sec.client_ip(self)

        def job(j):
            j.message = f"清理旧备份（保留最新 {keep} 份）"
            freed = failed = 0
            for i, fp in enumerate(victims):
                try:
                    freed += os.path.getsize(fp)
                except OSError:
                    pass
                try:
                    os.unlink(long_path(fp))
                except OSError:
                    failed += 1
                j.progress = {"phase": "清理备份", "done": i + 1, "total": len(victims)}
            note = (f"删除 {len(victims) - failed:,} 份旧备份，释放 {human_size(freed)}"
                    + (f"，{failed} 份被占用跳过" if failed else ""))
            j.message = note
            users_mod.audit(self.store.rw, me, ip, "cleanup",
                            f"backups(keep={keep})：{note}")

        if not JOB.start("cleanup", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True})

    # ---------- 扫描 / 封面 / 全文 ----------

    def _api_scan(self):
        self._admin()
        body = self._body()
        root = str(body.get("root") or "").strip().strip('"')
        if not root or not os.path.isdir(root):
            return self._err(400, f"目录不存在或不可访问：{root}")
        if JOB.busy():
            return self._err(409, "已有任务在运行，请等它结束")
        exclude = set(body.get("exclude") or [])
        me = self._user()
        ip = sec.client_ip(self)

        def job(j):
            j.message = "准备扫描"
            do_scan(root, exclude=exclude, job=j)
            self.store.invalidate_roots()
            users_mod.audit(self.store.rw, me, ip, "scan", root[:200])

        if not JOB.start("scan", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True})

    def _api_organize(self):
        """一键整理：增量扫描 → 补封面 → 全文索引 → 去重分析。

        body: {root?: "D:\\新建库"}  —— root 缺省时刷新所有已登记的在线库。
        复用同一个任务槽 /api/task 进度，前端无需特殊轮询。
        """
        self._admin()
        if JOB.busy():
            return self._err(409, "已有任务在运行，请等它结束")
        body = self._body()
        root = str(body.get("root") or "").strip().strip('"')
        if root and not os.path.isdir(root):
            return self._err(400, f"目录不存在或不可访问：{root}")
        me = self._user()
        ip = sec.client_ip(self)

        def job(j):
            j.message = "准备一键整理"
            r = do_organize_job(j, root=root or None)
            self.store.invalidate_roots()
            users_mod.audit(self.store.rw, me, ip, "organize",
                            (root or "all")[:200])
            j.progress = r if isinstance(r, dict) else {}

        if not JOB.start("organize", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True})

    def _api_thumbs(self):
        self._admin()
        body = self._body()
        if JOB.busy():
            return self._err(409, "已有任务在运行")
        workers = sec.clamp_int(body.get("workers"), 1, 8, 4)
        limit = sec.clamp_int(body.get("limit"), 0, 10 ** 7, 0)
        only = body.get("types")
        me = self._user()
        ip = sec.client_ip(self)

        def job(j):
            from filelib.core.indexer import enqueue_tasks as eq
            conn = connect()
            try:
                eq(conn, types=only or None)
            finally:
                conn.close()
            do_cover_job(limit, workers, j)
            users_mod.audit(self.store.rw, me, ip, "cover.build", str(only or "all"))

        if not JOB.start("thumbs", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True})

    def _api_cover_fix(self):
        """只补书库封面：把 thumb_state=0 的书重新入队（含之前失败的）。"""
        self._admin()
        if JOB.busy():
            return self._err(409, "已有任务在运行")
        body = self._body()
        retry_failed = bool(body.get("retry_failed"))
        conn = self.store.rw
        if retry_failed:
            conn.execute(
                "UPDATE tasks SET state='pending', attempts=0 WHERE type IN "
                "('thumb_book','thumb_pdf') AND state IN ('failed','skipped')")
        conn.execute(
            "UPDATE files SET thumb_state=0 WHERE ext IN "
            "('epub','mobi','azw3','azw','azw4','prc','pdb','fb2','lit','chm') "
            "AND thumb_state=3")
        conn.commit()
        cachemod.bump()
        conn2 = connect()
        try:
            q = enqueue_tasks(conn2, types=["thumb_book", "thumb_pdf"])
        finally:
            conn2.close()

        def job(j):
            do_cover_job(0, 4, j)

        if not JOB.start("thumbs", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True, "queued": q})

    def _api_rethumb(self):
        """缩略图存量重压缩（v1.0.5）：旧缩略图按新参数（384/q78）重编码，省空间。"""
        self._admin()
        if JOB.busy():
            return self._err(409, "已有任务在运行")
        me = self._user()
        ip = sec.client_ip(self)

        def job(j):
            jobs.do_rethumb_job(j)
            users_mod.audit(self.store.rw, me, ip, "thumb.recompress")

        if not JOB.start("rethumb", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True})

    def _api_cover_upload(self):
        """
        手工上传封面（base64）。用于自动提取失败的 AZW3。
        body: {id, data: "data:image/jpeg;base64,..."}
        """
        self._admin()
        body = self._body()
        fid = sec.clamp_int(body.get("id"), 0, 10 ** 12, 0)
        row = self._file_row(fid)
        if not row:
            return self._err(404, "记录不存在")
        data = str(body.get("data") or "")
        if "," in data:
            data = data.split(",", 1)[1]
        import base64
        import binascii
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return self._err(400, "图片数据无效")
        if len(raw) > 6 << 20:
            return self._err(413, "图片太大（上限 6MB）")
        if not bc.save_cover(raw, fid):
            return self._err(400, "不是有效的图片")
        from filelib.core.indexer import thumb_path
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(raw)) as im:
                im = im.convert("RGB")
                im.thumbnail((480, 720), Image.LANCZOS)
                im.save(thumb_path(fid), "JPEG", quality=85, optimize=True)
        except Exception:
            pass
        conn = self.store.rw
        conn.execute("UPDATE files SET thumb_state=2 WHERE id=?", (fid,))
        conn.commit()
        cachemod.bump()
        return self._json({"ok": True})

    def _api_fulltext_build(self):
        self._admin()
        if JOB.busy():
            return self._err(409, "已有任务在运行")
        me = self._user()
        ip = sec.client_ip(self)

        def job(j):
            do_fulltext_job(j)
            users_mod.audit(self.store.rw, me, ip, "fulltext.build", "")

        if not JOB.start("fulltext", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True})

    def _api_autotag(self):
        """自动打标签（v1.0.7，admin）：规则层+元数据+正文关键词三层混合，
        产出 auto: 前缀标签（source='auto'），人工标签不受影响。"""
        self._admin()
        if JOB.busy():
            return self._err(409, "已有任务在运行")
        me = self._user()
        ip = sec.client_ip(self)

        def job(j):
            do_autotag_job(j)
            users_mod.audit(self.store.rw, me, ip, "autotag.build", "")

        if not JOB.start("autotag", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True})

    def _api_autotag_clear(self):
        """清空全部自动标签（v1.0.7，admin）。即时 SQL；清空后可重新一键生成。"""
        self._admin()
        me = self._user()
        n = self.store.clear_auto_tags()
        users_mod.audit(self.store.rw, me, sec.client_ip(self),
                        "autotag.clear", f"清除 {n} 条自动标签")
        return self._json({"ok": True, "removed": n})

    def _api_dups_full(self):
        self._admin()
        if JOB.busy():
            return self._err(409, "已有任务在运行")

        def job(j):
            from filelib.core.dedup import run_dedup
            j.message = "去重分析"

            def prog(stage, i, total, msg=""):
                j.progress = {"stage": stage, "i": i, "total": total}
                j.message = msg or f"{stage} {i}/{total}"

            r = run_dedup(progress=prog, stop_flag=j.stop_flag)
            j.progress = r
            j.message = "去重完成"

        if not JOB.start("dedup", job):
            return self._err(409, "已有任务在运行")
        return self._json({"ok": True})

    def _api_backup(self):
        self._admin()
        conn = self.store.rw
        keep = int(dbmod.get_setting(conn, "backup_keep", 3) or 3)
        p = backup_db(keep=keep)
        return self._json({"ok": bool(p), "path": str(p) if p else None,
                           "backups": list_backups()})

    # ---------- 打开 / 删除（只在本机有效） ----------

    def _api_open(self, select: bool):
        # 仅 admin 可用：os.startfile / explorer 属于"在本机启动外部程序"的
        # 高权限动作，普通（可能是局域网）用户不应能远程触发。
        # 文件本身仍受 can_see 的导航权限约束（_file_row 内校验）。
        self._admin()
        body = self._body()
        fid = sec.clamp_int(body.get("id"), 0, 10 ** 12, 0)
        row = self._file_row(fid)
        if not row:
            return self._err(404, "记录不存在")
        u = self._user()
        if u["role"] != "admin" and not users_mod.can_see(u, row["vol_id"], "home"):
            return self._err(403, "无权访问该资料库")
        p = self._abs_path(row)
        if not os.path.isfile(long_path(p)):
            return self._err(404, "文件不存在（外接盘可能未连接）")
        # 只允许从本机回环地址触发（防止局域网用户远程打开本机程序）
        ip = sec.client_ip(self)
        if not (ip.startswith("127.") or ip in ("::1", "localhost")):
            return self._err(403, "「打开/定位」只允许在服务器本机操作")
        try:
            if select:
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
            else:
                os.startfile(p)  # noqa: S606
        except OSError as ex:
            return self._err(500, str(ex))
        return self._json({"ok": True})

    def _api_delete(self, u: dict):
        self._admin()
        body = self._body()
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return self._err(400, "未指定文件")
        results = []
        me = self._user()
        ip = sec.client_ip(self)
        for raw in ids[:50]:
            fid = sec.clamp_int(raw, 0, 10 ** 12, 0)
            row = self._file_row(fid)
            if not row:
                results.append({"id": fid, "ok": False, "msg": "记录不存在"})
                continue
            p = self._abs_path(row)
            if not os.path.isfile(long_path(p)):
                # 文件已经不在磁盘（用户手动删过/移动过）：把索引条目一并清掉，
                # 否则列表和查重复会一直显示这个"幽灵"条目
                self.store.mark_missing(fid)
                results.append({"id": fid, "ok": True,
                                "msg": "文件已不在磁盘，已从索引移除"})
                continue
            ok, msg = _recycle_with_timeout(p)
            if ok is None:
                # 回收站操作超时（结果未知：文件可能还在）。如实告知，
                # 绝不给用户"已删除"的假象，也不把索引标成 missing。
                results.append({"id": fid, "ok": False,
                                "msg": "系统回收站响应超时，文件未变动，请稍后重试"})
                continue
            if not ok and not os.path.isfile(long_path(p)):
                # 移动硬盘等场景：检查时还在、删的瞬间消失了（code=2 文件未找到）。
                # 文件确实没了，视为成功，把索引条目一并清掉。
                ok, msg = True, "文件已不在磁盘，已从索引移除"
            results.append({"id": fid, "ok": ok, "msg": msg})
            if ok:
                self.store.mark_missing(fid)
                users_mod.audit(self.store.rw, me, ip, "file.recycle", p[:250])
        return self._json({"results": results})

    # ---------- 文件管理（新建文件夹 / 实时目录列举） ----------
    # 刻意不提供重命名/移动/删除文件夹等改动已有文件的能力，
    # 本机文件整理交给资源管理器或一键整理流水线完成。

    @staticmethod
    def _norm_rel(rel) -> str:
        """把客户端传来的相对目录规范化为 'a/b/' 形式；含 '..'/':' 等直接拒绝。"""
        if not isinstance(rel, str):
            raise ApiError(400, "路径无效")
        parts = []
        for p in rel.replace("\\", "/").split("/"):
            p = p.strip()
            if not p:
                continue
            if p in (".", "..") or ":" in p or any(not ch.isprintable() for ch in p):
                raise ApiError(400, "路径无效")
            parts.append(p)
        return "/".join(parts) + "/" if parts else ""

    def _vol_row(self, vol_id: int):
        return self.store.conn.execute(
            "SELECT * FROM volumes WHERE id=?", (vol_id,)).fetchone()

    def _under_root(self, root: str, target: str) -> bool:
        """realpath 归一后校验 target 在 root 内（防穿越/防符号链接逃逸）。"""
        root_r = os.path.normcase(os.path.realpath(long_path(root)))
        tgt_r = os.path.normcase(os.path.realpath(long_path(target)))
        return tgt_r == root_r or tgt_r.startswith(root_r + os.sep)

    def _api_fm_dirs(self):
        """实时列举某卷某目录下的子文件夹（admin）。目录树接口来自索引，
        而新建的空文件夹在重扫前不入库，必须读真实文件系统。"""
        self._admin()
        vol = sec.clamp_int(self._q1("vol", "0"), 0, 10 ** 9, 0)
        vrow = self._vol_row(vol)
        if not vrow:
            return self._err(404, "资料库不存在")
        rel = self._norm_rel(self._q1("rel", "") or "")
        base = os.path.join(vrow["root_path"], rel.replace("/", os.sep)) if rel \
            else vrow["root_path"]
        if not self._under_root(vrow["root_path"], base):
            return self._err(400, "路径无效")
        if not os.path.isdir(long_path(base)):
            return self._err(404, "目录不存在")
        out = []
        try:
            with os.scandir(base) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            out.append({"name": e.name, "rel": rel + e.name + "/"})
                    except OSError:
                        continue
                    if len(out) >= 1000:
                        break
        except OSError as ex:
            return self._err(500, str(ex))
        out.sort(key=lambda x: x["name"].lower())
        return self._json({"vol": vol, "rel": rel, "dirs": out})

    def _api_fm_mkdir(self):
        return self._err(404, "功能已移除")

    def _stream_download(self, row: dict, src: str):
        size = os.path.getsize(src)
        fname = urllib.parse.quote(row["name"])
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{fname}")
        for k, v in sec.BASE_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            with open(src, "rb") as f:
                while True:
                    chunk = f.read(1 << 18)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _download_zip(self, u: dict):
        """打包下载：按列表筛选参数取 ≤200 个文件（总量 ≤2GB）流式输出 zip。"""
        vol = self._vol_for("home")
        if vol == -1:
            return self._err(403, "无权访问")
        q = self._query()

        def one(k, d=None):
            v = q.get(k)
            return v[0] if v else d

        res = self.store.query(
            q=one("q", "") or "", kind=one("kind"), rel_dir=one("dir"),
            recursive=(one("recursive", "1") != "0"), ext=one("ext"),
            topic=one("topic"), series=one("series"), author=one("author"),
            mark=one("mark"), tag=one("tag"),
            tags=[t for t in (one("tags", "") or "").split(",") if t],
            not_tags=[t for t in (one("ntags", "") or "").split(",") if t],
            min_size=None, sort=one("sort", "name_asc"),
            limit=sec.clamp_int(one("limit", "100"), 1, 200, 100),
            offset=sec.clamp_int(one("offset", "0"), 0, 10 ** 8, 0),
            vol_id=vol)
        files = []
        total = 0
        cap = 2 << 30   # 单次打包 2GB 上限：超过的文件按序跳过
        for it in res["items"]:
            row = self._file_row(it["id"])
            if not row:
                continue
            try:
                p = long_path(self._abs_path(row))
                if not os.path.isfile(p):
                    continue
                sz = os.path.getsize(p)
            except OSError:
                continue
            if sz > cap - total:
                continue
            total += sz
            files.append((row, p))
        if not files:
            return self._err(404, "没有可打包的文件（总量上限 2GB）")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''"
                         + urllib.parse.quote("FileLib_打包下载.zip"))
        self.send_header("Connection", "close")
        for k, v in sec.BASE_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.close_connection = True

        import zipfile

        class _SockWriter:
            """非 seek 的流式写入器；zipfile 会自动走 data descriptor 路径。"""

            def __init__(self, w):
                self._w = w
                self.n = 0

            def write(self, b):
                self._w.write(b)
                self.n += len(b)

            def tell(self):
                return self.n

            def flush(self):
                pass

            def seek(self, *_a):   # 显式标记不可 seek
                raise OSError("non-seekable")

        try:
            with zipfile.ZipFile(_SockWriter(self.wfile), "w",
                                 zipfile.ZIP_STORED, allowZip64=True) as zf:
                used = set()
                for row, p in files:
                    arc = row["name"]
                    base, ext2 = os.path.splitext(arc)
                    i = 2
                    while arc.lower() in used:
                        arc = f"{base} ({i}){ext2}"
                        i += 1
                    used.add(arc.lower())
                    zf.write(p, arc)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---------- 标记 / 标签 / 进度 ----------

    def _api_mark(self, u: dict):
        body = self._body()
        fid = sec.clamp_int(body.get("id"), 0, 10 ** 12, 0)
        if not fid:
            return self._err(400, "未指定文件")
        # 收藏/已读/待读是「个人口味」，普通用户也能用；
        # 但前提是他确实看得到这个文件所在的库。
        self._file_guard(fid, "home")
        mark = body.get("mark")
        if mark not in (None, "", "star", "read", "todo", "unread"):
            return self._err(400, "无效标记")
        if mark in (None, "", "unread"):
            self.store.set_mark(fid, None)          # 清空全部标记
            return self._json({"ok": True, "marks": []})
        on = body.get("on")
        if on is None:
            # 旧语义（兼容）：整体设置为该单个标记
            self.store.set_mark(fid, mark)
            return self._json({"ok": True, "marks": [mark]})
        # 多标记语义：勾选/取消单个；star 可与 read/todo 并存，read/todo 互斥
        marks = self.store.toggle_mark(fid, mark, bool(on))
        return self._json({"ok": True, "marks": marks})

    def _api_set_tags(self):
        body = self._body()
        fid = sec.clamp_int(body.get("id"), 0, 10 ** 12, 0)
        if not fid:
            return self._err(400, "未指定文件")
        self._file_guard(fid, "home")
        tags = body.get("tags") or []
        if not isinstance(tags, list) or len(tags) > 50:
            return self._err(400, "标签无效")
        names = [sec.safe_name(str(t), 40) for t in tags]
        names = [n for n in names if n]
        return self._json({"ok": True, "tags": self.store.set_tags(fid, names)})

    def _api_tag_bulk(self):
        self._admin()
        body = self._body()
        ids = [sec.clamp_int(x, 0, 10 ** 12, 0) for x in (body.get("ids") or [])][:500]
        add = [sec.safe_name(str(t), 40) for t in (body.get("add") or [])]
        remove = [sec.safe_name(str(t), 40) for t in (body.get("remove") or [])]
        add = [t for t in add if t]
        remove = [t for t in remove if t]
        n = self.store.tag_bulk(ids, add, remove)
        return self._json({"ok": True, "n": n})

    def _api_tag_admin(self):
        """标签管理：创建/改名/删除/合并/隐藏（admin）。"""
        self._admin()
        me = self._user()
        ip = sec.client_ip(self)
        body = self._body()
        act = body.get("action")
        if act == "create":
            name = sec.safe_name(str(body.get("name") or ""), 40)
            if not name:
                return self._err(400, "标签名不能为空")
            # 重名明确提示：INSERT OR IGNORE 静默成功会让用户以为
            # 「新建成功但搜不到」，实际上标签早已存在
            dup = self.store.conn.execute(
                "SELECT 1 FROM tags WHERE name=?", (name,)).fetchone()
            if dup:
                return self._err(409, f"标签「{name}」已存在")
            users_mod.audit(self.store.rw, me, ip, "tag.create", name)
            return self._json(self.store.tag_create(name, str(body.get("color") or "")))
        if act == "rename":
            old = sec.safe_name(str(body.get("old") or ""), 40)
            new = sec.safe_name(str(body.get("new") or ""), 40)
            if not old or not new:
                return self._err(400, "标签名不能为空")
            row = self.store.conn.execute(
                "SELECT grp FROM tags WHERE name=?", (old,)).fetchone()
            if row and row["grp"] == "auto":
                return self._err(400, "自动标签由系统生成，不能改名（可删除，或合并到人工标签）")
            users_mod.audit(self.store.rw, me, ip, "tag.rename", f"{old} → {new}")
            return self._json({"ok": self.store.tag_rename(old, new)})
        if act == "merge":
            src = sec.safe_name(str(body.get("src") or ""), 40)
            dst = sec.safe_name(str(body.get("dst") or ""), 40)
            if not src or not dst:
                return self._err(400, "合并参数缺失")
            try:
                r = self.store.tag_merge(src, dst)
            except ValueError as e:
                return self._err(400, str(e))
            users_mod.audit(self.store.rw, me, ip, "tag.merge", f"{src} → {dst}")
            return self._json(r)
        if act == "hide":
            name = sec.safe_name(str(body.get("name") or ""), 40)
            if not name:
                return self._err(400, "标签名不能为空")
            on = bool(body.get("on", True))
            self.store.tag_set_hidden([name], on)
            users_mod.audit(self.store.rw, me, ip, "tag.hide" if on else "tag.unhide", name)
            return self._json({"ok": True})
        if act == "delete":
            name = sec.safe_name(str(body.get("name") or ""), 40)
            r = self.store.tag_delete(name)
            if r:
                users_mod.audit(self.store.rw, me, ip, "tag.delete", name)
            return self._json({"ok": r})
        return self._err(400, "未知操作")

    def _api_progress(self, u: dict):
        body = self._body()
        fid = sec.clamp_int(body.get("id"), 0, 10 ** 12, 0)
        if not fid:
            return self._err(400, "未指定文件")
        self._file_guard(fid, "play")
        if "page" in body:
            self.store.save_read_progress(u["id"], fid,
                                          sec.clamp_int(body.get("page"), 0, 10 ** 7, 0),
                                          sec.clamp_int(body.get("total"), 0, 10 ** 7, 0))
        if "pos" in body:
            try:
                pos = float(body.get("pos") or 0)
                dur = float(body.get("dur") or 0)
            except (TypeError, ValueError):
                return self._err(400, "进度参数错误")
            if dur > 0 and pos > dur:
                pos = dur
            self.store.save_media_progress(u["id"], fid, max(0, pos), max(0, dur))
        return self._json({"ok": True})

    def _api_readlog(self, u: dict):
        """阅读心跳：阅读器开着时前端每 30 秒上报一次，用于打卡热力图。"""
        body = self._body()
        try:
            secs = float(body.get("secs") or 0)
        except (TypeError, ValueError):
            return self._err(400, "参数错误")
        if secs <= 0 or secs > 120:
            return self._err(400, "时长不合法")
        fid = sec.clamp_int(body.get("id"), 0, 10 ** 12, 0)
        self.store.log_read(u["id"], fid, secs)
        return self._json({"ok": True})

    def _api_continue(self, u: dict):
        """继续看记录管理：删除所选 / 一键清空（只影响当前用户自己的记录）。"""
        body = self._body()
        act = body.get("action")
        if act == "delete":
            ids = body.get("ids") or []
            if not isinstance(ids, list) or not ids:
                return self._err(400, "未指定记录")
            n = self.store.continue_delete(u["id"], ids)
            return self._json({"ok": True, "removed": n})
        if act == "clear":
            vol = body.get("vol")
            if vol is not None:
                try:
                    vol = int(vol)
                except (TypeError, ValueError):
                    return self._err(400, "vol 参数错误")
                if u["role"] != "admin" and not users_mod.can_see(u, vol, "continue"):
                    return self._err(403, "无权访问该资料库")
            n = self.store.continue_clear(u["id"], vol)
            return self._json({"ok": True, "removed": n})
        return self._err(400, "未知操作")


# ---------- 文件管理的模块级辅助 ----------

_WIN_BAD = set('\\/:*?"<>|')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def _clean_filename(name: str) -> str:
    """清洗为合法的 Windows 文件/目录名；不合法返回空串。
    去控制字符、去首尾空白与结尾的点和空格、拦保留名。"""
    if not isinstance(name, str):
        return ""
    name = "".join(ch for ch in name
                   if ch.isprintable() and ch != "\x7f").strip()
    name = name.rstrip(" .")
    if not name or len(name) > 200 or set(name) & _WIN_BAD:
        return ""
    if name.split(".")[0].upper() in _WIN_RESERVED:
        return ""
    return name


def _recycle_with_timeout(path: str, timeout: float = 15.0):
    """recycle() 的超时保护版。SHFileOperationW 在个别环境（回收站服务忙/
    被安全软件拦截/Explorer 卡死）会无限阻塞，若在请求线程里直接等，
    整个删除接口就会永远不响应。超时后返回 (None, None) 表示结果未知。"""
    import threading
    box: dict = {}

    def run():
        try:
            box["r"] = recycle(path)
        except BaseException as ex:  # noqa: BLE001 —— 转交调用方处理
            box["e"] = ex

    t = threading.Thread(target=run, daemon=True, name="recycle-op")
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, None
    if "e" in box:
        raise box["e"]
    return box["r"]


def recycle(path: str) -> tuple[bool, str]:
    """送入 Windows 回收站。绝不使用永久删除。"""
    if sys.platform != "win32":
        return False, "仅支持 Windows 回收站"
    try:
        import ctypes
        from ctypes import wintypes
        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        # 注意：回收站接口不支持 \\?\ 超长路径前缀（会报 DE_INVALIDFILES/124），
        # 必须传普通绝对路径，且以双 NUL 结尾（多路径分隔符）
        op.pFrom = os.path.abspath(path) + "\0"
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if res == 0:
            return True, "已移入回收站"
        if not os.path.isfile(path):
            return True, "文件已不存在（可能已被删除）"
        return False, f"失败 code={res}"
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}"


def make_server(port: int, host: str = "127.0.0.1",
                tls_cert: str | None = None, tls_key: str | None = None) -> ThreadingHTTPServer:
    init_db()
    conn = dbmod.connect()
    users_mod.migrate(conn)          # 兼容 1.x 老表结构（幂等）
    users_mod.purge_sessions(conn)
    # 首次启动：把默认设置写进库，方便后台查看/修改
    for k, v in cfg.DEFAULTS.items():
        if dbmod.get_setting(conn, k, "__missing__") == "__missing__":
            dbmod.put_setting(conn, k, v)
    # 限流额度以 settings 表为准（后台修改立即生效；filelib.json 只是出厂默认）
    global MEDIA_MAX
    for rl, key, dft in ((RL_USER, "rate_limit_user", 300),
                         (RL_GUEST, "rate_limit_guest", 60),
                         (RL_MEDIA, "rate_limit_media", 600)):
        try:
            rl.per_min = max(10, int(dbmod.get_setting(conn, key, dft)))
        except (TypeError, ValueError):
            pass
    try:
        MEDIA_MAX = max(1, int(dbmod.get_setting(conn, "max_media_sessions", 3)))
    except (TypeError, ValueError):
        pass
    conn.close()

    Handler.store = Store()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True

    # HTTPS：同时给出证书与私钥时启用（PEM 格式，自签证书即可）
    if tls_cert and tls_key:
        global TLS_ON
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        TLS_ON = True

    # 浏览器提前关闭页面/断开长连接是常态（ConnectionReset/Aborted 10054/10053），
    # 打一堆堆栈只会吓人，静默处理即可；TLS 下客户端中途掐断握手同理（SSLEOF）
    _real_handle_error = srv.handle_error
    def handle_error(request, client_address):
        import sys
        ex = sys.exception()
        if isinstance(ex, (ConnectionResetError, ConnectionAbortedError,
                           BrokenPipeError, TimeoutError, ssl.SSLError)):
            return
        _real_handle_error(request, client_address)
    srv.handle_error = handle_error

    Handler.allow_any_host = (host not in ("127.0.0.1", "localhost"))
    sec.register_host_aliases(["localhost"] + sec.lan_ips() + [host])
    return srv


def lan_ip() -> str:
    ips = sec.lan_ips()
    return ips[0] if ips else "127.0.0.1"


def main() -> int:
    ap = argparse.ArgumentParser(description="FileLib 本地资料库")
    ap.add_argument("--port", type=int, default=int(cfg.cfg("port", 8321)))
    ap.add_argument("--host", default=str(cfg.cfg("host", "127.0.0.1")),
                    help="127.0.0.1=仅本机；0.0.0.0=局域网/手机可访问")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--tls-cert", default="",
                    help="PEM 证书文件；与 --tls-key 同时给出时启用 HTTPS")
    ap.add_argument("--tls-key", default="",
                    help="PEM 私钥文件；自签证书生成方法见 安全说明.md")
    args = ap.parse_args()

    tls_cert, tls_key = args.tls_cert.strip(), args.tls_key.strip()
    if bool(tls_cert) != bool(tls_key):
        print("! --tls-cert 与 --tls-key 必须同时提供（PEM 格式）。")
        return 2
    for f in (tls_cert, tls_key):
        if f and not Path(f).is_file():
            print(f"! 找不到 TLS 文件：{f}")
            return 2

    # 防双启动：已有实例监听该端口时直接退出，避免第二个实例
    # 在 init_db 阶段与运行中的实例争抢数据库写锁（database is locked）
    import socket as _socket
    _probe = _socket.socket()
    _probe.settimeout(0.5)
    try:
        if _probe.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"! 端口 {args.port} 已被占用——服务很可能已在运行。")
            print(f"  请直接访问 http://127.0.0.1:{args.port}/ ，勿重复启动。")
            return 2
    finally:
        _probe.close()

    srv = make_server(args.port, args.host, tls_cert or None, tls_key or None)
    scheme = "https" if tls_cert else "http"
    local = f"{scheme}://127.0.0.1:{args.port}/"
    print("=" * 58)
    print(f"  FileLib {APP_VERSION}  已启动")
    print(f"  本机访问   {local}")
    if tls_cert:
        print("  HTTPS      已启用（自签证书浏览器提示「不安全」属正常，")
        print("             点「高级 → 继续前往」即可；手机端首次同理）")
    if args.host == "0.0.0.0":
        print(f"  手机/局域网 {scheme}://{lan_ip()}:{args.port}/")
        print(f"  （防火墙需放行 TCP {args.port}，start-lan.bat 会自动处理）")
    print(f"  数据目录   {dbmod.data_dir()}")
    print(f"  索引库     {dbmod.db_path()}")
    print(f"  ffmpeg     {find_ffmpeg() or '未找到（视频封面不可用）'}")
    print(f"  Calibre    {bc.find_calibre() or '未找到（AZW3/MOBI 阅读不可用）'}")
    print("=" * 58)
    print("按 Ctrl-C 停止")

    SCHEDULER.start()
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(local)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        try:
            srv.server_close()
        finally:
            HANDLER_STORE = Handler.store
            if HANDLER_STORE:
                try:
                    HANDLER_STORE.close()
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
