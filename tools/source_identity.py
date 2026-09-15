#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源身份工具 —— 默认只读校验；--write 才显式刷新。

    python tools/source_identity.py              # READ + CHECK（默认）
    python tools/source_identity.py --write      # 显式刷新声明
    python tools/source_identity.py --json       # 机器可读

## 为什么默认是只读

SOURCE_IDENTITY.json 的意义是「源码自证」。
如果 install / verify / CI 任何一处会自动刷新它，
那么让一份 dirty source 跑一次安装，它就把自己「合法化」了 ——
声明立刻失去意义，变成一句自我认证的废话。

所以：**写操作必须由人显式发起**，且只在这一个工具里提供。

## 什么时候需要 --write

    改动了 hooks/ 下的文件（含 VERSION）之后，
    且这次改动是要作为一个新的 source snapshot 被承认时。

它与「改 hook 必须同步改 VERSION」是同一条纪律的加强版：
改了内容，身份就必须跟着更新，否则 CI 的只读校验会红。

## 退出码

    0 = 源身份自证通过（或 --write 成功）
    2 = 源身份不一致 / 缺失 / 不可读（与包的 STOP 语义一致）

## 边界

本工具证明的是 **T1 本地一致性**：源码目录内部自洽且与声明一致。
它不证明源码正确、可部署、经过测试；
也不防有写权限者同时改源与声明（T2 NOT_PROTECTED）。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src_identity import (  # noqa: E402
    ALGORITHM, IDENTITY_NAME, SCHEMA_ID, check, default_pkg_dir,
    identity_path, write_identity,
)

DEFAULT_HOOKS = os.path.join(ROOT, "adapters", "claude-code", "hooks")


def main():
    ap = argparse.ArgumentParser(
        description="源身份：声明并校验「这份源码是什么快照」")
    ap.add_argument("--hooks", default=DEFAULT_HOOKS,
                    help="hooks 目录（默认 adapters/claude-code/hooks）")
    ap.add_argument("--pkg", default=None,
                    help="元数据层目录（默认取 hooks 的上一层）")
    ap.add_argument("--write", action="store_true",
                    help="显式刷新声明（默认只读校验）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    a = ap.parse_args()

    hooks = os.path.abspath(a.hooks)
    pkg = os.path.abspath(a.pkg) if a.pkg else default_pkg_dir(hooks)

    if a.write:
        ok, msg = write_identity(pkg, hooks)
        if a.json:
            print(json.dumps({"action": "write", "ok": ok, "message": msg},
                             ensure_ascii=False, indent=2))
        else:
            print("[source_identity] %s" % msg)
        return 0 if ok else 2

    r = check(pkg, hooks)
    if a.json:
        print(json.dumps({
            "action": "check",
            "ok": r["ok"],
            "hooksDir": hooks,
            "identityPath": identity_path(pkg),
            "declared": r["declared"],
            "actual": r["snapshot"],
            "reason": r["reason"],
            "algorithm": ALGORITHM,
            "schemaVersion": SCHEMA_ID,
            "claimBoundary": ("T1 本地一致性 only：只证明源码目录自洽且与声明一致；"
                              "不证明源码正确、可部署、经过测试；"
                              "不防有写权限者同时改源与声明（T2 NOT_PROTECTED）。"),
        }, ensure_ascii=False, indent=2))
    else:
        print("=" * 70)
        print("源身份校验（不是「源码正确」的检验）")
        print("=" * 70)
        print("  hooks   : %s" % hooks)
        print("  声明文件: %s" % identity_path(pkg))
        print("  算法    : %s" % ALGORITHM)
        print("  声明快照: %s" % ((r["declared"] or "（无）")[:16]))
        print("  实际快照: %s" % ((r["snapshot"] or "（不可读）")[:16]))
        print("")
        if r["ok"]:
            print("  判定: SOURCE_IDENTITY_OK")
            print("        %s" % r["reason"])
        else:
            print("  判定: SOURCE_IDENTITY_MISMATCH")
            print("        %s" % r["reason"])
            print("")
            print("  如果这是你刚改完 hooks 的预期结果，显式刷新声明：")
            print("      python tools/source_identity.py --write")
        print("")
        print("  ⚠️ 边界：本判定只覆盖【源码目录内部一致性】（T1）。")
        print("          不证明源码正确，也不构成不可篡改证明。")

    return 0 if r["ok"] else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write("[source_identity] 异常：%r\n" % (e,))
        sys.exit(2)
