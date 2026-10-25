"""
filelib.core.bookmeta —— 从 EPUB / MOBI / AZW3 里提取封面和元数据

原则：
  * 只读文件，绝不修改
  * 纯标准库（zipfile / struct / re）
  * 提取失败返回空 dict，上层回退到文件名解析
"""
from __future__ import annotations

import io
import re
import struct
import zipfile
from pathlib import Path

# ---------- EPUB ----------

_ITEM_TAG_RE = re.compile(rb"<item\b[^>]*>", re.S | re.I)
_ATTR_RE = {a: re.compile(rf'\b{a}\s*=\s*"([^"]*)"'.encode(), re.I) for a in
            ("id", "href", "media-type", "properties")}
_EPUB_TITLE_RE = re.compile(rb"<dc:title[^>]*>(.*?)</dc:title>", re.S | re.I)
_EPUB_CREATOR_RE = re.compile(rb"<dc:creator[^>]*>(.*?)</dc:creator>", re.S | re.I)
_GUIDE_COVER_RE = re.compile(
    rb'<reference\b[^>]*type\s*=\s*"cover"[^>]*>', re.S | re.I)

_IMG_MIME = ("image/jpeg", "image/png", "image/gif", "image/webp")
_JPEG_SOI = b"\xff\xd8\xff"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"

ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")


def _clean_meta_str(s: str | None) -> str | None:
    if not s:
        return None
    s = ZERO_WIDTH_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _plausible_text(s: str | None, max_len: int = 160) -> str | None:
    """元数据里的 title/author 常被转换工具填进垃圾。挡住明显不对的。"""
    s = _clean_meta_str(s)
    if not s:
        return None
    low = s.lower()
    # 文件名当标题：xxx.jpg / xxx.epub
    if re.search(r"\.(jpe?g|png|gif|opf|ncx|html?)$", low):
        return None
    # 纯十六进制哈希 / UUID
    if re.fullmatch(r"[0-9a-f]{16,}", low.replace("-", "")):
        return None
    if len(s) > max_len:
        s = s[:max_len].rstrip() + "…"
    return s


def _sniff_image(data: bytes) -> str | None:
    if not data:
        return None
    if data[:3] == _JPEG_SOI:
        return "image/jpeg"
    if data[:8] == _PNG_SIG:
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def epub_meta(path: str) -> dict:
    """
    EPUB 是 zip 包。封面定位顺序：
      1. OPF 里 properties 含 cover-image 的 item（EPUB3 标准）
      2. <meta name="cover" content="id"> 指向的 item（EPUB2 惯例）
      3. guide 里 <reference type="cover">
      4. manifest 里名字含 cover 的图片
      5. zip 里任何路径含 cover 的图片（部分转换器不写 manifest）
    """
    out: dict = {}
    try:
        zf = zipfile.ZipFile(path)
    except Exception:
        return out
    try:
        names = zf.namelist()
        container = zf.read("META-INF/container.xml")
        m = re.search(rb'full-path="([^"]+)"', container)
        if not m:
            return out
        opf_path = m.group(1).decode()
        opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        opf = zf.read(opf_path)

        if t := _plausible_text(_first_group(_EPUB_TITLE_RE, opf)):
            out["title"] = t
        if a := _plausible_text(_first_group(_EPUB_CREATOR_RE, opf), 60):
            if not out.get("title") or a != out["title"]:
                out["author"] = a

        # ---- manifest items：逐标签解析，属性顺序无关 ----
        items: dict[str, tuple[str, str, str]] = {}   # id -> (href, mime, properties)
        for tagm in _ITEM_TAG_RE.finditer(opf):
            tag = tagm.group(0)
            mid = _ATTR_RE["id"].search(tag)
            mhref = _ATTR_RE["href"].search(tag)
            if not mid or not mhref:
                continue
            mime = _ATTR_RE["media-type"].search(tag)
            props = _ATTR_RE["properties"].search(tag)
            items[mid.group(1).decode()] = (
                mhref.group(1).decode(),
                mime.group(1).decode() if mime else "",
                props.group(1).decode() if props else "",
            )

        def load(href: str) -> bytes:
            full = (opf_dir + href.lstrip("./")) if not href.startswith("/") else href.lstrip("/")
            try:
                return zf.read(full)
            except KeyError:
                low = full.lower()
                for n in names:
                    if n.lower() == low:
                        return zf.read(n)
                # 有些 href 带目录前缀但包内是平铺的
                tail = low.rsplit("/", 1)[-1]
                for n in names:
                    if n.lower().endswith(tail):
                        return zf.read(n)
                return b""

        candidates: list[str] = []
        # 1) EPUB3 cover-image
        for iid, (href, mime, props) in items.items():
            if "cover-image" in props:
                candidates.append(href)
        # 2) EPUB2 meta cover
        if not candidates:
            m2 = re.search(rb'<meta\s[^>]*name\s*=\s*"cover"\s[^>]*content\s*=\s*"([^"]+)"', opf)
            if not m2:
                m2 = re.search(rb'<meta\s[^>]*content\s*=\s*"([^"]+)"\s[^>]*name\s*=\s*"cover"', opf)
            if m2:
                iid = m2.group(1).decode()
                if iid in items and items[iid][1].startswith("image/"):
                    candidates.append(items[iid][0])
        # 3) guide reference
        if not candidates:
            gm = _GUIDE_COVER_RE.search(opf)
            if gm:
                hm = re.search(rb'href\s*=\s*"([^"]+)"', gm.group(0))
                if hm:
                    candidates.append(hm.group(1).decode())
        # 4) 名字含 cover 的 manifest 图片
        if not candidates:
            for iid, (href, mime, props) in items.items():
                if mime.startswith("image/") and "cover" in href.lower():
                    candidates.append(href)
        # 5) zip 里 cover 命名的图片
        if not candidates:
            for n in names:
                ln = n.lower()
                if re.search(r"cover[^/]*\.(jpe?g|png|gif|webp)$", ln):
                    candidates.append(n)

        for href in candidates:
            data = load(href) if not href.startswith(("http", "/")) else b""
            mime = _sniff_image(data)
            if mime:
                out["cover"] = data
                out["cover_mime"] = mime
                break
        return out
    except Exception:
        return out
    finally:
        try:
            zf.close()
        except Exception:
            pass


def _first_group(rx: re.Pattern, data: bytes) -> str | None:
    m = rx.search(data)
    if not m:
        return None
    inner = re.sub(rb"<!\[CDATA\[|\]\]>", b"", m.group(1))
    return inner.decode("utf-8", "replace").strip()



# ---------- MOBI / AZW3 ----------

EXTH_COVER_OFFSET = 201     # 指向 first image record
EXTH_THUMB_OFFSET = 202


def mobi_meta(path: str) -> dict:
    """
    MOBI：PalmDB 结构 + MOBI header + EXTH。
    EXTH 201 记录给出封面在记录里的偏移索引。
    失败时兜底：从文件头 3MB 里找第一个完整 JPEG（SOI...EOI）。
    """
    out: dict = {}
    try:
        f = open(path, "rb")
    except OSError:
        return out
    try:
        # ---- PalmDB 头：记录数在 offset 76 ----
        f.seek(76)
        nrec = struct.unpack(">H", f.read(2))[0]
        if nrec < 1 or nrec > 8192:
            return out
        recs = []
        for i in range(nrec):
            off = struct.unpack(">I", f.read(4))[0]
            f.read(4)  # attributes + uniqueID
            recs.append(off)
        if len(recs) < 2:
            return out

        # ---- MOBI header ----
        # record0 = PalmDOC 头(16字节) + MOBI 头。MOBI 魔数 'MOBI' 在偏移 16
        # （'BOOKMOBI' 8字节是文件头 60 偏移处的 type/creator 字段）。
        f.seek(recs[0])
        rec0 = f.read((recs[1] - recs[0]) if len(recs) > 1 else (1 << 20))
        if rec0[16:20] != b"MOBI":
            return out
        try:
            mobi_len = struct.unpack(">I", rec0[20:24])[0]   # MOBI 头长度（从16起算）
            exth_flag = struct.unpack(">I", rec0[0x70:0x74])[0]
        except struct.error:
            return out

        # 旧格式（MOBI<6，无 HUFF/CDIC）正文 record 偏移可直接用
        # EXTH 里通常带 title/author
        pos = 16 + mobi_len
        title = None
        author = None
        cover_rec = None
        if exth_flag & 0x40 and pos + 12 < len(rec0) and rec0[pos:pos + 4] == b"EXTH":
            exth_len = struct.unpack(">I", rec0[pos + 4:pos + 8])[0]
            p = pos + 12
            end = min(pos + exth_len, len(rec0))
            while p + 8 <= end:
                rtype, rlen = struct.unpack(">II", rec0[p:p + 8])
                if rlen < 8 or p + rlen > end:
                    break
                val = rec0[p + 8:p + rlen]
                if rtype == 100:
                    author = val.decode("utf-8", "replace").strip() or None
                elif rtype == 503:
                    t = val.decode("utf-8", "replace").strip()
                    title = t or None
                elif rtype == EXTH_COVER_OFFSET and len(val) == 4:
                    cover_rec = struct.unpack(">I", val)[0]
                p += rlen
        # MOBI header 里的 full name（0x54=偏移，0x58=长度，均相对 record0）
        if not title:
            try:
                toff, tlen = struct.unpack(">II", rec0[0x54:0x5C])
                if 0 < tlen < 1024 and 0 <= toff and toff + tlen <= len(rec0):
                    title = rec0[toff:toff + tlen].decode("utf-8", "replace").strip() or None
            except struct.error:
                pass

        if t := _plausible_text(title):
            out["title"] = t
        if a := _plausible_text(author, 60):
            if not out.get("title") or a != out["title"]:
                out["author"] = a

        # ---- 封面 ----
        if cover_rec is not None and 0 < cover_rec < nrec:
            idx = cover_rec + 1  # EXTH 201 的值是"相对 rec0 的记录偏移"
            if idx < nrec:
                start = recs[idx]
                end = recs[idx + 1] if idx + 1 < nrec else None
                f.seek(start)
                chunk = f.read(2 << 20) if end is None else f.read(end - start)
                img = chunk if _sniff_image(chunk[:16]) else _first_jpeg(chunk)
                if img:
                    out["cover"] = img
        # 兜底：逐记录找第一条以 JPEG/PNG 开头的（图片记录不压缩，魔数直接可见）
        if "cover" not in out:
            for i in range(1, min(nrec, 60)):
                f.seek(recs[i])
                head = f.read(16)
                mime = _sniff_image(head)
                if mime:
                    f.seek(recs[i])
                    end = recs[i + 1] if i + 1 < nrec else None
                    chunk = f.read(4 << 20) if end is None else f.read(end - recs[i])
                    img = chunk if _sniff_image(chunk[:16]) else _first_jpeg(chunk)
                    if img:
                        out["cover"] = img
                        break
        if out.get("cover"):
            out["cover_mime"] = _sniff_image(out["cover"][:16]) or "image/jpeg"
        return out
    except Exception:
        return out
    finally:
        try:
            f.close()
        except Exception:
            pass


def _first_jpeg(data: bytes) -> bytes | None:
    """取第一张完整 JPEG（SOI 开头，EOI 结尾）。"""
    if not data:
        return None
    s = data.find(b"\xff\xd8\xff")
    if s < 0:
        return None
    e = data.find(b"\xff\xd9", s + 3)
    if e < 0:
        # 封面被压缩分块截断时，取到末尾（能显示大部分内容）
        return data[s:] if len(data) - s > 8192 else None
    return data[s:e + 2]


def extract_book(path: str, ext: str) -> dict:
    """
    统一入口。返回 {title?, author?, cover?, cover_mime?}，全失败返回 {}。
    """
    ext = ext.lower().lstrip(".")
    if ext in ("epub",):
        return epub_meta(path)
    if ext in ("mobi", "azw", "azw3", "azw4", "prc", "kfx"):
        return mobi_meta(path)
    return {}
