#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认本项目【零第三方依赖】—— 只使用 Python 标准库。

为什么要单独做一个脚本而不是写在 workflow 里：
  1. Windows runner 的默认控制台编码是 GBK（cp936），
     内联脚本里任何非 ASCII 的 print 都会抛 UnicodeEncodeError。
     把输出强制成 UTF-8 并只写 ASCII 提示，才能在三平台一致。
  2. 这个检查在本地也该能跑（`python .github/scripts/check_no_deps.py`），
     内联在 YAML 里的代码没法复用。

输出只使用 ASCII，避免任何平台的编码差异。
"""

import pathlib
import re
import sys

# Python 标准库模块白名单（本项目实际用到的）
ALLOWED = {
    "argparse", "collections", "concurrent", "copy", "dataclasses", "datetime",
    "enum", "functools", "hashlib", "importlib", "io", "itertools", "json",
    "math", "os", "pathlib", "platform", "random", "re", "shutil", "socket",
    "stat", "string", "subprocess", "sys", "tempfile", "textwrap", "threading",
    "time", "traceback", "typing", "uuid", "warnings", "winreg",
    "unittest", "zipfile", "collections.abc",
}

# 本项目内部模块
LOCAL = {"_lib", "deploy_files", "find_python", "intent_gate"}

IMPORT_RE = re.compile(r"\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)")


def strip_docstrings(text):
    """去掉三引号字符串内容。

    为什么必须做：文档字符串和示例里会出现 import 语句，例如
        '''用法：from console_utf8 import force_utf8_console'''
    按行扫会把它当成真实依赖。实测误报：本脚本曾把
    lib/console_utf8.py 自己的文档示例报成第三方依赖。
    """
    out = []
    in_str = False
    quote = None
    for line in text.splitlines():
        if not in_str:
            for q in ('"""', "'''"):
                if q in line:
                    before, _, after = line.partition(q)
                    out.append(before)
                    in_str, quote = True, q
                    # 同一行内闭合
                    if q in after:
                        _, _, rest = after.partition(q)
                        out.append(rest)
                        in_str = False
                    break
            else:
                out.append(line)
        else:
            if quote in line:
                _, _, after = line.partition(quote)
                out.append(after)
                in_str = False
    return "\n".join(out)


def main():
    # Windows 上强制 UTF-8 输出，避免 GBK 编解码失败
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = pathlib.Path(__file__).resolve().parents[2]
    bad = []
    scanned = 0

    for path in sorted(root.rglob("*.py")):
        # 跳过虚拟环境与构建产物
        parts = set(path.parts)
        if {"__pycache__", ".venv", "venv", "build", "dist"} & parts:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            bad.append((str(path.relative_to(root)), "<not-utf8>"))
            continue
        for line in strip_docstrings(text).splitlines():
            m = IMPORT_RE.match(line)
            if not m:
                continue
            mod = m.group(1)
            if mod not in ALLOWED and mod not in LOCAL:
                bad.append((str(path.relative_to(root)), mod))

    print("[check_no_deps] scanned %d python files" % scanned)

    if bad:
        print("[check_no_deps] FAIL: third-party imports found")
        for path, mod in bad:
            print("  %s -> %s" % (path, mod))
        return 1

    print("[check_no_deps] OK: standard library only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
