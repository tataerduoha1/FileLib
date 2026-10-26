"""
filelib/core/config.py —— 统一配置（可移植性核心）

设计目标：把整个目录拷到另一台电脑就能跑，不需要改任何代码。

数据目录解析优先级：
  1. 环境变量 FILELIB_DATA（显式指定，最高）
  2. 便携模式：程序目录下存在 portable.txt（或 data/ 目录）→ 存到程序目录 data/
  3. 配置文件 ${FILELIB_HOME}/filelib.json 里的 data_dir
  4. 默认：%LOCALAPPDATA%\\FileLib  (Windows) / ~/.filelib (其他)

所有运行期设置（端口、限流、休息提醒、计划任务……）存在索引库的 settings 表里，
也可通过 filelib.json 覆盖初始值，方便迁移。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "FileLib"
APP_VERSION = "1.0.11"

# 程序根目录（server.py 所在目录）
APP_ROOT = Path(__file__).resolve().parent.parent.parent


def _portable_dir() -> Path | None:
    """便携模式：程序目录下有 portable.txt 或 data 子目录就用本地数据目录。"""
    for marker in ("portable.txt", "PORTABLE.txt"):
        if (APP_ROOT / marker).exists():
            return APP_ROOT / "data"
    cfg = APP_ROOT / "filelib.json"
    if cfg.exists():
        try:
            j = json.loads(cfg.read_text("utf-8"))
            if j.get("portable"):
                return APP_ROOT / "data"
        except Exception:
            pass
    _d = APP_ROOT / "data"
    if _d.is_dir() and (_d / "index.db").exists():
        return _d
    return None


def _default_data_dir() -> Path:
    """默认数据目录。

    优先复用「历史上真实存在过的」数据目录：老版本 1.x 把库放在
    ``~/FileIndex``，如果那里已经有 index.db，就直接继续用它，
    避免升级后凭空变出一个空库、看起来像「数据全丢了」。
    """
    legacy = Path.home() / "FileIndex"
    if (legacy / "index.db").is_file():
        return legacy
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".filelib"


def app_config_file() -> Path:
    """程序目录旁的 filelib.json（可选，用于迁移时固定路径/端口）。"""
    return APP_ROOT / "filelib.json"


def load_app_config() -> dict:
    f = app_config_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text("utf-8")) or {}
    except Exception:
        return {}


_APP_CFG = load_app_config()


def data_dir() -> Path:
    """数据目录（索引库、封面、备份、密钥）。"""
    env = os.environ.get("FILELIB_DATA")
    if env:
        p = Path(env).expanduser()
    elif _APP_CFG.get("data_dir"):
        p = Path(_APP_CFG["data_dir"]).expanduser()
    elif _portable_dir():
        p = _portable_dir()
    else:
        p = _default_data_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_portable() -> bool:
    env = os.environ.get("FILELIB_DATA")
    if env:
        return False
    if _APP_CFG.get("data_dir"):
        return False
    return _portable_dir() is not None


# ---------- 运行参数（命令行/配置文件的默认值） ----------

DEFAULTS = {
    "port": 8321,
    "host": "127.0.0.1",          # 0.0.0.0 = 允许局域网/手机访问
    "rate_limit_user": 300,        # 普通用户每分钟请求数
    "rate_limit_guest": 60,        # 未登录（仅登录页/静态资源）
    "rate_limit_media": 600,       # 媒体流/下载每分钟次数（拖进度条会连发 Range）
    "max_media_sessions": 3,       # 普通用户同时进行的视频播放数
    "max_transcode_sessions": 2,   # 全局同时进行的转码路数（每路一个 ffmpeg 进程）
    "rest_remind": False,          # 默认关闭，首次启动引导里可开
    "rest_interval_min": 45,
    "scan_temp_enabled": False,
    "scan_temp_interval_min": 120,  # 临时扫描间隔（分钟）
    "scan_daily_time": "08:30",     # 每天定时扫描时间
    "theme": "light",
    "backup_keep": 3,
    "auto_backup": True,
}


def cfg(key: str, default=None):
    v = _APP_CFG.get(key)
    if v is None:
        return DEFAULTS.get(key, default)
    return v


def ffmpeg_hint() -> list[str]:
    """ffmpeg 探测顺序（可由 filelib.json 的 ffmpeg 字段覆盖）。"""
    out: list[str] = []
    if _APP_CFG.get("ffmpeg"):
        out.append(str(_APP_CFG["ffmpeg"]))
    if os.environ.get("FILELIB_FFMPEG"):
        out.append(os.environ["FILELIB_FFMPEG"])
    out += [
        str(APP_ROOT / "bin" / "ffmpeg.exe"),
        str(APP_ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"),
        str(Path.home() / ".workbuddy" / "binaries" / "ffmpeg" / "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"D:\installation\ffmpeg\bin\ffmpeg.exe",
        r"D:\ffmpeg\bin\ffmpeg.exe",
    ]
    return out
