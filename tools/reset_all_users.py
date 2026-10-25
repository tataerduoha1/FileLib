"""
tools/reset_all_users.py —— 清空所有账号，回到「首次使用 → 创建管理员」

用途：忘记所有密码、或想换一个全新的管理员账号。
      只删除账号相关数据，**文件索引、封面、全文缓存全部保留**。

用法（先停掉服务，关掉 start.bat 的黑窗口）：
    python tools/reset_all_users.py            # 会先备份，再问一次确认
    python tools/reset_all_users.py --yes      # 跳过确认
    python tools/reset_all_users.py --data D:\\MyLib
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ACCOUNT_TABLES = ("user_perms", "sessions", "login_log", "users", "user_marks")
KEEP_TABLES = ("files", "volumes", "dirs", "file_text", "tags", "file_tags", "authors")


def server_running(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="清空所有账号，重新创建管理员")
    ap.add_argument("--data", help="数据目录（默认自动探测）")
    ap.add_argument("--yes", action="store_true", help="不再询问确认")
    ap.add_argument("--port", type=int, default=8321, help="用这个端口判断服务是否还在运行")
    a = ap.parse_args()

    from filelib.core.config import data_dir

    d = Path(a.data) if a.data else Path(data_dir())
    db_path = d / "index.db"
    if not db_path.exists():
        print(f"找不到索引库：{db_path}")
        print("用 --data 指定含 index.db 的目录。")
        return 1

    if server_running(a.port):
        print(f"检测到 {a.port} 端口有服务在运行。")
        print("请先关掉 start.bat 的黑窗口（或 Ctrl-C 停止服务）再执行本脚本，")
        print("否则写库会冲突，且被删掉的账号可能仍在内存会话里。")
        return 1

    conn = sqlite3.connect(str(db_path))
    n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    n_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    n_vols = conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0]
    who = conn.execute("SELECT username, role FROM users ORDER BY id").fetchall()

    print(f"数据目录：{d}")
    print(f"索引库  ：{db_path}  ({(db_path.stat().st_size / 1024 / 1024):.0f} MB)")
    print()
    if n_users == 0:
        print("当前没有任何账号，重启程序就会进入「创建管理员」流程，无需清理。")
        conn.close()
        return 0

    print(f"将删除 {n_users} 个账号：")
    for name, role in who:
        print(f"    - {name}  ({role})")
    print()
    print(f"以下数据会完整保留：{n_files:,} 条文件记录、{n_vols} 个资料库、全部封面与索引缓存")
    print()

    if not a.yes:
        ans = input("确认清空？输入 yes 继续：").strip().lower()
        if ans not in ("yes", "y"):
            print("已取消，什么都没改。")
            conn.close()
            return 0

    # 先备份（用 backup API，WAL 里的数据也能带上）
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = d / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    bak = backup_dir / f"index-before-reset-users-{ts}.db"
    try:
        dst = sqlite3.connect(str(bak))
        conn.backup(dst)
        dst.close()
        print(f"已备份 -> {bak}")
    except Exception as ex:
        print(f"[警告] 备份失败：{ex}")
        if not a.yes:
            print("备份没成功，已中止。想强行继续请加 --yes")
            conn.close()
            return 1

    for t in ACCOUNT_TABLES:
        try:
            n = conn.execute(f"DELETE FROM {t}").rowcount
            print(f"  清空 {t:<12} 删除 {n} 行")
        except sqlite3.OperationalError:
            pass  # 表不存在（老版本库）就跳过
    conn.commit()

    left = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    print()
    print(f"[完成] 账号已清空，文件记录仍有 {left:,} 条，未受影响。")
    print("       现在双击 start.bat，会看到「首次使用：创建管理员账号」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
