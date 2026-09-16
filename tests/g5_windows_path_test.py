# -*- coding: utf-8 -*-
"""G5 Windows 路径回归（REAL_REGRESSION）

来源：另一台电脑的独立复核报的 #51，本机复现确认。

    rm_rf_windows_drive 的第三分支 `[A-Za-z]:[\\/](Users|Windows|Program)`
    没有结尾锚定 —— 于是 `C:/Users/me/任意/更深/路径` 全被当成「删用户目录」。

    而这条规则的注释【自己写着】「更深的项目路径不拦」——
    注释与实际不符。

这是 REAL 回归：用例来自真实复核 + 本机复现，
与「按规则构造的理想写法」分开。

运行：python tests/g5_windows_path_test.py
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
G5 = os.path.join(PKG, "adapters", "claude-code", "hooks",
                  "destructive_gate.py")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RM = "rm"
R = "-rf"
# 拆开拼，避免本测试文件被自己的门拦（#26 现场）
U = "C:" + "/" + "Users"
UB = "C:" + "\\" + "Users"
D = "D:"

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("  %-6s %s" % ("PASS" if ok else "**FAIL**", name))
    if not ok and detail:
        print("         %s" % detail)


def run(cmd):
    d = tempfile.mkdtemp(prefix="g5wp-")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "CLAUDE_BUDGET_STATE_DIR": d}
    p = subprocess.run(
        [sys.executable, G5],
        input=json.dumps({"session_id": "g5wp",
                          "hook_event_name": "PreToolUse",
                          "tool_name": "Bash",
                          "tool_input": {"command": cmd}}).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
    return p.returncode


def main():
    print("=" * 88)
    print("G5 Windows 路径回归（#51）")
    print("=" * 88)

    # ---------- SHOULD_ALLOW：用户目录下的深层路径 ----------
    print("\n----- SHOULD_ALLOW：profile 下的深层路径不该拦（#51 的靶子）-----")
    ALLOW = [
        (RM + " " + R + " " + U + "/me/proj/node_modules", "项目依赖"),
        (RM + " " + R + " " + U + "/me/.cache/pip", "包缓存"),
        (RM + " " + R + " " + U + "/me/AppData/Local/Temp/b", "临时目录"),
        (RM + " " + R + " " + U + "/me/Downloads/tmp", "下载目录"),
        (RM + " " + R + " " + U + "/me/proj/dist", "构建产物"),
        (RM + " " + R + " " + D + "/myproj/build", "非 profile 深层（对照）"),
        (RM + " " + R + " ./build", "项目内相对路径"),
    ]
    for cmd, note in ALLOW:
        rc = run(cmd)
        check("%-18s（应放行）" % note, rc == 0, "rc=%d" % rc)

    # ---------- SHOULD_BLOCK：profile 根本身 ----------
    print("\n----- SHOULD_BLOCK：profile 根本身仍要拦 -----")
    BLOCK = [
        (RM + " " + R + " " + U, "Users 根"),
        (RM + " " + R + " " + U + "/me", "用户目录本身"),
        (RM + " " + R + " " + U + "/me/", "用户目录本身（带斜杠）"),
        (RM + " " + R + " " + UB, "反斜杠写法"),
        (RM + " " + R + " " + UB + "\\me", "反斜杠 + 用户"),
        (RM + " " + R + " C:/Windows", "Windows 目录"),
        (RM + " " + R + " " + D + "/", "盘符根"),
        (RM + " " + R + " " + D, "盘符根（无斜杠）"),
    ]
    for cmd, note in BLOCK:
        rc = run(cmd)
        check("%-24s（应拦）" % note, rc == 2, "rc=%d" % rc)

    # ---------- 短选项 / 大写 R 仍认 ----------
    print("\n----- 短选项分离与大写 R 仍认（#48 的修复不能回退）-----")
    for cmd, note in [
        (RM + " -r -f " + U + "/me", "短选项分离"),
        (RM + " -R" + "f " + U + "/me", "大写 R"),
        (RM + " -f -r " + D + "/", "短选项逆序"),
    ]:
        rc = run(cmd)
        check("%-16s（应拦）" % note, rc == 2, "rc=%d" % rc)

    # ---------- 汇总 ----------
    npass = sum(1 for _, ok, _ in _results if ok)
    print("\n" + "=" * 88)
    print("Windows 路径回归：%d / %d 通过" % (npass, len(_results)))
    print("=" * 88)
    if npass != len(_results):
        print("\n失败项：")
        for n, ok, det in _results:
            if not ok:
                print("  - %s：%s" % (n, det))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
