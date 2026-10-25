"""
filelib/core/jobs.py —— 后台任务 + 计划任务 + 备份轮转

单一后台任务槽（扫描/封面/全文/去重同一时刻只跑一个），避免磁盘和 CPU 打架。
计划任务独立守护线程：定时扫描、自动备份、清理过期静音记录。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import db as dbmod
from . import cache as cachemod
from .db import connect


class Job:
    """后台任务状态机。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.name = None
        self.state = "idle"          # idle / running / done / error / aborted
        self.message = ""
        self.progress = {}
        self.error = None
        self.started = 0
        self.finished = 0
        self.stop_flag = threading.Event()

    def busy(self) -> bool:
        with self.lock:
            return self.state == "running"

    def start(self, name: str, fn) -> bool:
        with self.lock:
            if self.state == "running":
                return False
            self.name = name
            self.state = "running"
            self.message = ""
            self.progress = {}
            self.error = None
            self.started = int(time.time())
            self.finished = 0
            self.stop_flag.clear()

        def run():
            try:
                fn(self)
                with self.lock:
                    if self.state == "running":
                        self.state = "done"
                        self.finished = int(time.time())
                        self.message = self.message or "完成"
            except BaseException as ex:
                import traceback
                traceback.print_exc()
                with self.lock:
                    self.state = "error"
                    self.error = f"{type(ex).__name__}: {ex}"
                    self.finished = int(time.time())
            finally:
                # 任何后台任务都可能改了 files/meta/tasks：聚合缓存全部失效
                cachemod.bump()
                self.stop_flag.clear()

        threading.Thread(target=run, daemon=True, name=f"job-{name}").start()
        return True

    def stop(self) -> None:
        self.stop_flag.set()
        with self.lock:
            if self.state == "running":
                self.state = "aborted"

    def snapshot(self) -> dict:
        with self.lock:
            return {"name": self.name, "state": self.state, "message": self.message,
                    "progress": dict(self.progress), "error": self.error,
                    "started": self.started, "finished": self.finished}


JOB = Job()


def _retry_write(fn, tries: int = 24, wait: float = 5.0):
    """维护任务里的写操作兜底重试：偶发 database is locked 时等几秒再试，
    而不是直接把整个后台任务打挂。最多等 tries*wait 秒。"""
    import sqlite3
    for k in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as ex:
            if "locked" not in str(ex) or k == tries - 1:
                raise
            time.sleep(wait)


# ---------- 扫描（含元数据/任务/备份） ----------

def do_scan(root: str, exclude: set[str] | None = None, job: Job | None = None,
            rebuild_authors: bool = True) -> dict:
    from .scanner import Scanner, ScanStats
    from .indexer import build_meta, enqueue_tasks
    from .store import Store

    j = job or JOB
    j.message = "扫描目录"
    conn = connect()
    try:
        sc = Scanner(conn, ScanStats())

        def on_prog(st):
            j.progress = st.as_dict()
            j.message = f"扫描中：{st.n_seen:,} 个文件"
        sc.on_progress = on_prog
        st = sc.scan(root, exclude_dirs=exclude)

        j.message = "解析书名/作者/编号"
        j.progress = {**st.as_dict(), "phase": "meta"}
        n_meta = build_meta(conn)

        j.message = "创建封面/全文任务"
        tasks = _retry_write(lambda: enqueue_tasks(conn))

        if rebuild_authors:
            j.message = "重建作者索引"
            try:
                conn.execute("DELETE FROM authors")
                conn.execute(
                    """
                    INSERT INTO authors(name, n, vol_ids)
                    SELECT json_extract(m.json,'$.author') AS name,
                           COUNT(*) n, GROUP_CONCAT(DISTINCT f.vol_id)
                    FROM files f JOIN meta m ON m.file_id=f.id
                    WHERE json_extract(m.json,'$.author') IS NOT NULL
                      AND json_extract(m.json,'$.author') <> ''
                    GROUP BY json_extract(m.json,'$.author')
                    """)
                conn.commit()
            except Exception:
                pass
        result = {"scan": st.as_dict(), "meta": n_meta, "tasks": tasks}
        try:
            conn.execute("INSERT INTO job_runs(ts, kind, result) VALUES(?,?,?)",
                         (int(time.time()), "scan", json.dumps(result, ensure_ascii=False)[:2000]))
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()

    # 自动备份（扫描后一份，保留 3 份）
    try:
        keep = 3
        c2 = connect()
        keep = int(dbmod.get_setting(c2, "backup_keep", 3) or 3)
        auto = dbmod.get_setting(c2, "auto_backup", True)
        c2.close()
        if auto:
            j.message = "备份索引库"
            dbmod.backup_db(keep=keep)
    except Exception:
        pass

    try:
        from .store import _ROOTS_CACHE  # 兼容旧代码
    except Exception:
        pass
    return result


def do_cover_job(limit: int, workers: int, job: Job, conv_only: bool = False) -> dict:
    """批量补封面 + 书籍元数据。"""
    from .indexer import run_thumb_worker, enqueue_tasks
    conn = connect()
    try:
        _retry_write(lambda: enqueue_tasks(conn))
        conn.close()
        job.message = "生成封面"

        def prog(done, failed, skipped, i, total):
            job.progress = {"done": done, "failed": failed, "skipped": skipped,
                            "i": i, "total": total}
            job.message = f"封面 {i:,}/{total:,}　成功 {done:,}"

        job.progress = {"done": 0, "failed": 0, "skipped": 0, "i": 0, "total": 0}
        r = run_thumb_worker(limit=limit, workers=workers, progress=prog,
                             stop_flag=job.stop_flag,
                             only_types=["thumb_video", "thumb_image",
                                         "thumb_pdf", "thumb_book"])
        job.progress = r
        job.message = f"完成：成功 {r['done']:,}　失败 {r['failed']:,}　跳过 {r['skipped']:,}"
        return r
    finally:
        try:
            conn.close()
        except Exception:
            pass


def do_fulltext_job(job: Job, limit: int = 0) -> dict:
    from .indexer import run_thumb_worker, enqueue_tasks
    conn = connect()
    try:
        j1 = job
        j1.message = "入队全文任务"
        _retry_write(lambda: enqueue_tasks(conn, types=["fulltext"]))
        n = conn.execute(
            "SELECT COUNT(*) n FROM tasks WHERE type='fulltext' AND state='pending'"
        ).fetchone()["n"]
        conn.close()
        if n == 0:
            # 没有可提取正文的文档时给一句明确交代，避免"点了没反应"的困惑
            job.message = "没有需要处理的文件（本库没有 TXT/PDF/EPUB/MOBI 等文档）"
            job.progress = {"done": 0, "failed": 0, "skipped": 0, "i": 0, "total": 0}
            return {"done": 0, "failed": 0, "skipped": 0, "total": 0}

        def prog(done, failed, skipped, i, total):
            job.progress = {"done": done, "failed": failed, "skipped": skipped,
                            "i": i, "total": total}
            job.message = f"全文索引 {i:,}/{total:,}"

        job.progress = {"i": 0, "total": n}
        r = run_thumb_worker(limit=limit, workers=2, progress=prog,
                             stop_flag=job.stop_flag, only_types=["fulltext"])
        job.progress = r
        job.message = f"完成：成功 {r['done']:,}"
        return r
    finally:
        try:
            conn.close()
        except Exception:
            pass


def do_autotag_job(job: Job, limit: int = 0) -> dict:
    """自动打标签（v1.0.7）：规则层 + 内嵌元数据 + 正文关键词，三层混合。
    标签带 auto: 前缀、source='auto'，人工标签完全不受影响。"""
    from .indexer import run_thumb_worker, enqueue_tasks
    conn = connect()
    try:
        j1 = job
        j1.message = "入队自动标签任务"
        _retry_write(lambda: enqueue_tasks(conn, types=["autotag"]))
        n = conn.execute(
            "SELECT COUNT(*) n FROM tasks WHERE type='autotag' AND state='pending'"
        ).fetchone()["n"]
        conn.close()
        if n == 0:
            # 全部文件都算过了，给一句明确交代
            job.message = "没有需要处理的文件（自动标签已全部生成）"
            job.progress = {"done": 0, "failed": 0, "skipped": 0, "i": 0, "total": 0}
            return {"done": 0, "failed": 0, "skipped": 0, "total": 0}

        def prog(done, failed, skipped, i, total):
            job.progress = {"done": done, "failed": failed, "skipped": skipped,
                            "i": i, "total": total}
            job.message = f"自动打标签 {i:,}/{total:,}"

        job.progress = {"i": 0, "total": n}
        r = run_thumb_worker(limit=limit, workers=3, progress=prog,
                             stop_flag=job.stop_flag, only_types=["autotag"])
        job.progress = r
        job.message = f"完成：成功 {r['done']:,}"
        return r
    finally:
        try:
            conn.close()
        except Exception:
            pass


def do_rethumb_job(job: Job, workers: int = 4) -> dict:
    """缩略图存量重压缩（v1.0.5）：把旧参数（480 宽/q82）生成的缩略图重编码为
    新参数（384 宽/q78）。直接读旧缩略图重编码，不碰原文件——比重新生成快一个
    数量级；重编码后若没变小则保留原文件。可随时停止，已完成的不回退。"""
    import io as _io
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    from .db import thumb_dir

    root = thumb_dir()
    files = [p for p in root.rglob("*.jpg") if p.is_file() and p.stat().st_size > 24 * 1024]
    total = len(files)
    job.message = f"重压缩 {total:,} 张缩略图"
    job.progress = {"i": 0, "total": total, "done": 0, "saved": 0,
                    "bytes_before": 0, "bytes_after": 0}
    state = {"i": 0, "done": 0, "saved": 0, "before": 0, "after": 0}
    lock = threading.Lock()

    def work(p: Path):
        if job.stop_flag and job.stop_flag.is_set():
            return None
        try:
            from PIL import Image
            before = p.stat().st_size
            with Image.open(p) as im:
                if max(im.size) <= 392:      # 尺寸已达标，跳过（避免多代重压缩损失）
                    return before
                im = im.convert("RGB")
                im.thumbnail((384, 576), Image.LANCZOS)
                buf = _io.BytesIO()
                im.save(buf, "JPEG", quality=78, optimize=True)
                if buf.tell() >= before:     # 没变小就不写，保留原图
                    return before
                p.write_bytes(buf.getvalue())
                return before
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for idx, before in enumerate(ex.map(work, files)):
            if before is None:
                continue
            with lock:
                state["i"] += 1
                state["done"] += 1
                state["before"] += before
                try:
                    state["after"] += files[idx].stat().st_size
                except Exception:
                    pass
                if state["i"] % 25 == 0 or state["i"] == total:
                    job.progress = {"i": state["i"], "total": total, "done": state["done"],
                                    "bytes_before": state["before"],
                                    "bytes_after": state["after"]}
                    job.message = (f"重压缩 {state['i']:,}/{total:,}"
                                   f"　省 {max(state['before'] - state['after'], 0) / 1048576:.1f} MB")
    job.progress = {"i": total, "total": total, "done": state["done"],
                    "bytes_before": state["before"], "bytes_after": state["after"]}
    job.message = (f"完成：{state['done']:,} 张　"
                   f"{state['before'] / 1048576:.1f} MB → {state['after'] / 1048576:.1f} MB")
    return {"done": state["done"], "total": total,
            "bytes_before": state["before"], "bytes_after": state["after"]}


# ---------- 一键整理流水线 ----------

def do_organize_job(job: Job, root: str | None = None) -> dict:
    """一键整理：增量扫描 → 补封面 → 全文索引 → 去重分析。

    root 为空时刷新所有已登记且在线的资料库；否则只处理该目录
    （首次接入新库也是一键完成）。每个阶段独立容错：某阶段出错
    记录原因后继续下一阶段；用户随时"停止任务"，当前阶段结束后
    不再进入下一阶段。阶段结果在 /api/task 里可见。
    """
    from .dedup import run_dedup

    out: dict = {"phases": [], "aborted": False}

    def phase(name: str, fn):
        if job.stop_flag.is_set():
            out["aborted"] = True
            return
        job.message = name
        try:
            r = fn() or {}
            out["phases"].append({"phase": name, "ok": True, **r})
        except Exception as ex:
            # 单阶段失败不阻断后续阶段（例如没装 PIL 导致封面失败，全文照常）
            out["phases"].append(
                {"phase": name, "ok": False, "error": f"{type(ex).__name__}: {ex}"})

    def p_scan():
        conn = connect()
        try:
            if root:
                roots = [root]
            else:
                roots = [r["root_path"] for r in conn.execute(
                    "SELECT root_path FROM volumes ORDER BY id").fetchall()
                    if os.path.isdir(r["root_path"])]
        finally:
            conn.close()
        n = 0
        for rt in roots:
            if job.stop_flag.is_set():
                out["aborted"] = True
                break
            do_scan(rt, job=job)
            n += 1
        return {"vols": n}

    def p_covers():
        return do_cover_job(0, 4, job)

    def p_fulltext():
        return do_fulltext_job(job)

    def p_dedup():
        def prog(stage, done, total, msg):
            job.progress = {"phase": "dedup", "stage": stage,
                            "done": done, "total": total}
            job.message = f"去重分析：{msg}"
        return run_dedup(progress=prog, stop_flag=job.stop_flag)

    phase("扫描", p_scan)
    phase("封面", p_covers)
    phase("全文", p_fulltext)
    phase("去重", p_dedup)
    job.message = ("一键整理完成" if not out["aborted"] else "一键整理已停止")
    return out


# ---------- 计划任务 ----------

class Scheduler:
    """
    守护线程，每 60 秒检查一次：
      * 每日定时扫描（scan_daily_time，默认 08:30）
      * 临时间隔扫描（scan_temp_enabled + scan_temp_interval_min）
      * 每日自动备份
    只在"该库在线（目录存在）"且"当前空闲"时执行。
    """

    def __init__(self):
        self._stop = threading.Event()
        self._th: threading.Thread | None = None
        self.last: dict = {}

    def start(self) -> None:
        if self._th and self._th.is_alive():
            return
        self._th = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._th.start()

    def stop(self) -> None:
        self._stop.set()

    def _settings(self) -> dict:
        conn = connect()
        try:
            return dbmod.all_settings(conn)
        finally:
            conn.close()

    def _loop(self) -> None:
        # 启动后等 30 秒，别和首页请求抢资源
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(60)

    def tick(self) -> None:
        if JOB.busy():
            return
        s = self._settings()
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # 每日定时
        dt = s.get("scan_daily_time")
        if s.get("scan_daily_enabled") and dt and re.match(r"^\d{1,2}:\d{2}$", str(dt)):
            hh, mm = [int(x) for x in str(dt).split(":")]
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if self.last.get("daily") != today and now >= target and now < target + timedelta(hours=6):
                if self._scan_all(reason="daily"):
                    self.last["daily"] = today

        # 间隔扫描
        if s.get("scan_temp_enabled"):
            iv = max(10, int(s.get("scan_temp_interval_min", 120) or 120))
            last = float(self.last.get("temp_ts", 0))
            if time.time() - last >= iv * 60:
                if self._scan_all(reason="interval"):
                    self.last["temp_ts"] = time.time()

        # 每日备份
        if s.get("auto_backup", True) and self.last.get("backup") != today:
            if now.hour >= 3:
                try:
                    keep = int(s.get("backup_keep", 3) or 3)
                    dbmod.backup_db(keep=keep)
                    self.last["backup"] = today
                except Exception:
                    pass

    def _scan_all(self, reason: str) -> bool:
        import os
        conn = connect()
        try:
            vols = [dict(r) for r in conn.execute("SELECT * FROM volumes")]
        finally:
            conn.close()
        targets = [v for v in vols if v["root_path"] and os.path.isdir(v["root_path"])]
        if not targets:
            return False

        def job(j):
            for v in targets:
                if j.stop_flag.is_set():
                    break
                j.message = f"[{reason}] 扫描 {v['root_path']}"
                try:
                    do_scan(v["root_path"], job=j, rebuild_authors=False)
                except Exception as ex:
                    j.message = f"扫描失败：{ex}"
            conn2 = connect()
            try:
                conn2.execute("INSERT INTO job_runs(ts, kind, result) VALUES(?,?,?)",
                              (int(time.time()), reason,
                               json.dumps({"vols": [v["id"] for v in targets]})))
                conn2.commit()
            finally:
                conn2.close()

        ok = JOB.start("scheduled_scan", job)
        return ok


SCHEDULER = Scheduler()
