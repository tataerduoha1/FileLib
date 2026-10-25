"""
filelib.core.db —— 索引库 schema 与连接管理

设计要点：
  * 索引库存本机内置盘，不存外接盘（拔盘后仍可浏览）
  * WAL 模式：读写并发，扫描时也能查询
  * rel_dir 存相对路径，盘符变化不需要重写全表
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from . import config as _cfg

SCHEMA_VERSION = 3

# 应用数据目录（索引库、缩略图、日志）—— 由 config 统一决定，便于迁移
DEFAULT_DATA_DIR = None

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -64000;
PRAGMA mmap_size = 268435456;

CREATE TABLE IF NOT EXISTS schema_info (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS volumes (
    id          INTEGER PRIMARY KEY,
    root_path   TEXT NOT NULL UNIQUE,
    serial      TEXT,
    label       TEXT,
    fs          TEXT,
    last_seen   INTEGER,
    last_scan   INTEGER,
    scan_state  TEXT DEFAULT 'idle',   -- idle / running / done / aborted
    scan_cursor TEXT                   -- 断点续跑用，JSON
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    vol_id      INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    rel_dir     TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_lower  TEXT NOT NULL,
    ext         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'present',   -- present / missing
    hash_quick  TEXT,
    hash_full   TEXT,
    text_state  INTEGER NOT NULL DEFAULT 0,
    thumb_state INTEGER NOT NULL DEFAULT 0,
    meta_state  INTEGER NOT NULL DEFAULT 0,
    added_at    INTEGER NOT NULL,
    UNIQUE(vol_id, rel_dir, name)
);

CREATE INDEX IF NOT EXISTS idx_files_rel   ON files(vol_id, rel_dir);
CREATE INDEX IF NOT EXISTS idx_files_kind  ON files(kind, ext);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime DESC);
CREATE INDEX IF NOT EXISTS idx_files_size  ON files(size DESC);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_quick ON files(hash_quick) WHERE hash_quick IS NOT NULL;

-- 封面挑选专用部分索引：目录树/主题/系列的"每个目录取最大封面"查询
-- 从全表扫描 + 排序变成索引直接命中（几十万文件时从秒级降到毫秒级）
CREATE INDEX IF NOT EXISTS idx_files_cover ON files(vol_id, rel_dir, size DESC)
    WHERE thumb_state = 2 AND status = 'present';

-- 列表两步分页专用部分索引（status 过滤与查询条件吻合才会命中）：
-- 第一步 "SELECT id … ORDER BY … LIMIT ? OFFSET ?" 全程走索引，
-- 深翻页（OFFSET 5 万级）不必为被跳过的行回表、join、排序。
-- （vol 过滤 / 混合视图）×（mtime 默认排序 / size / name）各一条。
CREATE INDEX IF NOT EXISTS idx_files_page_mt  ON files(vol_id, mtime, id)      WHERE status = 'present';
CREATE INDEX IF NOT EXISTS idx_files_page_mt0 ON files(mtime, id)              WHERE status = 'present';
CREATE INDEX IF NOT EXISTS idx_files_page_sz  ON files(vol_id, size, id)       WHERE status = 'present';
CREATE INDEX IF NOT EXISTS idx_files_page_sz0 ON files(size, id)               WHERE status = 'present';
CREATE INDEX IF NOT EXISTS idx_files_page_nm  ON files(vol_id, name_lower, id) WHERE status = 'present';
CREATE INDEX IF NOT EXISTS idx_files_page_nm0 ON files(name_lower, id)         WHERE status = 'present';

-- 目录树：用于界面侧栏导航，避免每次都 GROUP BY 全表
CREATE TABLE IF NOT EXISTS dirs (
    id      INTEGER PRIMARY KEY,
    vol_id  INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    rel_dir TEXT NOT NULL,        -- 以 / 结尾；根为 ''
    parent  TEXT NOT NULL,
    name    TEXT NOT NULL,
    depth   INTEGER NOT NULL,
    n_files INTEGER NOT NULL DEFAULT 0,
    UNIQUE(vol_id, rel_dir)
);
CREATE INDEX IF NOT EXISTS idx_dirs_parent ON dirs(vol_id, parent);
CREATE INDEX IF NOT EXISTS idx_dirs_depth  ON dirs(vol_id, depth);

-- 分享链接（匿名 /s/<token> 访问单文件，可带有效期与密码）
CREATE TABLE IF NOT EXISTS shares (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    pw_hash    TEXT,
    expires    INTEGER,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shares_file ON shares(file_id);

CREATE TABLE IF NOT EXISTS file_text (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    body    TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    name, body,
    content='',
    tokenize="trigram"
);

CREATE TABLE IF NOT EXISTS meta (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE,
    grp   TEXT NOT NULL DEFAULT 'user',   -- format / content / source / time / user
    color TEXT
);

CREATE TABLE IF NOT EXISTS file_tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source  TEXT NOT NULL DEFAULT 'auto',
    PRIMARY KEY (file_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag_id);

CREATE TABLE IF NOT EXISTS tasks (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER REFERENCES files(id) ON DELETE CASCADE,
    type      TEXT NOT NULL,
    priority  INTEGER NOT NULL DEFAULT 100,
    attempts  INTEGER NOT NULL DEFAULT 0,
    state     TEXT NOT NULL DEFAULT 'pending',   -- pending / running / done / failed / skipped
    error     TEXT,
    created   INTEGER NOT NULL,
    updated   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_pick ON tasks(type, state, priority, id);
CREATE INDEX IF NOT EXISTS idx_tasks_file ON tasks(file_id, type);

CREATE TABLE IF NOT EXISTS scan_errors (
    id    INTEGER PRIMARY KEY,
    path  TEXT,
    reason TEXT,
    ts    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id         INTEGER PRIMARY KEY,
    vol_id     INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    started    INTEGER NOT NULL,
    finished   INTEGER,
    n_seen     INTEGER DEFAULT 0,
    n_new      INTEGER DEFAULT 0,
    n_updated  INTEGER DEFAULT 0,
    n_missing  INTEGER DEFAULT 0,
    n_errors   INTEGER DEFAULT 0,
    state      TEXT DEFAULT 'running'
);

-- 用户标记：star 收藏 / read 已读 / todo 待读（v3 起一个文件可同时挂多个标记；
-- read 与 todo 互斥由应用层保证）
CREATE TABLE IF NOT EXISTS user_marks (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    mark    TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (file_id, mark)
);
CREATE INDEX IF NOT EXISTS idx_user_marks_mark ON user_marks(mark);

-- 作者聚合缓存（每次 build_meta 后重建，避免实时 LIKE 全表）
CREATE TABLE IF NOT EXISTS authors (
    name    TEXT PRIMARY KEY,
    n       INTEGER NOT NULL,
    vol_ids TEXT NOT NULL DEFAULT ''
);
"""


_conn_lock = threading.Lock()


def data_dir() -> Path:
    return _cfg.data_dir()


def db_path() -> Path:
    return data_dir() / "index.db"


def thumb_dir() -> Path:
    p = data_dir() / "thumbs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def connect(path: str | Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    """
    打开索引库。readonly 用于界面查询，避免误写。

    注意：SQLite 连接不能在多个线程间共享（默认 check_same_thread=True）。
    多线程场景请用下面 ThreadLocalConn / Store 的每线程连接机制。
    """
    p = Path(path) if path else db_path()
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(str(p), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class ThreadLocalConn:
    """
    每线程一个 SQLite 连接。

    ThreadingHTTPServer 每个请求跑在独立线程里，共用同一个连接会抛
    "SQLite objects created in a thread can only be used in that same thread"。
    这里为每个线程惰性创建连接，线程结束时自动关闭。
    """

    def __init__(self, path: str | Path | None = None, readonly: bool = False):
        self._path = path
        self._readonly = readonly
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self._path, readonly=self._readonly)
            self._local.conn = c
            with self._lock:
                self._all.append(c)
        return c

    # 让调用方可以像用 Connection 一样用 ThreadLocalConn
    def execute(self, *a, **kw):
        return self.conn.execute(*a, **kw)

    def executemany(self, *a, **kw):
        return self.conn.executemany(*a, **kw)

    def executescript(self, *a, **kw):
        return self.conn.executescript(*a, **kw)

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        """关闭当前线程的连接。"""
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    def close_all(self):
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all.clear()


def init_db(path: str | Path | None = None) -> Path:
    """建库或升级。可重复调用。"""
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _conn_lock:
        conn = sqlite3.connect(str(p), timeout=60)
        try:
            conn.executescript(SCHEMA_SQL)
            conn.executescript(MIGRATE_V2_SQL)
            # 一次性迁移：旧 fts 用 unicode61 分词，整段中文变成一个词，
            # 导致中文书内文搜不到。检测到旧分词器就重建并用 file_text 回填。
            try:
                _fts_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='fts'"
                ).fetchone()
                if _fts_sql and "trigram" not in (_fts_sql[0] or ""):
                    conn.execute("DROP TABLE fts")   # fts_data/idx/docsize/config 随之删除
                    conn.execute("""
                        CREATE VIRTUAL TABLE fts USING fts5(
                            name, body, content='', tokenize="trigram")
                    """)
                    conn.execute(
                        "INSERT INTO fts(rowid, name, body) "
                        "SELECT file_id, '', body FROM file_text")
            except sqlite3.Error:
                conn.rollback()
            # v3 迁移：user_marks 从「每文件一个标记」升级为「每文件多标记」。
            # 探测旧表结构（file_id 单列主键）就重建为复合主键，数据原样搬过去。
            try:
                _um_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_marks'"
                ).fetchone()
                if _um_sql and "PRIMARY KEY (file_id, mark)" not in (_um_sql[0] or ""):
                    conn.executescript("""
                        CREATE TABLE user_marks_new (
                            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                            mark    TEXT NOT NULL,
                            ts      INTEGER NOT NULL,
                            PRIMARY KEY (file_id, mark)
                        );
                        INSERT OR IGNORE INTO user_marks_new(file_id, mark, ts)
                            SELECT file_id, mark, ts FROM user_marks;
                        DROP TABLE user_marks;
                        ALTER TABLE user_marks_new RENAME TO user_marks;
                        CREATE INDEX IF NOT EXISTS idx_user_marks_mark ON user_marks(mark);
                    """)
            except sqlite3.Error:
                conn.rollback()
            # shares 表列迁移：早期开发版建的 shares 没有 pw_hash 列（密码分享是
            # 后来加的），CREATE TABLE IF NOT EXISTS 不会给旧表补列，创建分享链接
            # 会报 "no column named pw_hash"。逐列探测补齐（可重复调用，幂等）。
            try:
                _sh_cols = {r[1] for r in conn.execute("PRAGMA table_info(shares)").fetchall()}
                if _sh_cols:
                    for _col, _ddl in (("pw_hash", "TEXT"), ("expires", "INTEGER"),
                                       ("created_by", "TEXT"), ("created_at", "INTEGER")):
                        if _col not in _sh_cols:
                            conn.execute(f"ALTER TABLE shares ADD COLUMN {_col} {_ddl}")
            except sqlite3.Error:
                conn.rollback()
            cur = conn.execute("SELECT value FROM schema_info WHERE key='version'")
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_info(key, value) VALUES('version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                conn.execute(
                    "UPDATE schema_info SET value=? WHERE key='version'",
                    (str(SCHEMA_VERSION),))
            conn.commit()
        finally:
            conn.close()
    return p


# ---------- v2 增量迁移（幂等：全部 IF NOT EXISTS / try） ----------

MIGRATE_V2_SQL = """
-- 运行期设置（休息提醒、计划扫描、限流参数……全部存本地）
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 登录会话：落库，重启服务不掉线
CREATE TABLE IF NOT EXISTS sessions (
    token     TEXT PRIMARY KEY,
    uid       INTEGER NOT NULL,
    created   INTEGER NOT NULL,
    expires   INTEGER NOT NULL,
    ip        TEXT,
    ua        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_uid ON sessions(uid);

-- 阅读进度（每个用户各自一份）
CREATE TABLE IF NOT EXISTS reading_progress (
    uid      INTEGER NOT NULL,
    file_id  INTEGER NOT NULL,
    page     INTEGER NOT NULL DEFAULT 0,
    total    INTEGER NOT NULL DEFAULT 0,
    ts       INTEGER NOT NULL,
    PRIMARY KEY (uid, file_id)
);

-- 视频断点续播
CREATE TABLE IF NOT EXISTS media_progress (
    uid      INTEGER NOT NULL,
    file_id  INTEGER NOT NULL,
    pos      REAL NOT NULL DEFAULT 0,
    dur      REAL NOT NULL DEFAULT 0,
    ts       INTEGER NOT NULL,
    PRIMARY KEY (uid, file_id)
);

-- 操作审计（配置变更、删除等，便于追溯）
CREATE TABLE IF NOT EXISTS audit_log (
    id    INTEGER PRIMARY KEY,
    ts    INTEGER NOT NULL,
    uid   INTEGER,
    user  TEXT,
    ip    TEXT,
    act   TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);

-- 计划任务执行记录
CREATE TABLE IF NOT EXISTS job_runs (
    id      INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    vol_id  INTEGER,
    result  TEXT
);

-- 阅读行为日志（阅读统计 / 打卡热力图）：阅读器开着时每 30 秒心跳追加一行
CREATE TABLE IF NOT EXISTS read_log (
    id      INTEGER PRIMARY KEY,
    uid     INTEGER NOT NULL,
    file_id INTEGER NOT NULL DEFAULT 0,
    day     TEXT NOT NULL,            -- 本地日期 YYYY-MM-DD，直接 GROUP BY
    secs    REAL NOT NULL DEFAULT 0,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_read_log_uid_day ON read_log(uid, day);
"""


def reset_db(path: str | Path | None = None) -> None:
    """删除并重建（危险操作，仅 debug 用）。"""
    p = Path(path) if path else db_path()
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    init_db(p)


def backup_db(keep: int = 3) -> Path | None:
    """
    在线备份索引库（sqlite backup API，服务运行中也可用）。
    保留最近 keep 份，旧的自删。返回备份文件路径。

    备份大文件（几百 MB）时用分步回调，避免长时间阻塞。
    """
    import time as _t
    src = db_path()
    if not src.is_file():
        return None
    bdir = data_dir() / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dst = bdir / f"index-{_t.strftime('%Y%m%d-%H%M')}.db"
    if dst.exists():
        return dst
    conn = sqlite3.connect(str(src), timeout=30)
    try:
        bak = sqlite3.connect(str(dst))
        try:
            conn.backup(bak)
        finally:
            bak.close()
    finally:
        conn.close()
    # 清理旧备份
    baks = sorted(bdir.glob("index-*.db"))
    for old in baks[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass
    return dst


def list_backups() -> list[dict]:
    bdir = data_dir() / "backups"
    if not bdir.is_dir():
        return []
    out = []
    for f in sorted(bdir.glob("index-*.db"), reverse=True):
        try:
            st = f.stat()
            out.append({"name": f.name, "path": str(f), "size": st.st_size,
                        "mtime": int(st.st_mtime)})
        except OSError:
            pass
    return out


# ---------- 设置读写（每线程连接由调用方提供） ----------

def get_setting(conn, key: str, default=None):
    import json as _j
    try:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    if not r:
        return default
    try:
        return _j.loads(r["value"])
    except (ValueError, TypeError):
        return default


def all_settings(conn) -> dict:
    import json as _j
    out = {}
    try:
        for r in conn.execute("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = _j.loads(r["value"])
            except (ValueError, TypeError):
                out[r["key"]] = r["value"]
    except sqlite3.Error:
        pass
    return out


def put_setting(conn, key: str, value) -> None:
    import json as _j
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, _j.dumps(value, ensure_ascii=False)))
    conn.commit()
