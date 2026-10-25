"""
filelib/core/security.py —— 防护措施集中实现

覆盖：
  * CSRF 双保险（SameSite=Lax Cookie + Origin/Referer 校验 + X-FileLib 自定义头）
  * DNS 重绑定防护（Host 白名单）
  * 登录暴力破解防护（IP+账号 双维度，指数退避锁定）
  * 请求体大小上限（防内存耗尽）
  * 安全响应头（CSP / X-Frame-Options / nosniff / Referrer-Policy / 权限策略）
  * 静态资源路径穿越防护（由 server 调用 resolve 比较）
  * 输入校验助手

设计原则：不引入第三方依赖，全部标准库；默认拒绝（deny by default）。
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time

# ---------- 安全响应头 ----------

CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'self'"
)

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    # 只对本站请求带 Referer，跨站一律不带。
    # 不能用 no-referrer：CSRF 校验依赖 Origin/Referer，
    # 某些浏览器（微信内置、手机端、隐私模式）同源请求也不发 Origin，
    # 再掐掉 Referer 就会让所有写操作 403，表现为「退不出登录、改不了设置」。
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

# 页面级响应加 CSP；API 响应不必（但加了也无害，统一加更省心）
PAGE_HEADERS = dict(BASE_HEADERS, **{
    "Content-Security-Policy": CSP,
    "X-XSS-Protection": "0",          # 现代浏览器已废弃，显式关闭以避免旧漏洞
})


# ---------- Host 白名单（防 DNS 重绑定） ----------

_local_names: set[str] = set()


def register_host_aliases(names) -> None:
    for n in names:
        if n:
            _local_names.add(n.lower())


def is_allowed_host(host_header: str, allow_any: bool = False) -> bool:
    """
    host_header 形如 '192.168.1.8:8321'。
    允许：localhost / 127.0.0.1 / ::1 / 本机各网卡 IP / 已注册别名。
    allow_any=True（--host 指定了具体 IP 或用户显式放开）时只要求 host 部分合法。
    """
    if not host_header:
        return False
    host = host_header
    if host.startswith("["):                      # [::1]:8321
        end = host.find("]")
        host = host[1:end] if end > 0 else host
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    host = host.strip().lower().rstrip(".")
    if not host or len(host) > 253:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host in _local_names:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_loopback:
            return True
        # 仅放行私有网段（局域网）。公网 IP 直连需要用户显式 --trust-any-host
        return ip.is_private or ip.is_link_local
    return allow_any


def origin_ok(origin: str | None, referer: str | None, host_header: str) -> bool:
    """
    CSRF 校验：浏览器跨站发起的写请求会带上自己的 Origin。
    要求 Origin（或 Referer）的 host 必须与本机 host 一致。
    两者都没有时（curl / 原生客户端）视为失败——写操作必须带自定义头。
    """
    val = (origin or "").strip()
    if not val and referer:
        val = referer
    if not val or val == "null":
        return False
    try:
        from urllib.parse import urlparse
        p = urlparse(val if "//" in val else "//" + val)
        h = (p.hostname or "").lower()
    except Exception:
        return False
    if not h:
        return False
    target = host_header
    if target.startswith("["):
        end = target.find("]")
        target = target[1:end] if end > 0 else target
    elif target.count(":") == 1:
        target = target.rsplit(":", 1)[0]
    target = target.lower()
    if h == target:
        return True
    loop = {"localhost", "127.0.0.1", "::1"}
    return h in loop and target in loop


# ---------- 登录失败锁定 ----------

class LoginGuard:
    """
    失败计数 + 指数退避。
    key = f"{ip}|{username}" 与 "ip|*" 两条线同时计；超限后暂时拒绝（即使密码正确也要等）。
    """

    def __init__(self, max_fail: int = 5, window: int = 600, lock_base: int = 60):
        self.max_fail = max_fail
        self.window = window
        self.lock_base = lock_base
        self._hit: dict[str, list[float]] = {}
        self._lock_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _keys(self, ip: str, username: str) -> list[str]:
        return [f"{ip}|*", f"{ip}|{(username or '').lower()}"]

    def blocked_for(self, ip: str, username: str) -> int:
        now = time.time()
        worst = 0
        with self._lock:
            for k in self._keys(ip, username):
                until = self._lock_until.get(k, 0)
                if until > now:
                    worst = max(worst, int(until - now) + 1)
        return worst

    def fail(self, ip: str, username: str) -> int:
        now = time.time()
        with self._lock:
            for k in self._keys(ip, username):
                arr = [t for t in self._hit.get(k, []) if now - t < self.window]
                arr.append(now)
                self._hit[k] = arr
                if len(arr) >= self.max_fail:
                    extra = len(arr) - self.max_fail
                    cool = min(self.lock_base * (2 ** extra), 3600)
                    self._lock_until[k] = now + cool
            return len(self._hit.get(self._keys(ip, username)[1], []))

    def success(self, ip: str, username: str) -> None:
        with self._lock:
            for k in self._keys(ip, username):
                self._hit.pop(k, None)
                self._lock_until.pop(k, None)

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            return {k: int(v - now) for k, v in self._lock_until.items() if v > now}

    def clear(self) -> None:
        """清空全部失败计数与锁定（管理员自检/压测前重置，不对外暴露）。"""
        with self._lock:
            self._hit.clear()
            self._lock_until.clear()


# ---------- 请求限流（令牌桶/滑动窗口） ----------

class RateLimiter:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self._win: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, cost: int = 1) -> tuple[bool, int]:
        """返回 (是否允许, 剩余次数)。"""
        now = time.time()
        with self._lock:
            win, cnt = self._win.get(key, (now, 0))
            if now - win >= 60:
                win, cnt = now, 0
            cnt += cost
            self._win[key] = (win, cnt)
            if len(self._win) > 5000:      # 防内存膨胀
                for k, (w, _) in list(self._win.items()):
                    if now - w >= 120:
                        self._win.pop(k, None)
            return cnt <= self.per_min, max(0, self.per_min - cnt)

    def reset(self, key: str) -> None:
        """清掉某个 key 的计数（登录成功后为游客额度放行）。"""
        with self._lock:
            self._win.pop(key, None)

    def clear(self) -> None:
        """清空全部计数（供管理员做连通性自检/压测前后重置）。"""
        with self._lock:
            self._win.clear()


# ---------- 输入校验 ----------

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fa5]{1,32}$")


def valid_username(s: str) -> bool:
    return bool(s) and bool(USERNAME_RE.match(s))


def clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def safe_name(s: str, maxlen: int = 80) -> str:
    """标签名等自由文本：去掉控制字符，限长。"""
    if not isinstance(s, str):
        return ""
    s = "".join(ch for ch in s if ch.isprintable())
    return s.strip()[:maxlen]


# ---------- 网络工具 ----------

def is_loopback(ip: str) -> bool:
    """判断是否本机回环地址（127.0.0.0/8 或 ::1）。"""
    if not ip:
        return False
    ip = ip.strip().strip("[]")
    if ip in ("::1", "localhost"):
        return True
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def lan_ips() -> list[str]:
    """本机所有局域网 IPv4（尽力而为，失败返回空）。"""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def client_ip(handler) -> str:
    """
    取真实客户端 IP。
    注意：不盲目信任 X-Forwarded-For（可伪造）。只有在显式配置了
    FILELIB_TRUST_PROXY=1（前面有反向代理）时才采用其第一个值。
    """
    if os.environ.get("FILELIB_TRUST_PROXY") == "1":
        xff = handler.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()[:64]
    try:
        return handler.client_address[0]
    except Exception:
        return ""
