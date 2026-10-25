"""
tools/reset_pw.py —— 忘密码时在命令行重置（不用进网页）

用法（在程序目录下执行）：
    python tools/reset_pw.py                       # 列出所有账号
    python tools/reset_pw.py --user tata           # 给 tata 重置为随机强密码并打印
    python tools/reset_pw.py --user tata --pw Abc12345    # 指定新密码
    python tools/reset_pw.py --data D:\\MyLib       # 指定数据目录

安全说明：必须在能访问数据目录的本机执行；重置会同时清掉该账号所有登录会话。
"""
from __future__ import annotations

import argparse
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="FileLib 账号密码重置")
    ap.add_argument("--user", help="要重置的账号名；不给则只列出账号")
    ap.add_argument("--pw", help="新密码；不给则生成随机密码")
    ap.add_argument("--data", help="数据目录（默认自动探测，如 C:\\Users\\你\\FileIndex）")
    a = ap.parse_args()

    from filelib.core import db, users
    from filelib.core.config import data_dir

    d = Path(a.data) if a.data else Path(data_dir())
    dbx = d / "index.db"
    if not dbx.exists():
        print(f"找不到索引库：{dbx}")
        print("如果程序装在别处，请用 --data 指定数据目录（含 index.db 的那个文件夹）。")
        return 1
    print(f"数据目录：{d}")

    conn = db.connect(dbx)
    rows = users.list_users(conn)
    if not rows:
        print("还没有任何账号（下次启动网页会进入初始化向导）。")
        return 0

    if not a.user:
        print(f"\n共 {len(rows)} 个账号：")
        print(f"  {'ID':<4}{'账号':<20}{'角色':<8}{'状态':<8}密码")
        for u in rows:
            state = "已停用" if u.get("disabled") else "正常"
            print(f"  {u['id']:<4}{u['username']:<20}{u['role']:<8}{state:<8}{u.get('password') or ''}")
        print("\n重置某个账号：python tools/reset_pw.py --user 账号 [--pw 新密码]")
        return 0

    u = users.get_by_name(conn, a.user)
    if not u:
        print(f"账号不存在：{a.user}")
        print("现有账号：" + "、".join(x["username"] for x in rows))
        return 1

    pw = a.pw
    if not pw:
        alphabet = string.ascii_letters + string.digits
        pw = "".join(secrets.choice(alphabet) for _ in range(12))
    if len(pw) < 4:
        print("密码至少 4 位")
        return 1

    users.update(conn, u["id"], password=pw)
    # 旧会话全部作废，避免密码已换但别处还挂着登录
    conn.execute("DELETE FROM sessions WHERE uid=?", (u["id"],))
    conn.commit()

    print(f"\n已重置账号「{u['username']}」")
    print(f"  新密码：{pw}")
    print("  该账号此前的登录状态已全部失效，请用新密码重新登录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
