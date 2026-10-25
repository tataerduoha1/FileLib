"""tests/test_logout_keepalive.py —— 登出 / 长连接 / CSRF 专项回归

针对两个真实故障写的，改动这两个地方时必须重跑：
  1. 退出登录报 "501 Unsupported method ('{}GET')"
     —— POST 的请求体没读完就回错误，残留字节污染同一条 keep-alive 连接
  2. 部分浏览器所有写操作 403（退不出登录、改不了设置）
     —— Referrer-Policy 掐掉 Referer + 浏览器不发 Origin

用法：
    python tests\test_logout_keepalive.py                 # 默认 8321
    python tests\test_logout_keepalive.py 8899            # 指定端口
"""
import socket, json, sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8321


def reset_rate():
    """清空本机 IP 的限流计数。

    游客额度只有 60 次/分，而本脚本刻意大量连发（正是要验证连接行为），
    不清理的话跑到后半段全是 429，看起来像修复失败。
    `/api/admin/rate_reset` 只允许回环地址调用。
    """
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        s.sendall((f"GET /api/admin/rate_reset HTTP/1.1\r\n"
                   f"Host: 127.0.0.1:{PORT}\r\nX-Requested-With: FileLib\r\n\r\n").encode())
        s.settimeout(2)
        s.recv(65536)
        s.close()
    except Exception:
        pass


def req(method, path, body=None, hdrs=None, sock=None):
    own = sock is None
    s = sock or socket.create_connection(("127.0.0.1", PORT), timeout=5)
    h = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
    for k, v in (hdrs or {}).items():
        h += f"{k}: {v}\r\n"
    data = b""
    if body is not None:
        data = json.dumps(body).encode()
        h += f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
    h += "\r\n"
    s.sendall(h.encode() + data)
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(65536)
        if not c: break
        buf += c
    head, _, rest = buf.partition(b"\r\n\r\n")
    cl = 0
    for ln in head.split(b"\r\n"):
        if ln.lower().startswith(b"content-length:"):
            cl = int(ln.split(b":")[1])
    while len(rest) < cl:
        c = s.recv(65536)
        if not c: break
        rest += c
    if own: s.close()
    code = int(head.split(b"\r\n")[0].split()[1])
    try: js = json.loads(rest[:cl].decode() or "{}")
    except Exception: js = {}
    return code, js, head.decode("latin1")

P, F = 0, 0
def ck(name, cond, extra=""):
    global P, F
    if cond: P += 1; print(f"  \033[32m✓\033[0m {name}")
    else:    F += 1; print(f"  \033[31m✗\033[0m {name}  {extra}")

OK = {"X-Requested-With": "FileLib"}
ORI = {"X-Requested-With": "FileLib", "Origin": f"http://127.0.0.1:{PORT}"}

reset_rate()
print("\n\033[36m▌1. 静态资源与 ETag\033[0m")
c, j, h = req("GET", "/")
ck("首页 200", c == 200, str(c))
ck("Referrer-Policy 是 same-origin（不是 no-referrer）",
   "referrer-policy: same-origin" in h.lower(), h.lower().split("referrer-policy:")[-1][:40] if "referrer-policy" in h.lower() else "缺失")
c, j, h = req("GET", "/static/app.js")
ck("app.js 200", c == 200, str(c))
ck("app.js 带 ETag", "etag:" in h.lower())
ck("app.js 用 no-cache 而非 max-age 强缓存", "no-cache" in h.lower())

etag = ""
for ln in h.split("\r\n"):
    if ln.lower().startswith("etag:"):
        etag = ln.split(":", 1)[1].strip()
c2, j2, h2 = req("GET", "/static/app.js", hdrs={"If-None-Match": etag})
ck("携带 ETag 返回 304", c2 == 304, str(c2))

reset_rate()
print("\n\033[36m▌2. 残留请求体污染（本次修复的核心）\033[0m")
cases = [
    ({}, 2, "无CSRF + 2字节体"),
    ({"X-Requested-With": "FileLib"}, 2, "CSRF缺Origin + 2字节体"),
    ({"X-Requested-With": "FileLib"}, 1, "CSRF缺Origin + 1字节体"),
    ({"X-Requested-With": "FileLib", "Origin": "http://evil.com"}, 2, "跨站Origin + 2字节体"),
]
for hd, blen, label in cases:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    b = b"x" * blen
    hdrs = "".join(f"{k}: {v}\r\n" for k, v in hd.items())
    s.sendall((f"POST /api/logout HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n{hdrs}"
               f"Content-Type: application/json\r\nContent-Length: {blen}\r\n\r\n").encode() + b)
    import time; time.sleep(0.35)
    r0 = s.recv(65536).split(b"\r\n")[0].decode()
    try:
        s.sendall(f"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n\r\n".encode())
        time.sleep(0.35); s.settimeout(2)
        r1 = s.recv(65536).split(b"\r\n")[0].decode()
    except Exception:
        r1 = "(连接已关闭，客户端重连即可)"
    s.close()
    ok = "501" not in r1
    ck(f"{label} → 下一请求不是 501", ok, f"req0={r0} req1={r1}")

reset_rate()
print("\n\033[36m▌3. 登出全流程\033[0m")
s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
c, j, h = req("POST", "/api/logout", body={}, hdrs=ORI, sock=s)
ck("登出 200", c == 200, str(c))
ck("登出返回 ok", j.get("ok") is True, str(j))
ck("清 Cookie (Max-Age=0)", "flsid=; Path=/; Max-Age=0" in h)
c, j, h = req("GET", "/", sock=s)
ck("同连接紧接着 GET / → 200（原先这里是 501）", c == 200, str(c))
for i in range(3):
    c, j, h = req("GET", "/api/ping", sock=s)
    ck(f"同连接 ping{i+1} → 200", c == 200, str(c))
s.close()

reset_rate()
print("\n\033[36m▌4. CSRF 各分支\033[0m")
c, j, h = req("POST", "/api/logout", body={}, hdrs={})
ck("无 X-Requested-With → 403", c == 403, str(c))
c, j, h = req("POST", "/api/logout", body={}, hdrs={"X-Requested-With": "FileLib"})
ck("有自定义头但无 Origin/Referer/Sec-Fetch → 403", c == 403, str(c))
c, j, h = req("POST", "/api/logout", body={},
              hdrs={"X-Requested-With": "FileLib", "Sec-Fetch-Site": "same-origin"})
ck("无 Origin 但 Sec-Fetch-Site: same-origin → 放行", c == 200, str(c))
c, j, h = req("POST", "/api/logout", body={},
              hdrs={"X-Requested-With": "FileLib", "Sec-Fetch-Site": "cross-site"})
ck("Sec-Fetch-Site: cross-site → 403", c == 403, str(c))
c, j, h = req("POST", "/api/logout", body={},
              hdrs={"X-Requested-With": "FileLib", "Origin": "http://evil.com",
                    "Sec-Fetch-Site": "same-origin"})
ck("有跨站 Origin（即使 Sec-Fetch 同源）→ 403", c == 403, str(c))
c, j, h = req("POST", "/api/logout", body={}, hdrs=ORI)
ck("Origin 同源 → 200", c == 200, str(c))

reset_rate()
print("\n\033[36m▌5. 登录/初始化仍正常\033[0m")
# 空/错误凭据统一返回 401（防用户名枚举，不区分"用户不存在"与"密码错误"）
c, j, h = req("POST", "/api/login", body={"username": "", "password": ""}, hdrs=ORI)
ck("空凭据 → 401（统一话术，防枚举）", c == 401, str(c))
ck("错误话术不泄露账号是否存在", "不存在" not in j.get("error", ""), j.get("error", ""))
c, j, h = req("GET", "/api/me")
ck("/api/me 200", c == 200, str(c))
ck("/api/me 含 logged 字段", "logged" in j, str(j)[:60])
c, j, h = req("POST", "/api/setup", body={"username": "x", "password": "123"}, hdrs=ORI)
ck("已有账号时 setup → 409 或 400", c in (400, 409), f"{c} {j}")

reset_rate()
print("\n\033[36m▌6. 安全头完整\033[0m")
c, j, h = req("GET", "/")
hl = h.lower()
for name in ("content-security-policy", "x-content-type-options", "x-frame-options",
             "referrer-policy", "cross-origin-opener-policy"):
    ck(f"响应头含 {name}", name in hl)

print(f"\n{'='*52}\n  通过 \033[32m{P}\033[0m · 失败 \033[31m{F}\033[0m · 共 {P+F}\n{'='*52}")
sys.exit(1 if F else 0)
