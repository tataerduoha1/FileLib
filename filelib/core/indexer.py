"""
filelib/core/indexer.py —— 把扫描结果变成可浏览的资料库

流程：
    1. 扫描（scanner）→ files 表
    2. 结构化解析（patterns）→ meta 表 + tags
    3. 缩略图/媒体信息（thumbs）→ 后台任务
    4. 全文（仅文档类，本盘极少）→ 可选

所有步骤可独立重跑，不破坏既有数据。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .db import connect, thumb_dir
from .kinds import kind_of, get_ext, parse_dir_label, split_topic
from .patterns import parse_relpath, episode_key
from .books import parse_book, STRUCT_DIRS, BULK_DIR_RE, BULK_HINT_RE
from .bookmeta import extract_book
from .scanner import long_path, short_display, Scanner, ScanStats

# ---------- 外部工具探测 ----------

def find_ffmpeg() -> str | None:
    """按优先级探测：配置文件 → 环境变量 → 程序自带 bin → 本机常见位置 → PATH。"""
    from .config import ffmpeg_hint
    for cand in ffmpeg_hint():
        if cand and os.path.isfile(cand):
            return cand
    w = shutil.which("ffmpeg")
    if w:
        return w
    return None


def find_ffprobe() -> str | None:
    ff = find_ffmpeg()
    if ff:
        p = Path(ff).with_name("ffprobe.exe")
        if p.is_file():
            return str(p)
    return shutil.which("ffprobe")


# ---------- 结构化解析写库 ----------

# 这些扩展名按"书"解析（书名/作者），其余按影视那套（系列/番号）
BOOK_EXTS = {
    "epub", "mobi", "azw", "azw3", "azw4", "prc", "pdb", "fb2", "fbz",
    "lit", "lrf", "lrx", "rb", "snb", "tcr", "ceb", "iba", "ibooks",
    "djvu", "djv", "chm", "kfx", "txt", "pdf",
}


# 影视解析器只对这几类有意义（有番号/系列/集数概念）。
# 文档、压缩包、图片、代码走影视解析器会让"系列"退化成文件名本身，
# 一个 20 万文件的书库会因此冒出 7 万个"系列"，全是噪音。
MEDIA_KINDS = {"video", "audio"}


def _meta_for(rel_dir: str, name: str, ext: str, kind: str) -> dict:
    """按文件类型选择解析策略。"""
    if ext in BOOK_EXTS or kind == "book":
        b = parse_book(rel_dir, name, ext)
        return {
            "title": b["title"],
            "author": b["author"],
            "series": b["series"],
            "topic": b["topic"],
            "keywords": b["keywords"],
            "quality": [],
            "year": None,
            "kind_label": "book",
        }
    if kind in MEDIA_KINDS:
        p = parse_relpath(rel_dir, name)
        return {
            "code": p.code,
            "series": p.series,
            "topic": p.topic,
            "keywords": p.keywords,
            "quality": p.quality,
            "epkey": episode_key(p),
            "year": p.year,
        }
    # 其余类型：只留题材，不给系列/编号
    return {
        "code": None,
        "series": None,
        "topic": _dir_topic(rel_dir),
        "keywords": [],
        "quality": [],
        "epkey": None,
        "year": None,
    }


def _dir_topic(rel_dir: str) -> str | None:
    """从目录层级里取第一个主题标签（跳过"按平台分"这类组织性目录）。"""
    for seg in [p for p in rel_dir.split("/") if p]:
        if BULK_DIR_RE.match(seg.strip()) or BULK_HINT_RE.search(seg):
            continue
        lab = parse_dir_label(seg)
        if not lab:
            continue
        for kw in split_topic(lab):
            k = kw.strip()
            if not k or len(k) < 2:
                continue
            if k in STRUCT_DIRS or k in ("视频", "图片", "资源", "其他"):
                continue
            if BULK_DIR_RE.match(k) or BULK_HINT_RE.search(k):
                continue
            if len(k) > 24:
                continue
            return k
    return None


def build_meta(conn: sqlite3.Connection, progress=None,
               vol_id: int | None = None, force: bool = False) -> int:
    """
    为每个文件生成 meta（书名/作者 或 编号/系列/主题）并打标签。
    vol_id 不为空时只处理该资料库；force=True 时重算已有的 meta。
    """
    where = ["f.status='present'"]
    params: list = []
    if vol_id is not None:
        where.append("f.vol_id = ?")
        params.append(vol_id)
    if not force:
        where.append("m.file_id IS NULL")

    rows = conn.execute(
        f"""
        SELECT f.id, f.rel_dir, f.name, f.ext, f.kind, f.size
        FROM files f LEFT JOIN meta m ON m.file_id = f.id
        WHERE {' AND '.join(where)}
        """,
        params,
    ).fetchall()
    n = 0
    batch = []
    for r in rows:
        m = _meta_for(r["rel_dir"], r["name"], r["ext"], r["kind"])
        batch.append((r["id"], json.dumps(m, ensure_ascii=False)))
        n += 1
        if len(batch) >= 2000:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(file_id, json) VALUES(?,?)", batch)
            conn.commit()
            batch.clear()
            if progress:
                progress(n)
    if batch:
        conn.executemany("INSERT OR REPLACE INTO meta(file_id, json) VALUES(?,?)", batch)
    conn.commit()
    return n


# ---------- 缩略图任务入队 ----------

def enqueue_tasks(conn: sqlite3.Connection, types: list[str] | None = None,
                  vol_id: int | None = None) -> dict[str, int]:
    """为需要的文件创建任务。幂等：已有 pending 任务的不重复加。"""
    types = types or ["thumb_video", "thumb_image", "thumb_pdf", "thumb_book",
                      "fulltext", "autotag"]
    out: dict[str, int] = {}
    vf = " AND f.vol_id = ?" if vol_id is not None else ""
    vp = [vol_id] if vol_id is not None else []

    def add(kind_filter: str, ttype: str, state_col: str, prio: int) -> int:
        cur = conn.execute(
            f"""
            INSERT INTO tasks(file_id, type, priority, state, created)
            SELECT f.id, ?, ?, 'pending', ?
            FROM files f
            WHERE f.kind = ? AND f.status='present' AND f.{state_col} = 0 {vf}
              AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.file_id=f.id AND t.type=?)
            """,
            [ttype, prio, int(time.time()), kind_filter] + vp + [ttype],
        )
        return cur.rowcount

    if "thumb_video" in types:
        out["thumb_video"] = add("video", "thumb_video", "thumb_state", 50)
    if "thumb_image" in types:
        out["thumb_image"] = add("image", "thumb_image", "thumb_state", 10)
    if "thumb_pdf" in types:
        # PDF 可能在 book 或 doc 两类里（不同扩展名归类不同）
        out["thumb_pdf"] = conn.execute(
            f"""
            INSERT INTO tasks(file_id, type, priority, state, created)
            SELECT f.id, 'thumb_pdf', 30, 'pending', ?
            FROM files f
            WHERE f.ext = 'pdf' AND f.status='present' AND f.thumb_state = 0 {vf}
              AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.file_id=f.id AND t.type='thumb_pdf')
            """, [int(time.time())] + vp).rowcount
    if "thumb_book" in types:
        # EPUB/MOBI/AZW3 封面提取
        cur = conn.execute(
            f"""
            INSERT INTO tasks(file_id, type, priority, state, created)
            SELECT f.id, 'thumb_book', 30, 'pending', ?
            FROM files f
            WHERE f.status='present' AND f.thumb_state = 0 {vf}
              AND f.ext IN ('epub','mobi','azw','azw3','azw4','prc','pdb','fb2','lit','chm')
              AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.file_id=f.id AND t.type='thumb_book')
            """, [int(time.time())] + vp)
        out["thumb_book"] = cur.rowcount
    if "fulltext" in types:
        cur = conn.execute(
            f"""
            INSERT INTO tasks(file_id, type, priority, state, created)
            SELECT f.id, 'fulltext', 40, 'pending', ?
            FROM files f
            WHERE f.status='present' AND f.text_state = 0 {vf}
              AND (f.ext IN ('txt','pdf','epub','md','markdown','mobi','azw3','azw')
                   OR f.kind = 'book')
              AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.file_id=f.id AND t.type='fulltext')
            """, [int(time.time())] + vp)
        out["fulltext"] = cur.rowcount
    if "autotag" in types:
        # 自动标签（v1.0.7）：只对还没有任何 auto 标签的文件入队；
        # pending/running 的不重复加，已算过（有 auto 标签）的不再算
        cur = conn.execute(
            f"""
            INSERT INTO tasks(file_id, type, priority, state, created)
            SELECT f.id, 'autotag', 60, 'pending', ?
            FROM files f
            WHERE f.status='present' {vf}
              AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.file_id=f.id
                              AND t.type='autotag' AND t.state IN ('pending','running'))
              AND NOT EXISTS (SELECT 1 FROM file_tags ft WHERE ft.file_id=f.id
                              AND ft.source='auto')
            """, [int(time.time())] + vp)
        out["autotag"] = cur.rowcount
    conn.commit()
    return out


# ---------- 缩略图生成 ----------

def _videogen_thumb(ffmpeg: str, src: str, dst: str, at: float = 0.15) -> bool:
    """在视频 15% 处抽一帧。选择该位置而非首帧，避免黑场/片头字幕。"""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{at:.2f}", "-i", long_path(src),
        "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4",
        "-y", long_path(dst),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=45,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        return p.returncode == 0 and os.path.getsize(dst) > 0
    except (subprocess.TimeoutExpired, OSError):
        try:
            os.unlink(dst)
        except OSError:
            pass
        return False


def _probe(path: str, ffprobe: str) -> dict:
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", long_path(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        if p.returncode != 0:
            return {}
        data = json.loads(p.stdout.decode("utf-8", "replace"))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {}
    out: dict = {}
    fmt = data.get("format") or {}
    try:
        out["duration"] = round(float(fmt.get("duration", 0)), 1) or None
    except (TypeError, ValueError):
        pass
    out["bitrate"] = int(fmt.get("bit_rate", 0)) or None
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video":
            out["width"] = s.get("width")
            out["height"] = s.get("height")
            out["vcodec"] = s.get("codec_name")
            break
    return out


def thumb_path(file_id: int) -> Path:
    d = thumb_dir()
    sub = d / f"{file_id % 1000:03d}"
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{file_id}.jpg"


def run_thumb_worker(limit: int = 0, workers: int = 4,
                     progress=None, stop_flag=None,
                     only_types: list[str] | None = None) -> dict[str, int]:
    """
    批量处理缩略图/全文/自动标签任务。可中断：处理到一半不会丢已完成的部分。
    limit=0 表示处理完队列。

    v1.0.8：分批流式取任务（每批 BATCH 条，处理完再取下一批）。一次性全量
    SELECT 在几十万任务时既慢又占内存，中途停止还会浪费整批已取出的任务。
    线程安全：工作线程只做计算（ffmpeg/PIL/解析），写库统一回到主线程单连接；
    每条结果立即提交，避免全文任务工作线程自开连接写库时与主线程互相卡写锁。
    """
    from . import bookcover as bc

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    conn = connect()
    done = 0
    failed = 0
    skipped = 0
    processed = 0

    tfilter = ""
    tparams: list = []
    if only_types:
        tfilter = " AND t.type IN (" + ",".join("?" * len(only_types)) + ")"
        tparams = list(only_types)
    twhere = (f"t.state='pending' AND (t.type LIKE 'thumb_%' OR t.type='fulltext'"
              f" OR t.type='autotag'){tfilter}")

    # 预估总量供进度条显示（执行中若有并发变化以实际处理数为准）
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM tasks t WHERE {twhere}", tparams
        ).fetchone()["n"]
    except sqlite3.Error:
        total = 0
    if limit > 0:
        total = min(total, limit)

    def write_autotag(fid: int, tags: list[str]) -> None:
        """主线程写一个文件的 auto 标签（替代逐文件 apply_autotag 的自开连接）。"""
        conn.execute("DELETE FROM file_tags WHERE file_id=? AND source='auto'", (fid,))
        if tags:
            conn.executemany("INSERT OR IGNORE INTO tags(name, grp) VALUES(?, 'auto')",
                             [(t,) for t in tags])
            conn.executemany(
                "INSERT OR IGNORE INTO file_tags(file_id, tag_id, source) "
                "SELECT ?, id, 'auto' FROM tags WHERE name=?",
                [(fid, t) for t in tags])

    BATCH = 400

    def work(r):
        src = os.path.join(r["root_path"], r["rel_dir"].replace("/", os.sep), r["name"])
        if r["ttype"] == "autotag":
            # 自动标签（v1.0.7）：纯计算不写库，不碰 thumb 目录；正文缓存已在
            # 批量查询时 LEFT JOIN file_text 带出。文件缺失时规则层照样可用。
            from .autotag import compute_tags
            try:
                tags = compute_tags(r["id"], r["root_path"], r["rel_dir"],
                                    r["name"], r["ext"], r["kind"],
                                    body=r["body"] or "")
            except Exception:
                return (r["task_id"], r["id"], "fail", None, r["ttype"])
            return (r["task_id"], r["id"], "ok" if tags else "empty",
                    {"_tags": tags} if tags else None, r["ttype"])
        dst = thumb_path(r["id"])
        if not os.path.isfile(long_path(src)):
            return (r["task_id"], r["id"], "missing", None, r["ttype"])
        meta: dict = {}
        ok = False
        if r["ttype"] == "fulltext":
            n = extract_text_into(r["id"], src, r["ext"])
            return (r["task_id"], r["id"], "ok" if n else "empty",
                    {"chars": n} if n else None, r["ttype"])
        if r["ext"] == "pdf":
            ok = _pdf_thumb(src, dst)
            if ok:
                try:
                    m = bc.extract(src, "pdf", allow_calibre=False)
                    meta = {k: m[k] for k in ("title", "author", "description") if m.get(k)}
                except Exception:
                    pass
        elif r["kind"] == "video":
            if not ffmpeg:
                return (r["task_id"], r["id"], "no_ffmpeg", None, r["ttype"])
            ok = _videogen_thumb(ffmpeg, src, str(dst))
            if ok and ffprobe:
                meta = _probe(src, ffprobe)
        elif r["kind"] == "image":
            ok = _image_thumb(src, dst)
        elif r["ttype"] == "thumb_book" or r["kind"] == "book":
            ok, meta = _book_thumb(src, r["ext"], dst, r["id"])
        else:
            return (r["task_id"], r["id"], "unsupported", None, r["ttype"])
        return (r["task_id"], r["id"], "ok" if ok else "fail", meta, r["ttype"])

    stopped = False
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        while not stopped:
            if stop_flag is not None and stop_flag.is_set():
                break
            if limit > 0 and processed >= limit:
                break
            rows = conn.execute(
                f"""
                SELECT t.id AS task_id, t.type AS ttype, f.id, f.vol_id, f.rel_dir,
                       f.name, f.ext, f.kind, v.root_path, ft.body
                FROM tasks t
                JOIN files f ON f.id = t.file_id
                JOIN volumes v ON v.id = f.vol_id
                LEFT JOIN file_text ft ON ft.file_id = f.id
                WHERE {twhere}
                ORDER BY t.priority, t.id
                LIMIT ?
                """,
                tparams + [BATCH if limit <= 0 else min(BATCH, limit - processed)],
            ).fetchall()
            if not rows:
                break
            futs = {ex.submit(work, r): r for r in rows}
            db_errs = 0
            for fut in as_completed(futs):
                if stop_flag is not None and stop_flag.is_set():
                    for f in futs:
                        f.cancel()
                    stopped = True
                    break
                r = futs[fut]
                try:
                    task_id, fid, state, meta, ttype = fut.result()
                except Exception:
                    # 工作线程抛异常：任务标记 failed，避免留在队列被下一批反复重取
                    failed += 1
                    processed += 1
                    try:
                        conn.execute(
                            "UPDATE tasks SET state='failed', attempts=attempts+1, "
                            "error='worker_error', updated=? WHERE id=?",
                            (int(time.time()), r["task_id"]))
                        conn.commit()
                    except sqlite3.Error:
                        pass
                    continue
                now = int(time.time())
                # autotag 没有 files 状态列（thumb_state/text_state 均不适用）
                state_col = ("text_state" if ttype == "fulltext"
                             else None if ttype == "autotag" else "thumb_state")
                try:
                    if state == "ok":
                        if state_col:
                            conn.execute(f"UPDATE files SET {state_col}=2 WHERE id=?", (fid,))
                        conn.execute("UPDATE tasks SET state='done', updated=? WHERE id=?",
                                     (now, task_id))
                        if ttype == "autotag":
                            write_autotag(fid, (meta or {}).get("_tags") or [])
                        elif meta:
                            row = conn.execute("SELECT json FROM meta WHERE file_id=?",
                                               (fid,)).fetchone()
                            base = {}
                            if row:
                                try:
                                    base = json.loads(row["json"])
                                except ValueError:
                                    base = {}
                            base.update({k: v for k, v in meta.items() if v})
                            conn.execute(
                                "INSERT OR REPLACE INTO meta(file_id, json) VALUES(?,?)",
                                (fid, json.dumps(base, ensure_ascii=False)))
                        done += 1
                    elif state in ("missing", "unsupported", "no_ffmpeg", "empty"):
                        conn.execute(
                            "UPDATE tasks SET state='skipped', error=?, updated=? WHERE id=?",
                            (state, now, task_id))
                        if state_col:
                            conn.execute(f"UPDATE files SET {state_col}=3 WHERE id=?", (fid,))
                        skipped += 1
                    else:
                        conn.execute(
                            "UPDATE tasks SET state='failed', attempts=attempts+1, "
                            "error='gen_failed', updated=? WHERE id=?", (now, task_id))
                        if state_col:
                            conn.execute(f"UPDATE files SET {state_col}=3 WHERE id=?", (fid,))
                        failed += 1
                except sqlite3.Error:
                    failed += 1
                    db_errs += 1
                # 每条结果写完立即提交：写事务保持短促，全文任务工作线程自开连接
                # 写库时不会被主线程长期持有的写锁卡住
                try:
                    conn.commit()
                except sqlite3.Error:
                    pass
                processed += 1
                if processed % 20 == 0:
                    if progress:
                        progress(done, failed, skipped, processed, total)
                    # 让界面上"封面完成度"等统计随任务进度刷新（缓存版本失效）
                    from . import cache as _cache
                    _cache.bump()
            # 一批结束：补一次进度刷新；整批都因数据库错误没写成则退出，避免空转
            if progress and processed % 20 != 0:
                progress(done, failed, skipped, processed, total)
            from . import cache as _cache
            _cache.bump()
            if db_errs >= len(rows):
                break
    try:
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass
    return {"done": done, "failed": failed, "skipped": skipped, "total": processed}



def _image_thumb(src: str, dst: Path) -> bool:
    try:
        from PIL import Image, ImageOps
        with Image.open(src) as im:
            # draft：JPEG 解码时直接按 1/N 采样（仅解码所需像素），大图提速 3~5 倍；
            # 非 JPEG 格式为 no-op，无副作用。请求 768 保证缩到 384 仍有足够像素。
            im.draft("RGB", (768, 768))
            im = ImageOps.exif_transpose(im)
            im.thumbnail((384, 384), Image.LANCZOS)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(dst, "JPEG", quality=78, optimize=True)
        return dst.stat().st_size > 0
    except Exception:
        return False


def _pdf_thumb(src: str, dst: Path) -> bool:
    try:
        import fitz  # PyMuPDF
        with fitz.open(src) as doc:
            if doc.page_count == 0:
                return False
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2))
            pix.save(str(dst))
        return dst.stat().st_size > 0
    except Exception:
        return False


def _book_thumb(src: str, ext: str, dst: Path, file_id: int = 0,
                allow_calibre: bool | None = None) -> tuple[bool, dict]:
    """
    EPUB/MOBI/AZW3/PDF 封面：提取原始封面字节，统一转成 JPEG 存缩略图。
    同时返回元数据（title/author/description/publisher），上层合并进 meta 表。
    allow_calibre 为 None 时读设置 cover_calibre（默认关：Calibre 转换每本可达
    数十秒，是补封面最慢的一环；后台设置页可开启）。
    """
    from . import bookcover as bc
    if allow_calibre is None:
        try:
            from .db import get_setting
            allow_calibre = bool(get_setting(connect(), "cover_calibre", 0))
        except Exception:
            allow_calibre = False
    try:
        m = bc.extract(src, ext, allow_calibre=allow_calibre)
    except Exception:
        return False, {}
    meta = {k: m[k] for k in ("title", "author", "description", "publisher", "year",
                              "language", "subjects") if m.get(k)}
    cover = m.get("cover")
    if not cover:
        return False, meta
    try:
        from PIL import Image, ImageOps
        import io as _io
        with Image.open(_io.BytesIO(cover)) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            im.thumbnail((384, 576), Image.LANCZOS)
            im.save(dst, "JPEG", quality=78, optimize=True)
        ok = dst.stat().st_size > 0
        if ok and file_id:
            try:
                bc.save_cover(cover, file_id)
            except Exception:
                pass
        return ok, meta
    except Exception:
        return False, meta


# ---------- 全文提取 ----------

_TEXT_MAX = 300_000  # 每本书最多索引前 30 万字符


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "utf-16", "big5"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _strip_html(raw: bytes) -> str:
    txt = _decode_bytes(raw)
    txt = re.sub(r"<(script|style)\b.*?</\1>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"&nbsp;|&#160;", " ", txt)
    txt = re.sub(r"&lt;", "<", txt)
    txt = re.sub(r"&gt;", ">", txt)
    txt = re.sub(r"&amp;", "&", txt)
    return re.sub(r"[ \t\r]+", " ", txt)


def extract_text(src: str, ext: str, limit: int = _TEXT_MAX) -> str:
    """从文件提取纯文本。ext 小写。失败返回 ''。"""
    ext = (ext or "").lower()
    try:
        if ext in ("txt", "md", "markdown"):
            with open(long_path(src), "rb") as f:
                return _decode_bytes(f.read(limit * 3))[:limit]
        if ext == "pdf":
            import fitz
            with fitz.open(long_path(src)) as doc:
                parts = []
                for i in range(min(doc.page_count, 200)):
                    parts.append(doc.load_page(i).get_text())
                    if sum(len(p) for p in parts) > limit:
                        break
                return "".join(parts)[:limit]
        if ext == "epub":
            from .bookcover import epub_chapters
            _t, chs = epub_chapters(long_path(src), max_chars=limit * 2)
            return "\n\n".join(c[1] for c in chs)[:limit]
        if ext in ("mobi", "azw3", "prc", "azw"):
            from .bookcover import mobi_pages
            _t, chs = mobi_pages(long_path(src))
            if chs:
                return "\n\n".join(c[1] for c in chs)[:limit]
            # 无 Calibre 时退化为内置的 PalmDOC 解压
            import struct, zlib
            with open(long_path(src), "rb") as f:
                f.seek(76)
                nrec = struct.unpack(">H", f.read(2))[0]
                recs = []
                for _ in range(min(nrec, 600)):
                    off = struct.unpack(">I", f.read(4))[0]
                    f.read(4)
                    recs.append(off)
                f.seek(recs[0])
                rec0 = f.read((recs[1] - recs[0]) if len(recs) > 1 else 0)
                if rec0[16:20] != b"MOBI":
                    return ""
                f.seek(0)
                raw = f.read(recs[min(200, len(recs) - 1)])
            parts = []
            for i in range(1, min(len(recs), 200)):
                b = raw[recs[i]:recs[i + 1]] if i + 1 < len(recs) else raw[recs[i]:]
                try:
                    parts.append(_strip_html(zlib.decompress(b)))
                except Exception:
                    continue
                if sum(len(p) for p in parts) > limit:
                    break
            return "".join(parts)[:limit]
    except Exception:
        return ""
    return ""


def extract_text_into(file_id: int, src: str, ext: str) -> int:
    """
    提取正文写入 file_text + fts。返回字符数。

    注意：本函数自己开连接（在调用线程内创建并使用），
    绝不把外部连接传进来跨线程用。
    """
    body = extract_text(src, ext)
    if not body or len(body) < 20:
        return 0
    body = body[:_TEXT_MAX]
    conn = connect()
    try:
        conn.execute("INSERT OR REPLACE INTO file_text(file_id, body) VALUES(?,?)",
                     (file_id, body))
        conn.execute("DELETE FROM fts WHERE rowid=?", (file_id,))
        conn.execute("INSERT INTO fts(rowid, name, body) VALUES(?,?,?)",
                     (file_id, "", body))
        # 摘要顺带进 meta（首行 160 字，卡片/详情直接可用，避免列表查询 JOIN 大表）
        try:
            head = " ".join(body[:200].split())[:160]
            row = conn.execute("SELECT json FROM meta WHERE file_id=?", (file_id,)).fetchone()
            base = {}
            if row:
                try:
                    base = json.loads(row["json"])
                except ValueError:
                    base = {}
            if head:
                base["summary"] = head
                conn.execute("INSERT OR REPLACE INTO meta(file_id, json) VALUES(?,?)",
                             (file_id, json.dumps(base, ensure_ascii=False)))
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()
    return len(body)


# ---------- 一键建库 ----------

def full_build(root: str, exclude: set[str] | None = None,
               make_thumbs: bool = True, workers: int = 4,
               log=print) -> dict:
    """扫描 → 解析 → 缩略图。返回汇总。"""
    from .db import init_db
    init_db()
    conn = connect()

    log("① 扫描目录 …")
    sc = Scanner(conn, ScanStats())
    st = sc.scan(root, exclude_dirs=exclude)
    log(f"   完成：{st.n_seen} 个文件，{st.bytes_seen/1e9:.2f} GB，"
        f"{st.elapsed:.1f} 秒，错误 {st.n_errors} 处")

    log("② 解析编号/系列/主题 …")
    n = build_meta(conn)
    log(f"   完成：{n} 条")

    log("③ 生成缩略图任务 …")
    q = enqueue_tasks(conn)
    log(f"   {q}")

    result = {"scan": st.as_dict(), "meta": n, "tasks": q}
    conn.close()

    if make_thumbs:
        log("④ 生成缩略图（视频抽帧较慢，可随时中断）…")
        last = [0]

        def prog(d, f, s, i, total):
            if i - last[0] >= 200:
                last[0] = i
                log(f"   {i}/{total}  成功 {d}  失败 {f}  跳过 {s}")

        r = run_thumb_worker(workers=workers, progress=prog)
        log(f"   缩略图：成功 {r['done']}，失败 {r['failed']}，跳过 {r['skipped']}")
        result["thumbs"] = r

    return result
