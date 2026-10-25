"""
filelib/core/dedup.py —— 三层去重漏斗

为什么不是直接全量哈希：
  1.7TB 全算一遍要完整读盘，机械盘可能数小时。
  漏斗能把读取量压到几十 GB。

  第一层  size 分组        —— 零 IO，纯 SQL，通常能排除 90%+
  第二层  quick hash       —— 头 1MB + 尾 1MB + size，只需读 2MB/文件
  第三层  full hash        —— 只对第二层仍冲突的算全文件

哈希算法：优先 BLAKE3（极快），没有则退回 SHA256（标准库）。
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Callable

from .db import connect
from .scanner import long_path

CHUNK = 1 << 20  # 1MB


def _hasher():
    try:
        import blake3  # type: ignore
        return "blake3", blake3.blake3()
    except ImportError:
        return "sha256", hashlib.sha256()


def quick_hash(path: str, size: int) -> str | None:
    """头 1MB + 尾 1MB + 文件大小。足够区分绝大多数不同文件。"""
    alg, h = _hasher()
    try:
        with open(long_path(path), "rb") as f:
            head = f.read(CHUNK)
            h.update(head)
            if size > 2 * CHUNK:
                f.seek(-CHUNK, os.SEEK_END)
                h.update(f.read(CHUNK))
            elif size > CHUNK:
                h.update(f.read())
        h.update(str(size).encode())
        return f"{alg[:4]}:{h.hexdigest()}"
    except (OSError, PermissionError):
        return None


def full_hash(path: str) -> str | None:
    alg, h = _hasher()
    try:
        with open(long_path(path), "rb") as f:
            while True:
                b = f.read(CHUNK * 8)
                if not b:
                    break
                h.update(b)
        return f"{alg[:4]}:{h.hexdigest()}"
    except (OSError, PermissionError):
        return None


def run_dedup(progress: Callable[[str, int, int, str], None] | None = None,
              stop_flag=None, min_size: int = 64 * 1024) -> dict:
    conn = connect()
    t0 = time.time()

    # ---------- 第一层：按 size 分组 ----------
    if progress:
        progress("分组", 0, 1, "按文件大小分组")
    groups = conn.execute(
        """
        SELECT size, COUNT(*) n FROM files
        WHERE status='present' AND size >= ?
        GROUP BY size HAVING n > 1
        """, (min_size,)).fetchall()
    candidates = []
    for g in groups:
        rows = conn.execute(
            "SELECT f.id, f.rel_dir, f.name, f.size, v.root_path "
            "FROM files f JOIN volumes v ON v.id=f.vol_id "
            "WHERE f.status='present' AND f.size=?",
            (g["size"],)).fetchall()
        candidates.append(rows)

    layer1_files = sum(len(g) for g in candidates)
    total_files = conn.execute(
        "SELECT COUNT(*) n FROM files WHERE status='present'").fetchone()["n"]

    # ---------- 第二层：quick hash ----------
    need_quick = sum(
        sum(1 for r in g if _needs(conn, r["id"], "hash_quick")) for g in candidates)
    done = 0
    still_groups = []
    for g in candidates:
        buckets: dict[str, list] = {}
        for r in g:
            if stop_flag is not None and stop_flag.is_set():
                conn.commit()
                conn.close()
                return {"aborted": True, "layer1": layer1_files}
            cached = conn.execute(
                "SELECT hash_quick FROM files WHERE id=?", (r["id"],)).fetchone()
            qh = cached["hash_quick"] if cached else None
            if not qh:
                src = os.path.join(r["root_path"], r["rel_dir"].replace("/", os.sep), r["name"])
                qh = quick_hash(src, r["size"])
                if qh:
                    conn.execute("UPDATE files SET hash_quick=? WHERE id=?", (qh, r["id"]))
                    # 写完立刻提交：WAL 下 commit 开销极小，
                    # 若攒批提交，写事务会横跨后面多个文件的慢速哈希读取，长期持有写锁
                    conn.commit()
                done += 1
                if progress and done % 25 == 0:
                    progress("快速哈希", done, need_quick, f"已读 {done}/{need_quick}")
        for r in g:
            qh = conn.execute(
                "SELECT hash_quick FROM files WHERE id=?", (r["id"],)).fetchone()["hash_quick"]
            if qh:
                buckets.setdefault(qh, []).append(r)
        for qh, members in buckets.items():
            if len(members) > 1:
                still_groups.append(members)
    conn.commit()

    # ---------- 第三层：full hash ----------
    need_full = sum(
        sum(1 for r in g if _needs(conn, r["id"], "hash_full")) for g in still_groups)
    done = 0
    real_dups = 0
    wasted = 0
    for g in still_groups:
        buckets: dict[str, list] = {}
        for r in g:
            if stop_flag is not None and stop_flag.is_set():
                conn.commit()
                conn.close()
                return {"aborted": True, "layer1": layer1_files}
            cached = conn.execute(
                "SELECT hash_full FROM files WHERE id=?", (r["id"],)).fetchone()
            fh = cached["hash_full"] if cached else None
            if not fh:
                src = os.path.join(r["root_path"], r["rel_dir"].replace("/", os.sep), r["name"])
                fh = full_hash(src)
                if fh:
                    conn.execute("UPDATE files SET hash_full=? WHERE id=?", (fh, r["id"]))
                    conn.commit()  # 同上：写完立即提交，不让写锁横跨后续慢速哈希
                done += 1
                if progress and done % 5 == 0:
                    progress("完整哈希", done, need_full, f"已读 {done}/{need_full} 个大文件")
            if fh:
                buckets.setdefault(fh, []).append(r)
        conn.commit()
        for fh, members in buckets.items():
            if len(members) > 1:
                real_dups += len(members) - 1
                wasted += members[0]["size"] * (len(members) - 1)

    conn.commit()
    conn.close()
    if progress:
        progress("完成", 1, 1, "去重分析完成")
    return {
        "layer1_files": layer1_files,
        "layer2_groups": len(still_groups),
        "dup_files": real_dups,
        "wasted_bytes": wasted,
        "scanned_files": total_files,
        "elapsed": round(time.time() - t0, 1),
    }


def _needs(conn, fid: int, col: str) -> bool:
    r = conn.execute(f"SELECT {col} AS v FROM files WHERE id=?", (fid,)).fetchone()
    return bool(r) and r["v"] is None
