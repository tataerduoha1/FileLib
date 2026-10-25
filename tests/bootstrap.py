"""
tests/bootstrap.py —— 测试环境引导（配合 tests/test_all.py 使用）

前置：服务已用隔离数据目录启动，例如
    FILELIB_DATA=C:\\fltest-data python server.py --port 8791

本脚本做三件事：
  1. 首次访问时创建管理员（账号密码与 test_all.py 默认一致）
  2. 调 /api/scan 扫描指定根目录
  3. 轮询 /api/task 直到任务结束，打印扫描统计

用法：
  python tests/bootstrap.py --base http://127.0.0.1:8791 --root C:\\fltest
可选：
  --user 账号 --pass 密码 --timeout 600
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

os.environ["no_proxy"] = "*"
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)


def req(base, path, method="GET", body=None, headers=None, cookie=None):
    h = {"X-Requested-With": "FileLib", "User-Agent": "bootstrap/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
        h["Origin"] = base
    if headers:
        h.update(headers)
    if cookie:
        h["Cookie"] = cookie
    r = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}"), dict(e.headers)
        except Exception:
            return e.code, {}, {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8791")
    ap.add_argument("--root", required=True)
    ap.add_argument("--user", default="")
    ap.add_argument("--pass", dest="pw", default="")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    base = a.base.rstrip("/")

    # 1. 等服务可用
    t0 = time.time()
    while True:
        try:
            c, d, _ = req(base, "/api/ping")
            if c == 200:
                break
        except Exception:
            pass
        if time.time() - t0 > 30:
            print("服务 30 秒内未就绪")
            return 1
        time.sleep(0.5)
    print(f"服务就绪：{base}  v{d.get('version')}")

    # 2. 首次创建管理员；已有账号则直接登录
    c, d, hd = req(base, "/api/setup",
                   "POST", {"username": a.user, "password": a.pw})
    if c == 200:
        print("已创建管理员", a.user)
    else:
        c, d, hd = req(base, "/api/login", "POST",
                       {"username": a.user, "password": a.pw})
        if c != 200:
            print(f"登录失败 {c}: {d}")
            return 1
        print("已登录现有账号", a.user)
    cookie = "flsid=" + hd.get("Set-Cookie", "").split("flsid=")[1].split(";")[0]

    # 3. 扫描
    c, d, _ = req(base, "/api/scan", "POST", {"root": a.root}, cookie=cookie)
    if c != 200:
        print(f"扫描发起失败 {c}: {d}（可能任务正在跑，继续轮询）")
    else:
        print("扫描已发起：", a.root)

    # 4. 轮询任务状态
    t0 = time.time()
    last = ""
    while time.time() - t0 < a.timeout:
        c, d, _ = req(base, "/api/task", cookie=cookie)
        state = d.get("state")
        msg = d.get("message") or ""
        prog = d.get("progress") or {}
        line = f"[{d.get('name')}] {state} {msg} {json.dumps(prog, ensure_ascii=False)[:110]}"
        if line != last:
            print(" ", line[:150])
            last = line
        if state in ("done", "error", "aborted", "idle"):
            break
        time.sleep(1.0)
    c, st, _ = req(base, "/api/stats?vol=0", cookie=cookie)
    vols = st.get("volumes") or []
    print(f"最终任务状态: {d.get('state')} / {d.get('error') or ''}")
    print(f"索引规模: {st.get('total_files'):,} 文件 · {len(vols)} 个库")
    return 0 if d.get("state") in ("done", "idle") else 1


if __name__ == "__main__":
    sys.exit(main())
