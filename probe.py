"""
probe.py —— 只读探测：统计目录规模、类型分布、深度，不做任何写入。

用法：
    python probe.py I:\
    python probe.py I:\0000 --depth 2 --limit-dirs 30
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filelib.core.kinds import get_ext, kind_of, KIND_LABEL, parse_dir_label
from filelib.core.scanner import long_path, JUNK_DIRS, JUNK_FILES, JUNK_EXTS, short_display


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--depth", type=int, default=99, help="最大遍历深度")
    ap.add_argument("--limit-dirs", type=int, default=25)
    ap.add_argument("--max-files", type=int, default=0, help="0=不限，用于快速抽样")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(long_path(root)):
        print(f"[错误] 无法访问：{root}")
        return 2

    print(f"探测目标：{root}")
    print(f"开始时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 72)

    kind_count: Counter[str] = Counter()
    kind_bytes: Counter[str] = Counter()
    ext_count: Counter[str] = Counter()
    top_dirs: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # rel -> [n, bytes]
    depth_count: Counter[int] = Counter()
    errors: list[tuple[str, str]] = []

    n_files = 0
    total_bytes = 0
    t0 = time.time()
    last_report = t0

    stack: list[tuple[str, str, int]] = [(long_path(root), "", 0)]
    while stack:
        abs_dir, rel_dir, depth = stack.pop()
        if depth > args.depth:
            continue
        try:
            with os.scandir(abs_dir) as it:
                entries = list(it)
        except (OSError, PermissionError) as ex:
            errors.append((short_display(abs_dir), f"{type(ex).__name__}: {ex}"))
            continue

        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() in JUNK_DIRS:
                        continue
                    stack.append((e.path, f"{rel_dir}{e.name}/", depth + 1))
                elif e.is_file(follow_symlinks=False):
                    ext = get_ext(e.name)
                    if ext in JUNK_EXTS or e.name.lower() in JUNK_FILES:
                        continue
                    st = e.stat(follow_symlinks=False)
                    k = kind_of(ext)
                    n_files += 1
                    total_bytes += st.st_size
                    kind_count[k] += 1
                    kind_bytes[k] += st.st_size
                    ext_count[ext] += 1
                    depth_count[rel_dir.count("/")] += 1
                    # 顶层目录统计
                    first = rel_dir.split("/")[0] if rel_dir else "(根目录)"
                    top_dirs[first][0] += 1
                    top_dirs[first][1] += st.st_size
            except (OSError, PermissionError) as ex:
                errors.append((short_display(e.path), f"{type(ex).__name__}: {ex}"))

            if args.max_files and n_files >= args.max_files:
                stack.clear()
                break

        now = time.time()
        if now - last_report >= 3:
            last_report = now
            el = now - t0
            print(f"  …{n_files:>8,} 文件  {human(total_bytes):>9}  "
                  f"{n_files/max(el,1):,.0f} 文件/秒  {short_display(abs_dir)[:50]}",
                  end="\r")

    el = time.time() - t0
    print(" " * 100, end="\r")
    print(f"\n完成：{n_files:,} 个文件，{human(total_bytes)}，耗时 {el:.1f} 秒 "
          f"({n_files/max(el,1):,.0f} 文件/秒)")

    print("\n" + "=" * 72)
    print("类型分布")
    print("=" * 72)
    print(f"{'类型':<8}{'数量':>12}{'占比':>9}{'总大小':>12}{'大小占比':>10}")
    for k, c in kind_count.most_common():
        label = KIND_LABEL.get(k, k)
        print(f"{label:<8}{c:>12,}{c/max(n_files,1)*100:>8.1f}%"
              f"{human(kind_bytes[k]):>12}{kind_bytes[k]/max(total_bytes,1)*100:>9.1f}%")

    print("\n" + "=" * 72)
    print(f"扩展名 Top 20")
    print("=" * 72)
    for i, (ext, c) in enumerate(ext_count.most_common(20), 1):
        print(f"{i:>2}. {ext or '(无扩展名)':<12}{c:>10,}")

    print("\n" + "=" * 72)
    print(f"顶层目录（按文件数，Top {args.limit_dirs}）")
    print("=" * 72)
    for name, (c, b) in sorted(top_dirs.items(), key=lambda x: -x[1][0])[:args.limit_dirs]:
        lab = parse_dir_label(name) or name
        print(f"{c:>10,}  {human(b):>9}  {name[:48]}")

    print("\n" + "=" * 72)
    print("目录深度分布")
    print("=" * 72)
    for d, c in sorted(depth_count.items()):
        bar = "#" * min(60, int(c / max(max(depth_count.values()), 1) * 60))
        print(f"深度 {d:>2}: {c:>9,} {bar}")

    if errors:
        print("\n" + "=" * 72)
        print(f"无法访问（共 {len(errors)} 处，显示前 10）")
        print("=" * 72)
        for p, msg in errors[:10]:
            print(f"  {p[:70]}\n     └ {msg[:100]}")

    print(f"\n结束时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
