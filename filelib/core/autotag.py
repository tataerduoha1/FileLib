"""
autotag.py —— 自动打标签（纯标准库，无 LLM 依赖）

三层信号，全部产出「auto:」前缀标签（file_tags.source='auto'，tags.grp='auto'）：
  1. 规则层   ：文件名/路径解析（复用 patterns.parse_relpath，秒出，不碰磁盘）
  2. 元数据层 ：音频 ID3 / EPUB zip+xml / PDF 书元数据 / 图片 EXIF
  3. 正文层   ：复用 file_text 全文缓存（中文 n-gram + 词频 + 停用词，英文按词计）

类型范围（与决策一致）：
  txt/md/epub/pdf/mobi… → 正文关键词（无正文时退回书元数据）
  音频                  → ID3（歌手/专辑/流派/年份）
  图片                  → EXIF（拍摄年份/相机）+ 文件夹名
  视频                  → 文件名规则（清晰度/系列/年份）+ 文件夹名
  压缩包/字体/软件等    → 仅文件名规则

精简策略（v1.0.8，控制标签爆炸）：
  · 单文件上限 5 个，标签最长 16 字；正文关键词只取 3 个
  · 优先级 = 规则层（文件名/多级父文件夹）> 元数据 > 正文
  · 多级父文件夹最多吸收 2 个新信息，组织性目录（合集/按xx分/5000本…）不算信息
  · 纯数字（年份除外）/十六进制哈希样标签直接丢弃
  · 全局再在聚合层降噪：占比 >30% 的泛化标签、使用数 <3 的低频标签会被标记，
    侧栏与标签管理器可识别（详见 store._all_tags / 标签管理器）

精准度增强（v1.0.9）：
  · 技术/格式类 quality（HD/1080P/x264/PART1…）不再进标签——全库通用无区分度；
    保留 无码/有码/中字/字幕 等有筛选价值的语义标记
  · 组织/分发词（合集/大全/下载/网盘/资源站…）出现在候选中即丢弃，
    补上「某某合集12345678abcd」这类品牌前缀形态（books.py 的 BULK_* 只覆盖数字开头形态）
  · 噪声识别扩展：域名水印、数字区间（346-374）、≥12 位十六进制片段
  · 正文关键词：头部 3000 字双倍计权（主题词倾向在开头/简介）、
    选择顺序 3字中文→英文→2字中文（2 字假词只做兜底）、英文专名加权；
    且与规则层/元数据层已产出标签互为子串（共有信息）的不重复

人工标签永远优先：详情面板人工在前、自动折叠；set_tags 只替换 source='user'。
"""
from __future__ import annotations

import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

from .books import BULK_DIR_RE, BULK_HINT_RE, STRUCT_DIRS
from .db import connect
from .patterns import parse_relpath
from .scanner import long_path

MAX_TAGS = 5           # 单个文件自动标签上限（少而精：规则/元数据优先，正文只补 3 个）
_MAX_TAG_LEN = 16      # 单个标签值上限，超长丢弃（文件名常混入哈希等噪声）

# ---------- 停用词 ----------

_ZH_STOP = {
    "我们", "你们", "他们", "自己", "什么", "这个", "那个", "没有", "就是", "可以",
    "一个", "一下", "所以", "因为", "但是", "如果", "这样", "那样", "还是", "已经",
    "时候", "现在", "出来", "起来", "东西", "事情", "问题", "地方", "知道", "觉得",
    "的话", "这本", "一个", "一些", "不过", "之后", "然后", "其他", "最后", "第一",
    "中国", "世界", "作者", "读者", "出版", "译", "简介", "目录", "正文", "完整",
    "最新", "整理", "收藏", "高清", "全集", "正版", "扫描", "电子", "文件",
}
_EN_STOP = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "her",
    "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "man",
    "new", "now", "old", "see", "two", "way", "who", "boy", "did", "its", "let",
    "put", "say", "she", "too", "use", "that", "with", "have", "this", "will",
    "your", "from", "they", "know", "want", "been", "good", "much", "some",
    "time", "very", "when", "come", "here", "just", "like", "long", "make",
    "part", "over", "such", "take", "than", "them", "well", "were", "www",
    "com", "http", "https", "pdf", "epub", "txt", "mobi", "azw3", "html", "isbn",
}
_ZH_STOP_CHARS = set("的了是在有和不被这那也都就很又把对为与之其或等")

# ---------- 正文关键词（纯统计，秒级） ----------

_HEAD = 3000   # 头部加权窗口：主题词倾向出现在开头/简介，头部命中视为双倍权重


def keyword_tags(body: str, top: int = 3, exclude=()) -> list[str]:
    """从正文提关键词：中文 2~3 字组词频 + 英文词频，头部加权、长词优先。

    默认只取 3 个：正文关键词区分度天然弱于文件名/文件夹，多给只会稀释标签质量。
    提分手段：
      · 邻接多样性：候选的左右邻接字种类合计 ≥3 才可信——页眉/版权页/重复句式
        产出的假词链（「一段关」「段关于」）邻接固定，直接出局；句首/句尾记为
        自由边界（""），不影响判定
      · 头部 3000 字内出现的候选双倍计权（主题词倾向在标题/简介附近）
      · 选择顺序 3 字中文 → 英文 → 2 字中文（2 字假词最多，只做兜底），
        且与已选词及 exclude 互为子串（共有信息）的一律不重复
      · 英文专名（首字母大写占比高，人名/地名/作品名）加权 1.3
    exclude：规则层/元数据层已产出的标签（裸值），与其重复的正文候选直接跳过。
    """
    text = (body or "")[:20000]
    if len(text) < 100:
        return []

    def zh_ngrams(s: str, with_ctx: bool):
        """中文 2/3 字滑窗计数；with_ctx 时同时记录左右邻接字集合（判真词用）。"""
        c: Counter = Counter()
        lc: dict = {}
        rc: dict = {}
        for seg in re.sub(r"[^\u4e00-\u9fff]+", " ", s).split():
            if len(seg) < 2:
                continue
            for n in (2, 3):
                for i in range(len(seg) - n + 1):
                    g = seg[i:i + n]
                    if g in _ZH_STOP or any(ch in _ZH_STOP_CHARS for ch in g):
                        continue
                    c[g] += 1
                    if with_ctx:
                        lc.setdefault(g, set()).add(seg[i - 1] if i > 0 else "")
                        rc.setdefault(g, set()).add(seg[i + n] if i + n < len(seg) else "")
        return c, lc, rc

    def en_counts(s: str) -> tuple[Counter, Counter]:
        words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,15}", s)
                 if w.lower() not in _EN_STOP]
        caps = Counter(w for w in re.findall(r"\b[A-Z][a-zA-Z]{2,15}\b", s)
                       if w.lower() not in _EN_STOP)
        return Counter(words), caps

    cnt, lc, rc = zh_ngrams(text, True)
    hcnt = zh_ngrams(text[:_HEAD], False)[0]
    ecnt, ecap = en_counts(text)
    ehcnt, _ = en_counts(text[:_HEAD])

    cands: list[tuple[str, int]] = []
    for w, c in cnt.items():
        if (len(w) == 3 and c >= 3) or (len(w) == 2 and c >= 5):
            # 邻接多样性：真词左右邻接字丰富；页眉/版权页等重复句式的假词链邻接固定
            if len(lc.get(w, ())) + len(rc.get(w, ())) >= 3:
                cands.append((w, c + hcnt.get(w, 0)))
    for w, c in ecnt.items():
        if c >= 3:
            s = c + ehcnt.get(w, 0)
            if ecap.get(w, 0) / c > 0.5:
                s = int(s * 1.3)          # 专名加权
            cands.append((w, s))

    # 三轮优先级：3 字中文 → 英文 → 2 字中文；同分时头部命中者优先
    zh3 = sorted((x for x in cands if len(x[0]) == 3 and not x[0].isascii()),
                 key=lambda x: (-x[1], -hcnt.get(x[0], 0), x[0]))
    en = sorted((x for x in cands if x[0].isascii()), key=lambda x: (-x[1], x[0]))
    zh2 = sorted((x for x in cands if len(x[0]) == 2 and not x[0].isascii()),
                 key=lambda x: (-x[1], -hcnt.get(x[0], 0), x[0]))

    out: list[str] = []
    pool = list(exclude)
    for w, _s in (*zh3, *en, *zh2):
        if _dup_info(w, pool):
            continue
        out.append(w)
        pool.append(w)
        if len(out) >= top:
            break
    return out


# ---------- 元数据解析（各自独立容错） ----------

def parse_id3(path: str) -> dict:
    """ID3v2.3/2.4 + v1 兜底：title/artist/album/genre/year。"""
    out: dict = {}
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if head[:3] == b"ID3" and head[3] in (3, 4):
                size = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 15) \
                     | ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
                body = f.read(min(size, 512 * 1024))
                pos = 0
                want = {"TIT2": "title", "TPE1": "artist", "TALB": "album",
                        "TCON": "genre", "TYER": "year", "TDRC": "year"}
                while pos + 10 <= len(body):
                    fid = body[pos:pos + 4]
                    if not fid.strip(b"\x00 "):
                        break
                    if head[3] == 4:
                        fsize = ((body[pos + 4] & 0x7F) << 21) | ((body[pos + 5] & 0x7F) << 15) \
                              | ((body[pos + 6] & 0x7F) << 7) | (body[pos + 7] & 0x7F)
                    else:
                        fsize = struct.unpack(">I", body[pos + 4:pos + 8])[0]
                    if fsize <= 0 or pos + 10 + fsize > len(body):
                        break
                    if fid in want and fsize > 1:
                        raw = body[pos + 11:pos + 10 + fsize]
                        try:
                            enc = body[pos + 10]
                            val = (raw.decode("utf-16-le" if enc == 1 else
                                              "utf-8" if enc == 3 else "latin-1",
                                              "replace").strip("\x00").strip())
                            val = val.split("\x00")[0].strip()
                            if val:
                                out[want[fid]] = val[:40]
                        except Exception:
                            pass
                    pos += 10 + fsize
            else:
                f.seek(-128, 2)
                tail = f.read(128)
                if tail[:3] == b"TAG":
                    for key, a, b in (("title", 3, 33), ("artist", 33, 63),
                                      ("album", 63, 93), ("year", 93, 97)):
                        v = tail[a:b].split(b"\x00")[0].decode("latin-1", "replace").strip()
                        if v:
                            out[key] = v[:40]
    except OSError:
        pass
    return out


def parse_epub(path: str) -> dict:
    """EPUB：container.xml → OPF → dc:title/creator/subject。"""
    out: dict = {}
    try:
        with zipfile.ZipFile(path) as z:
            rootfile = "OEBPS/content.opf"
            try:
                c = ET.fromstring(z.read("META-INF/container.xml"))
                rf = c.find(".//{*}rootfile")
                if rf is not None and rf.get("full-path"):
                    rootfile = rf.get("full-path")
            except (KeyError, ET.ParseError):
                pass
            opf = ET.fromstring(z.read(rootfile))
            ns = {"dc": "http://purl.org/dc/elements/1.1/"}
            t = opf.find(".//dc:title", ns)
            if t is not None and t.text:
                out["title"] = t.text.strip()[:60]
            a = opf.find(".//dc:creator", ns)
            if a is not None and a.text:
                out["author"] = a.text.strip()[:40]
            subs = [s.text.strip() for s in opf.findall(".//dc:subject", ns)
                    if s.text and s.text.strip()]
            if subs:
                out["subjects"] = subs[:5]
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError):
        pass
    return out


def parse_exif(path: str) -> dict:
    """JPEG EXIF 极简解析：Make/Model/DateTimeOriginal（拍摄时间）。"""
    out: dict = {}
    try:
        with open(path, "rb") as f:
            data = f.read(256 * 1024)
        if data[:2] != b"\xff\xd8":
            return out
        i = 2
        seg = None
        while i + 4 < len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
                seg = data[i + 10:i + 2 + seglen]
                break
            if marker == 0xDA:            # SOS：EXIF 在这之前
                break
            i += 2 + seglen
        if not seg or seg[:2] not in (b"II", b"MM"):
            return out
        endian = "<" if seg[:2] == b"II" else ">"

        def read_ifd(off: int) -> tuple[dict[int, tuple], int]:
            n = struct.unpack(endian + "H", seg[off:off + 2])[0]
            entries = {}
            for k in range(n):
                p = off + 2 + k * 12
                if p + 12 > len(seg):
                    break
                tag, typ, cnt = struct.unpack(endian + "HHI", seg[p:p + 8])
                valpos = p + 8
                sz = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}.get(typ, 1) * cnt
                if sz > 4:
                    vo = struct.unpack(endian + "I", seg[valpos:valpos + 4])[0]
                    valpos = vo
                if valpos + sz <= len(seg):
                    entries[tag] = (typ, cnt, valpos)
            nxt = struct.unpack(endian + "I",
                                seg[off + 2 + n * 12:off + 6 + n * 12])[0] \
                if off + 6 + n * 12 <= len(seg) else 0
            return entries, nxt

        def ascii(e) -> str:
            typ, cnt, pos = e
            try:
                return seg[pos:pos + cnt].split(b"\x00")[0].decode("ascii", "replace").strip()
            except Exception:
                return ""

        ifd0, nxt = read_ifd(8)
        make, model = ascii(ifd0.get(0x010F, (0, 0, 0))), ascii(ifd0.get(0x0110, (0, 0, 0)))
        if make:
            out["make"] = make[:24]
        if model:
            out["model"] = model[:24]
        exif_ptr = ifd0.get(0x8769)
        if exif_ptr:
            sub, _ = read_ifd(exif_ptr[2])
            dt = ascii(sub.get(0x9003, (0, 0, 0))) or ascii(ifd0.get(0x0132, (0, 0, 0)))
            m = re.match(r"(\d{4})", dt)
            if m:
                out["year"] = m.group(1)
    except (OSError, struct.error):
        pass
    return out


# ---------- 规则层 ----------

# 首尾的装饰性符号：目录名/文件名常见「！！xxx」「【xxx】」「xxx -」之类
_DECOR = " \t\v\r\n.。,，、;；:：！!？?~～·•-_=+*#@#$%^&\"'`|\\/<>《》〈〉「」『』[]【】（）()｛｝{}"

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,}$")

# 技术/格式类 quality：全库通用、不承载内容信息（一千个文件八百个「1080P」），
# 打出来只会稀释标签质量——直接不进标签；保留 无码/有码/中字/字幕 这类有筛选价值的语义标记
_TECH_QUALITY = {
    "HD", "FHD", "UHD", "4K", "1080P", "720P", "480P",
    "AVI", "MP4", "MKV", "WMV", "RMVB", "H264", "H265",
    "X264", "X265", "HEVC", "AAC", "AC3",
    "PART1", "PART2", "CD1", "CD2",
}

# 组织/分发类词：出现在候选标签里即视为组织性目录名（books.py 的 BULK_* 只覆盖
# 「13000本…」「按拼音排序」形态，这里补「某某合集xxx」「xx资源站」等品牌前缀形态）
_ORG_HINT_RE = re.compile(
    r"合集|大全|精选|打包|批量|下载|资源|分享|网盘|种子|磁力|迅雷|"
    r"百度云|夸克|阿里云盘|整理版|收藏夹|我的收藏|福利",
    re.IGNORECASE,
)

# 域名样片段（资源站水印）：music.163.com / xxx.vip 之类
_DOMAIN_RE = re.compile(
    r"[a-z0-9-]+\.(com|net|org|cc|tv|me|xyz|info|vip|top|site|club|icu)\b",
    re.IGNORECASE,
)
# 数字区间/片段编号：「346-374」「01~05」——拆分产物，不是主题
_RANGE_RE = re.compile(r"^\d+\s*[-~—]\s*\d+$")
# 长十六进制片段（≥12 位）：哈希/资源 ID 混在词中，如「某某合集12345678abcd」
_LONG_HEX_RE = re.compile(r"[0-9a-f]{12,}", re.IGNORECASE)


def _strip_decor(s: str) -> str:
    return s.strip(_DECOR)


def _is_noise(v: str) -> bool:
    """噪声标签：纯数字（年份除外）、哈希样（整体或长片段）、域名、数字区间、组织词。"""
    if _YEAR_RE.match(v):
        return False
    if v.isdigit() or _HEX_RE.match(v) or _LONG_HEX_RE.search(v):
        return True
    if _DOMAIN_RE.search(v) or _RANGE_RE.match(v) or _ORG_HINT_RE.search(v):
        return True
    return False


def _clean(v) -> str | None:
    s = _strip_decor(str(v or ""))
    if not s or len(s) < 2 or len(s) > _MAX_TAG_LEN or "\n" in s or _is_noise(s):
        return None
    return s


def _dup_info(v: str, pool) -> bool:
    """v 与池中标签是否共享同一段信息（互为子串，较短者 ≥2 字视为重复）。

    例：父文件夹「轻易不可更改 书合集分类」⊃ 已有标签「轻易不可更改」→ 重复；
    系列名「三体1 刘慈欣」⊃ 文件夹「刘慈欣」→ 文件名解析优先，文件夹跳过。"""
    for t in pool:
        if len(t) >= 2 and t in v:
            return True
        if len(v) >= 2 and v in t:
            return True
    return False


def rule_tags(rel_dir: str, name: str, kind: str) -> list[str]:
    """文件名/路径规则：清晰度、系列、年份、题材 + 多级父文件夹（少而精）。

    文件夹信号：从最近一级向上扫，最多吸收 2 个「新信息」标签——
      · 跳过组织性/批量目录（复用 books.py 的 BULK_DIR_RE / BULK_HINT_RE / STRUCT_DIRS）
      · 与文件名解析结果、更近层级文件夹互为子串（共有信息）的只留一个
      · 文件名解析结果优先于文件夹名（信息重复时只留一个）
    """
    tags: list[str] = []
    qualities: list = []
    extras: list = []
    try:
        p = parse_relpath(rel_dir, name)      # 返回 Parsed 对象（属性访问）
        qualities = list(p.quality or [])
        extras = [p.series, p.topic,
                  str(p.year) if p.year and 1900 <= p.year <= 2100 else None]
    except Exception:
        pass
    for q in qualities:
        if str(q).upper() in _TECH_QUALITY:
            continue                      # 编码/格式/分卷参数不是内容标签
        v = _clean(q)
        if v:
            tags.append(v)
    for v in extras:
        v = _clean(v)
        if not v:
            continue
        if v in STRUCT_DIRS or v.lower() in STRUCT_DIRS:
            continue
        if BULK_DIR_RE.match(v) or BULK_HINT_RE.search(v):
            continue
        if _dup_info(v, tags):
            continue
        tags.append(v)
    # 多级父文件夹：最近两级有效值
    segs = [s for s in (rel_dir or "").replace("\\", "/").split("/") if s.strip()]
    n_dir = 0
    for seg in reversed(segs):
        s = seg.strip()
        if BULK_DIR_RE.match(s) or BULK_HINT_RE.search(s):
            continue
        if s.lower() in STRUCT_DIRS or s in STRUCT_DIRS:
            continue
        v = _clean(s)
        if not v or _dup_info(v, tags):
            continue
        tags.append(v)
        n_dir += 1
        if n_dir >= 2:
            break
    return tags


# ---------- 汇总入口 ----------

_TEXT_EXTS = {"txt", "md", "markdown", "pdf", "epub", "mobi", "azw3", "azw", "prc", "fb2"}


def compute_tags(file_id: int, root_path: str, rel_dir: str,
                 name: str, ext: str, kind: str,
                 body: str | None = None) -> list[str]:
    """计算一个文件的自动标签（含 auto: 前缀），不写库。
    body=None 时自行查 file_text；批量管线应把正文缓存 JOIN 进来直接传入，
    避免工作线程各开数据库连接。"""
    src = os.path.join(root_path, rel_dir.replace("/", os.sep), name)
    on_disk = os.path.isfile(src)
    ext_l = (ext or "").lower()
    values: list[str] = []

    def add(*vals) -> None:
        for v in vals:
            v = _clean(v)
            if v:
                values.append(v)

    # ① 规则层：所有类型都有
    add(*rule_tags(rel_dir, name, kind))

    # ② 元数据层
    if kind == "audio" and on_disk:
        id3 = parse_id3(long_path(src))
        add(id3.get("artist"), id3.get("album"), id3.get("genre"), id3.get("year"))
    elif kind == "image" and on_disk and ext_l in ("jpg", "jpeg", "tif", "tiff"):
        ex = parse_exif(long_path(src))
        add(ex.get("year"), ex.get("make"), ex.get("model"))
    elif kind == "book" and on_disk and ext_l == "epub":
        ep = parse_epub(long_path(src))
        add(*(ep.get("subjects") or []))
        add(ep.get("author"))

    # ③ 正文层：文档类从 file_text 拿缓存正文（不重读磁盘，秒级）
    if ext_l in _TEXT_EXTS or kind == "book":
        if body is None:
            body = ""
            try:
                conn = connect()
                try:
                    row = conn.execute(
                        "SELECT body FROM file_text WHERE file_id=?", (file_id,)).fetchone()
                    body = row["body"] if row else ""
                finally:
                    conn.close()
            except Exception:
                body = ""
        if body:
            add(*keyword_tags(body, exclude=values))   # 与文件名/元数据已有标签共有信息 → 不重复
        elif kind == "book" and on_disk and ext_l in ("mobi", "azw3", "azw", "prc"):
            pass   # MOBI 元数据解析复杂度高，本版先靠规则层，正文有缓存则已覆盖

    return [f"auto:{v}" for v in dict.fromkeys(values)][:MAX_TAGS]


def apply_autotag(file_id: int, root_path: str, rel_dir: str,
                  name: str, ext: str, kind: str) -> int:
    """计算并写入一个文件的自动标签。返回实际新增标签数。自带连接。

    幂等：先把该文件旧的 auto 标签清掉再写，重跑结果一致；
    人工标签（source='user'）完全不碰。
    """
    tags = compute_tags(file_id, root_path, rel_dir, name, ext, kind)
    conn = connect()
    try:
        conn.execute("DELETE FROM file_tags WHERE file_id=? AND source='auto'", (file_id,))
        n = 0
        for t in tags:
            conn.execute("INSERT OR IGNORE INTO tags(name, grp) VALUES(?, 'auto')", (t,))
            cur = conn.execute(
                "INSERT OR IGNORE INTO file_tags(file_id, tag_id, source) "
                "SELECT ?, id, 'auto' FROM tags WHERE name=?", (file_id, t))
            n += cur.rowcount if cur.rowcount > 0 else 0
        conn.commit()
        return n
    finally:
        conn.close()
