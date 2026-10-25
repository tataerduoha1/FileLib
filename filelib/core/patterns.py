"""
filelib/core/patterns.py —— 从文件名/目录名中提取结构化信息

针对真实目录形态设计：
    盘符:\\0000\\{分类}\\【视频|图片】\\{厂商/系列}\\{作品目录或文件}
例：
    'ABC-123-456-HD'                         -> series=ABC, nums=[123,456]
    '[LAB-019] LABO-5'                       -> code=LAB-019, series=LABO
    'XYZ-789.mp4'                            -> code=XYZ-789
    'QRS-066(AVI)'                           -> code=QRS-066, tag=AVI
    '！【系列名】'                            -> topic='系列名'
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .kinds import parse_dir_label, split_topic, normalize_name

# 常见番号格式：ABC-123 / AB-1234 / ABC123
# 允许前缀含内部连字符（ABC-123），但排除纯数字前缀
CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z][A-Za-z0-9]*)*)[-_ ]?(\d{2,5})(?![0-9])")
# 仅数字编号（ABC-123 这类）
SERIES_NUM_RE = re.compile(r"(?<![0-9])(\d{2,4})(?![0-9])")

# 分辨率/版本修饰
QUALITY_TOKENS = {
    "hd": "HD", "fhd": "FHD", "uhd": "UHD", "4k": "4K", "2160p": "4K",
    "1080p": "1080P", "720p": "720P", "480p": "480P",
    "avi": "AVI", "mp4": "MP4", "mkv": "MKV", "wmv": "WMV", "rmvb": "RMVB",
    "h264": "H264", "h265": "H265", "x264": "X264", "x265": "X265",
    "hevc": "HEVC", "aac": "AAC", "ac3": "AC3",
    "无码": "无码", "有码": "有码", "中字": "中字", "字幕": "字幕",
    "uncensored": "无码", "censored": "有码", "subbed": "字幕",
    "part1": "PART1", "part2": "PART2", "cd1": "CD1", "cd2": "CD2",
}

# 需要从"系列名"里剔除的噪音词
NOISE = re.compile(
    r"\b(hd|fhd|uhd|4k|1080p|720p|2160p|avi|mp4|mkv|wmv|rmvb|"
    r"h264|h265|x264|x265|hevc|aac|ac3|complete|collection|合集|收藏|"
    r"uncensored|censored|subbed|part\d|cd\d|vol\.?\d*)\b",
    re.IGNORECASE,
)

# 文件名里紧贴数字的分辨率/规格后缀：xxx_gc20 / xxx-guochan20 / xxx_4k30
# 这类不是系列名，去掉后才能得到真正的系列词
SPEC_SUFFIX_RE = re.compile(
    r"[-_\s]*(gc|guochan|uncensored|censored|leak|hd|fhd|uhd|sd|"
    r"4k|2k|8k|2160p?|1080p?|720p?|480p?|60fps|30fps)"
    r"\s*\d+\s*$",
    re.IGNORECASE,
)

# 站名/域名（example.com-1 / www.example.com-[DYF]100 / https://x.com）
SITE_RE = re.compile(
    r"[A-Za-z0-9\-]*\b\w{2,}\.(?:com|net|org|cn|cc|tv|me|info|blog|xyz|top|vip|club|io|co)\b"
    r"|https?://\S*",
    re.IGNORECASE,
)

# 纯 1-2 个字母的前缀基本是码率/规格标记（Y / F / CC），不是系列
TINY_SERIES_RE = re.compile(r"^[A-Za-z]{1,2}\d{0,3}$")

# 永远不是系列名的通用词
GENERIC_SERIES = {"视频", "图片", "资源", "其他", "杂", "合集", "未分类",
                  "新建文件夹", "待整理", "待筛选", "other", "video", "img"}


@dataclass
class Parsed:
    """从一条路径解析出的结构化信息。"""
    code: str | None = None            # 作品编号，如 XYZ-789
    series: str | None = None          # 系列名，如 ABC / LABO
    numbers: list[int] = field(default_factory=list)
    quality: list[str] = field(default_factory=list)   # ['AVI','HD']
    topic: str | None = None           # 目录主题，如 '分类'
    keywords: list[str] = field(default_factory=list)  # 目录/文件名拆分出的关键词
    year: int | None = None


def _clean_series(s: str) -> str:
    s = SITE_RE.sub(" ", s)
    s = SPEC_SUFFIX_RE.sub("", s)
    s = NOISE.sub(" ", s)
    s = re.sub(r"[\[\]()（）【】{}]", " ", s)
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_.")
    return s


def _series_ok(s: str | None) -> bool:
    """系列名是否可信。挡住规格标记、站名、纯数字、泛化词。"""
    if not s:
        return False
    t = s.strip()
    if len(t) < 2:
        return False
    if t.lower() in GENERIC_SERIES:
        return False
    if TINY_SERIES_RE.match(t):
        return False
    if SITE_RE.search(t):
        return False
    if re.fullmatch(r"[\d\s.\-_]+", t):
        return False
    return True


def parse_name(name: str) -> Parsed:
    """
    解析单个文件名（不含目录）。返回编号、系列、质量标记。
    """
    p = Parsed()
    stem = name.rsplit(".", 1)[0] if "." in name else name
    low = stem.lower()

    # 质量/版本标记
    for tok, label in QUALITY_TOKENS.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])", low):
            if label not in p.quality:
                p.quality.append(label)

    # 括号内的东西常是版本标记，如 QRS-066(AVI)
    for inner in re.findall(r"[\(\[]([^\)\]]{1,20})[\)\]]", stem):
        il = inner.strip().lower()
        if il in QUALITY_TOKENS:
            lab = QUALITY_TOKENS[il]
            if lab not in p.quality:
                p.quality.append(lab)

    # 作品编号：优先取字母+数字
    m = CODE_RE.search(stem)
    if m:
        prefix, digits = m.group(1), m.group(2)
        # 站名/域名里的数字不算编号（example.com-1 → 不是 EX-20-48）
        head = stem[: m.start(1)]
        if SITE_RE.search(stem) and not SITE_RE.search(prefix):
            m = CODE_RE.search(SITE_RE.sub(" ", stem))
        if m:
            prefix, digits = m.group(1), m.group(2)
            # 前缀保留原始大小写形态（ABC / LABO / XYZ），作为系列名
            p.code = f"{prefix.upper()}-{digits}"
            cand = _clean_series(prefix)
            p.series = cand if _series_ok(cand) else None
        else:
            p.series = None
    else:
        cand = _clean_series(stem)
        p.series = cand if _series_ok(cand) else None
        p.numbers = [int(x) for x in SERIES_NUM_RE.findall(stem)][:6]

    ym = re.search(r"(20\d{2})", stem)
    if ym:
        y = int(ym.group(1))
        if 1990 <= y <= 2100:
            p.year = y

    return p


def parse_relpath(rel_dir: str, name: str) -> Parsed:
    """
    结合目录层级解析。目录里的信息优先于文件名。
    rel_dir 形如 '0000/！！【分类】/【视频】/！【系列名】/ABC-123-456-HD/'
    """
    p = parse_name(name)
    parts = [x for x in rel_dir.split("/") if x]

    # 目录里出现【视频】【图片】前的那一层通常是国别/主题
    topics: list[str] = []
    for seg in parts:
        lab = parse_dir_label(seg)
        if not lab:
            continue
        for kw in split_topic(lab):
            if kw in ("视频", "图片", "资源", "其他"):
                continue
            topics.append(kw)
    if topics:
        # 取最靠前的、非泛化的作为主题
        p.topic = topics[0]
        p.keywords = [t for t in topics if t != p.topic][:8]

    # 系列名兜底：从最近的目录名推断
    if not p.series and parts:
        cand = _clean_series(parse_dir_label(parts[-1]) or parts[-1])
        if _series_ok(cand):
            p.series = cand

    # 目录里的编号也算（ABC-123）
    if not p.code:
        for seg in reversed(parts[-3:]):
            m = CODE_RE.search(seg)
            if m and m.group(1).lower() not in ("hd", "sd", "vol"):
                p.code = f"{m.group(1).upper()}-{m.group(2)}"
                break

    return p


def series_key(p: Parsed) -> str | None:
    """生成用于归堆的系列键（归一化，便于聚合）。"""
    if p.series:
        return normalize_name(p.series)
    return None


def episode_key(p: Parsed) -> str | None:
    """同一作品的不同版本应共享同一个 key，用于"同作合并"。"""
    if p.code:
        return p.code
    if p.numbers:
        return f"{normalize_name(p.series or '')}#{p.numbers[0]}"
    return None
