"""
filelib/core/books.py —— 电子书 / 书库专用解析

书库和影视库的组织方式完全不同：
    影视：{国别}/【视频】/{系列}/{作品}    → 系列是文件夹，作品是番号
    书库：{题材}/{书名 - 作者}.mobi       → 系列往往不存在，重点是书名+作者

如果直接用影视那套 `parse_relpath`，每本书都会变成独立的"系列"，
导致 20,761 本书产生 16 万个系列，毫无意义。

这里改为提取：
    title   书名
    author  作者（`书名 - 作者` 或 `书名 (作者)` 等格式）
    topic   题材（目录层级）
    series  仅当明确是合集时才设（如「XX全集」「XX系列」）
"""
from __future__ import annotations

import re

from .kinds import parse_dir_label, split_topic, normalize_name

# 常见分隔符：书名 - 作者 / 书名_作者 / 书名（作者）/ 书名 by 作者
SEP_DASH = re.compile(r"\s+[-–—]\s+")
PAREN_AUTHOR = re.compile(r"[（(]([^（()）]{2,20})[)）]\s*$")
BY_AUTHOR = re.compile(r"\s+by\s+", re.IGNORECASE)

# 合集 / 系列 标记：只有这些才认为存在"系列"
SERIES_MARKERS = re.compile(
    r"全集|系列|套装|合集|文集|作品集|丛书|第[\d一二三四五六七八九十]+[辑季卷部]|"
    r"[（(]\s*\d+\s*[-~至到]\s*\d+\s*[)）]|全\s*\d+\s*[册卷]"
)

# 文件名前缀噪音：序号、待读标记等
PREFIX_NOISE = re.compile(
    r"^\s*\d{2,}\s*(待读|已读|未读)?\s*[-_—.、~～]*\s*"
    r"|^\s*(待读|已读|未读|推荐|精选|新书|完本|全本)\s*[-_—]*\s*"
)

# 书名开头表示作者国籍/朝代的方括号，如 [日] [美] [英] [清]
NATIONALITY_RE = re.compile(
    r"^\s*[\[【(（]\s*([日美英法德俄意韩印澳加]|清|明|宋|唐|元|汉|民国|当代|美籍|日籍)\s*[\]】)）]\s*"
)

# 结尾的修饰词，不是作者
TAIL_NOISE = re.compile(
    r"\s*[（(]?\s*(精选|精校|校对|全本|完本|完结|无删减|典藏|珍藏|插图|修订|epub|mobi|txt|扫描版|高清)\s*[)）]?\s*$",
    re.IGNORECASE,
)

# 作者名里常见的干扰
AUTHOR_NOISE = re.compile(r"^\s*著\s*$|^\s*编著\s*$")

# 目录里属于"组织方式"而非"题材"的层级，不作为题材
STRUCT_DIRS = {
    "按题材分", "按平台分", "按作者分", "按字母", "按拼音首字排序", "按拼音",
    "个人兴趣", "网络小说", "电子书", "视频", "图片", "资源", "其他",
    "杂", "合集", "未分类", "完结", "精品", "推荐", "小说", "书籍", "书",
    "kindle", "豆瓣", "开卷有益", "天涯论坛", "樊登读书会",
}

# 形如 "13000本EPUB书籍" / "42000本小说" / "7万篇小说-按拼音首字排序" 的批量目录，不是题材
BULK_DIR_RE = re.compile(
    r"^\d+\s*本.*(书|小说|合集|书籍|篇)$"
    r"|^(epub|mobi|txt|pdf|azw3?)书?籍?$"
    r"|^[\d一二三四五六七八九十百千万]+\s*(本|篇|部|套)?.*(书|小说|合集|书籍|篇)",
    re.IGNORECASE,
)

# 单独判断"这个目录名是不是批量/组织性目录"
BULK_HINT_RE = re.compile(
    r"^\d+\s*[万千百]?\s*(本|篇|部|套)"
    r"|按拼音|按字母|排序",
    re.IGNORECASE,
)

# 这类文件不参与"书籍"维度统计
NON_BOOK_KINDS = {"other", "image", "audio", "video", "app", "code"}


def parse_book(rel_dir: str, name: str, ext: str) -> dict:
    """
    解析一本书。返回 {title, author, series, topic, keywords}。
    """
    stem = name
    if ext and stem.lower().endswith("." + ext.lower()):
        stem = stem[: -(len(ext) + 1)]

    # 先剥掉开头国籍标记和序号前缀
    nationality = None
    nm = NATIONALITY_RE.match(stem)
    if nm:
        nationality = nm.group(1)
        stem = stem[nm.end():]
    clean = PREFIX_NOISE.sub("", stem).strip()

    # 去掉结尾修饰词（精选/精校版/全本…）后再判断作者
    def strip_tail(s: str) -> str:
        prev = None
        while prev != s:
            prev = s
            s = TAIL_NOISE.sub("", s).strip()
        return s

    title = strip_tail(clean)
    author: str | None = None

    # 格式1：书名 - 作者
    m = SEP_DASH.search(clean)
    if m:
        left = strip_tail(clean[: m.start()].strip())
        right = strip_tail(clean[m.end():].strip())
        if left and right and len(right) <= 30 and not SERIES_MARKERS.search(right):
            # 方括号标注国籍时，`[日]东野圭吾 - 白夜行` 其实是「作者 - 书名」
            if nationality and right and not has_series_suffix(right):
                title, author = right, left
            else:
                title, author = left, right

    # 格式2：书名（作者）
    if not author:
        pm = PAREN_AUTHOR.search(clean)
        if pm:
            cand = pm.group(1).strip()
            if (cand and not SERIES_MARKERS.search(cand)
                    and not TAIL_NOISE.fullmatch("(" + cand + ")")
                    and not re.fullmatch(r"[\d\s.,~\-至到册卷集辑部]+", cand)
                    and len(cand) >= 2):
                title = strip_tail(clean[: pm.start()].strip()) or clean
                author = cand

    # 格式3：书名 by 作者
    if not author:
        bm = BY_AUTHOR.search(clean)
        if bm:
            left = strip_tail(clean[: bm.start()].strip())
            right = strip_tail(clean[bm.end():].strip())
            if left and right and len(right) <= 30:
                title, author = left, right

    # 作者位置清理
    if author:
        author = re.sub(r"^[\[【(（]|[\]】)）]$", "", author).strip()
        author = re.sub(r"\s*(著|编著|主编|译|等著)$", "", author).strip()
        if AUTHOR_NOISE.match(author) or len(author) < 2:
            author = None

    # 系列：只在标题明确带合集标记时设置
    series = None
    sm = SERIES_MARKERS.search(title)
    if sm:
        base = title[: sm.start()].strip(" -_·:：")
        base = re.sub(r"[（(]\s*$", "", base).strip()
        series = base if len(base) >= 2 else title
        # 系列名去掉尾部数字区间
        series = re.sub(r"\s*[\d一二三四五六七八九十]+\s*$", "", series).strip() or series

    # 题材：目录层级里【】内的标签，跳过纯组织性目录
    topics: list[str] = []
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
            if k.lower() in STRUCT_DIRS or k in STRUCT_DIRS:
                continue
            if BULK_DIR_RE.match(k) or BULK_HINT_RE.search(k):
                continue
            if re.fullmatch(r"[\d\s]+", k):
                continue
            topics.append(k)

    return {
        "title": title or strip_tail(clean) or stem,
        "author": author,
        "series": series,
        "topic": topics[0] if topics else None,
        "keywords": topics[1:6],
        "nationality": nationality,
        "norm_title": normalize_name(title or stem),
    }


def has_series_suffix(s: str) -> bool:
    """判断字符串是否像书名（而非作者名）。"""
    return bool(SERIES_MARKERS.search(s)) or bool(re.search(r"[\u4e00-\u9fff]{4,}", s))
