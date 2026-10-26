"""
filelib/core/users.py —— 用户 / 角色 / 权限 / 会话

权限模型（三层）：
  1. 角色 role      : 'admin' 最高权限（唯一可写）、'user' 只读
  2. 资料库 perms   : 允许查看的 vol_id 列表；不在列表里的库对该用户完全隐藏
  3. 导航项 perms   : 在"有权限的库"里，还限制能看到哪些导航项
                     （总览/按主题/按作者/按系列/目录树/收藏/已读/待读/标签/大文件/重复/清理/全文搜索…）

  存储形式：user_perms(uid, vol_id, nav) —— nav 为 '*' 表示整库全开，
  否则为单个导航项 key。这样"某个库只给看视频目录"也能表达。

密码：
  * 登录校验用 PBKDF2-HMAC-SHA256（每个用户独立随机盐 + 12 万轮）
  * 同时用本机密钥（data_dir/secret.key）做 AES 风格可逆保存，供 admin 查看
    —— 这是用户明确要求的"查看用户密码"功能，密钥只在本机，拷走 index.db 也读不出
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from .db import data_dir

# ---------- 密钥 ----------

def _key() -> bytes:
    p = Path(data_dir()) / "secret.key"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return p.read_bytes()


def _keystream(nonce: bytes, n: int) -> bytes:
    key = _key()
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:n])


def encrypt_pw(pw: str) -> str:
    import base64
    nonce = os.urandom(12)
    raw = pw.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(raw, _keystream(nonce, len(raw))))
    mac = hmac.new(_key(), nonce + ct, hashlib.sha256).digest()[:16]
    return base64.b64encode(nonce + mac + ct).decode("ascii")


def decrypt_pw(enc: str) -> str:
    import base64
    try:
        data = base64.b64decode(enc)
    except Exception:
        return ""
    nonce, mac, ct = data[:12], data[12:28], data[28:]
    if hmac.new(_key(), nonce + ct, hashlib.sha256).digest()[:16] != mac:
        return "（密钥不匹配，无法解密）"
    return bytes(a ^ b for a, b in zip(ct, _keystream(nonce, len(ct)))).decode("utf-8", "replace")


def hash_pw(pw: str, salt: str | None = None) -> str:
    """PBKDF2 单向哈希，格式 pbkdf2$sha256$轮数$盐$摘要。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120_000)
    return f"pbkdf2$sha256$120000${salt}${dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    """校验密码。兼容 1.x 的裸 sha256 / 加盐 sha256 老格式。"""
    if not stored:
        return False
    if "$" not in stored:
        h = hashlib.sha256(pw.encode()).hexdigest()
        if hmac.compare_digest(h, stored):
            return True
        # 老的加盐形式：salt$hash
        for sep in (":", "|"):
            if sep in stored:
                salt, _, dig = stored.partition(sep)
                if hmac.compare_digest(
                        hashlib.sha256((salt + pw).encode()).hexdigest(), dig):
                    return True
                if hmac.compare_digest(
                        hashlib.sha256((pw + salt).encode()).hexdigest(), dig):
                    return True
        return False
    try:
        algo, h, rounds, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds))
    return hmac.compare_digest(dk.hex(), digest)


def need_rehash(stored: str) -> bool:
    """老格式（非 pbkdf2$…）在登录成功后应自动升级。"""
    return not (stored or "").startswith("pbkdf2$")


# ---------- 表结构 ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE,
    username_lc  TEXT NOT NULL,
    pw_hash      TEXT NOT NULL,
    pw_enc       TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'user',
    perms        TEXT NOT NULL DEFAULT '[]',
    disabled     INTEGER NOT NULL DEFAULT 0,
    created      INTEGER NOT NULL,
    last_login   INTEGER,
    last_ip      TEXT,
    last_ua      TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS user_perms (
    uid     INTEGER NOT NULL,
    vol_id  INTEGER NOT NULL,
    nav     TEXT NOT NULL,
    PRIMARY KEY (uid, vol_id, nav)
);

CREATE TABLE IF NOT EXISTS login_log (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    username TEXT,
    ip       TEXT,
    ua       TEXT,
    ok       INTEGER NOT NULL,
    reason   TEXT
);
"""


def _cols(conn, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def migrate(conn) -> None:
    """把 1.x 的老表升级到现在的结构（幂等，可反复执行）。

    1.x 的 users / login_log 缺 username_lc / ua / reason / note 等列，
    直接 executescript(SCHEMA) 会因为 CREATE INDEX 引用不存在的列而报
    "no such column" —— 所以先补列再建索引。
    """
    t = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    if "users" in t:
        cols = _cols(conn, "users")
        for name, ddl in (
            ("username_lc", "TEXT"),
            ("note", "TEXT"),
            ("pw_enc", "TEXT NOT NULL DEFAULT ''"),
            ("perms", "TEXT NOT NULL DEFAULT '[]'"),
            ("disabled", "INTEGER NOT NULL DEFAULT 0"),
            ("last_ua", "TEXT"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        # 回填 username_lc（老库没有这一列）
        conn.execute(
            "UPDATE users SET username_lc=lower(username) "
            "WHERE username_lc IS NULL OR username_lc=''")
        # 老库 idx_users_lc 不存在，重复用户名（大小写不同）时先改名避免建索引失败
        dup = conn.execute(
            "SELECT lower(username) l, COUNT(*) n FROM users GROUP BY l HAVING n>1"
        ).fetchall()
        for r in dup:
            rows = conn.execute(
                "SELECT id, username FROM users WHERE lower(username)=? ORDER BY id",
                (r[0],)).fetchall()
            for i, (uid, un) in enumerate(rows[1:], start=2):
                conn.execute("UPDATE users SET username=? WHERE id=?",
                             (f"{un}_{i}", uid))
                conn.execute("UPDATE users SET username_lc=lower(username) WHERE id=?",
                             (uid,))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_lc ON users(username_lc)")

    if "login_log" in t:
        cols = _cols(conn, "login_log")
        for name, ddl in (("ua", "TEXT"), ("reason", "TEXT")):
            if name not in cols:
                conn.execute(f"ALTER TABLE login_log ADD COLUMN {name} {ddl}")

    # 建缺失的表（全新库 / 老库都可能缺）。索引统一放最后：
    # 必须等列都补齐、重复用户名处理完，否则 CREATE INDEX 会失败。
    conn.executescript(SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_perms_uid ON user_perms(uid, vol_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_ts ON login_log(ts DESC)")
    # username_lc 唯一索引必须在重名改完之后建
    if "users" in t:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_lc ON users(username_lc)")
    conn.commit()

# 导航项 key —— 前后端共用（前端 ALSO 有一份用于渲染，值需保持一致）
NAV_ITEMS = [
    ("home", "总览"),
    ("tree", "目录树"),
    ("topics", "按主题"),
    ("authors", "按作者"),
    ("series", "按系列"),
    ("kinds", "按类型"),
    ("tags", "标签"),
    ("star", "收藏"),
    ("read", "已读"),
    ("todo", "待读"),
    ("continue", "继续看"),
    ("stats", "阅读统计"),
    ("random", "随机看"),
    ("recent", "最近添加"),
    ("big", "大文件"),
    ("search", "搜索"),
    ("fulltext", "全文搜索"),
    ("play", "播放/阅读"),
    ("dups", "重复文件"),
    ("junk", "清理建议"),
    ("download", "下载原文件"),
]
NAV_KEYS = {k for k, _ in NAV_ITEMS}


# ---------- CRUD ----------

def _row(u, conn=None) -> dict:
    d = {
        "id": u["id"], "username": u["username"], "role": u["role"],
        "perms": json.loads(u["perms"] or "[]"), "disabled": bool(u["disabled"]),
        "created": u["created"], "last_login": u["last_login"],
        "last_ip": u["last_ip"], "last_ua": u["last_ua"],
        "note": u["note"] if "note" in u.keys() else None,
        "password": "",
    }
    try:
        d["password"] = decrypt_pw(u["pw_enc"])
    except Exception:
        d["password"] = "（无法解密）"
    if conn is not None:
        d["nav"] = nav_map(conn, d["id"])
    return d


def count_users(conn) -> int:
    return conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


def count_admins(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM users WHERE role='admin' AND disabled=0").fetchone()["n"]


def create_user(conn, username: str, password: str, role: str = "user",
                perms: list | None = None, note: str = "") -> dict:
    from .security import valid_username
    username = (username or "").strip()
    if not valid_username(username):
        raise ValueError("用户名不合法（1-32 位字母/数字/中文/._-）")
    if not password or len(password) < 4:
        raise ValueError("密码至少 4 位")
    if len(password) > 128:
        raise ValueError("密码过长")
    if role not in ("admin", "user"):
        raise ValueError("角色无效")
    dup = conn.execute("SELECT 1 FROM users WHERE username_lc=?", (username.lower(),)).fetchone()
    if dup:
        raise ValueError("用户名已存在")
    conn.execute(
        "INSERT INTO users(username, username_lc, pw_hash, pw_enc, role, perms, disabled,"
        " created, note) VALUES(?,?,?,?,?,?,0,?,?)",
        (username, username.lower(), hash_pw(password), encrypt_pw(password), role,
         json.dumps(sorted(set(int(v) for v in (perms or [])))), int(time.time()), note))
    conn.commit()
    return get_by_name(conn, username)


def get_by_name(conn, username: str):
    u = conn.execute("SELECT * FROM users WHERE username_lc=?",
                     ((username or "").lower(),)).fetchone()
    return _row(u, conn) if u else None


def get_by_id(conn, uid: int):
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return _row(u, conn) if u else None


def verify(conn, username: str, password: str):
    u = conn.execute("SELECT * FROM users WHERE username_lc=?",
                     ((username or "").lower(),)).fetchone()
    if not u:
        # 用户不存在也做一次哈希，避免靠响应时间枚举用户名
        hash_pw(password or "x")
        return None
    if not check_pw(password or "", u["pw_hash"]):
        return None
    if u["disabled"]:
        return None
    # 老格式密码 → 登录成功后透明升级为 PBKDF2（顺带补上可查看密文）
    if need_rehash(u["pw_hash"]):
        try:
            conn.execute("UPDATE users SET pw_hash=?, pw_enc=? WHERE id=?",
                         (hash_pw(password), encrypt_pw(password), u["id"]))
            conn.commit()
        except Exception:
            pass
    return _row(u, conn)


def list_users(conn) -> list[dict]:
    return [_row(u, conn) for u in
            conn.execute("SELECT * FROM users ORDER BY role DESC, id")]


def update(conn, uid: int, *, perms=None, role=None, password=None,
           disabled=None, note=None, nav=None) -> dict | None:
    if perms is not None:
        # API 边界入参净化：跳过 None / 非数字项，畸形请求不该打 500
        pv: set[int] = set()
        for v in perms:
            try:
                pv.add(int(v))
            except (TypeError, ValueError):
                continue
        plist = sorted(pv)
        conn.execute("UPDATE users SET perms=? WHERE id=?",
                     (json.dumps(plist), uid))
        # 同步清理导航授权行：can_see 只看 user_perms 表，
        # 不删的话「取消库勾选」在权限判定上不生效（残留行继续放行）。
        if plist:
            ph = ",".join("?" * len(plist))
            conn.execute(f"DELETE FROM user_perms WHERE uid=? AND vol_id NOT IN ({ph})",
                         (uid, *plist))
        else:
            conn.execute("DELETE FROM user_perms WHERE uid=?", (uid,))
    if role in ("admin", "user"):
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    if password:
        if len(password) < 4:
            raise ValueError("密码至少 4 位")
        conn.execute("UPDATE users SET pw_hash=?, pw_enc=? WHERE id=?",
                     (hash_pw(password), encrypt_pw(password), uid))
    if disabled is not None:
        conn.execute("UPDATE users SET disabled=? WHERE id=?",
                     (1 if disabled else 0, uid))
        if disabled:
            conn.execute("DELETE FROM sessions WHERE uid=?", (uid,))
    if note is not None:
        conn.execute("UPDATE users SET note=? WHERE id=?", (note[:200], uid))
    if nav is not None:
        set_nav_map(conn, uid, nav)
    conn.commit()
    return get_by_id(conn, uid)


def delete_user(conn, uid: int) -> None:
    conn.execute("DELETE FROM user_perms WHERE uid=?", (uid,))
    conn.execute("DELETE FROM sessions WHERE uid=?", (uid,))
    conn.execute("DELETE FROM reading_progress WHERE uid=?", (uid,))
    conn.execute("DELETE FROM media_progress WHERE uid=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()


# ---------- 导航级权限 ----------

def set_nav_map(conn, uid: int, nav_map_: dict) -> None:
    """
    nav_map_ = { "3": ["home","tree","play"], "5": "*" }
      "*"   → 该库所有导航项
      列表  → 仅这些导航项
      空列表 → 该库对用户不可见（从 perms 移除）

    合并语义：只改动传入的库，不碰其他库的授权。
    （旧实现先 DELETE 全表再用传入的库覆盖 users.perms，
    前端逐库保存时会把其他库的授权和勾选全部冲掉——真实事故：
    「勾了 A 库的权限，点 B 库的『全部』，A 的勾选就没了」。）
    """
    row = conn.execute("SELECT perms FROM users WHERE id=?", (uid,)).fetchone()
    try:
        perms: set[int] = set(json.loads(row["perms"]) if row and row["perms"] else [])
    except (ValueError, TypeError):
        perms = set()
    for k, v in (nav_map_ or {}).items():
        try:
            vid = int(k)
        except (TypeError, ValueError):
            continue
        # 先清掉这个库的旧授权行（只动这一个库）
        conn.execute("DELETE FROM user_perms WHERE uid=? AND vol_id=?", (uid, vid))
        if v == "*" or v is True:
            perms.add(vid)
            conn.execute(
                "INSERT OR IGNORE INTO user_perms(uid, vol_id, nav) VALUES(?,?,?)",
                (uid, vid, "*"))
        elif isinstance(v, list) and v:
            clean = sorted({x for x in v if x in NAV_KEYS})
            if clean:
                perms.add(vid)
                conn.executemany(
                    "INSERT OR IGNORE INTO user_perms(uid, vol_id, nav) VALUES(?,?,?)",
                    [(uid, vid, x) for x in clean])
        else:
            # 空列表 / None：清掉该库的导航明细。
            # 库勾选（perms）保持不变 —— 管理员常做「先勾库、再逐项配明细」，
            # 取消『全部』不应该把库勾选也撤掉；明细全空时用户本来就看不到内容
            # （can_see 逐导航项判定，没有授权行即拒绝）。
            pass
    conn.execute("UPDATE users SET perms=? WHERE id=?",
                 (json.dumps(sorted(perms)), uid))
    conn.commit()


def nav_map(conn, uid: int) -> dict:
    """返回 {'3': '*'} 或 {'3': ['home','play']}"""
    out: dict[str, list | str] = {}
    for r in conn.execute(
            "SELECT vol_id, nav FROM user_perms WHERE uid=? ORDER BY vol_id", (uid,)):
        vid = str(r["vol_id"])
        if r["nav"] == "*":
            out[vid] = "*"
        else:
            cur = out.get(vid)
            if cur == "*":
                continue
            if isinstance(cur, list):
                cur.append(r["nav"])
            else:
                out[vid] = [r["nav"]]
    return out


def allowed_nav(user: dict, vol_id: int | None) -> set[str] | str:
    """某库下允许的导航项集合；'*' 表示全开；空集表示无权。"""
    if user.get("role") == "admin":
        return "*"
    m = user.get("nav") or {}
    v = m.get(str(vol_id)) if vol_id is not None else None
    if v == "*":
        return "*"
    if isinstance(v, list):
        return set(v)
    return set()


def can_see(user: dict, vol_id: int, nav: str) -> bool:
    if user.get("role") == "admin":
        return True
    a = allowed_nav(user, vol_id)
    if a == "*":
        return True
    if nav == "home":
        # 总览/统计是进入一个库的基础能力：只要对该库有任何明细权限即可
        return bool(a)
    return nav in a


# ---------- 会话 ----------

def create_session(conn, uid: int, ip: str, ua: str, ttl: int = 7 * 86400) -> str:
    tok = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("INSERT INTO sessions(token, uid, created, expires, ip, ua) VALUES(?,?,?,?,?,?)",
                 (tok, uid, now, now + ttl, ip, (ua or "")[:200]))
    conn.commit()
    return tok


def get_session(conn, token: str):
    if not token or len(token) > 200:
        return None
    r = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not r:
        return None
    if r["expires"] < time.time():
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    return {"uid": r["uid"], "ip": r["ip"]}


def drop_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()


def purge_sessions(conn) -> None:
    conn.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
    conn.commit()


# ---------- 日志 ----------

def log_login(conn, username: str, ip: str, ok: bool, ua: str = "", reason: str = "") -> None:
    conn.execute(
        "INSERT INTO login_log(ts, username, ip, ua, ok, reason) VALUES(?,?,?,?,?,?)",
        (int(time.time()), (username or "")[:40], ip, (ua or "")[:200], 1 if ok else 0, reason))
    if ok:
        conn.execute("UPDATE users SET last_login=?, last_ip=?, last_ua=? WHERE username_lc=?",
                     (int(time.time()), ip, (ua or "")[:200], (username or "").lower()))
    conn.commit()


def recent_logins(conn, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT ts, username, ip, ok, reason FROM login_log ORDER BY id DESC LIMIT ?",
        (limit,))]


def audit(conn, user: dict | None, ip: str, act: str, detail: str = "") -> None:
    conn.execute("INSERT INTO audit_log(ts, uid, user, ip, act, detail) VALUES(?,?,?,?,?,?)",
                 (int(time.time()), (user or {}).get("id"), (user or {}).get("username") or "-",
                  ip, act[:60], (detail or "")[:400]))
    conn.commit()


def recent_audit(conn, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT ts, user, ip, act, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]
