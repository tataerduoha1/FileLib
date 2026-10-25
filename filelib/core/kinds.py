"""
filelib.core.kinds —— 文件类型识别与归类

只依赖标准库，不引入第三方包即可运行阶段 1。
"""
from __future__ import annotations

import re

# kind -> 扩展名集合
KIND_MAP: dict[str, set[str]] = {
    # 电子书（占比大的重点类别）
    "book": {
        "epub", "mobi", "azw", "azw3", "azw4", "prc", "pdb", "fb2", "fbz",
        "lit", "lrf", "lrx", "rb", "snb", "tcr", "ceb", "iba", "ibooks",
        "djvu", "djv", "chm", "cbr", "cbz", "cb7", "cbt", "kfx", "tpz", "oebzip",
    },
    # 文档
    "doc": {
        "pdf", "txt", "md", "markdown", "rtf", "doc", "docx", "odt", "wps",
        "xls", "xlsx", "csv", "tsv", "ods", "ppt", "pptx", "odp", "dps",
        "html", "htm", "mht", "mhtml", "xml", "json", "yaml", "yml", "toml",
        "log", "tex", "epub.bak",
    },
    # 视频
    "video": {
        "mp4", "mkv", "avi", "mov", "wmv", "flv", "f4v", "rmvb", "rm",
        "m4v", "mpg", "mpeg", "mpe", "ts", "m2ts", "mts", "vob", "3gp",
        "3g2", "ogv", "webm", "asf", "divx", "xvid", "dat", "iso",
    },
    # 图片
    "image": {
        "jpg", "jpeg", "jpe", "jfif", "png", "gif", "bmp", "tif", "tiff",
        "webp", "heic", "heif", "avif", "svg", "ico", "psd", "raw", "cr2",
        "cr3", "nef", "arw", "dng", "orf", "rw2", "srw", "pef", "raf", "jxl",
    },
    # 音频
    "audio": {
        "mp3", "flac", "wav", "aac", "m4a", "ogg", "oga", "opus", "wma",
        "ape", "alac", "aiff", "aif", "dsf", "dff", "mka", "amr", "mid", "midi",
    },
    # 压缩包
    "archive": {
        "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "tgz", "tbz",
        "cab", "arj", "lzh", "lha", "ace", "z", "zipx", "001", "r00",
    },
    # 代码与脚本
    "code": {
        "py", "js", "ts", "jsx", "tsx", "java", "c", "h", "cpp", "hpp",
        "cc", "cs", "go", "rs", "rb", "php", "swift", "kt", "scala",
        "sh", "bash", "zsh", "ps1", "bat", "cmd", "sql", "lua", "r",
        "vue", "css", "scss", "less", "ipynb",
    },
    # 字幕
    "subtitle": {"srt", "ass", "ssa", "vtt", "sub", "idx", "smi", "lrc", "sup"},
    # 可执行与安装
    "app": {
        "exe", "msi", "dll", "apk", "appx", "msix", "deb", "rpm", "dmg",
        "pkg", "jar", "bin", "run", "appimage", "ipa",
    },
}

_EXT2KIND: dict[str, str] = {}
for _kind, _exts in KIND_MAP.items():
    for _e in _exts:
        _EXT2KIND.setdefault(_e, _kind)

# 界面上 kind 的中文显示名与配色（前端也用，保持单一来源）
KIND_LABEL: dict[str, str] = {
    "book": "电子书",
    "doc": "文档",
    "video": "视频",
    "image": "图片",
    "audio": "音频",
    "archive": "压缩包",
    "code": "代码",
    "subtitle": "字幕",
    "app": "程序",
    "other": "其他",
}

KIND_ORDER = list(KIND_LABEL.keys())


def get_ext(name: str) -> str:
    """取小写扩展名。处理 .tar.gz 这类双扩展名。"""
    lower = name.lower()
    if lower.endswith(".tar.gz"):
        return "tar.gz"
    i = lower.rfind(".")
    if i <= 0 or i == len(lower) - 1:
        return ""
    return lower[i + 1:]


def kind_of(ext: str) -> str:
    if ext == "tar.gz":
        return "archive"
    return _EXT2KIND.get(ext, "other")


# ---------- 文件名规范化 ----------

_FULLWIDTH_BRACKETS = str.maketrans({
    "【": "[", "】": "]", "［": "[", "］": "]", "（": "(", "）": ")",
    "｛": "{", "｝": "}", "！": "!", "：": ":", "；": ";", "，": ",",
    "、": ",", "　": " ", "～": "~", "－": "-", "—": "-", "–": "-",
    "“": '"', "”": '"', "‘": "'", "’": "'",
})


def normalize_name(name: str) -> str:
    """
    归一化用于搜索匹配的名字：
    全角转半角、合并空白、小写。
    注意：仅用于索引，显示仍用原名。
    """
    s = name.translate(_FULLWIDTH_BRACKETS)
    s = re.sub(r"[\s\u3000]+", " ", s)
    return s.strip().lower()


# ---------- 目录名解析（本盘强特征：！！【主题】） ----------

_BRACKET_RE = re.compile(r"[【\[]([^】\]]{1,60})[】\]]")
_LEAD_DECOR = re.compile(r"^[\s!！~～\-—–_\*\.\d]+")


def parse_dir_label(dirname: str) -> str | None:
    """
    从目录名中提取"主题标签"。
    针对形如 '！！【动漫 动画 国漫 日漫 3D动漫】' 的命名，
    提取括号内的主题文本；没有括号则用去掉装饰前缀的名字。
    """
    m = _BRACKET_RE.search(dirname)
    if m:
        inner = m.group(1).strip()
        if inner:
            return inner
    cleaned = _LEAD_DECOR.sub("", dirname).strip()
    return cleaned or None


def split_topic(label: str) -> list[str]:
    """把 '动漫 动画 国漫 日漫 3D动漫' 拆成多个关键词。"""
    parts = re.split(r"[\s/|,、]+", label)
    return [p.strip() for p in parts if p.strip()]


SEASON_EP_RE = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,4})")
EP_CN_RE = re.compile(r"第\s*(\d{1,4})\s*[集话話期]")
YEAR_RE = re.compile(r"(19|20)\d{2}")
