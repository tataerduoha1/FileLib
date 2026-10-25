"""
tools/gen_icons.py —— 生成 FileLib PWA 图标（纯标准库，零依赖）

输出 filelib/ui/icons/icon-192.png 与 icon-512.png：
圆角方块 + 靛蓝渐变底 + 白色"翻开的书"图形。
PNG 编码手写（zlib/struct），不需要 Pillow。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

S = 512                      # 母版尺寸
RADIUS = 100                 # 圆角半径
TOP = (0x3B, 0x82, 0xF0)     # 渐变顶色（--acc 亮一档）
BOT = (0x1F, 0x50, 0xC0)     # 渐变底色（--acc2 深一档）
WHITE = (255, 255, 255)


def rounded(x: int, y: int) -> bool:
    """点是否在圆角方块内"""
    r = RADIUS
    cx = min(max(x, r), S - 1 - r)
    cy = min(max(y, r), S - 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r or (r <= x < S - r and r <= y < S - r)


def in_book(x: int, y: int) -> bool:
    """白色"翻开的书"：左右两页 + 中缝，用简单多边形判定"""
    # 书的整体范围
    if not (76 <= x <= 436 and 150 <= y <= 372):
        return False
    t = (y - 150) / 222.0            # 0 顶 → 1 底
    # 中缝在 x≈256，两页外缘随 y 轻微下坠（书页微弯的观感）
    sag = int(26 * (1 - abs(2 * t - 1)) ** 2)     # 中间下垂最多
    if x <= 256:
        inner = 256 - 10
        outer = 76 + int(14 * t)
        top_y = 168 + int(20 * t) - (sag * (256 - x)) // 180
        bot_y = 356 - int(10 * t) - (sag * (256 - x)) // 260
    else:
        inner = 256 + 10
        outer = 436 - int(14 * t)
        top_y = 168 + int(20 * t) - (sag * (x - 256)) // 180
        bot_y = 356 - int(10 * t) - (sag * (x - 256)) // 260
    if not (top_y <= y <= bot_y):
        return False
    if x <= 256:
        return outer <= x <= inner
    return inner <= x <= outer


def render() -> list[list[tuple[int, int, int, int]]]:
    px = []
    for y in range(S):
        row = []
        t = y / (S - 1)
        col = tuple(round(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3))
        for x in range(S):
            if not rounded(x, y):
                row.append((0, 0, 0, 0))
            elif in_book(x, y):
                row.append((*WHITE, 255))
            else:
                row.append((*col, 255))
        px.append(row)
    return px


def box_downscale(px, k: int):
    """k×k 均值缩样（对图标足够好，还顺带做了抗锯齿）"""
    n = S // k
    out = []
    for by in range(n):
        row = []
        for bx in range(n):
            r = g = b = a = 0
            for dy in range(k):
                for dx in range(k):
                    p = px[by * k + dy][bx * k + dx]
                    r += p[0]; g += p[1]; b += p[2]; a += p[3]
            c = k * k
            row.append((r // c, g // c, b // c, a // c))
        out.append(row)
    return out


def png(rows) -> bytes:
    h, w = len(rows), len(rows[0])
    raw = b"".join(
        b"\x00" + bytes(v for p in r for v in p) for r in rows
    )
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "filelib" / "ui" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    master = render()
    for size in (512, 192):
        k = S // size
        rows = master if k == 1 else box_downscale(master, k)
        p = out_dir / f"icon-{size}.png"
        p.write_bytes(png(rows))
        print("written", p, p.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
