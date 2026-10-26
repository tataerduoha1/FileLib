"""
filelib/core/bookcover.py —— 书籍封面 + 元数据（书库主力功能）

提取顺序（尽量不依赖外部程序）：
  EPUB   zip 内 OPF 的 cover-image / meta[name=cover] / guide / 名字含 cover 的图片
  MOBI/AZW3  二进制解析：EXTH 201 封面偏移 → 首张完整 JPEG；失败则扫描 image record
  PDF    PyMuPDF 首页渲染（同时取内嵌元数据）
  兜底   Calibre 的 ebook-convert（已装则用），把 azw3/mobi 转成临时 epub 再取封面

额外能力：
  * 从 EPUB 的 dc:description 提取简介（清洗 HTML）
  * 中文作者/书名排序用拼音首字（pypinyin 可选，没有就退化成 Unicode 序）
  * 封面存 <data>/covers/<id 取模>/<id>.jpg，统一 JPEG，处理 EXIF 方向
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .db import data_dir
from .config import APP_ROOT

BOOK_TYPES = {"epub", "mobi", "azw", "azw3", "azw4", "prc", "pdb", "fb2", "lit", "kfx", "pdf",
              "djvu", "chm", "txt"}

_NOISE_DIR_RE = re.compile(r"【|】|！！|^！|^\d+\s*[、.\-]|合集|分类|整理|打包|全部")


# ---------- 封面缓存路径 ----------

def cover_dir() -> Path:
    p = data_dir() / "covers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cover_path(file_id: int, size: str = "") -> Path:
    sub = cover_dir() / ("b" if size else "") / f"{file_id % 1000:03d}"
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{file_id}{'_' + size if size else ''}.jpg"


# ---------- Calibre 探测 ----------

def find_calibre() -> str | None:
    env = os.environ.get("FILELIB_CALIBRE")
    cands = []
    if env:
        cands.append(env)
    cands += [
        r"D:\installation\calibre\ebook-convert.exe",
        r"C:\Program Files\Calibre2\ebook-convert.exe",
        r"C:\Program Files (x86)\Calibre2\ebook-convert.exe",
        str(APP_ROOT / "bin" / "ebook-convert.exe"),
    ]
    w = shutil.which("ebook-convert")
    if w:
        cands.insert(0, w)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


# ---------- 文本清洗 ----------

def _clean_text(s: str | None, maxlen: int = 1200) -> str | None:
    if not s:
        return None
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'"))
    s = re.sub(r"[\u200b-\u200f\ufeff]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > maxlen:
        s = s[:maxlen].rstrip() + "…"
    return s or None


def _plausible(s: str | None, maxlen: int = 120) -> str | None:
    s = _clean_text(s, maxlen)
    if not s:
        return None
    low = s.lower()
    if re.search(r"\.(jpe?g|png|gif|opf|ncx|html?|css|js|xml|ttf)$", low):
        return None
    if re.fullmatch(r"[0-9a-f]{16,}", low.replace("-", "")):
        return None
    if len(s) <= 2 and not re.search(r"[\u4e00-\u9fa5]", s):
        return None
    return s


# ---------- EPUB ----------

def epub_meta(path: str) -> dict:
    out: dict = {}
    try:
        zf = zipfile.ZipFile(path)
    except Exception:
        return out
    with zf:
        try:
            names = zf.namelist()
            opf_path = None
            if "META-INF/container.xml" in names:
                cont = zf.read("META-INF/container.xml")
                m = re.search(rb'full-path="([^"]+)"', cont)
                if m:
                    opf_path = m.group(1).decode("utf-8", "replace")
            if not opf_path:
                cands = [n for n in names if n.lower().endswith(".opf")]
                opf_path = cands[0] if cands else None
            if not opf_path:
                return out
            opf = zf.read(opf_path)
            opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""

            def grab(tag: str) -> list[str]:
                return [x.decode("utf-8", "replace") for x in re.findall(
                    rb"<(?:\w+:)?%s[^>]*>(.*?)</(?:\w+:)?%s>" % (tag.encode(), tag.encode()),
                    opf, re.S | re.I)]

            titles = grab("title")
            if titles:
                out["title"] = _plausible(titles[0])
            creators = grab("creator")
            if creators:
                out["author"] = _plausible(creators[0], 60)
            descs = grab("description")
            if descs:
                out["description"] = _clean_text(descs[0])
            pub = grab("publisher")
            if pub:
                out["publisher"] = _plausible(pub[0], 60)
            lang = grab("language")
            if lang:
                out["language"] = _plausible(lang[0], 20)
            dates = grab("date")
            if dates:
                ym = re.search(r"(19|20)\d{2}", dates[0])
                if ym:
                    out["year"] = int(ym.group(0))
            subs = grab("subject")
            if subs:
                out["subjects"] = [x for x in (_plausible(s, 40) for s in subs) if x][:10]

            # manifest
            items = {}
            for tag in re.findall(rb"<(?:\w+:)?item\b[^>]*>", opf, re.I):
                def attr(a):
                    mm = re.search(rb'\b%s\s*=\s*"([^"]*)"' % a.encode(), tag, re.I)
                    return mm.group(1).decode("utf-8", "replace") if mm else ""
                iid = attr("id")
                if iid:
                    items[iid] = (attr("href"), attr("media-type"), attr("properties"))

            def read_href(href: str) -> bytes | None:
                from urllib.parse import unquote
                name = href.lstrip("./")
                name = unquote(name)
                full = (opf_dir + name) if not name.startswith("/") else name.lstrip("/")
                for cand in (full, full.replace("%20", " ")):
                    if cand in names:
                        try:
                            return zf.read(cand)
                        except Exception:
                            pass
                base = full.rsplit("/", 1)[-1]
                for n in names:
                    if n.rsplit("/", 1)[-1] == base:
                        try:
                            return zf.read(n)
                        except Exception:
                            pass
                return None

            # 1) EPUB3 properties=cover-image
            cov = None
            for iid, (href, mime, props) in items.items():
                if "cover-image" in props and href:
                    cov = read_href(href)
                    if cov:
                        break
            # 2) <meta name="cover" content="id">
            if not cov:
                mm = re.search(rb'<meta[^>]*name\s*=\s*"cover"[^>]*content\s*=\s*"([^"]*)"',
                               opf, re.I)
                if mm:
                    iid = mm.group(1).decode()
                    if iid in items:
                        cov = read_href(items[iid][0])
            # 3) guide reference type=cover
            if not cov:
                mm = re.search(rb'<reference[^>]*type\s*=\s*"cover"[^>]*href\s*=\s*"([^"]*)"',
                               opf, re.I)
                if mm:
                    cov = read_href(mm.group(1).decode())
            # 4) manifest 名字含 cover 的图片
            if not cov:
                for iid, (href, mime, props) in items.items():
                    if "cover" in (href or "").lower() and (mime or "").startswith("image/"):
                        cov = read_href(href)
                        if cov:
                            break
            # 5) zip 里任何含 cover 的图片
            if not cov:
                for n in names:
                    ln = n.lower()
                    if "cover" in ln and ln.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                        try:
                            cov = zf.read(n)
                            break
                        except Exception:
                            pass
            # 6) 第一张图片
            if not cov:
                for n in names:
                    if n.lower().endswith((".jpg", ".jpeg", ".png")) and "image" in n.lower():
                        try:
                            data = zf.read(n)
                        except Exception:
                            continue
                        if len(data) > 8000:
                            cov = data
                            break
            if cov:
                out["cover"] = cov
        except Exception:
            pass
    return out


# ---------- MOBI / AZW3 ----------

def mobi_meta(path: str) -> dict:
    out: dict = {}
    f = None
    try:
        f = open(path, "rb")
        data = f.read(78)
        if len(data) < 78:
            return out
        nrec = struct.unpack(">H", data[76:78])[0]
        offs = []
        for i in range(nrec):
            b = f.read(8)
            if len(b) < 8:
                break
            offs.append(struct.unpack(">I", b[:4])[0])
        if not offs:
            return out
        f.seek(offs[0])
        rec0_len = (offs[1] - offs[0]) if len(offs) > 1 else 0
        rec0 = f.read(rec0_len) if rec0_len > 0 else f.read(65536)
        if len(rec0) < 200:
            return out
        # PalmDOC header 16 字节 + MOBI header
        mobi_start = 16
        if rec0[mobi_start:mobi_start + 4] != b"MOBI":
            return out
        hdr_len = struct.unpack(">I", rec0[mobi_start + 4:mobi_start + 8])[0]
        text_enc = struct.unpack(">I", rec0[mobi_start + 12:mobi_start + 16])[0]
        full_name_off = struct.unpack(">I", rec0[mobi_start + 84:mobi_start + 88])[0]
        full_name_len = struct.unpack(">I", rec0[mobi_start + 88:mobi_start + 92])[0]
        exth_flags = struct.unpack(">I", rec0[mobi_start + 128:mobi_start + 132])[0]
        enc = "utf-8" if text_enc == 65001 else "cp1252"

        if 0 < full_name_off < len(rec0) and full_name_len:
            nm = rec0[full_name_off:full_name_off + full_name_len]
            for e in (enc, "utf-8", "gb18030"):
                try:
                    out["title"] = _plausible(nm.decode(e))
                    break
                except Exception:
                    continue

        cover_off = None
        p = mobi_start + hdr_len
        # 注意：不能依赖 exth_flags 的 0x40 位——不少工具生成的 azw3/mobi
        # flags 全 0 但 EXTH 实际存在（实测批量样本如此），直接探测魔数最稳
        has_exth = rec0[p:p + 4] == b"EXTH"
        if (exth_flags & 0x40 or has_exth) and has_exth:
            cnt = struct.unpack(">I", rec0[p + 8:p + 12])[0]
            q = p + 12
            for _ in range(min(cnt, 200)):
                if q + 8 > len(rec0):
                    break
                rtype, rlen = struct.unpack(">II", rec0[q:q + 8])
                payload = rec0[q + 8:q + rlen]
                if rtype == 100:          # author
                    for e in (enc, "utf-8", "gb18030"):
                        try:
                            a = payload.decode(e)
                            a = _plausible(a, 60)
                            if a and not out.get("author"):
                                out["author"] = a
                            break
                        except Exception:
                            continue
                elif rtype == 103:        # description
                    for e in (enc, "utf-8", "gb18030"):
                        try:
                            out["description"] = _clean_text(payload.decode(e))
                            break
                        except Exception:
                            continue
                elif rtype == 101:        # publisher
                    for e in (enc, "utf-8", "gb18030"):
                        try:
                            out["publisher"] = _plausible(payload.decode(e), 60)
                            break
                        except Exception:
                            continue
                elif rtype in (503, 524):  # updated title / title（full_name 指针为 0 时的书名来源）
                    if not out.get("title"):
                        for e in (enc, "utf-8", "gb18030"):
                            try:
                                out["title"] = _plausible(payload.decode(e))
                                break
                            except Exception:
                                continue
                elif rtype in (201, 202): # cover / thumb offset
                    if len(payload) >= 4 and rtype == 201:
                        cover_off = struct.unpack(">I", payload[:4])[0]
                elif rtype == 106:        # publication date
                    for e in (enc, "utf-8"):
                        try:
                            mm = re.search(r"(19|20)\d{2}", payload.decode(e))
                            if mm:
                                out["year"] = int(mm.group(0))
                            break
                        except Exception:
                            continue
                q += rlen

        # EXTH 封面偏移 → 从该 record 里找完整 JPEG（0 = 未指定，跳过走逐 record 兜底）
        if cover_off is not None and 0 < cover_off < len(offs):
            start = offs[cover_off]
            end = offs[cover_off + 1] if cover_off + 1 < len(offs) else os.path.getsize(path)
            f.seek(start)
            blob = f.read(min(max(end - start, 0), 4 << 20))
            img = _first_image(blob)
            if img:
                out["cover"] = img
    except Exception:
        pass
    finally:
        if f:
            try:
                f.close()
            except Exception:
                pass
    if out.get("cover"):
        return out

    # 兜底：EXTH 201 缺失/为 0 时逐 record 探测图片签名，取第一张大图。
    # （旧版只盲扫前 60 条，而 azw3 的文本区动辄上百条 record，封面资源
    #   排在其后，永远扫不到；文本 record 是 PalmDOC 压缩数据，不会以
    #   JPEG/PNG 签名开头，逐条读 4 字节判断既准又快。）
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f2:
            data = f2.read(78)
            nrec = struct.unpack(">H", data[76:78])[0]
            offs = []
            for _ in range(nrec):
                b = f2.read(8)
                if len(b) < 8:
                    break
                offs.append(struct.unpack(">I", b[:4])[0])
            for i in range(1, len(offs)):
                start = offs[i]
                end = offs[i + 1] if i + 1 < len(offs) else size
                if end - start < 3000:
                    continue
                f2.seek(start)
                sig = f2.read(8)
                if not (sig.startswith(b"\xff\xd8\xff") or sig.startswith(b"\x89PNG\r\n\x1a\n")):
                    continue
                f2.seek(start)   # 回到 record 起点：sig 读取推进了文件位置，直接续读会切掉 SOI 头
                blob = f2.read(min(end - start, 4 << 20))
                img = _first_image(blob)
                if img and len(img) > 3000:
                    out["cover"] = img
                    break
    except Exception:
        pass
    return out


def _first_image(blob: bytes) -> bytes | None:
    """从二进制块里取第一张完整 JPEG / PNG。"""
    sj = blob.find(b"\xff\xd8\xff")
    sp = blob.find(b"\x89PNG\r\n\x1a\n")
    cands = []
    if sj >= 0:
        e = blob.find(b"\xff\xd9", sj + 3)
        cands.append(blob[sj: e + 2] if e > 0 else blob[sj:])
    if sp >= 0:
        e = blob.find(b"IEND", sp)
        cands.append(blob[sp: e + 8] if e > 0 else blob[sp:])
    cands = [c for c in cands if len(c) > 2000]
    return max(cands, key=len) if cands else None


# ---------- PDF ----------

def pdf_meta(path: str) -> dict:
    out: dict = {}
    try:
        import fitz  # PyMuPDF
    except Exception:
        return out
    try:
        with fitz.open(path) as doc:
            md = doc.metadata or {}
            if md.get("title"):
                out["title"] = _plausible(md["title"], 120)
            if md.get("author"):
                out["author"] = _plausible(md["author"], 60)
            if md.get("subject"):
                out["description"] = _clean_text(md["subject"])
            if doc.page_count:
                pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                out["cover"] = pix.tobytes("png")
    except Exception:
        pass
    return out


# ---------- Calibre 兜底 ----------

def calibre_cover(path: str, ext: str) -> dict:
    """用 ebook-convert 把 azw3/mobi 转成临时 epub，再走 epub 解析。"""
    conv = find_calibre()
    if not conv:
        return {}
    tmpdir = tempfile.mkdtemp(prefix="flbcv")
    try:
        dst = os.path.join(tmpdir, "out.epub")
        p = subprocess.run([conv, path, dst, "--no-default-epub-cover"],
                           capture_output=True, timeout=180,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        if p.returncode == 0 and os.path.isfile(dst):
            return epub_meta(dst)
    except Exception:
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {}


# ---------- 统一入口 ----------

def extract(path: str, ext: str, allow_calibre: bool = True) -> dict:
    ext = (ext or "").lower().lstrip(".")
    if ext == "epub":
        m = epub_meta(path)
    elif ext in ("mobi", "azw", "azw3", "azw4", "prc", "pdb", "kfx"):
        m = mobi_meta(path)
    elif ext == "pdf":
        m = pdf_meta(path)
    else:
        m = {}
    if not m.get("cover") and allow_calibre and ext in ("azw3", "azw", "mobi", "prc", "pdb", "fb2", "lit"):
        m2 = calibre_cover(path, ext)
        for k, v in m2.items():
            m.setdefault(k, v)
    return m


def save_cover(img_bytes: bytes, file_id: int) -> bool:
    """保存封面：统一转 JPEG，最长边 480（详情面板展示足够），另存 160 宽小图给书架用。"""
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(img_bytes)) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            big = im.copy()
            big.thumbnail((480, 720), Image.LANCZOS)
            big.save(cover_path(file_id), "JPEG", quality=80, optimize=True)
            small = im.copy()
            small.thumbnail((160, 240), Image.LANCZOS)
            small.save(cover_path(file_id, "s"), "JPEG", quality=78, optimize=True)
        return True
    except Exception:
        return False


def fit_titles(conn, file_id: int, meta: dict) -> None:
    """把提取到的元数据合并进 meta 表（保留已有字段）。"""
    row = conn.execute("SELECT json FROM meta WHERE file_id=?", (file_id,)).fetchone()
    base = {}
    if row:
        try:
            base = json.loads(row["json"])
        except Exception:
            base = {}
    for k in ("title", "author", "description", "publisher", "year", "language", "subjects"):
        v = meta.get(k)
        if not v:
            continue
        old = base.get(k)
        # 提取到的信息比文件名解析出来的更可信，但要避免把垃圾覆盖好数据
        if k == "title":
            base["meta_title"] = v
            if not old or len(str(old)) < 2:
                base["title"] = v
        else:
            base[k] = v
    conn.execute("INSERT OR REPLACE INTO meta(file_id, json) VALUES(?,?)",
                 (file_id, json.dumps(base, ensure_ascii=False)))


# ---------- 拼音排序 ----------

_pinyin_cache: dict[str, str] = {}


def sort_key(name: str) -> str:
    """中文按拼音首字母排序；没有 pypinyin 时退化。"""
    if not name:
        return ""
    k = _pinyin_cache.get(name)
    if k is not None:
        return k
    try:
        from pypinyin import lazy_pinyin
        k = "".join(lazy_pinyin(name)).lower()
    except Exception:
        k = name.lower()
    if len(_pinyin_cache) < 50000:
        _pinyin_cache[name] = k
    return k


# ---------- 电子书文本提取（阅读器 + 全文搜索共用） ----------

_HTML_TAG = re.compile(r"<[^>]+>")


def _html_to_text(raw: bytes) -> str:
    txt = raw.decode("utf-8", "replace")
    txt = re.sub(r"<(script|style)\b.*?</\1>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", txt, flags=re.I)
    txt = _HTML_TAG.sub(" ", txt)
    txt = (txt.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
              .replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'"))
    txt = re.sub(r"[ \t\r\f\v]+", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def epub_chapters(path: str, max_chars: int = 60_000_000) -> tuple[str, list[tuple[str, str]]]:
    """
    返回 (书名, [(章节标题, 正文), ...])。
    按 spine 顺序读 xhtml，保证阅读顺序正确。
    """
    chapters: list[tuple[str, str]] = []
    title = ""
    try:
        zf = zipfile.ZipFile(path)
    except Exception:
        return title, chapters
    with zf:
        try:
            names = set(zf.namelist())
            opf_path = None
            if "META-INF/container.xml" in names:
                m = re.search(rb'full-path="([^"]+)"', zf.read("META-INF/container.xml"))
                if m:
                    opf_path = m.group(1).decode("utf-8", "replace")
            if not opf_path:
                cands = [n for n in names if n.lower().endswith(".opf")]
                opf_path = cands[0] if cands else None
            if not opf_path:
                return title, chapters
            opf = zf.read(opf_path)
            opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
            mt = re.search(rb"<(?:\w+:)?title[^>]*>(.*?)</(?:\w+:)?title>", opf, re.S | re.I)
            if mt:
                title = _clean_text(mt.group(1).decode("utf-8", "replace"), 120) or ""

            items: dict[str, str] = {}
            for tag in re.findall(rb"<(?:\w+:)?item\b[^>]*>", opf, re.I):
                mi = re.search(rb'\bid\s*=\s*"([^"]*)"', tag, re.I)
                mh = re.search(rb'\bhref\s*=\s*"([^"]*)"', tag, re.I)
                mtype = re.search(rb'\bmedia-type\s*=\s*"([^"]*)"', tag, re.I)
                if not (mi and mh):
                    continue
                if mtype and b"html" not in mtype.group(1).lower():
                    continue
                items[mi.group(1).decode()] = mh.group(1).decode("utf-8", "replace")

            order = []
            spine = re.search(rb"<(?:\w+:)?spine\b[^>]*>(.*?)</(?:\w+:)?spine>", opf, re.S | re.I)
            if spine:
                for m in re.finditer(rb'idref\s*=\s*"([^"]*)"', spine.group(1), re.I):
                    iid = m.group(1).decode()
                    if iid in items:
                        order.append(items[iid])
            if not order:
                order = [h for h in items.values() if h.lower().endswith((".xhtml", ".html", ".htm"))]

            from urllib.parse import unquote
            total = 0
            for href in order:
                fn = unquote(href.lstrip("./"))
                full = (opf_dir + fn) if not fn.startswith("/") else fn.lstrip("/")
                if full not in names:
                    base = full.rsplit("/", 1)[-1]
                    hit = [n for n in names if n.rsplit("/", 1)[-1] == base]
                    if not hit:
                        continue
                    full = hit[0]
                try:
                    raw = zf.read(full)
                except Exception:
                    continue
                txt = _html_to_text(raw)
                if len(txt) < 8:
                    continue
                # 章节标题：第一个 h1-h3，取不到就用正文首行
                h = re.search(rb"<h[1-3][^>]*>(.*?)</h[1-3]>", raw, re.S | re.I)
                head = _clean_text(h.group(1).decode("utf-8", "replace"), 60) if h else ""
                if not head:
                    head = txt.split("\n", 1)[0][:40]
                chapters.append((head or f"第 {len(chapters) + 1} 节", txt))
                total += len(txt)
                if total > max_chars:
                    break
        except Exception:
            pass
    return title, chapters


def mobi_pages(path: str) -> tuple[str, list[tuple[str, str]]]:
    """
    MOBI/AZW3 纯 Python 解压：PalmDOC LZ77 + 可选 HUFF/CDIC。
    纯标准库实现较复杂，这里走 ebook-convert（Calibre）转 txt 的路线；
    没有 Calibre 时返回空，前端提示"请安装 Calibre 或下载原文件"。
    """
    conv = find_calibre()
    if not conv:
        return "", []
    tmpdir = tempfile.mkdtemp(prefix="flbtxt")
    try:
        dst = os.path.join(tmpdir, "out.txt")
        p = subprocess.run([conv, path, dst], capture_output=True, timeout=300,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        if p.returncode != 0 or not os.path.isfile(dst):
            return "", []
        raw = open(dst, "rb").read()
        txt = raw.decode("utf-8", "replace")
        # 按 "第x章" 之类切章，切不出来就整体一章
        parts = re.split(r"\n(?=(?:第[0-9一二三四五六七八九十百千零]+[章节回卷篇]|Chapter\s+\d+))",
                         txt)
        chapters = [(p.split("\n", 1)[0][:40] or f"第 {i + 1} 节", p.strip())
                    for i, p in enumerate(parts) if p.strip()]
        return chapters[0][1][:80] if chapters else "", chapters
    except Exception:
        return "", []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
