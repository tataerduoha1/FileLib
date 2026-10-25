"""
tools/perf_bench.py —— FileLib 性能基准（配合大库数据使用）

用法：
  python tools/perf_bench.py --base http://127.0.0.1:8792 --user 账号 --pass 密码

测量内容：
  1. 热点接口延迟（冷/热对比：缓存收益）
  2. 列表深翻页（offset 5 万级）
  3. 名称搜索 / 全文搜索
  4. 目录树 / 聚合类接口
  5. 缩略图服务吞吐
  6. 连续翻页速率 & 8 并发列表吞吐
输出：人读表格 + perf_result.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

os.environ["no_proxy"] = "*"
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)


class Client:
    def __init__(self, base, user, pw):
        self.base = base.rstrip("/")
        self.cookie = ""
        self._login(user, pw)

    def _login(self, user, pw):
        c, d, h = self.req("/api/login", "POST",
                           {"username": user, "password": pw})
        if c != 200:
            print(f"登录失败 {c}: {d}")
            sys.exit(1)
        self.cookie = "flsid=" + h.get("Set-Cookie", "").split("flsid=")[1].split(";")[0]

    def req(self, path, method="GET", body=None):
        h = {"X-Requested-With": "FileLib", "User-Agent": "perf/1.0"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
            h["Origin"] = self.base
        if self.cookie:
            h["Cookie"] = self.cookie
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers or {})


def bench(cli, label, path, runs=5, save_json=False):
    """顺序执行 runs 次，返回 (min, avg, p95) 毫秒；最后一次的结果体可另存。"""
    ts = []
    last = b""
    for i in range(runs):
        t0 = time.perf_counter()
        c, body, _ = cli.req(path)
        dt = (time.perf_counter() - t0) * 1000
        if c != 200:
            print(f"  ! {label} -> HTTP {c}（跳过）")
            return None
        ts.append(dt)
        last = body
    row = (min(ts), statistics.mean(ts), sorted(ts)[int(len(ts) * 0.95) - 1 if len(ts) > 1 else 0])
    if save_json and last:
        try:
            row = row + (json.loads(last.decode()),)
        except Exception:
            pass
    print(f"  {label:<44} min {row[0]:8.1f}  avg {row[1]:8.1f}  p95 {row[2]:8.1f} ms")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8792")
    ap.add_argument("--user", default="")
    ap.add_argument("--pass", dest="pw", default="")
    ap.add_argument("--deep-offset", type=int, default=50000)
    ap.add_argument("--out", default="perf_result.json")
    a = ap.parse_args()

    cli = Client(a.base, a.user, a.pw)
    print(f"== FileLib 性能基准 @ {a.base} ==")

    # 规模
    c, body, _ = cli.req("/api/stats?vol=0")
    st = json.loads(body.decode())
    print(f"索引规模: {st.get('total_files', 0):,} 文件 / "
          f"{st.get('total_size', 0) / 1e9:.1f} GB / {len(st.get('volumes') or [])} 库")
    result = {"scale": {"files": st.get("total_files"), "bytes": st.get("total_size")}}

    print("\n-- 聚合/树类（第 1 次=冷，第 2 次=热，观察缓存收益）--")
    cold_hot = {}
    for ep, label in [("/api/stats?vol=0", "stats 聚合统计"),
                      ("/api/home", "home 首页聚合"),
                      ("/api/tree?vol=0", "tree 目录树"),
                      ("/api/dirtree?vol=1", "dirtree 目录树")]:
        r1 = bench(cli, label + " [冷]", ep, runs=1)
        r2 = bench(cli, label + " [热]", ep, runs=5)
        if r1 and r2:
            cold_hot[label] = {"cold_ms": r1[0], "hot_min_ms": r2[0], "hot_avg_ms": r2[1]}
    result["cold_hot"] = cold_hot

    print("\n-- 列表/翻页 --")
    list_rows = {}
    list_rows["list 首页 offset=0"] = bench(cli, "list 首页 offset=0",
                                           "/api/list?limit=120&offset=0", runs=8, save_json=True)
    list_rows[f"list 深页 offset={a.deep_offset}"] = bench(
        cli, f"list 深页 offset={a.deep_offset}",
        f"/api/list?limit=120&offset={a.deep_offset}", runs=8)
    list_rows["list 大目录子树递归"] = bench(
        cli, "list 大目录子树递归",
        "/api/list?limit=120&offset=0&dir=%E3%80%90%E5%B0%8F%E8%AF%B4%E3%80%91&recursive=1", runs=5)
    result["list"] = {k: (v[:3] if v else None) for k, v in list_rows.items()}

    # 连续翻页 20 页
    t0 = time.perf_counter()
    pages = 20
    for p in range(pages):
        c, body, _ = cli.req(f"/api/list?limit=120&offset={p * 120}")
        if c != 200:
            print(f"  ! 翻页第 {p} 页 HTTP {c}")
            break
    seq_s = time.perf_counter() - t0
    print(f"  连续 {pages} 页（每页 120 条）合计 {seq_s:.2f}s → {pages / seq_s:.1f} 页/s")
    result["seq_paging"] = {"pages": pages, "seconds": seq_s, "pages_per_s": pages / seq_s}

    print("\n-- 搜索 --")
    search_rows = {}
    search_rows["名称搜索 q=三体"] = bench(cli, "名称搜索 q=三体",
                                       "/api/list?limit=120&q=%E4%B8%89%E4%BD%93", runs=5)
    search_rows["名称搜索 q=IMG_01"] = bench(cli, "名称搜索 q=IMG_01",
                                        "/api/list?limit=120&q=IMG_01", runs=5)
    search_rows["语法搜索 ext:epub"] = bench(cli, "语法搜索 ext:epub",
                                        "/api/list?limit=120&q=ext%3Aepub", runs=5)
    ft = bench(cli, "全文搜索 q=灯火", "/api/fulltext?q=%E7%81%AF%E7%81%AB&limit=30", runs=5)
    result["search"] = {k: (v[:3] if v else None) for k, v in search_rows.items()}
    result["fulltext"] = ft[:3] if ft else None

    print("\n-- 聚合视图 --")
    agg = {}
    agg["topics 主题"] = bench(cli, "topics 主题", "/api/topics?vol=0", runs=5)
    agg["series 系列"] = bench(cli, "series 系列", "/api/series?vol=0", runs=5)
    agg["authors 作者"] = bench(cli, "authors 作者", "/api/authors?vol=0", runs=5)
    agg["tags 标签"] = bench(cli, "tags 标签", "/api/tags?limit=500", runs=5)
    agg["dups 重复组"] = bench(cli, "dups 重复组", "/api/dups?vol=0", runs=3)
    result["agg"] = {k: (v[:3] if v else None) for k, v in agg.items()}

    # 缩略图：从首页列表抓一个有缩略图的 id
    thumb_id = None
    if list_rows.get("list 首页 offset=0") and len(list_rows["list 首页 offset=0"]) > 3:
        for it in (list_rows["list 首页 offset=0"][3].get("items") or []):
            if it.get("thumb"):
                thumb_id = it["id"]
                break
    if thumb_id:
        t0 = time.perf_counter()
        n = 20
        ok = 0
        for _ in range(n):
            c, _, _ = cli.req(f"/thumb/{thumb_id}.jpg")
            ok += (c == 200)
        dt = (time.perf_counter() - t0) * 1000 / n
        print(f"  缩略图 /thumb（同一文件 ×{n}）avg {dt:.1f} ms（{ok}/{n} 成功）")
        result["thumb_avg_ms"] = dt

    # 8 并发列表吞吐
    def worker(_):
        c, _, _ = cli.req("/api/list?limit=120&offset=0")
        return c == 200

    with ThreadPoolExecutor(8) as ex:
        t0 = time.perf_counter()
        oks = sum(ex.map(worker, range(40)))
        conc_s = time.perf_counter() - t0
    print(f"  8 并发 × 40 次列表请求合计 {conc_s:.2f}s → {40 / conc_s:.1f} req/s（成功 {oks}/40）")
    result["concurrent"] = {"requests": 40, "seconds": conc_s, "req_per_s": 40 / conc_s, "ok": oks}

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写入 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
