"""
tools/check_bat_encoding.py —— 检查 .bat 是否为合法 GBK

Windows cmd 按系统代码页（中文系统 936/GBK）解析批处理文件。
若 bat 存成 UTF-8，中文字节会被误读，行被截断，导致命令拆碎、
脚本一闪就退出（chcp 65001 放在任何位置都救不了，因为 cmd 已解析完）。

用法：python tools/check_bat_encoding.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check(p: Path) -> bool:
    raw = p.read_bytes()
    ok = True
    if raw[:3] == b"\xef\xbb\xbf":
        print(f"[FAIL] {p.name}: 带 UTF-8 BOM，cmd 会把它当成命令的一部分")
        ok = False
    try:
        raw.decode("gbk")
    except UnicodeDecodeError as e:
        print(f"[FAIL] {p.name}: 不是合法 GBK → {e}")
        print("       修复：用 GBK/ANSI 另存，或执行下面的一行转换")
        print(f"       python -c \"import io;d=open(r'{p.name}','rb').read();"
              f"open(r'{p.name}','wb').write(d.decode('utf-8').encode('gbk'))\"")
        return False
    if b"\n" in raw.replace(b"\r\n", b""):
        print(f"[WARN] {p.name}: 存在单独的 LF 行尾，建议统一为 CRLF")
    if ok:
        print(f"[ OK ] {p.name}: 合法 GBK，{len(raw)} 字节")
    return ok


def main() -> int:
    bats = sorted(list(ROOT.glob("*.bat")) + list(ROOT.glob("tools/*.bat")))
    if not bats:
        print("没有找到 .bat 文件")
        return 0
    bad = [p for p in bats if not check(p)]
    print()
    if bad:
        print(f"有 {len(bad)} 个文件编码不对，双击会闪退。")
        return 1
    print(f"全部 {len(bats)} 个 bat 编码正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
