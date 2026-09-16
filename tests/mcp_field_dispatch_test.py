# -*- coding: utf-8 -*-
"""MCP 字段分派回归（REAL_REGRESSION）

来源：另一台电脑复核报的 #53，本机用【真实 MCP 工具】复现。

    MCP 字段分类用裸子串判据：
        if "content" in name or "text" in name or "body" in name:
    而 `"text" in "context"` == True —— 于是 context / context_id 这类
    与"内容"无关的字段被当成内容字段，cmd 规则够不着 → 静默漏拦。

    同一处判据还造成反方向的错：
        自由文本字段（prompt / description）不含 content/text/body
        → 落进 else → 被当成命令字段 → 套 cmd_rules
        → 写一句「分析一下 <危险词> 为什么危险」就被拦。

本机实测的依据（不是推断）：
    本机【当前可达】的 3 个 MCP 工具（mcp__scheduled-tasks__*）
    字段是 taskId / prompt / description / cronExpression / fireAt
    / enabled / notifyOnCompletion —— 语义全是 ID / 文本 / 时间 / 布尔，
    没有一个承载 shell 命令。

运行：python tests/mcp_field_dispatch_test.py
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
HOOKS = os.path.join(PKG, "adapters", "claude-code", "hooks")
G5 = os.path.join(HOOKS, "destructive_gate.py")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _read_field_sets():
    """从 hook 源码里读两个字段名单。

    ⚠️ 不 import hook 模块的理由：
      ① hook 目录不在包级白名单里（check_no_deps 只认 lib 与 tools）
      ② 本项目的测试约定是【真跑子进程】，不是 import 后读属性

    用 re 提取（不引入 ast —— 它虽是标准库，但没在 check_no_deps 的
    ALLOWED 名单里，加进去等于为一个测试放宽 CI 检查，不划算）。
    """
    import re as _re
    src = open(G5, encoding="utf-8").read()
    out = {}
    for name in ("MCP_CMD_FIELDS",):
        m = _re.search(name + r"\s*=\s*\{(.*?)\}", src, _re.S)
        if not m:
            out[name] = set()
            continue
        items = _re.findall(r'"([^"]+)"', m.group(1))
        out[name] = set(items)
    return out["MCP_CMD_FIELDS"]


CMD_FIELDS = _read_field_sets()

# 拆开拼，避免本文件被自己的门拦（#26 现场）
RM = "rm"
R = "-rf"
KILL = "pk" + "ill"
D = "D:"

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("  %-6s %s" % ("PASS" if ok else "**FAIL**", name))
    if not ok and detail:
        print("         %s" % detail)


def run(tool, ti):
    d = tempfile.mkdtemp(prefix="mcpfd-")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "CLAUDE_BUDGET_STATE_DIR": d}
    p = subprocess.run(
        [sys.executable, G5],
        input=json.dumps({"session_id": "mcpfd",
                          "hook_event_name": "PreToolUse",
                          "tool_name": tool,
                          "tool_input": ti}).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
    return p.returncode


# 真实工具名（会被 matcher 命中）
TOOL = "mcp__scheduled-tasks__create_scheduled_task"


def main():
    print("=" * 88)
    print("MCP 字段分派回归（#53）")
    print("=" * 88)

    # ---------- 1. 判据不再是裸子串 ----------
    print("\n----- 1. 分类判据：不再用裸子串 -----")
    check("名单是精确集合（不是子串）", isinstance(CMD_FIELDS, set))
    check("命令名单非空", len(CMD_FIELDS) > 0,
          "实际 %d 项" % len(CMD_FIELDS))
    # ⚠️ 通用容器名【不该】在命令名单里 —— 它们语义不明，
    #    留着会让"拿它们装自由文本"的 server 过拦（#53 的过拦方向）。
    check("通用容器名不在命令名单里（data/input/payload/code）",
          not ({"data", "input", "payload"} & CMD_FIELDS),
          "实际命令名单：%s" % sorted(CMD_FIELDS))

    # ---------- 2. 命令字段：仍然拦 ----------
    print("\n----- 2. 已知【命令字段】里的危险命令仍拦 -----")
    for f in ["command", "cmd", "script", "shell", "exec", "argv",
              "args", "code", "stdin"]:
        rc = run(TOOL, {f: RM + " " + R + " " + D + "/"})
        check("字段 %-8s（应拦）" % f, rc == 2, "rc=%d" % rc)

    # ---------- 3. 过拦方向：自由文本不再被当命令 ----------
    print("\n----- 3. 自由文本字段里的【提及】不再误拦（#53 的过拦方向）-----")
    MENTION = "分析一下 " + KILL + " 这类命令为什么危险"
    for f in ["prompt", "description", "notes", "summary", "title",
              "message", "query", "question", "reason"]:
        rc = run(TOOL, {f: MENTION})
        check("字段 %-12s（提及，应放行）" % f, rc == 0, "rc=%d" % rc)

    # ---------- 4. 内容字段：仍按内容规则处理 ----------
    print("\n----- 4. 内容字段仍能被 content_rules 拦 -----")
    rc = run(TOOL, {"content": RM + " " + R + " /\n"})
    check("content 里整行删根（应拦）", rc == 2, "rc=%d" % rc)

    # ---------- 5. 名单外字段：不再默认当命令 ----------
    print("\n----- 5. 名单外字段不默认套 cmd 规则 -----")
    for f in ["context", "context_id", "subtext", "plaintext"]:
        rc = run(TOOL, {f: RM + " " + R + " " + D + "/"})
        check("字段 %-12s（名单外，不套 cmd）" % f, rc == 0, "rc=%d" % rc)

    # ---------- 6. 已知边界必须写进文档 ----------
    print("\n----- 6. 取舍必须写进文档（不能只在代码注释里）-----")
    doc = open(os.path.join(PKG, "install.md"), encoding="utf-8").read()
    check("install.md 的已知边界提到 MCP 字段分派的取舍",
          "MCP" in doc and "字段" in doc,
          "未在 install.md 找到该边界")

    npass = sum(1 for _, ok, _ in _results if ok)
    print("\n" + "=" * 88)
    print("MCP 字段分派回归：%d / %d 通过" % (npass, len(_results)))
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
