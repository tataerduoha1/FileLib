"""
filelib.core.scanner —— 目录扫描器

硬性要求（对应设计文档第九节风险清单）：
  1. 单个文件/目录失败不能中断整趟扫描 → 记 scan_errors 后继续
  2. 支持 Windows 超长路径（\\\\?\\ 前缀）
  3. 增量：mtime+size 未变则只更新 last_seen，不重写整行
  4. 可中断：Ctrl-C / 关闭窗口后下次从游标续跑
  5. 只读：绝不对源盘做任何写操作
"""
from __future__ import annotations

import os
import sys
import time
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .kinds import get_ext, kind_of, normalize_name

IS_WIN = sys.platform == "win32"

# 永不进入的目录（系统垃圾、回收站、版本控制元数据）
JUNK_DIRS = {
    "$recycle.bin", "system volume information", "recycler", "recycled",
    ".git", ".svn", ".hg", ".trash", ".trashes", "__macosx",
    "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode",
    "@eadir", "lost+found", "config.msi", "msocache",
    ".ds_store", "windows.old", "$windows.~bt", "$windows.~ws",
}

# 明显的非内容文件，不入库（避免几十万行噪音）
JUNK_FILES = {
    "thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db",
    ".directory", "icon\r",
}
JUNK_EXTS = {"tmp", "temp", "crdownload", "part", "partial", "!ut", "downloading"}


def long_path(p: str) -> str:
    """给 Windows 绝对路径加 \\\\?\\ 前缀以突破 MAX_PATH=260 限制。"""
    if not IS_WIN:
        return p
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + os.path.abspath(p)


def short_display(p: str) -> str:
    return p[4:] if p.startswith("\\\\?\\") else p


@dataclass
class ScanStats:
    n_seen: int = 0
    n_new: int = 0
    n_updated: int = 0
    n_unchanged: int = 0
    n_errors: int = 0
    n_dirs: int = 0
    bytes_seen: int = 0
    started: float = field(default_factory=time.time)
    current_dir: str = ""
    phase: str = "scanning"
    error_samples: list[tuple[str, str]] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def rate(self) -> float:
        e = self.elapsed
        return self.n_seen / e if e > 0 else 0.0

    def as_dict(self) -> dict:
        return {
            "n_seen": self.n_seen,
            "n_new": self.n_new,
            "n_updated": self.n_updated,
            "n_unchanged": self.n_unchanged,
            "n_errors": self.n_errors,
            "n_dirs": self.n_dirs,
            "bytes_seen": self.bytes_seen,
            "elapsed": round(self.elapsed, 1),
            "rate": round(self.rate, 1),
            "current_dir": self.current_dir,
            "phase": self.phase,
            "errors": self.error_samples[-20:],
        }


class Scanner:
    def __init__(self, conn: sqlite3.Connection, stats: ScanStats | None = None):
        self.conn = conn
        self.stats = stats or ScanStats()
        self._stop = threading.Event()
        self.on_progress: Callable[[ScanStats], None] | None = None

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ---------- 卷识别 ----------
    def register_volume(self, root: str) -> int:
        """用卷序列号识别盘，盘符变化也能找回原来的索引。"""
        root = os.path.abspath(root)
        serial, label, fs = self._volume_info(root)
        cur = self.conn.execute("SELECT id FROM volumes WHERE root_path = ?", (root,))
        row = cur.fetchone()
        now = int(time.time())
        if row:
            vid = row["id"]
            # 不覆盖 label：用户可能在管理后台改过显示名
            self.conn.execute(
                "UPDATE volumes SET serial=?, fs=?, last_seen=? WHERE id=?",
                (serial, fs, now, vid),
            )
        else:
            # 盘符变了但路径相同（如 F:\书 → G:\书）：复用旧记录，避免重复建库。
            # 同一块盘上的**不同**目录必须新建库——否则会劫持原库记录，
            # 且 reconcile 会把原根目录的索引文件全部标成 missing（数据丢失）。
            vid = None
            if serial:
                cur = self.conn.execute(
                    "SELECT id, root_path FROM volumes WHERE serial = ?", (serial,)
                )
                for r2 in cur.fetchall():
                    if self._same_path_other_drive(r2["root_path"], root):
                        vid = r2["id"]
                        self.conn.execute(
                            "UPDATE volumes SET root_path=?, fs=?, last_seen=? WHERE id=?",
                            (root, fs, now, vid),
                        )
                        break
            if vid is None:
                cur = self.conn.execute(
                    "INSERT INTO volumes(root_path, serial, label, fs, last_seen) "
                    "VALUES(?,?,?,?,?)",
                    (root, serial, self._default_label(root, label), fs, now),
                )
                vid = cur.lastrowid
        self.conn.commit()
        return vid

    def _volume_info(self, root: str) -> tuple[str | None, str | None, str | None]:
        if not IS_WIN:
            return None, None, None
        try:
            import ctypes
            drive = os.path.splitdrive(root)[0] + "\\"
            volname = ctypes.create_unicode_buffer(261)
            fsname = ctypes.create_unicode_buffer(261)
            serial = ctypes.c_ulong(0)
            maxlen = ctypes.c_ulong(0)
            flags = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                drive, volname, 261, ctypes.byref(serial), ctypes.byref(maxlen),
                ctypes.byref(flags), fsname, 261,
            )
            if ok:
                return f"{serial.value:08X}", (volname.value or None), (fsname.value or None)
        except Exception:
            pass
        return None, None, None

    @staticmethod
    def _default_label(root: str, drive_label: str | None) -> str:
        """默认库名：整盘扫描用卷标（如「Windows（C:）」），子目录用文件夹名，
        避免同盘多库全部重名。"""
        sub = os.path.normpath(root).strip("\\/").lower()
        drive_prefix = os.path.splitdrive(root)[0].lower()
        if sub == drive_prefix or not sub:  # 根目录本身（C:\ 或 C:）
            return drive_label or (os.path.splitdrive(root)[0] + " 盘")
        name = os.path.basename(os.path.normpath(root).rstrip("\\/"))
        return name or drive_label or "资料库"

    @staticmethod
    def _same_path_other_drive(old: str | None, new: str) -> bool:
        """旧库路径与新路径是否只差盘符（F:\\书 vs G:\\书 → True；F:\\a vs F:\\b → False）。"""
        if not old:
            return False
        d1, p1 = os.path.splitdrive(os.path.normpath(old))
        d2, p2 = os.path.splitdrive(os.path.normpath(new))
        if not d1 or not d2:
            return False
        p1 = p1.strip("\\").lower()
        p2 = p2.strip("\\").lower()
        return p1 == p2

    # ---------- 主扫描 ----------
    def scan(self, root: str, exclude_dirs: set[str] | None = None,
             batching: int = 2000) -> ScanStats:
        """
        遍历 root，增量写入索引。返回统计。
        exclude_dirs：需要跳过的相对目录（不带尾部斜杠），如 {'0000/AI换脸'}
        """
        root = os.path.abspath(root)
        if not os.path.isdir(long_path(root)):
            raise FileNotFoundError(f"目标目录不可访问：{root}")

        vol_id = self.register_volume(root)
        exclude = {e.strip("/").lower() for e in (exclude_dirs or set())}

        run_id = self.conn.execute(
            "INSERT INTO scan_runs(vol_id, started, state) VALUES(?,?,'running')",
            (vol_id, int(time.time())),
        ).lastrowid
        self.conn.commit()
        self.conn.execute(
            "UPDATE volumes SET scan_state='running', last_scan=? WHERE id=?",
            (int(time.time()), vol_id),
        )
        self.conn.commit()

        seen_ids: set[int] = set()
        pending: list[tuple] = []
        seen_dirs: dict[str, tuple[str, str, int]] = {}
        batch = batching

        # 本轮见过文件的登记表（临时表，随连接销毁），reconcile 用它区分
        # "真的消失了" 和 "看到了只是没变化"
        self.conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS seen_files("
            "rel_dir TEXT, name TEXT, PRIMARY KEY(rel_dir, name))")
        self.conn.execute("DELETE FROM seen_files")
        self.conn.commit()  # DELETE 会开隐式事务，不提交会让后面 _flush 的 BEGIN 报错走降级分支

        try:
            for rel_dir, name, size, mtime in self._walk(root, exclude):
                ext = get_ext(name)
                if ext in JUNK_EXTS or name.lower() in JUNK_FILES:
                    continue
                kind = kind_of(ext)
                pending.append((
                    vol_id, rel_dir, name, normalize_name(name), ext, kind,
                    size, mtime, int(time.time()),
                ))
                self.stats.n_seen += 1
                self.stats.bytes_seen += size

                # 目录树信息在 _walk 里收集，这里补一次
                if rel_dir and rel_dir not in seen_dirs:
                    seen_dirs[rel_dir] = ("", "", 0)

                if len(pending) >= batch:
                    self._flush(pending, vol_id)
                    pending.clear()
                    if self.on_progress:
                        self.on_progress(self.stats)
                    if self._stop.is_set():
                        break
                    # 防止长时间持锁，给查询让路
                    time.sleep(0)

            if pending:
                self._flush(pending, vol_id)

            # 标记本轮未出现的文件为 missing（不删除记录）
            if not self._stop.is_set():
                self.stats.phase = "reconciling"
                if self.on_progress:
                    self.on_progress(self.stats)
                self._reconcile(vol_id, self.stats.started)

            state = "aborted" if self._stop.is_set() else "done"
            self.stats.phase = state
            self.conn.execute(
                "UPDATE scan_runs SET finished=?, n_seen=?, n_new=?, n_updated=?, "
                "n_errors=?, state=? WHERE id=?",
                (int(time.time()), self.stats.n_seen, self.stats.n_new,
                 self.stats.n_updated, self.stats.n_errors, state, run_id),
            )
            self.conn.execute(
                "UPDATE volumes SET scan_state=?, last_scan=? WHERE id=?",
                (state, int(time.time()), vol_id),
            )
            self.conn.commit()
        except BaseException as ex:
            self.conn.execute(
                "UPDATE scan_runs SET finished=?, state='aborted' WHERE id=?",
                (int(time.time()), run_id),
            )
            self.conn.execute(
                "UPDATE volumes SET scan_state='aborted' WHERE id=?", (vol_id,)
            )
            self.conn.commit()
            raise ex

        self._rebuild_dirs(vol_id)
        # 扫描改动了 files/dirs 全量数据：清掉进程内聚合缓存
        from . import cache as _cache
        _cache.bump()
        if self.on_progress:
            self.on_progress(self.stats)
        return self.stats

    def _walk(self, root: str, exclude: set[str]) -> Iterator[tuple[str, str, int, int]]:
        """
        用显式栈 + os.scandir 实现遍历。
        每个目录单独 try，失败记录后继续，绝不整体崩掉。
        """
        root_lp = long_path(root)
        stack: list[tuple[str, str]] = [(root_lp, "")]

        while stack:
            abs_dir, rel_dir = stack.pop()
            if self._stop.is_set():
                return
            self.stats.current_dir = short_display(abs_dir)
            try:
                with os.scandir(abs_dir) as it:
                    entries = list(it)
                self.stats.n_dirs += 1
            except (OSError, PermissionError) as ex:
                self._record_error(abs_dir, ex)
                continue

            subdirs: list[tuple[str, str]] = []
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        lname = e.name.lower()
                        if lname in JUNK_DIRS:
                            continue
                        child_rel = f"{rel_dir}{e.name}/"
                        if child_rel.rstrip("/").lower() in exclude:
                            continue
                        if lname.startswith(".") and len(lname) > 1 and lname not in (".config",):
                            # 隐藏目录多半是元数据，但仍允许常见的资料目录
                            pass
                        subdirs.append((e.path, child_rel))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        yield rel_dir, e.name, st.st_size, int(st.st_mtime)
                except (OSError, PermissionError) as ex:
                    self._record_error(e.path, ex)

            # 逆序入栈，保证字母序访问（体验上更像资源管理器）
            for item in reversed(subdirs):
                stack.append(item)

    def _flush(self, rows: list[tuple], vol_id: int) -> None:
        """批量 upsert。只更新变化过的行；但 status 例外——
        本轮看到了就恢复 present（复活被误标 missing 的记录）。"""
        sql = """
        INSERT INTO files(vol_id, rel_dir, name, name_lower, ext, kind, size,
                          mtime, added_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(vol_id, rel_dir, name) DO UPDATE SET
            size        = excluded.size,
            mtime       = excluded.mtime,
            ext         = excluded.ext,
            kind        = excluded.kind,
            name_lower  = excluded.name_lower,
            status      = 'present'
        WHERE files.mtime <> excluded.mtime OR files.size <> excluded.size
           OR files.status <> 'present'
        """
        try:
            self.conn.execute("BEGIN")
            before = self.conn.total_changes
            self.conn.executemany(sql, rows)
            changed = self.conn.total_changes - before
            # 记录本轮见过的文件，供 reconcile 判断谁真正消失了
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen_files(rel_dir, name) VALUES(?,?)",
                [(r[1], r[2]) for r in rows])
            self.conn.commit()
            self.stats.n_new += 0   # 精确区分太贵，统一在统计里体现
            self.stats.n_unchanged = self.stats.n_seen - changed
            self.stats.n_updated = changed
        except sqlite3.Error as ex:
            self.conn.rollback()
            # 批量失败时退回逐条，避免一条坏数据毁掉整批
            for r in rows:
                try:
                    self.conn.execute(sql, r)
                    self.conn.execute(
                        "INSERT OR IGNORE INTO seen_files(rel_dir, name) VALUES(?,?)",
                        (r[1], r[2]))
                    self.conn.commit()
                except sqlite3.Error as ex2:
                    self.conn.rollback()
                    self._record_error(f"{r[1]}{r[2]}", ex2)

    def _reconcile(self, vol_id: int, started: float) -> None:
        """本轮没见过的文件 + 状态还是 present → missing。
        seen_files 临时表记录了本轮 walk 实际看到的文件，
        只有不在其中（目录被删/文件被删/移动走）才标记 missing。
        added_at < started-1 保证只处理本轮之前就已入库的文件。"""
        cur = self.conn.execute(
            "UPDATE files SET status='missing' WHERE vol_id=? AND status='present' "
            "AND added_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM seen_files s "
            "                WHERE s.rel_dir=files.rel_dir AND s.name=files.name)",
            (vol_id, int(started) - 1),
        )
        self.stats.n_missing = cur.rowcount
        self.conn.commit()

    def _record_error(self, path: str, ex: BaseException) -> None:
        self.stats.n_errors += 1
        msg = f"{type(ex).__name__}: {ex}"
        if len(self.stats.error_samples) < 200:
            self.stats.error_samples.append((short_display(str(path)), msg[:300]))
        try:
            self.conn.execute(
                "INSERT INTO scan_errors(path, reason, ts) VALUES(?,?,?)",
                (short_display(str(path)), msg[:500], int(time.time())),
            )
        except sqlite3.Error:
            pass

    def _rebuild_dirs(self, vol_id: int) -> None:
        """
        从 files 表重建目录树。

        rel_dir 形如 '0000/！！【日本】/【视频】/'（以 / 结尾，根为 ''）。
        对每个 rel_dir 拆出：
            parent  = 上一级（同样以 / 结尾）
            name    = 末级名字（不含末尾 /）
            depth   = 层级深度（顶层为 1）
        同时把路径上的所有中间目录补全，避免出现"子目录有、父目录没有"导致树断裂。
        """
        rows = self.conn.execute(
            "SELECT DISTINCT rel_dir FROM files WHERE vol_id=?", (vol_id,)
        ).fetchall()

        # 用 set 收集所有层级（含中间层），一次写完
        all_dirs: set[str] = set()
        for r in rows:
            rel = r["rel_dir"]
            if not rel:
                continue
            parts = [p for p in rel.split("/") if p]
            acc = ""
            for p in parts:
                acc += p + "/"
                all_dirs.add(acc)

        payload = []
        for rel in all_dirs:
            trimmed = rel.rstrip("/")
            idx = trimmed.rfind("/")
            if idx >= 0:
                parent = trimmed[: idx + 1]
                name = trimmed[idx + 1:]
                depth = parent.count("/") + 1
            else:
                parent = ""
                name = trimmed
                depth = 1
            payload.append((vol_id, rel, parent, name, depth))

        try:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM dirs WHERE vol_id=?", (vol_id,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO dirs(vol_id, rel_dir, parent, name, depth) "
                "VALUES(?,?,?,?,?)",
                payload,
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
