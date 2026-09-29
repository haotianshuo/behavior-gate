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

# ⚠️⚠️ 两张白名单已改为【运行时构建】——不再手写。
#
# 根因（2026-09-29 复盘，v3.5.16 发布后 CI 六矩阵全红）：
#   原实现手写 ALLOWED（标准库）与 LOCAL（本地模块）两张表。
#   v3.5.16 新增 review_unit.py 用了 urllib/http、且被 effect_gate 等 import——
#   两张表都没跟上 → 第一步"确认零第三方依赖"失败 → 六平台矩阵全部红灯，
#   后续 16 个测试套件全被 SKIPPED（发布证据链断裂）。
#   教训：手写表的失效模式不是"写错"，而是"没人记得更新"。
#   消除这一整类问题的方式不是把表补全，而是【取消手写表】。
#
# 借鉴（只取思想，未复制任何实现）：
#   · 标准库清单交给解释器自带的权威列表（sys.stdlib_module_names，3.10+）；
#   · 本地模块 = 仓库内所有 .py 的模块名（自动发现）。
#   两者都做到"新增文件零维护"。


def _stdlib_modules():
    """解释器自带的权威标准库清单（Python 3.10+）。"""
    names = getattr(sys, "stdlib_module_names", None)
    if names is None:
        # 老解释器没有这份清单。明确失败，而不是回退到手写表
        # （回退等于把这个缺陷原样保留）。
        sys.stderr.write(
            "[check_no_deps] need Python 3.10+ (sys.stdlib_module_names)\n")
        raise SystemExit(2)
    return set(names)


def _local_modules(root):
    """本项目内部模块：仓库内所有 .py 的模块名（自动发现）。"""
    mods = set()
    for p in root.rglob("*.py"):
        if {"__pycache__", ".venv", "venv", "build", "dist"} & set(p.parts):
            continue
        mods.add(p.stem)
    return mods

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
    stdlib = _stdlib_modules()
    local = _local_modules(root)
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
            if mod not in stdlib and mod not in local:
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
