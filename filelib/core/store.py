"""
filelib/core/store.py —— 面向界面的查询层

所有查询都走这里，便于统一优化和加缓存。
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any

from .db import connect, ThreadLocalConn, get_setting, put_setting
from .kinds import KIND_LABEL
from . import cache

# 惰性时长探测失败过的文件（进程内负缓存，避免坏文件每次打开都重探）
_DUR_FAIL: set[int] = set()


def _ffmpeg_duration(ffmpeg: str, path: str) -> float:
    """无 ffprobe 时的兜底：解析 `ffmpeg -i` 打印的 Duration 行（无输出文件时会报错退出，但时长已打出）。"""
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path],
                           capture_output=True, timeout=20,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
    except (subprocess.TimeoutExpired, OSError):
        return 0.0
    m = re.search(rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr or b"")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

SORT_MAP = {
    "mtime_desc": "f.mtime DESC, f.id DESC",
    "mtime_asc": "f.mtime ASC, f.id ASC",
    "name_asc": "f.name_lower ASC",
    "name_desc": "f.name_lower DESC",
    # size 排序补 id 次序：与 (size, id) 部分索引完全匹配，免 TEMP B-TREE
    # （20 万行级库实测 ~350ms → ~40ms），同尺寸文件顺序也稳定不翻页抖动
    "size_desc": "f.size DESC, f.id DESC",
    "size_asc": "f.size ASC, f.id ASC",
}


def _row_to_item(r: sqlite3.Row, root_cache: dict[int, str]) -> dict[str, Any]:
    vol_id = r["vol_id"]
    root = root_cache.get(vol_id, "")
    rel = r["rel_dir"]
    keys = r.keys()
    item = {
        "id": r["id"],
        "vol_id": vol_id,
        "name": r["name"],
        "dir": rel,
        "ext": r["ext"],
        "kind": r["kind"],
        "kind_label": KIND_LABEL.get(r["kind"], r["kind"]),
        "size": r["size"],
        "mtime": r["mtime"],
        "status": r["status"],
        "thumb": r["thumb_state"] == 2,
        "code": None,
        "series": None,
        "topic": None,
        "quality": [],
        "title": None,
        "author": None,
    }
    # 多标记（v3）：user_mark 是 GROUP_CONCAT 出来的 "star,read" 形式；
    # mark 字段保留单值兼容（优先级 star > read > todo，供卡片角标等旧逻辑使用）
    raw_marks = r["user_mark"] if "user_mark" in keys else None
    marks = [x for x in (raw_marks or "").split(",") if x]
    item["marks"] = marks
    item["mark"] = next((mk for mk in ("star", "read", "todo") if mk in marks), None)
    # meta json 里的结构化字段拍平
    if r["meta_json"]:
        try:
            m = json.loads(r["meta_json"])
            item["code"] = m.get("code")
            item["series"] = m.get("series")
            item["topic"] = m.get("topic")
            item["quality"] = m.get("quality") or []
            item["duration"] = m.get("duration")
            item["width"] = m.get("width")
            item["height"] = m.get("height")
            item["title"] = m.get("title")
            item["author"] = m.get("author")
            item["summary"] = m.get("summary")
        except (ValueError, TypeError):
            pass
    item["path"] = os.path.join(root, rel.replace("/", os.sep), r["name"])
    return item


BASE_SELECT = """
SELECT f.id, f.vol_id, f.rel_dir, f.name, f.ext, f.kind, f.size, f.mtime,
       f.status, f.thumb_state, m.json AS meta_json, um.marks AS user_mark
FROM files f
LEFT JOIN meta m ON m.file_id = f.id
LEFT JOIN (SELECT file_id, GROUP_CONCAT(mark) AS marks
           FROM user_marks GROUP BY file_id) um ON um.file_id = f.id
"""

# 列表查询用的轻量字段（不含全文正文，减少 IO）
BASE_SELECT_LITE = BASE_SELECT


class Store:
    """
    查询层。

    conn 是 ThreadLocalConn：每个请求线程拿到自己的 SQLite 连接。
    读取索引时走 readonly 连接，避免界面误写数据库。
    """

    def __init__(self, path: str | None = None, readonly: bool = True):
        self._tl = ThreadLocalConn(path, readonly=readonly)
        self._path = path
        self._rw_tl: ThreadLocalConn | None = None
        self._roots_cache: dict[int, str] = {}
        self.refresh_roots()

    @property
    def conn(self):
        return self._tl.conn

    @property
    def rw(self):
        """需要写入时用（如把文件标记为已删除）。每线程独立可写连接。"""
        if self._rw_tl is None:
            self._rw_tl = ThreadLocalConn(self._path, readonly=False)
        return self._rw_tl.conn

    def _load_roots(self) -> dict[int, str]:
        return {r["id"]: r["root_path"] for r in self.conn.execute(
            "SELECT id, root_path FROM volumes")}

    def refresh_roots(self) -> None:
        if not self._roots_cache:
            self._roots_cache = self._load_roots()

    def _roots(self) -> dict[int, str]:
        if not self._roots_cache:
            self._roots_cache = self._load_roots()
        return self._roots_cache

    def invalidate_roots(self) -> None:
        """扫描后调用，让下次查询重新读取卷信息。"""
        self._roots_cache = {}

    def close(self) -> None:
        self._tl.close_all()
        if self._rw_tl is not None:
            self._rw_tl.close_all()

    # ---------- 写操作 ----------
    def mark_missing(self, file_id: int) -> None:
        c = self.rw
        c.execute("UPDATE files SET status='missing' WHERE id=?", (file_id,))
        c.commit()
        cache.bump()

    # ---------- 文件管理（新建文件夹） ----------

    def fm_mkdir(self, vol_id: int, rel_dir: str) -> None:
        """把新建目录（含可能未入库的祖先链）补进 dirs 表，目录树立即可见。"""
        parts = [p for p in rel_dir.split("/") if p]
        parent = ""
        depth = 0
        for p in parts:
            cur = parent + p + "/"
            self.rw.execute(
                "INSERT OR IGNORE INTO dirs(vol_id, rel_dir, parent, name, depth, n_files) "
                "VALUES(?,?,?,?,?,0)",
                (vol_id, cur, parent, p, depth))
            parent = cur
            depth += 1
        self.rw.commit()
        cache.bump()

    # ---------- 用户标记 ----------
    def set_mark(self, file_id: int, mark: str | None) -> None:
        """整体设置标记（旧接口语义，兼容保留）：None=清空全部，否则替换为该单个标记。"""
        c = self.rw
        c.execute("DELETE FROM user_marks WHERE file_id=?", (file_id,))
        if mark:
            c.execute("INSERT OR IGNORE INTO user_marks(file_id, mark, ts) VALUES(?,?,?)",
                      (file_id, mark, int(time.time())))
        c.commit()
        cache.bump()

    def toggle_mark(self, file_id: int, mark: str, on: bool) -> list[str]:
        """勾选/取消单个标记（v3 多标记）：star 可与 read/todo 并存，read 与 todo 互斥。
        返回该文件当前的全部标记。"""
        c = self.rw
        if on:
            if mark == "read":
                c.execute("DELETE FROM user_marks WHERE file_id=? AND mark='todo'",
                          (file_id,))
            elif mark == "todo":
                c.execute("DELETE FROM user_marks WHERE file_id=? AND mark='read'",
                          (file_id,))
            c.execute("INSERT OR REPLACE INTO user_marks(file_id, mark, ts) VALUES(?,?,?)",
                      (file_id, mark, int(time.time())))
        else:
            c.execute("DELETE FROM user_marks WHERE file_id=? AND mark=?",
                      (file_id, mark))
        c.commit()
        cache.bump()
        return [r["mark"] for r in c.execute(
            "SELECT mark FROM user_marks WHERE file_id=?", (file_id,))]

    def get_mark(self, file_id: int) -> list[str]:
        return [r["mark"] for r in self.conn.execute(
            "SELECT mark FROM user_marks WHERE file_id=?", (file_id,))]

    # ---------- 用户标签 ----------

    def set_tags(self, file_id: int, names: list[str]) -> list[dict[str, Any]]:
        """替换某文件的用户标签（source='user'），自动标签保留。"""
        names = [n.strip() for n in names if n and n.strip()]
        names = list(dict.fromkeys(names))          # 去重保序
        c = self.rw
        c.execute(
            "DELETE FROM file_tags WHERE file_id = ? AND source = 'user'", (file_id,))
        for name in names:
            c.execute("INSERT OR IGNORE INTO tags(name, grp) VALUES(?, 'user')", (name,))
            c.execute(
                "INSERT OR IGNORE INTO file_tags(file_id, tag_id, source) "
                "SELECT ?, id, 'user' FROM tags WHERE name = ?", (file_id, name))
        c.commit()
        cache.bump()
        return [
            {"name": r["name"], "grp": r["grp"], "source": r["source"]}
            for r in self.conn.execute(
                "SELECT t.name, t.grp, ft.source FROM file_tags ft "
                "JOIN tags t ON t.id = ft.tag_id "
                "WHERE ft.file_id = ? ORDER BY t.grp, t.name", (file_id,))
        ]

    def all_tags(self, vol_id: int | None = None, q: str = "",
                 limit: int = 100, show_hidden: bool = False) -> dict[str, Any]:
        """标签聚合（带文件数）。vol_id 过滤到具体库。进数据版本缓存。
        show_hidden=True 时连被隐藏的标签一起返回（带 hidden 标记），标签管理器用。"""
        return cache.cached(f"tags:{vol_id}:{q}:{limit}:{int(show_hidden)}",
                            lambda: self._all_tags(vol_id, q, limit, show_hidden))

    # 隐藏标签名单存在 settings 里（JSON 名单）。隐藏 ≠ 删除：
    # 只是不再出现在侧栏/聚合里，文件筛选与「详情-标签」仍然可见可用。
    HIDDEN_KEY = "hidden_tags"

    def hidden_tags(self) -> list[str]:
        try:
            v = get_setting(self.conn, self.HIDDEN_KEY, [])
            return v if isinstance(v, list) else []
        except Exception:
            return []

    def tag_set_hidden(self, names: list[str], on: bool) -> None:
        """批量隐藏/取消隐藏标签（admin）。"""
        cur = set(self.hidden_tags())
        cur |= set(names) if on else set()
        cur -= set() if on else set(names)
        put_setting(self.rw, self.HIDDEN_KEY, sorted(cur))
        cache.bump()

    def tag_merge(self, src: str, dst: str) -> dict[str, Any]:
        """把标签 src 并入 dst：src 的文件关联迁到 dst（同文件两者都有时保留 dst），
        然后删除 src。dst 是人工标签时，迁来的关联一并转为人工（source='user'），
        以后「清空自动」不会误删——这也是把自动标签「转为人工」的通道。
        人工标签不允许并入自动标签（反向可以）。"""
        c = self.rw
        s = c.execute("SELECT id, grp FROM tags WHERE name=?", (src,)).fetchone()
        d = c.execute("SELECT id, grp FROM tags WHERE name=?", (dst,)).fetchone()
        if not s or not d or s["id"] == d["id"]:
            return {"ok": False, "moved": 0}
        if s["grp"] == "user" and d["grp"] == "auto":
            raise ValueError("人工标签不能并入自动标签（可反向合并，实现「转为人工」）")
        if d["grp"] == "user":
            cur = c.execute(
                "UPDATE OR IGNORE file_tags SET tag_id=?, source='user' WHERE tag_id=?",
                (d["id"], s["id"]))
        else:
            cur = c.execute(
                "UPDATE OR IGNORE file_tags SET tag_id=? WHERE tag_id=?",
                (d["id"], s["id"]))
        moved = max(0, cur.rowcount)
        c.execute("DELETE FROM file_tags WHERE tag_id=?", (s["id"],))  # 与目标重复的残留
        c.execute("DELETE FROM tags WHERE id=?", (s["id"],))
        c.commit()
        cache.bump()
        return {"ok": True, "moved": moved}

    def clear_auto_tags(self) -> int:
        """清空全部自动标签（v1.0.7，admin 用）。人工标签（source='user'）不受影响。"""
        c = self.rw
        cur = c.execute("DELETE FROM file_tags WHERE source = 'auto'")
        # 顺手清掉不再被任何文件引用的 auto 组标签，避免标签云残留
        c.execute(
            "DELETE FROM tags WHERE grp = 'auto' AND NOT EXISTS "
            "(SELECT 1 FROM file_tags ft WHERE ft.tag_id = tags.id)")
        c.commit()
        cache.bump()
        return cur.rowcount

    def _all_tags(self, vol_id: int | None = None, q: str = "",
                  limit: int = 100, show_hidden: bool = False) -> dict[str, Any]:
        hidden = set(self.hidden_tags())
        where = ["f.status = 'present'"]
        params: list[Any] = []
        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)
        if q:
            where.append("t.name LIKE ?")
            params.append(f"%{q}%")
        wsql = " AND ".join(where)
        total = self.conn.execute(
            "SELECT COUNT(DISTINCT t.id) n FROM tags t "
            "JOIN file_tags ft ON ft.tag_id = t.id "
            "JOIN files f ON f.id = ft.file_id WHERE " + wsql,
            params).fetchone()["n"]
        rows = self.conn.execute(
            "SELECT t.id, t.name, COUNT(DISTINCT ft.file_id) n, "
            "SUM(CASE WHEN ft.source = 'user' THEN 1 ELSE 0 END) > 0 is_user "
            "FROM tags t JOIN file_tags ft ON ft.tag_id = t.id "
            "JOIN files f ON f.id = ft.file_id WHERE " + wsql + " "
            "GROUP BY t.id ORDER BY n DESC, t.name LIMIT ?",
            params + [limit]).fetchall()
        # 范围内 present 文件总数：算标签覆盖率（泛化判定）用
        dsql = ("SELECT COUNT(*) n FROM files f WHERE f.status='present'"
                + (" AND f.vol_id=?" if vol_id is not None else ""))
        denom = self.conn.execute(
            dsql, [vol_id] if vol_id is not None else []).fetchone()["n"] or 1
        items: list[dict[str, Any]] = []
        for r in rows:
            hid = r["name"] in hidden
            if hid and not show_hidden:
                total -= 1
                continue
            items.append({
                "name": r["name"], "n": r["n"], "user": bool(r["is_user"]),
                "hidden": hid,
                # 泛化：覆盖 >30% 文件，几乎人人都有，区分度低
                "generic": r["n"] / denom > 0.30,
                # 低频：只有 1~2 个文件在用，多为长尾噪声
                "rare": r["n"] < 3,
            })
        return {"total": total, "items": items}

    # ---------- 作者聚合 ----------
    def rebuild_authors(self) -> int:
        """build_meta 后调用。把 meta 里的 author 聚合成表。"""
        c = self.rw
        c.execute("DELETE FROM authors")
        c.execute(
            """
            INSERT INTO authors(name, n, vol_ids)
            SELECT json_extract(m.json,'$.author') AS name,
                   COUNT(*) n,
                   GROUP_CONCAT(DISTINCT f.vol_id)
            FROM files f JOIN meta m ON m.file_id=f.id
            WHERE json_extract(m.json,'$.author') IS NOT NULL
              AND json_extract(m.json,'$.author') <> ''
            GROUP BY json_extract(m.json,'$.author')
            """)
        c.commit()
        cache.bump()
        return c.execute("SELECT COUNT(*) n FROM authors").fetchone()["n"]

    def rebuild_authors_conn(self, conn) -> int:
        """给后台线程用的版本（传自己的连接）。"""
        conn.execute("DELETE FROM authors")
        conn.execute(
            """
            INSERT INTO authors(name, n, vol_ids)
            SELECT json_extract(m.json,'$.author') AS name,
                   COUNT(*) n,
                   GROUP_CONCAT(DISTINCT f.vol_id)
            FROM files f JOIN meta m ON m.file_id=f.id
            WHERE json_extract(m.json,'$.author') IS NOT NULL
              AND json_extract(m.json,'$.author') <> ''
            GROUP BY json_extract(m.json,'$.author')
            """)
        conn.commit()
        return conn.execute("SELECT COUNT(*) n FROM authors").fetchone()["n"]

    def authors(self, vol_id: int | None = None, q: str = "",
                limit: int = 200, offset: int = 0) -> dict[str, Any]:
        """按作者聚合。vol_id 过滤用 vol_ids 里的逗号分隔列表。进数据版本缓存。"""
        return cache.cached(f"authors:{vol_id}:{q}:{limit}:{offset}",
                            lambda: self._authors(vol_id, q, limit, offset))

    def _authors(self, vol_id: int | None = None, q: str = "",
                 limit: int = 200, offset: int = 0) -> dict[str, Any]:
        where = ["1=1"]
        params: list[Any] = []
        if vol_id is not None:
            where.append("(',' || a.vol_ids || ',') LIKE ?")
            params.append(f"%,{vol_id},%")
        if q:
            where.append("a.name LIKE ?")
            params.append(f"%{q}%")
        wsql = " AND ".join(where)
        total = self.conn.execute(
            f"SELECT COUNT(*) n FROM authors a WHERE {wsql}", params).fetchone()["n"]
        rows = self.conn.execute(
            f"SELECT a.name, a.n FROM authors a WHERE {wsql} "
            f"ORDER BY a.n DESC, a.name LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        return {"total": total,
                "items": [{"name": r["name"], "n": r["n"]} for r in rows]}

    def author_books(self, name: str, vol_id: int | None = None,
                     limit: int = 300) -> list[dict]:
        """某作者的全部作品（按标题排序），带封面标记。"""
        where = ["f.status='present'", "json_extract(m.json,'$.author') = ?"]
        params: list[Any] = [name]
        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)
        rows = self.conn.execute(
            f"{BASE_SELECT} WHERE {' AND '.join(where)} ORDER BY f.name_lower LIMIT ?",
            params + [limit]).fetchall()
        return [_row_to_item(r, self._roots()) for r in rows]

    # ---------- 概览 ----------
    def stats(self, vol_id: int | None = None) -> dict[str, Any]:
        """
        vol_id 不为空时，返回"该资料库自己的"统计（顶部的类型分布、封面完成度、
        任务数都只算这一个库），同时仍附带全部资料库列表供切换用。

        性能：多轮 GROUP BY 全表，几十万文件时单次要几百毫秒 ——
        进数据版本缓存，任何写库动作会立即失效，不会读到旧数据。
        """
        return cache.cached(f"stats:{vol_id}", lambda: self._stats(vol_id))

    def _stats(self, vol_id: int | None = None) -> dict[str, Any]:
        c = self.conn
        # 只统计磁盘上真实存在的文件；已删除（missing）的条目不应计入
        vwhere = ("WHERE f.vol_id = ? AND f.status = 'present'"
                  if vol_id is not None else "WHERE f.status = 'present'")
        params: list[Any] = [vol_id] if vol_id is not None else []

        total = c.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM files f {vwhere}",
            params).fetchone()
        by_kind = [
            {"kind": r["kind"], "label": KIND_LABEL.get(r["kind"], r["kind"]),
             "n": r["n"], "size": r["s"]}
            for r in c.execute(
                f"SELECT kind, COUNT(*) n, SUM(size) s FROM files f {vwhere} "
                f"GROUP BY kind ORDER BY n DESC", params)
        ]
        tparams = params
        thumb_done = c.execute(
            f"SELECT COUNT(*) n FROM files f {vwhere} "
            f"{'AND' if vwhere else 'WHERE'} f.thumb_state=2", tparams).fetchone()["n"]
        tasks = {
            r["type"]: {"pending": r["p"], "total": r["t"]}
            for r in c.execute(
                f"SELECT t.type, SUM(t.state='pending') p, COUNT(*) t "
                f"FROM tasks t JOIN files f ON f.id=t.file_id {vwhere} "
                f"GROUP BY t.type", params)
        }
        vols = [
            {"id": r["id"], "root": r["root_path"], "label": r["label"],
             "serial": r["serial"], "state": r["scan_state"], "last_scan": r["last_scan"],
             "online": os.path.isdir(r["root_path"]) if r["root_path"] else False,
             "n": r["n"], "size": r["size"],
             "dir_count": r["dir_count"]}
            for r in c.execute(
                "SELECT v.*, "
                "  (SELECT COUNT(*) FROM files WHERE vol_id=v.id AND status='present') n, "
                "  (SELECT COALESCE(SUM(size),0) FROM files WHERE vol_id=v.id AND status='present') size, "
                "  (SELECT COUNT(*) FROM dirs WHERE vol_id=v.id) dir_count "
                "FROM volumes v ORDER BY v.id")
        ]
        return {
            "vol_id": vol_id,
            "total_files": total["n"],
            "total_size": total["s"],
            "by_kind": by_kind,
            "thumbs_done": thumb_done,
            "tasks": tasks,
            "volumes": vols,
        }

    # ---------- 目录树 ----------
    def dir_children(self, rel_dir: str = "", vol_id: int | None = None) -> list[dict[str, Any]]:
        """
        返回某目录下的子目录（含文件数、大小、封面缩略图）。
        vol_id 不为空时只在该资料库内查询，避免不同库的同名目录被合并。
        进数据版本缓存（GROUP BY + 相关子查询，写动作立即失效）。
        """
        return cache.cached(f"dirc:{vol_id}:{rel_dir}",
                            lambda: self._dir_children(rel_dir, vol_id))

    def _dir_children(self, rel_dir: str = "", vol_id: int | None = None) -> list[dict[str, Any]]:
        parent = rel_dir.rstrip("/")
        if parent:
            parent += "/"
        where = ["d.parent = ?"]
        params: list[Any] = [parent]
        if vol_id is not None:
            where.append("d.vol_id = ?")
            params.append(vol_id)
            join_vol = "AND f.vol_id = d.vol_id"
        else:
            join_vol = ""
        sql = f"""
            SELECT d.rel_dir, d.name, d.vol_id,
                   COUNT(f.id) AS n,
                   COALESCE(SUM(f.size), 0) AS s,
                   (SELECT f2.id FROM files f2
                     WHERE f2.rel_dir = d.rel_dir AND f2.vol_id = d.vol_id
                       AND f2.thumb_state = 2
                     ORDER BY f2.size DESC LIMIT 1) AS cover_id
            FROM dirs d
            LEFT JOIN files f ON f.rel_dir = d.rel_dir {join_vol}
            WHERE {' AND '.join(where)}
            GROUP BY d.rel_dir, d.name, d.vol_id
            ORDER BY d.name COLLATE NOCASE
        """
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {"type": "dir", "name": r["name"], "rel": r["rel_dir"],
             "vol_id": r["vol_id"], "n": r["n"], "size": r["s"], "cover": r["cover_id"]}
            for r in rows if r["rel_dir"] != parent
        ]

    def dir_subtree(self, vol_id: int) -> dict[str, Any]:
        """
        返回整个资料库的目录树（一次查完，前端按需展开）。
        199,627 个文件的书库也只有几千个目录，一次性传输没有压力。

        每层带 n = 该目录下所有后代文件数（含自身直属），nd = 直属文件数，
        这样侧栏既能看到"这个目录一共多少东西"，也能判断点进去有几条。

        性能：整棵树聚合 + 封面挑选，进数据版本缓存；
        封面挑选用窗口函数 + 封面部分索引，避免对全部已生成缩略图的
        文件做全表排序。
        """
        return cache.cached(f"tree:{vol_id}", lambda: self._dir_subtree(vol_id))

    def _dir_subtree(self, vol_id: int) -> dict[str, Any]:
        rows = self.conn.execute(
            """
            SELECT d.rel_dir AS rel, d.name, d.parent,
                   COUNT(f.id) AS nd,
                   COALESCE(SUM(f.size), 0) AS s
            FROM dirs d
            LEFT JOIN files f ON f.rel_dir = d.rel_dir AND f.vol_id = d.vol_id
            WHERE d.vol_id = ?
            GROUP BY d.rel_dir, d.name, d.parent
            ORDER BY d.rel_dir
            """,
            (vol_id,),
        ).fetchall()

        nodes = [
            {"rel": r["rel"], "name": r["name"], "parent": r["parent"],
             "nd": r["nd"], "size": r["s"], "tsize": r["s"], "n": r["nd"], "cover": None}
            for r in rows
        ]
        # 汇总：把每个目录的直属文件数/体积累加到所有祖先上
        # （size 始终是直属体积；tsize 是含全部后代的累计体积，供空间分析用）
        by_rel = {n["rel"]: n for n in nodes}
        for n in nodes:
            p = n["parent"]
            while p:
                if p in by_rel:
                    by_rel[p]["n"] += n["nd"]
                    by_rel[p]["tsize"] += n["size"]
                p = p[: p.rstrip("/").rfind("/") + 1] if "/" in p.rstrip("/") else ""

        # 每个目录挑一张封面（该目录直属文件里最大的那张缩略图）。
        # 窗口函数走 idx_files_cover 部分索引，无排序；旧写法
        # ORDER BY rel_dir, size DESC 需要对全部已生成缩略图的行排序。
        for r in self.conn.execute(
            """
            SELECT rel_dir, id FROM (
                SELECT rel_dir, id,
                       ROW_NUMBER() OVER (
                           PARTITION BY rel_dir ORDER BY size DESC) AS rn
                FROM files
                WHERE vol_id = ? AND thumb_state = 2 AND status = 'present'
            ) WHERE rn = 1
            """, (vol_id,)):
            n = by_rel.get(r["rel_dir"])
            if n is not None and n["cover"] is None:
                n["cover"] = r["id"]

        root_n = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM files "
            "WHERE vol_id=? AND rel_dir=''", (vol_id,)).fetchone()
        return {
            "vol_id": vol_id,
            "nodes": nodes,
            "root_files": {"n": root_n["n"], "size": root_n["s"]},
        }

    # ---------- 列表查询 ----------
    def query(self, *, q: str = "", kind: str | None = None,
              rel_dir: str | None = None, recursive: bool = True,
              ext: str | None = None, topic: str | None = None,
              series: str | None = None, author: str | None = None,
              mark: str | None = None, tag: str | None = None,
              tags: list[str] | None = None, not_tags: list[str] | None = None,
              min_size: int | None = None, sort: str = "mtime_desc",
              limit: int = 120, offset: int = 0,
              vol_id: int | None = None) -> dict[str, Any]:
        where: list[str] = ["f.status='present'"]
        params: list[Any] = []
        qtags: list[str] = []
        # 谓词是否只引用 files 表（决定能否走"两步分页"快速路径；
        # json_extract 需要 meta join，只能走老路径）
        f_only = True

        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)

        if q:
            terms = [t for t in q.split() if t]
            for t in terms:
                if ":" in t:
                    key, _, val = t.partition(":")
                    key = key.lower()
                    if key == "ext" and val:
                        for v in val.split(","):
                            where.append("f.ext = ?")
                            params.append(v.lower().lstrip("."))
                        continue
                    if key == "kind" and val:
                        where.append("f.kind = ?")
                        params.append(val.lower())
                        continue
                    if key in ("tag", "tags") and val:
                        # tag:a,b 表示同时具备 a 和 b
                        qtags += [x for x in val.split(",") if x]
                        continue
                    if key in ("-tag", "notag") and val:
                        where.append(
                            "f.id NOT IN (SELECT ft.file_id FROM file_tags ft "
                            "JOIN tags t ON t.id = ft.tag_id "
                            "WHERE t.name = ? OR t.name = 'auto:' || ?)")
                        params += [val, val]
                        continue
                    if key == "author" and val:
                        where.append("json_extract(m.json,'$.author') LIKE ?")
                        params.append(f"%{val}%")
                        f_only = False
                        continue
                    if key == "size" and val:          # size:>100mb
                        m = re.match(r"^([<>]=?)?\s*([\d.]+)\s*(k|m|g|kb|mb|gb|t|tb)?$", val.lower())
                        if m:
                            mul = {"k": 1024, "kb": 1024, "m": 1 << 20, "mb": 1 << 20,
                                   "g": 1 << 30, "gb": 1 << 30, "t": 1 << 40, "tb": 1 << 40}.get(
                                       m.group(3) or "b", 1)
                            num = float(m.group(2)) * mul
                            op = m.group(1) or ">"
                            where.append(f"f.size {op} ?")
                            params.append(int(num))
                        continue
                    if key == "year" and val:
                        where.append("json_extract(m.json,'$.year') = ?")
                        try:
                            params.append(int(val))
                        except ValueError:
                            params.append(-1)
                        f_only = False
                        continue
                where.append("(f.name_lower LIKE ? OR f.rel_dir LIKE ?)")
                low = t.lower()
                params += [f"%{low}%", f"%{low}%"]

        if kind:
            where.append("f.kind = ?")
            params.append(kind)
        if ext:
            where.append("f.ext = ?")
            params.append(ext.lower())
        if rel_dir is not None:
            if recursive:
                base = rel_dir if rel_dir.endswith("/") or rel_dir == "" else rel_dir + "/"
                where.append("f.rel_dir LIKE ?")
                params.append(base + "%")
            else:
                where.append("f.rel_dir = ?")
                params.append(rel_dir if rel_dir == "" or rel_dir.endswith("/") else rel_dir + "/")
        if topic:
            where.append("json_extract(m.json,'$.topic') = ?")
            params.append(topic)
            f_only = False
        if series:
            where.append("json_extract(m.json,'$.series') = ?")
            params.append(series)
            f_only = False
        if author:
            where.append("json_extract(m.json,'$.author') = ?")
            params.append(author)
            f_only = False
        if mark:
            where.append(f"f.id IN (SELECT file_id FROM user_marks WHERE mark = ?)")
            params.append(mark)
        if min_size:
            where.append("f.size >= ?")
            params.append(min_size)

        # 多标签 AND：每个标签都要求命中（用 GROUP BY 计数实现，索引友好）。
        # tag 参数兼容 "a,b" 逗号分隔多值（与搜索语法 tag:a,b 一致）
        all_tags = qtags + [x for x in (tag or "").split(",") if x] + list(tags or [])
        all_tags = list(dict.fromkeys([t for t in all_tags if t]))
        if all_tags:
            # 兼容 auto: 前缀（v1.0.7 起自动标签 name 带 auto:）：查询值同时匹配
            # 裸名与 auto: 名；规范名（剥前缀）去重计数，防止同一文件同时有
            # 人工「科幻」与自动「auto:科幻」两个标签行虚增 AND 计数
            names: list[str] = []
            for t in all_tags:
                if t.startswith("auto:"):
                    names += [t, t[5:]]
                else:
                    names += [t, "auto:" + t]
            names = list(dict.fromkeys(names))
            marks = ",".join("?" * len(names))
            where.append(
                "f.id IN (SELECT ft.file_id FROM file_tags ft "
                "JOIN tags t ON t.id = ft.tag_id "
                f"WHERE t.name IN ({marks}) "
                "GROUP BY ft.file_id "
                "HAVING COUNT(DISTINCT CASE WHEN t.name LIKE 'auto:%' "
                "THEN substr(t.name, 6) ELSE t.name END) = ?)")
            params += names + [len(all_tags)]
        for nt in (not_tags or []):
            where.append(
                "f.id NOT IN (SELECT ft.file_id FROM file_tags ft "
                "JOIN tags t ON t.id = ft.tag_id "
                "WHERE t.name = ? OR t.name = 'auto:' || ?)")
            params += [nt, nt]

        wsql = " AND ".join(where)
        order = SORT_MAP.get(sort, SORT_MAP["mtime_desc"])

        if f_only:
            # 两步分页快速路径：谓词只涉及 files 表时，
            # 第一步只取本页 id（部分索引免回表/免排序/免 join，深翻页 OFFSET 5 万级
            # 从 ~650ms 降到 ~50ms），第二步按 id 取整行再按原顺序拼回。
            total = self.conn.execute(
                f"SELECT COUNT(*) n FROM files f WHERE {wsql}",
                params).fetchone()["n"]
            ids = [r["id"] for r in self.conn.execute(
                f"SELECT f.id FROM files f WHERE {wsql} ORDER BY {order} LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()]
            rows: list[sqlite3.Row] = []
            if ids:
                marks = ",".join("?" * len(ids))
                by = {r["id"]: r for r in self.conn.execute(
                    f"{BASE_SELECT} WHERE f.id IN ({marks})", ids).fetchall()}
                rows = [by[i] for i in ids if i in by]
            return {"total": total,
                    "items": [_row_to_item(r, self._roots()) for r in rows],
                    "limit": limit, "offset": offset}

        total = self.conn.execute(
            f"SELECT COUNT(*) n FROM files f LEFT JOIN meta m ON m.file_id=f.id WHERE {wsql}",
            params).fetchone()["n"]

        # 非 f_only（谓词含 meta 的 json_extract）也走两步分页：第一步只取本页 id
        # （OFFSET 深翻页免回表/免 volumes JOIN/免 meta 大字段搬运，20 万行级库
        # OFFSET 24 万实测 7.1s → 0.1s），第二步按 id 取整行再按原顺序拼回。
        ids = [r["id"] for r in self.conn.execute(
            f"SELECT f.id FROM files f LEFT JOIN meta m ON m.file_id=f.id "
            f"WHERE {wsql} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()]
        rows: list[sqlite3.Row] = []
        if ids:
            marks = ",".join("?" * len(ids))
            by = {r["id"]: r for r in self.conn.execute(
                f"{BASE_SELECT} WHERE f.id IN ({marks})", ids).fetchall()}
            rows = [by[i] for i in ids if i in by]
        items = [_row_to_item(r, self._roots()) for r in rows]

        return {"total": total, "items": items, "limit": limit, "offset": offset}

    def get_file(self, file_id: int) -> dict[str, Any] | None:
        r = self.conn.execute(
            f"{BASE_SELECT} WHERE f.id = ?", (file_id,)).fetchone()
        if not r:
            return None
        item = _row_to_item(r, self._roots())
        item["tags"] = [
            {"name": t["name"], "grp": t["grp"], "source": t["source"]}
            for t in self.conn.execute(
                "SELECT t.name, t.grp, ft.source FROM file_tags ft "
                "JOIN tags t ON t.id=ft.tag_id WHERE ft.file_id=? ORDER BY t.grp, t.name",
                (file_id,))
        ]
        if r["meta_json"]:
            try:
                item["meta"] = json.loads(r["meta_json"])
            except ValueError:
                item["meta"] = {}
        return item

    # ---------- 维度聚合（侧栏用） ----------
    def topics(self, vol_id: int | None = None) -> list[dict[str, Any]]:
        """
        按国别/主题聚合。
        vol_id 不为空时只统计该资料库；不同库的主题概念完全不同（影视国别 vs 书籍题材），
        混在一起会没意义。

        性能：两轮 json_extract 全表 GROUP BY，进数据版本缓存。
        """
        return cache.cached(f"topics:{vol_id}", lambda: self._topics(vol_id))

    def _topics(self, vol_id: int | None = None) -> list[dict[str, Any]]:
        vfilter = "AND f.vol_id = ?" if vol_id is not None else ""
        params: list[Any] = [vol_id] if vol_id is not None else []
        rows = self.conn.execute(
            f"""
            SELECT json_extract(m.json, '$.topic') AS topic,
                   COUNT(*) AS n, SUM(f.size) AS s
            FROM files f JOIN meta m ON m.file_id=f.id
            WHERE json_extract(m.json,'$.topic') IS NOT NULL
              AND json_extract(m.json,'$.topic') <> '' {vfilter}
            GROUP BY topic ORDER BY n DESC
            """,
            params,
        ).fetchall()
        items = [{"topic": r["topic"], "n": r["n"], "size": r["s"], "cover": None}
                 for r in rows if r["topic"]]
        if not items:
            return items

        # 封面：每页只查一次，避免"每个主题跑一次全表"造成几十秒等待
        names = [i["topic"] for i in items]
        marks = ",".join("?" * len(names))
        cwhere = [f"json_extract(m2.json,'$.topic') IN ({marks})",
                  "f2.thumb_state=2", "f2.status='present'"]
        cparams: list[Any] = list(names)
        if vol_id is not None:
            cwhere.append("f2.vol_id = ?")
            cparams.append(vol_id)
        covers = {
            r["topic"]: r["cid"] for r in self.conn.execute(
                f"""
                SELECT json_extract(m2.json,'$.topic') AS topic, MAX(f2.id) AS cid
                FROM files f2 JOIN meta m2 ON m2.file_id=f2.id
                WHERE {' AND '.join(cwhere)}
                GROUP BY topic
                """, cparams)
        }
        for it in items:
            it["cover"] = covers.get(it["topic"])
        return items

    def series_list(self, kind: str | None = None, topic: str | None = None,
                    q: str = "", limit: int = 200, offset: int = 0,
                    vol_id: int | None = None) -> dict[str, Any]:
        """
        按系列聚合。

        注意排除两类噪音：
          * 书籍类（epub/mobi/txt/pdf…）的 meta 里没有"系列"概念，只有书名/作者，
            它们的 series 字段可能残留目录名，不应参与系列聚合
          * other 类（如 .sgf 棋谱）没有系列概念
        真正有系列概念的是影视、有声书、漫画等。

        性能：两轮 json_extract 全表聚合，进数据版本缓存。
        """
        return cache.cached(
            f"series:{vol_id}:{kind}:{topic}:{q}:{limit}:{offset}",
            lambda: self._series_list(kind, topic, q, limit, offset, vol_id))

    def _series_list(self, kind: str | None = None, topic: str | None = None,
                     q: str = "", limit: int = 200, offset: int = 0,
                     vol_id: int | None = None) -> dict[str, Any]:
        where = [
            "f.status='present'",
            "json_extract(m.json,'$.series') IS NOT NULL",
            "json_extract(m.json,'$.series') <> ''",
            "f.ext NOT IN ('sgf','txt','epub','mobi','azw','azw3','azw4','pdf','prc','pdb','fb2','lit','djvu','chm')",
            "f.kind NOT IN ('other', 'doc', 'subtitle', 'code', 'app')",
        ]
        params: list[Any] = []
        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)
        if kind:
            where.append("f.kind = ?")
            params.append(kind)
        if topic:
            where.append("json_extract(m.json,'$.topic') = ?")
            params.append(topic)
        if q:
            where.append("json_extract(m.json,'$.series') LIKE ?")
            params.append(f"%{q}%")
        wsql = " AND ".join(where)

        total = self.conn.execute(
            f"SELECT COUNT(*) n FROM ("
            f"  SELECT DISTINCT json_extract(m.json,'$.series') AS s "
            f"  FROM files f JOIN meta m ON m.file_id=f.id WHERE {wsql})",
            params).fetchone()["n"]

        # 先取本页的系列名，再单独查封面。
        # 原来用相关子查询（每行跑一次 GROUP BY），20 万文件要 30 秒。
        rows = self.conn.execute(
            f"""
            SELECT json_extract(m.json,'$.series') AS series,
                   COUNT(*) AS n, SUM(f.size) AS s, MAX(f.mtime) AS last
            FROM files f JOIN meta m ON m.file_id=f.id
            WHERE {wsql}
            GROUP BY series ORDER BY n DESC, last DESC
            LIMIT ? OFFSET ?
            """, params + [limit, offset]).fetchall()

        names = [r["series"] for r in rows if r["series"]]
        covers: dict[str, int] = {}
        if names:
            marks = ",".join("?" * len(names))
            cwhere = ["json_extract(m2.json,'$.series') IN (" + marks + ")",
                      "f2.thumb_state=2", "f2.status='present'"]
            cparams: list[Any] = list(names)
            if vol_id is not None:
                cwhere.append("f2.vol_id = ?")
                cparams.append(vol_id)
            for r in self.conn.execute(
                f"""
                SELECT json_extract(m2.json,'$.series') AS series, MAX(f2.id) AS cid
                FROM files f2 JOIN meta m2 ON m2.file_id=f2.id
                WHERE {' AND '.join(cwhere)}
                GROUP BY series
                """, cparams):
                covers[r["series"]] = r["cid"]

        return {
            "total": total,
            "items": [{"series": r["series"], "n": r["n"], "size": r["s"],
                       "last": r["last"], "cover": covers.get(r["series"])}
                      for r in rows],
        }

    def duplicates(self, vol_id: int | None = None,
                   cross: bool = False, min_size: int = 64 * 1024,
                   limit: int = 2000) -> list[dict[str, Any]]:
        """
        基于 full hash 的重复组。min_size 与 run_dedup 的 64KB 保持一致，
        否则分析阶段算出来的小文件重复会在展示时被过滤掉。
        cross=True：只看分属不同资料库的重复（跨库冗余清理用），忽略 vol_id。
        limit 只是展示上限（防止极端库把弹窗撑爆）；duplicates_total 返回真实组数。
        """
        vf = "" if cross else (" AND vol_id = ?" if vol_id is not None else "")
        vp: list[Any] = [] if cross else ([vol_id] if vol_id is not None else [])
        rows = self.conn.execute(
            f"""
            SELECT hash_full, COUNT(*) n, SUM(size) s,
                   COUNT(DISTINCT vol_id) nv
            FROM files WHERE hash_full IS NOT NULL AND size >= ? {vf}
              AND status = 'present'
            GROUP BY hash_full HAVING n > 1
            {"AND nv > 1" if cross else ""}
            ORDER BY s DESC LIMIT ?
            """, [min_size] + vp + [limit]).fetchall()
        out = []
        for r in rows:
            members = self.conn.execute(
                f"{BASE_SELECT} WHERE f.hash_full = ? AND f.status='present' ORDER BY f.id",
                (r["hash_full"],)).fetchall()
            out.append({
                "hash": r["hash_full"], "n": r["n"], "size": r["s"],
                "wasted": r["s"] - members[0]["size"],
                "items": [_row_to_item(m, self._roots()) for m in members],
            })
        return out

    def duplicates_total(self, vol_id: int | None = None,
                         cross: bool = False, min_size: int = 64 * 1024) -> int:
        """重复组总数（不受展示 limit 影响），让用户知道"是否查完了"。"""
        vf = "" if cross else (" AND vol_id = ?" if vol_id is not None else "")
        vp: list[Any] = [] if cross else ([vol_id] if vol_id is not None else [])
        r = self.conn.execute(
            f"""
            SELECT COUNT(*) n FROM (
              SELECT hash_full FROM files
              WHERE hash_full IS NOT NULL AND size >= ? {vf} AND status = 'present'
              GROUP BY hash_full HAVING COUNT(*) > 1
              {"AND COUNT(DISTINCT vol_id) > 1" if cross else ""}
            )""", [min_size] + vp).fetchone()
        return int(r["n"])

    def junk_candidates(self, vol_id: int | None = None) -> dict[str, Any]:
        """.bc! 未完成下载 / .torrent 种子 / 0 字节文件 —— 常见可清理项。"""
        vf = " AND vol_id = ?" if vol_id is not None else ""
        vp: list[Any] = [vol_id] if vol_id is not None else []

        def grab(where: str, label: str) -> dict[str, Any]:
            r = self.conn.execute(
                f"SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM files "
                f"WHERE {where}{vf}", vp
            ).fetchone()
            return {"label": label, "n": r["n"], "size": r["s"]}

        return {
            "unfinished": grab("ext = 'bc!'", "未完成的 BT 下载（.bc!）"),
            "torrent": grab("ext = 'torrent'", "种子文件（.torrent）"),
            "zero": grab("size = 0", "0 字节空文件"),
            "part": grab("ext IN ('part','partial','crdownload')", "下载临时文件"),
        }

    # ---------- 推荐 / 统计扩展 ----------

    def publishers(self, vol_id: int | None = None, q: str = "",
                   limit: int = 200) -> dict[str, Any]:
        """按出版社聚合（来自 EPUB/PDF 元数据）。进数据版本缓存。"""
        return cache.cached(f"pubs:{vol_id}:{q}:{limit}",
                            lambda: self._publishers(vol_id, q, limit))

    def _publishers(self, vol_id: int | None = None, q: str = "",
                    limit: int = 200) -> dict[str, Any]:
        where = ["json_extract(m.json,'$.publisher') IS NOT NULL",
                 "json_extract(m.json,'$.publisher') <> ''"]
        params: list[Any] = []
        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)
        if q:
            where.append("json_extract(m.json,'$.publisher') LIKE ?")
            params.append(f"%{q}%")
        sql = ("SELECT json_extract(m.json,'$.publisher') AS publisher, COUNT(*) n "
               "FROM files f JOIN meta m ON m.file_id=f.id "
               f"WHERE {' AND '.join(where)} GROUP BY publisher ORDER BY n DESC LIMIT ?")
        rows = self.conn.execute(sql, params + [limit]).fetchall()
        return {"items": [{"name": r["publisher"], "n": r["n"]} for r in rows]}

    def recent_files(self, vol_id: int | None = None, limit: int = 12) -> list[dict]:
        """最近添加（按入库时间倒序），首页「继续看」用。进数据版本缓存。"""
        return cache.cached(f"recent:{vol_id}:{limit}",
                            lambda: self._recent_files(vol_id, limit))

    def _recent_files(self, vol_id: int | None = None, limit: int = 12) -> list[dict]:
        where = ["f.status='present'"]
        params: list[Any] = []
        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)
        rows = self.conn.execute(
            f"{BASE_SELECT} WHERE {' AND '.join(where)} ORDER BY f.added_at DESC, f.id DESC LIMIT ?",
            params + [limit]).fetchall()
        return [_row_to_item(r, self._roots()) for r in rows]

    def continue_watching(self, uid: int, vol_id: int | None = None,
                          limit: int = 12) -> list[dict]:
        """最近播放/阅读过的内容（含进度）。"""
        where = ["f.status='present'"]
        params: list[Any] = [uid]
        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)
        rows = self.conn.execute(
            f"""
            SELECT f.id, f.vol_id, f.rel_dir, f.name, f.ext, f.kind, f.size, f.mtime,
                   f.status, f.thumb_state, m.json AS meta_json, um.marks AS user_mark,
                   p.pos, p.dur, p.ts AS p_ts
            FROM media_progress p
            JOIN files f ON f.id = p.file_id
            LEFT JOIN meta m ON m.file_id = f.id
            LEFT JOIN (SELECT file_id, GROUP_CONCAT(mark) AS marks
                       FROM user_marks GROUP BY file_id) um ON um.file_id = f.id
            WHERE p.uid = ? AND {' AND '.join(where)}
            ORDER BY p.ts DESC LIMIT ?
            """, params + [limit]).fetchall()
        out = []
        for r in rows:
            it = _row_to_item(r, self._roots())
            it["pos"] = round(r["pos"] or 0, 1)
            it["dur"] = round(r["dur"] or 0, 1)
            it["progress"] = round((r["pos"] / r["dur"]) * 100, 1) if r["dur"] else 0
            out.append(it)
        return out

    def save_media_progress(self, uid: int, file_id: int, pos: float, dur: float) -> None:
        c = self.rw
        c.execute(
            "INSERT INTO media_progress(uid, file_id, pos, dur, ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(uid, file_id) DO UPDATE SET pos=excluded.pos, dur=excluded.dur, ts=excluded.ts",
            (uid, file_id, float(pos), float(dur), int(time.time())))
        c.commit()

    def get_media_progress(self, uid: int, file_id: int) -> dict | None:
        r = self.conn.execute(
            "SELECT pos, dur, ts FROM media_progress WHERE uid=? AND file_id=?",
            (uid, file_id)).fetchone()
        return dict(r) if r else None

    def continue_delete(self, uid: int, file_ids: list[int]) -> int:
        """删除指定文件的播放/阅读进度记录（只删当前用户自己的记录）。"""
        ids = [int(x) for x in (file_ids or []) if int(x) > 0][:500]
        if not ids:
            return 0
        q = ",".join("?" for _ in ids)
        cur = self.rw.execute(
            f"DELETE FROM media_progress WHERE uid=? AND file_id IN ({q})",
            [uid] + ids)
        self.rw.commit()
        return cur.rowcount

    def continue_clear(self, uid: int, vol_id: int | None = None) -> int:
        """清空当前用户的进度记录；vol_id 给定时只清该库的。"""
        if vol_id is None:
            cur = self.rw.execute(
                "DELETE FROM media_progress WHERE uid=?", (uid,))
        else:
            cur = self.rw.execute(
                "DELETE FROM media_progress WHERE uid=? AND file_id IN "
                "(SELECT id FROM files WHERE vol_id=?)", (uid, vol_id))
        self.rw.commit()
        return cur.rowcount

    def random_files(self, vol_id: int | None = None, limit: int = 60) -> list[dict]:
        """随机挑一批文件（当前库或全部库）。"""
        where = ["f.status='present'"]
        params: list[Any] = []
        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)
        rows = self.conn.execute(
            f"{BASE_SELECT} WHERE {' AND '.join(where)} ORDER BY RANDOM() LIMIT ?",
            params + [limit]).fetchall()
        return [_row_to_item(r, self._roots()) for r in rows]

    # ---------- 今日推荐 ----------

    def daily_pick(self, vol_id: int | None = None) -> dict | None:
        """今日推荐：以日期做种子的确定性随机，同一天内怎么刷都不变。

        只从电子书 / 文档里挑（这两类点开就能读）。摘录优先取全文索引，
        其次直接读 txt/md 文件头，都没有就不带摘录。
        """
        where = ["f.status='present'", "f.kind IN ('book','doc')"]
        params: list[Any] = []
        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)
        rows = self.conn.execute(
            f"SELECT f.id FROM files f WHERE {' AND '.join(where)}", params).fetchall()
        if not rows:
            return None
        rng = random.Random(f"daily:{time.strftime('%Y%m%d')}:{vol_id or ''}")
        fid = rng.choice([r["id"] for r in rows])
        row = self.conn.execute(f"{BASE_SELECT} WHERE f.id = ?", (fid,)).fetchone()
        if not row:
            return None
        it = _row_to_item(row, self._roots())
        it["excerpt"] = self._excerpt_for(it)
        return it

    def _excerpt_for(self, item: dict) -> str:
        """推荐卡片的一句话摘录。"""
        try:
            r = self.conn.execute(
                "SELECT body FROM file_text WHERE file_id = ?",
                (int(item.get("id") or 0),)).fetchone()
            if r and (r["body"] or "").strip():
                return re.sub(r"\s+", " ", r["body"].strip())[:160]
        except (sqlite3.Error, TypeError, ValueError):
            pass
        path = item.get("path") or ""
        if (item.get("ext") or "").lower() in ("txt", "md") and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    return re.sub(r"\s+", " ", fh.read(4096).strip())[:160]
            except OSError:
                pass
        return ""

    def ensure_media_duration(self, item: dict) -> float:
        """
        补齐媒体真实时长（秒），结果就地写入 item["duration"]。
        转码流（fMP4 管道）本身不携带总时长，进度条全靠这个值。
        优先 meta 表已有值（补封面时 ffprobe 写入）；视频/音频缺失时惰性探测一次并
        持久化回 meta——此后每次打开零开销。探测失败记入进程内负缓存，不反复重试。
        """
        if item.get("kind") not in ("video", "audio"):
            return 0.0
        try:
            have = float(item.get("duration") or 0)
        except (TypeError, ValueError):
            have = 0.0
        if have > 0:
            return have
        fid = int(item.get("id") or 0)
        if not fid or fid in _DUR_FAIL:
            return 0.0
        path = item.get("path") or ""
        if not path or not os.path.isfile(path):
            _DUR_FAIL.add(fid)
            return 0.0
        from .indexer import find_ffprobe, find_ffmpeg, _probe
        dur = 0.0
        fp = find_ffprobe()
        for attempt in (0, 1):   # 新文件可能被杀软短锁，失败立即重试一次
            if fp:
                try:
                    dur = float(_probe(path, fp).get("duration") or 0)
                except (TypeError, ValueError):
                    dur = 0.0
            if dur <= 0:
                ff = find_ffmpeg()
                if ff:
                    dur = _ffmpeg_duration(ff, path)
            if dur > 0:
                break
            if attempt == 0:
                time.sleep(0.3)
        if dur <= 0:
            _DUR_FAIL.add(fid)
            return 0.0
        meta = item.get("meta") or {}
        meta["duration"] = round(dur, 1)
        c = self.rw
        c.execute("INSERT OR REPLACE INTO meta(file_id, json) VALUES(?,?)",
                  (fid, json.dumps(meta, ensure_ascii=False)))
        c.commit()
        cache.bump()
        item["meta"] = meta
        item["duration"] = meta["duration"]
        return dur

    def save_read_progress(self, uid: int, file_id: int, page: int, total: int) -> None:
        c = self.rw
        c.execute(
            "INSERT INTO reading_progress(uid, file_id, page, total, ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(uid, file_id) DO UPDATE SET page=excluded.page, "
            "total=excluded.total, ts=excluded.ts",
            (uid, file_id, int(page), int(total), int(time.time())))
        c.commit()

    def get_read_progress(self, uid: int, file_id: int) -> dict | None:
        r = self.conn.execute(
            "SELECT page, total, ts FROM reading_progress WHERE uid=? AND file_id=?",
            (uid, file_id)).fetchone()
        return dict(r) if r else None

    # ---------- 阅读统计（打卡热力图） ----------

    def log_read(self, uid: int, file_id: int, secs: float) -> None:
        """追加一条阅读心跳。secs <= 0 忽略。"""
        try:
            secs = float(secs)
        except (TypeError, ValueError):
            return
        if secs <= 0:
            return
        lt = time.localtime()
        self.rw.execute(
            "INSERT INTO read_log(uid, file_id, day, secs, ts) VALUES(?,?,?,?,?)",
            (uid, int(file_id or 0), time.strftime("%Y-%m-%d", lt),
             round(min(secs, 86400), 1), int(time.time())))
        self.rw.commit()

    def read_stats(self, uid: int) -> dict:
        """当前用户的阅读统计：逐日时长、汇总、连续打卡天数。"""
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 400 * 86400))
        rows = self.conn.execute(
            "SELECT day, SUM(secs) AS s, COUNT(DISTINCT file_id) AS f "
            "FROM read_log WHERE uid=? AND day >= ? GROUP BY day ORDER BY day",
            (uid, cutoff)).fetchall()
        by_day = {r["day"]: (round(r["s"] or 0, 1), r["f"] or 0) for r in rows}
        days = [{"d": d, "secs": s, "n": f} for d, (s, f) in sorted(by_day.items())]
        total_secs = round(sum(s for s, _ in by_day.values()), 1)
        books = self.conn.execute(
            "SELECT COUNT(DISTINCT file_id) AS n FROM read_log WHERE uid=? AND file_id>0",
            (uid,)).fetchone()["n"]
        today = time.strftime("%Y-%m-%d")
        # 连续打卡：今天还没读就从昨天起算（不把 streak 打断在早上）
        def _ago(n: int) -> str:
            return time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))
        streak = 0
        d = 0 if today in by_day else 1
        while by_day.get(_ago(d), (0, 0))[0] > 0:
            streak += 1
            d += 1
        return {
            "days": days,
            "today": by_day.get(today, (0, 0))[0],
            "streak": streak,
            "totals": {"secs": total_secs, "days": len(by_day), "books": books},
        }

    def mark_map(self, ids: list[int]) -> dict[int, list[str]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        out: dict[int, list[str]] = {}
        for r in self.conn.execute(
            f"SELECT file_id, GROUP_CONCAT(mark) AS marks FROM user_marks "
            f"WHERE file_id IN ({marks}) GROUP BY file_id", ids):
            out[r["file_id"]] = [x for x in (r["marks"] or "").split(",") if x]
        return out

    def tag_map(self, ids: list[int]) -> dict[int, list[str]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        out: dict[int, list[str]] = {}
        for r in self.conn.execute(
                f"SELECT ft.file_id, t.name FROM file_tags ft JOIN tags t ON t.id=ft.tag_id "
                f"WHERE ft.file_id IN ({marks}) ORDER BY t.name", ids):
            out.setdefault(r["file_id"], []).append(r["name"])
        return out

    def volume(self, vol_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM volumes WHERE id=?", (vol_id,)).fetchone()
        return dict(r) if r else None

    def volumes(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM volumes ORDER BY id")]

    def set_book_meta(self, file_id: int, title: str, author: str, publisher: str = "") -> None:
        r = self.conn.execute("SELECT json FROM meta WHERE file_id=?", (file_id,)).fetchone()
        base = {}
        if r:
            try:
                base = json.loads(r["json"])
            except ValueError:
                base = {}
        if title:
            base["title"] = title
        if author:
            base["author"] = author
        if publisher:
            base["publisher"] = publisher
        c = self.rw
        c.execute("INSERT OR REPLACE INTO meta(file_id, json) VALUES(?,?)",
                  (file_id, json.dumps(base, ensure_ascii=False)))
        c.commit()
        cache.bump()

    def tag_rename(self, old: str, new: str) -> bool:
        c = self.rw
        row = c.execute("SELECT id FROM tags WHERE name=?", (old,)).fetchone()
        if not row:
            return False
        dup = c.execute("SELECT id FROM tags WHERE name=?", (new,)).fetchone()
        if dup:
            c.execute("UPDATE OR IGNORE file_tags SET tag_id=? WHERE tag_id=?",
                      (dup["id"], row["id"]))
            c.execute("DELETE FROM file_tags WHERE tag_id=?", (row["id"],))
            c.execute("DELETE FROM tags WHERE id=?", (row["id"],))
        else:
            c.execute("UPDATE tags SET name=? WHERE id=?", (new, row["id"]))
        c.commit()
        cache.bump()
        return True

    def tag_delete(self, name: str) -> bool:
        c = self.rw
        row = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        c.execute("DELETE FROM file_tags WHERE tag_id=?", (row["id"],))
        c.execute("DELETE FROM tags WHERE id=?", (row["id"],))
        c.commit()
        cache.bump()
        return True

    def tag_create(self, name: str, color: str = "") -> dict:
        c = self.rw
        c.execute("INSERT OR IGNORE INTO tags(name, grp, color) VALUES(?, 'user', ?)",
                  (name, color or None))
        c.commit()
        cache.bump()
        n = c.execute("SELECT COUNT(DISTINCT file_id) n FROM file_tags ft "
                      "JOIN tags t ON t.id=ft.tag_id WHERE t.name=?", (name,)).fetchone()["n"]
        return {"name": name, "n": n, "user": True, "color": color}

    def tag_bulk(self, ids: list[int], add: list[str] | None = None,
                 remove: list[str] | None = None) -> int:
        c = self.rw
        n = 0
        for fid in ids:
            if add:
                for name in add:
                    c.execute("INSERT OR IGNORE INTO tags(name, grp) VALUES(?, 'user')", (name,))
                    c.execute(
                        "INSERT OR IGNORE INTO file_tags(file_id, tag_id, source) "
                        "SELECT ?, id, 'user' FROM tags WHERE name=?", (fid, name))
                    n += 1
            if remove:
                for name in remove:
                    c.execute(
                        "DELETE FROM file_tags WHERE file_id=? AND tag_id IN "
                        "(SELECT id FROM tags WHERE name=?)", (fid, name))
                    n += 1
        c.commit()
        cache.bump()
        return n

    def dir_stats(self, vol_id: int | None = None, rel: str = "") -> dict:
        """某目录下按类型统计（点进目录时顶部显示"这里有什么"）。"""
        base = rel if rel == "" or rel.endswith("/") else rel + "/"
        where = ["f.rel_dir LIKE ?", "f.status='present'"]
        params: list[Any] = [base + "%"]
        if vol_id is not None:
            where.append("f.vol_id = ?")
            params.append(vol_id)
        rows = self.conn.execute(
            f"SELECT kind, COUNT(*) n, SUM(size) s FROM files f "
            f"WHERE {' AND '.join(where)} GROUP BY kind ORDER BY n DESC", params).fetchall()
        return {"by_kind": [{"kind": r["kind"], "label": KIND_LABEL.get(r["kind"], r["kind"]),
                             "n": r["n"], "size": r["s"]} for r in rows]}
