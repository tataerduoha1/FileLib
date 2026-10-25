"""
cleanup.py —— 索引库与缓存清理工具

所有数据都在本机，此工具只操作本地文件，不联网。

用法：
    python cleanup.py                 查看占用情况（只读，不删任何东西）
    python cleanup.py --vacuum        压缩索引库（删记录后回收磁盘空间）
    python cleanup.py --thumbs        只删缩略图缓存 thumbs/（下次用会重新生成）
    python cleanup.py --covers        只删书封缓存 covers/（下次打开书会重新抓取）
    python cleanup.py --backups       只删自动备份 backups/（最占空间，优先清它）
    python cleanup.py --cache         只删电子书正文缓存 + 自动备份
    python cleanup.py --db            只删索引库（保留缩略图）
    python cleanup.py --all           全删（索引库 + 全部缓存，回到初始状态）
    python cleanup.py --all --yes     全删且跳过确认
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from filelib.core import db as dbmod


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{u}" if u != "B" else f"{int(n)}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def dir_size(p: Path) -> tuple[int, int]:
    """返回 (总字节数, 文件数)"""
    total = 0
    count = 0
    if not p.exists():
        return 0, 0
    if p.is_file():
        try:
            return p.stat().st_size, 1
        except OSError:
            return 0, 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count


def survey() -> dict:
    """统计各组成部分占用。"""
    base = dbmod.data_dir()
    dbp = dbmod.db_path()
    thp = dbmod.thumb_dir()

    bt = base / "booktext"
    bk = base / "backups"
    parts = []
    for name, path in [
        ("索引库 index.db", dbp),
        ("索引库日志 index.db-wal", Path(str(dbp) + "-wal")),
        ("索引库共享内存 index.db-shm", Path(str(dbp) + "-shm")),
        ("缩略图缓存 thumbs/", thp),
        ("书封缓存 covers/", base / "covers"),
        ("电子书正文缓存 booktext/", bt),
        ("自动备份 backups/", bk),
        ("本机密钥 secret.key", base / "secret.key"),
    ]:
        size, n = dir_size(path)
        parts.append({"name": name, "path": str(path), "size": size, "files": n})

    total = sum(p["size"] for p in parts)
    result = {
        "base_dir": str(base),
        "total_size": total,
        "total_human": human(total),
        "parts": parts,
        "db_exists": dbp.exists(),
        "thumbs_exist": thp.exists(),
    }

    # 数据库里的统计（只读打开，避免意外创建）
    if dbp.exists():
        try:
            # URI 模式下相对路径不可用，必须转成绝对路径并把反斜杠换成 /
            uri = "file:" + dbp.resolve().as_posix() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM files").fetchone()
                result["db_files"] = r["n"]
                result["db_media_size"] = r["s"]
                result["db_thumbs_done"] = conn.execute(
                    "SELECT COUNT(*) n FROM files WHERE thumb_state=2").fetchone()["n"]
                # 按库分别统计，方便只清某一个库的索引
                result["libraries"] = [
                    {"vol_id": x["id"], "root": x["root_path"], "label": x["label"],
                     "n": x["n"] or 0, "size": x["s"] or 0,
                     "online": os.path.isdir(x["root_path"]) if x["root_path"] else False}
                    for x in conn.execute(
                        """
                        SELECT v.id, v.root_path, v.label,
                               COUNT(f.id) n, COALESCE(SUM(f.size),0) s
                        FROM volumes v LEFT JOIN files f ON f.vol_id = v.id
                        GROUP BY v.id ORDER BY v.id
                        """)
                ]
            finally:
                conn.close()
        except sqlite3.Error as ex:
            result["db_error"] = str(ex)
    return result


def print_survey(s: dict) -> None:
    print("=" * 62)
    print("FileLib 本地数据占用")
    print("=" * 62)
    print(f"位置：{s['base_dir']}")
    print()
    print(f"{'组成':<30}{'大小':>10}{'文件数':>10}")
    print("-" * 62)
    for p in s["parts"]:
        exists = "" if p["size"] or Path(p["path"]).exists() else " (不存在)"
        cnt = f"{p['files']:,}" if p["files"] else "-"
        print(f"{p['name']:<30}{human(p['size']):>10}{cnt:>10}{exists}")
    print("-" * 62)
    print(f"{'合计':<30}{s['total_human']:>10}")
    print()
    if "db_files" in s:
        print(f"索引了 {s['db_files']:,} 个文件，"
              f"对应磁盘数据 {human(s['db_media_size'])}")
        print(f"已生成封面 {s['db_thumbs_done']:,} 张")
    if s.get("libraries"):
        print()
        print("已登记的资料库：")
        for lib in s["libraries"]:
            flag = "在线" if lib["online"] else "离线"
            print(f"  [{lib['vol_id']}] {lib['n']:>8,} 个  {human(lib['size']):>9}  "
                  f"{flag}  {lib['root']}")
    if "db_error" in s:
        print(f"⚠ 读取索引库出错：{s['db_error']}")
    print()
    print("说明：这里只有「索引和缩略图」，不含你自己的原文件。")
    print("      原文件在外接盘上，本工具不会去动它们。")


def remove_path(p: Path, label: str) -> bool:
    try:
        if not p.exists():
            print(f"  跳过（不存在）：{label}")
            return True
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        print(f"  已删除：{label}")
        return True
    except OSError as ex:
        print(f"  ✗ 删除失败 {label}：{ex}")
        return False


def db_is_locked(p: Path) -> bool:
    """
    判断是否还有进程占着索引库。

    方法：尝试以独占方式打开文件。Windows 下用 msvcrt 加锁最可靠；
    非 Windows 用 SQLite 的 EXCLUSIVE 事务兜底。

    为什么必须检测：服务运行时执行 VACUUM，会在 WAL 里堆出几百 MB 的日志，
    反而让占用翻倍（实测 599MB → 1.1GB）。
    """
    if not p.exists():
        return False

    if sys.platform == "win32":
        try:
            import msvcrt
            with open(p, "r+b") as f:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    return False
                except OSError:
                    return True
        except OSError:
            pass

    try:
        conn = sqlite3.connect(str(p), timeout=0.3, isolation_level=None)
        try:
            conn.execute("PRAGMA locking_mode=EXCLUSIVE")
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
            return False
        finally:
            conn.close()
    except sqlite3.Error:
        return True


def server_running(port: int = 8321) -> bool:
    """检查 FileLib 服务是否在监听端口。"""
    import socket
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def do_vacuum() -> None:
    dbp = dbmod.db_path()
    if not dbp.exists():
        print("索引库不存在，无需压缩。")
        return

    if server_running() or db_is_locked(dbp):
        print("！索引库正被占用，现在不能压缩。")
        print()
        print("  原因：FileLib 服务还在运行。在它运行时执行压缩，")
        print("  会让日志文件膨胀到几百 MB，占用反而翻倍。")
        print()
        print("  请先关闭 FileLib 窗口（或在任务管理器结束 python.exe），")
        print("  然后重新执行：python cleanup.py --vacuum")
        print()
        print("  提示：服务正常关闭时，日志文件会自动归零，通常不需要手动压缩。")
        return

    before = dir_size(dbp)[0]
    wal = Path(str(dbp) + "-wal")
    wal_before = dir_size(wal)[0]
    print(f"压缩前：主库 {human(before)}，日志 {human(wal_before)}")
    conn = sqlite3.connect(str(dbp), timeout=60, isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()
    after = dir_size(dbp)[0]
    wal_after = dir_size(wal)[0]
    print(f"压缩后：主库 {human(after)}，日志 {human(wal_after)}")
    saved = max(0, (before + wal_before) - (after + wal_after))
    print(f"共释放 {human(saved)}")


def drop_library(vol_id: int) -> bool:
    """
    只删除某个资料库的索引记录（files/meta/tasks/dirs/volumes），
    缩略图和索引库文件本身保留。用于不再管理的某个盘。
    """
    dbp = dbmod.db_path()
    if not dbp.exists():
        print("索引库不存在。")
        return False
    conn = sqlite3.connect(str(dbp), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, root_path, label FROM volumes WHERE id=?", (vol_id,)).fetchone()
        if not row:
            print(f"没有 id={vol_id} 的资料库。")
            return False
        n = conn.execute(
            "SELECT COUNT(*) FROM files WHERE vol_id=?", (vol_id,)).fetchone()[0]
        print(f"将删除资料库索引：{row['root_path']}")
        print(f"  共 {n:,} 条文件记录")

        # 先删依赖行，避免外键约束问题
        conn.execute(
            "DELETE FROM tasks WHERE file_id IN (SELECT id FROM files WHERE vol_id=?)",
            (vol_id,))
        conn.execute(
            "DELETE FROM file_text WHERE file_id IN (SELECT id FROM files WHERE vol_id=?)",
            (vol_id,))
        conn.execute(
            "DELETE FROM file_tags WHERE file_id IN (SELECT id FROM files WHERE vol_id=?)",
            (vol_id,))
        conn.execute(
            "DELETE FROM meta WHERE file_id IN (SELECT id FROM files WHERE vol_id=?)",
            (vol_id,))
        conn.execute("DELETE FROM files WHERE vol_id=?", (vol_id,))
        conn.execute("DELETE FROM dirs WHERE vol_id=?", (vol_id,))
        conn.execute("DELETE FROM volumes WHERE id=?", (vol_id,))
        conn.commit()
        print("  已删除。原文件不受影响。")
        print("\n建议再执行 --vacuum 回收磁盘空间。")
        return True
    finally:
        conn.close()


def confirm(prompt: str) -> bool:
    try:
        ans = input(prompt + " [输入 yes 确认] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("yes", "y")


def main() -> int:
    ap = argparse.ArgumentParser(description="FileLib 本地数据清理")
    ap.add_argument("--thumbs", action="store_true", help="删除缩略图缓存 thumbs/")
    ap.add_argument("--covers", action="store_true", help="删除书封缓存 covers/")
    ap.add_argument("--backups", action="store_true", help="删除自动备份 backups/")
    ap.add_argument("--cache", action="store_true",
                    help="删除电子书正文缓存与自动备份（保留索引库）")
    ap.add_argument("--db", action="store_true", help="删除索引库")
    ap.add_argument("--all", action="store_true",
                    help="删除全部（索引库+缩略图+书封+正文缓存+备份）")
    ap.add_argument("--vacuum", action="store_true", help="压缩索引库回收空间")
    ap.add_argument("--drop-library", type=int, metavar="N",
                    help="只删除指定 id 的资料库索引（缩略图保留）")
    ap.add_argument("--yes", action="store_true", help="跳过确认")
    args = ap.parse_args()

    if args.drop_library is not None:
        if not args.yes and not confirm(f"确认删除资料库 id={args.drop_library} 的索引？"):
            print("已取消。")
            return 0
        return 0 if drop_library(args.drop_library) else 1

    if not (args.thumbs or args.covers or args.backups or args.cache
            or args.db or args.all or args.vacuum):
        print_survey(survey())
        print("可用操作：")
        print("  --vacuum           压缩索引库（保留数据，只回收空间）")
        print("  --thumbs           只删缩略图缓存 thumbs/（重新扫描会重建）")
        print("  --covers           只删书封缓存 covers/（打开书会重新抓取）")
        print("  --backups          只删自动备份 backups/（最占空间，优先清它）")
        print("  --cache            只删电子书正文缓存 + 自动备份（保留索引库）")
        print("  --db               只删索引库（保留缩略图）")
        print("  --all              全删（含正文缓存与备份），回到初始状态")
        print("  --drop-library N   只删第 N 个资料库的索引，其余保留")
        print()
        print("加 --yes 跳过确认。原文件不受任何影响。")
        return 0

    if args.vacuum:
        do_vacuum()
        return 0

    dbp = dbmod.db_path()
    thp = dbmod.thumb_dir()

    targets: list[tuple[Path, str]] = []
    base = dbmod.data_dir()
    if args.all:
        targets = [
            (thp, "缩略图缓存 thumbs/"),
            (base / "covers", "书封缓存 covers/"),
            (base / "booktext", "电子书正文缓存 booktext/"),
            (base / "backups", "自动备份 backups/"),
            (Path(str(dbp) + "-wal"), "索引库日志"),
            (Path(str(dbp) + "-shm"), "索引库共享内存"),
            (dbp, "索引库 index.db"),
        ]
    else:
        if args.thumbs:
            targets.append((thp, "缩略图缓存 thumbs/"))
        if args.covers:
            targets.append((base / "covers", "书封缓存 covers/"))
        if args.backups:
            targets.append((base / "backups", "自动备份 backups/"))
        if args.cache:
            targets += [
                (base / "booktext", "电子书正文缓存 booktext/"),
                (base / "backups", "自动备份 backups/"),
            ]
        if args.db:
            targets += [
                (Path(str(dbp) + "-wal"), "索引库日志"),
                (Path(str(dbp) + "-shm"), "索引库共享内存"),
                (dbp, "索引库 index.db"),
            ]

    total = sum(dir_size(p)[0] for p, _ in targets)
    print("将删除：")
    for p, label in targets:
        size, n = dir_size(p)
        print(f"  · {label}  ({human(size)}, {n} 个文件)")
    print(f"\n合计释放：{human(total)}")
    print("\n注意：只删除索引和缓存。你的原文件在外接盘上，完全不受影响。")
    if not args.yes and not confirm("确认删除？"):
        print("已取消，什么都没删。")
        return 0

    print()
    ok = all(remove_path(p, label) for p, label in targets)
    print()
    if ok:
        print("完成。下次启动会重新建库（扫描全盘，约几秒到几分钟）。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
