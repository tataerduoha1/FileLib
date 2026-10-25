"""
tools/job_probe.py —— 后台任务吞吐采样（封面/全文/一键整理）

用法：
  python tools/job_probe.py --base http://127.0.0.1:8792 --action thumbs --seconds 180
  python tools/job_probe.py --base http://127.0.0.1:8792 --action fulltext --seconds 240

流程：触发任务 → 每 10s 采样进度 → 到时停止任务 → 输出吞吐与全量预估。
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


def req(base, path, method="GET", body=None, cookie=""):
    h = {"X-Requested-With": "FileLib", "User-Agent": "probe/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
        h["Origin"] = base
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
    ap.add_argument("--base", required=True)
    ap.add_argument("--action", required=True,
                    choices=["thumbs", "fulltext", "organize"])
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--user", default="")
    ap.add_argument("--pass", dest="pw", default="")
    a = ap.parse_args()
    base = a.base.rstrip("/")

    c, d, h = req(base, "/api/login", "POST",
                  {"username": a.user, "password": a.pw})
    if c != 200:
        print("登录失败", c, d)
        return 1
    cookie = "flsid=" + h.get("Set-Cookie", "").split("flsid=")[1].split(";")[0]

    # 触发任务（若已有任务在跑则直接采样）
    ep = {"thumbs": "/api/thumbs", "fulltext": "/api/fulltext_build",
          "organize": "/api/organize"}[a.action]
    c, d, _ = req(base, ep, "POST", {}, cookie)
    print(f"触发 {a.action}: HTTP {c} {json.dumps(d, ensure_ascii=False)[:120]}")

    t0 = time.time()
    samples = []
    last = ""
    stopped = False
    while time.time() - t0 < a.seconds:
        c, d, _ = req(base, "/api/task", cookie=cookie)
        state = d.get("state")
        prog = d.get("progress") or {}
        line = json.dumps(prog, ensure_ascii=False)
        if line != last:
            samples.append((time.time() - t0, d.get("name"), state, prog))
            last = line
        if state in ("done", "error", "aborted", "idle"):
            print(f"任务提前结束: state={state} err={d.get('error')}")
            break
        if time.time() - t0 >= a.seconds and not stopped:
            req(base, "/api/stop", "POST", {}, cookie)
            stopped = True
            print("…到达采样时长，已请求停止，等待退出")
            time.sleep(2)
        time.sleep(2)

    # 汇总吞吐：取最后一个含 n_done/n 的进度
    print("\n采样序列（前 8 + 后 4）:")
    head = samples[:8]
    tail = samples[-4:] if len(samples) > 12 else []
    for t, name, st, prog in head + ([(None, "", "", {})] if tail else []) + tail:
        print(f"  t={t and f'{t:6.0f}s'} [{name}] {st} {json.dumps(prog, ensure_ascii=False)[:150]}")

    if len(samples) >= 2:
        p1, p2 = samples[0][3], samples[-1][3]
        n1 = p1.get("n_done") or p1.get("done") or 0
        n2 = p2.get("n_done") or p2.get("done") or 0
        t1, t2 = samples[0][0], samples[-1][0]
        if t2 > t1 and n2 > n1:
            rate = (n2 - n1) / (t2 - t1)
            total = p2.get("n") or p2.get("total") or 0
            remain = (total - n2) / rate if total and rate else 0
            print(f"\n吞吐: {rate:.1f} 文件/s   已处理 {n2:,}/{total:,}")
            if remain:
                print(f"全量预估剩余: {remain/60:.0f} 分钟（{remain/3600:.1f} 小时）")
    c, d, _ = req(base, "/api/task", cookie=cookie)
    print(f"最终任务状态: {d.get('state')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
