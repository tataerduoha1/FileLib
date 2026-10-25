"""
filelib/core/cache.py —— 进程内查询缓存（聚合类热点查询专用）

为什么需要：
    stats / 目录树 / 主题 / 系列 / 作者 这类聚合查询，每次都要对几十万行
    GROUP BY。数据没变时结果完全一样，重复计算纯属浪费 —— 首页每刷新一次
    就全表扫一遍，是大库里界面"发卡"的主要原因。

失效策略（双保险）：
    1) 版本号：任何写库动作（扫描/建封面/打标记/改标签/删文件…）调用
       bump()，全部缓存立即失效 —— 保证界面看到的数据永远是最新的；
    2) TTL 兜底：防止个别写入路径漏调 bump()，超时自动过期。

线程安全：ThreadingHTTPServer 每请求一个线程，统一加锁。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

TTL = 12.0  # 秒；正常情况下版本号失效先于 TTL 生效

_lock = threading.Lock()
_ver = 1
_store: dict[str, tuple[int, float, Any]] = {}


def bump() -> None:
    """数据发生变化：清空全部缓存。

    O(1) 全清而不是逐 key 失效 —— 聚合结果彼此关联
    （stats 里含任务数，目录树含封面…），全清最不容易漏。
    """
    global _ver
    with _lock:
        _ver += 1
        _store.clear()


def get(key: str) -> Any | None:
    with _lock:
        ent = _store.get(key)
        if ent is None:
            return None
        ver, exp, val = ent
        if ver != _ver or time.monotonic() > exp:
            _store.pop(key, None)
            return None
        return val


def put(key: str, val: Any) -> None:
    with _lock:
        _store[key] = (_ver, time.monotonic() + TTL, val)


def cached(key: str, fn: Callable[[], Any]) -> Any:
    """带缓存的执行：命中直接返回，未命中计算后写入。"""
    val = get(key)
    if val is None:
        val = fn()
        put(key, val)
    return val
