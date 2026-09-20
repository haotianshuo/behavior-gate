#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门行为核验 —— 验证「门真的在拦」，不是「代码逻辑对」。

⚠️ 它不替代 tests/。两者测的是【不同的东西】：

    tests/*.py            →  代码逻辑是否正确（单元/回归）
    本工具                →  门在当前部署下是否真的生效

    一个门可以「代码逻辑全绿」但「实际不生效」：
      · hook 没注册进 settings.json
      · 被平台跳过（matcher 写错、事件名写错）
      · 异常时静默放行（fail-open）
    那正是本项目定义的最严重缺陷：**看起来装了门、其实门是假的**。

## 方法

用【真实部署的 hook】+【真实 hook 输入 schema】喂进去，只看**退出码**。
不 mock、不 import 门内部函数 —— 走的是平台同一条路径。

## 为什么用隔离状态目录

核验会写 gate-events.jsonl 与 budget 状态。
用 CLAUDE_BUDGET_STATE_DIR 重定向到临时目录，**不污染真实会话状态**。

## 边界（必须如实声明）

    证明    : 在【本机 + 当前版本 + 当前注册配置】下，这四道门按预期退出
    不证明  : 门的判定语义正确、误报率可接受、其他门也生效
    不证明  : 平台一定会按这个退出码行事（那是宿主行为，非本工具范围）
    时效性  : 门的实际行为随版本/平台/配置变化 —— 改动 hook 后必须重跑

## 退出码

    0 = 全部核验通过
    2 = 有核验失败（与包的 STOP 语义一致）

用法：
    python tools/verify_gates_live.py
    python tools/verify_gates_live.py --json
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def find_hooks_dir():
    """部署侧的 hooks 目录。优先环境变量，其次 ~/.claude/hooks。"""
    env = os.environ.get("BEHAVIOR_GATE_HOOKS")
    if env and os.path.isdir(env):
        return env
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    cand = os.path.join(home, ".claude", "hooks")
    if os.path.isdir(cand):
        return cand
    # 退回到包内副本（供 CI / 未安装环境）
    return os.path.join(ROOT, "adapters", "claude-code", "hooks")


def find_python():
    here = os.path.join(ROOT, "lib")
    sys.path.insert(0, here)
    try:
        from find_python import find_python as fp
        exe, _ver, _src, *_ = fp(verbose=False)
        return exe
    except Exception:
        return sys.executable


class Runner(object):
    def __init__(self, hooks, pyexe, state_dir):
        self.hooks = hooks
        self.pyexe = pyexe
        self.state = state_dir
        self.env = dict(os.environ)
        self.env["CLAUDE_BUDGET_STATE_DIR"] = state_dir
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.results = []

    def call(self, script, payload):
        p = subprocess.run(
            [self.pyexe, os.path.join(self.hooks, script)],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True, env=self.env)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))

    def check(self, gate, label, got, want, note=""):
        ok = (got == want)
        self.results.append({"gate": gate, "label": label,
                             "got": got, "want": want, "ok": ok, "note": note})
        return ok

    def events(self):
        p = os.path.join(self.state, "gate-events.jsonl")
        if not os.path.exists(p):
            return 0
        with open(p, encoding="utf-8") as f:
            return sum(1 for l in f if l.strip())


def run_all(r):
    # ---------------- G1 预算门 ----------------
    # ① 无预算行 → 应拦
    r.call("inject_budget.py", {"session_id": "cap-g1-a", "transcript_path": "x",
                                "cwd": ".", "hook_event_name": "UserPromptSubmit",
                                "prompt": "帮我看看"})
    rc, _, _ = r.call("budget_gate.py", {"session_id": "cap-g1-a", "transcript_path": "x",
                                         "cwd": ".", "hook_event_name": "PreToolUse",
                                         "tool_name": "Agent",
                                         "tool_input": {"subagent_type": "general-purpose",
                                                        "description": "t"}})
    r.check("G1", "无预算行时派 Agent → 应拦", rc, 2)

    # ② 显式授权 → 应放行
    r.call("inject_budget.py", {"session_id": "cap-g1-b", "transcript_path": "x",
                                "cwd": ".", "hook_event_name": "UserPromptSubmit",
                                "prompt": "agent_spawns: 2"})
    rc, _, _ = r.call("budget_gate.py", {"session_id": "cap-g1-b", "transcript_path": "x",
                                         "cwd": ".", "hook_event_name": "PreToolUse",
                                         "tool_name": "Agent",
                                         "tool_input": {"subagent_type": "general-purpose",
                                                        "description": "t"}})
    r.check("G1", "显式 agent_spawns: 2 后派 → 应放行", rc, 0)

    # ③ 子代理内再派 → 应拦
    rc, _, _ = r.call("budget_gate.py", {"session_id": "cap-g1-d", "transcript_path": "x",
                                         "cwd": ".", "hook_event_name": "PreToolUse",
                                         "tool_name": "Agent", "agent_id": "sub-1",
                                         "tool_input": {"subagent_type": "general-purpose",
                                                        "description": "t"}})
    r.check("G1", "子代理内再派 → 应拦", rc, 2)

    # ---------------- G3 生效门 ----------------
    rc, _, _ = r.call("effect_gate.py", {"session_id": "cap-g3-a", "transcript_path": "x",
                                         "cwd": ".", "hook_event_name": "Stop",
                                         "stop_hook_active": False,
                                         "last_assistant_message": "已完成"})
    r.check("G3", "说「已完成」无证据块 → 应打回", rc, 2)

    rc, _, _ = r.call("effect_gate.py", {
        "session_id": "cap-g3-b", "transcript_path": "x", "cwd": ".",
        "hook_event_name": "Stop", "stop_hook_active": False,
        "last_assistant_message": "已完成。\n\n**证据**：L3 端到端\n**风险级别**：低"})
    r.check("G3", "有证据块 L3 + 风险级别 → 应放行", rc, 0)

    rc, _, _ = r.call("effect_gate.py", {"session_id": "cap-g3-c", "transcript_path": "x",
                                         "cwd": ".", "hook_event_name": "Stop",
                                         "stop_hook_active": False,
                                         "last_assistant_message": "我不认为它已修复"})
    r.check("G3", "否定语境（不算完成）→ 应放行", rc, 0)

    # ---------------- G5 危险门 ----------------
    # 注意：posix_tmp 在 3.5.13 由 DENY 降为 WARN
    #       → rc=0 是【正确行为】，但【必须打警告】，否则就是"静默通过"
    for cmd, want, label, need_warn, need_silent in [
            ("rm -rf /", 2, "递归强删根 → 应拦", False, False),
            ("pkill -f python", 2, "模糊 kill → 应拦", False, False),
            ("find /tmp -name x", 0, "POSIX /tmp（WARN）→ 应放行且有警告", True, False),
            ("ls -la", 0, "正常命令（对照）→ 应放行且静默", False, True)]:
        rc, _out, err = r.call("destructive_gate.py",
                               {"session_id": "cap-g5", "transcript_path": "x", "cwd": ".",
                                "hook_event_name": "PreToolUse", "tool_name": "Bash",
                                "tool_input": {"command": cmd}})
        ok = r.check("G5", label, rc, want)
        if need_warn:
            has = bool(err.strip())
            r.check("G5", "  └ 警告输出存在", has, True,
                    "" if has else "静默通过 —— 正是要消灭的形状")
        if need_silent:
            silent = not err.strip()
            r.check("G5", "  └ 完全静默", silent, True)

    # ---------------- G7 意图门 ----------------
    r.call("inject_budget.py", {"session_id": "cap-g7", "transcript_path": "x", "cwd": ".",
                                "hook_event_name": "UserPromptSubmit",
                                "prompt": "不要再问我了，直接做"})
    rc, _, _ = r.call("intent_gate.py", {
        "session_id": "cap-g7", "transcript_path": "x", "cwd": ".",
        "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "x", "header": "h",
                                      "options": [{"label": "a", "description": "d"},
                                                  {"label": "b", "description": "d"}],
                                      "multiSelect": False}]}})
    r.check("G7", "用户说「不要再问」后弹询问 → 应拦", rc, 2)

    # ---------------- 观测层 ----------------
    n = r.events()
    r.check("观测层", "gate-events.jsonl 有落盘", n > 0, True,
            "%d 条" % n)


def main():
    ap = argparse.ArgumentParser(description="门行为核验（门真的在拦吗）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--hooks", default=None, help="hooks 目录（默认自动探测）")
    args = ap.parse_args()

    hooks = args.hooks or find_hooks_dir()
    pyexe = find_python()
    state = os.path.join(tempfile.gettempdir(), "bg-capability-check")
    shutil.rmtree(state, ignore_errors=True)
    os.makedirs(state, exist_ok=True)

    r = Runner(hooks, pyexe, state)
    run_all(r)

    passed = sum(1 for x in r.results if x["ok"])
    total = len(r.results)
    failed = [x for x in r.results if not x["ok"]]

    if args.json:
        print(json.dumps({"hooks": hooks, "passed": passed, "total": total,
                          "results": r.results}, ensure_ascii=False, indent=2))
    else:
        print("=" * 78)
        print("门行为核验（门真的在拦吗 —— 不是「代码逻辑对不对」）")
        print("=" * 78)
        print("  hooks  : %s" % hooks)
        print("  python : %s" % pyexe)
        print("  状态   : %s（隔离，不污染真实会话）" % state)
        print("  时间   : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        print()
        cur = None
        for x in r.results:
            if x["gate"] != cur:
                cur = x["gate"]
                print("  [%s]" % cur)
            mark = "OK " if x["ok"] else "!!!"
            extra = ("  %s" % x["note"]) if x.get("note") else ""
            print("    %s %-44s got=%s want=%s%s"
                  % (mark, x["label"], x["got"], x["want"], extra))
        print()
        print("=" * 78)
        print("  结果: %d/%d 通过" % (passed, total))
        if failed:
            print()
            print("  失败项（门可能没生效 —— 那正是本项目定义的最严重缺陷）:")
            for x in failed:
                print("    · [%s] %s  got=%s want=%s"
                      % (x["gate"], x["label"], x["got"], x["want"]))
        print()
        print("  边界: 只证明【本机 + 当前版本 + 当前注册配置】下这四道门按预期退出。")
        print("        不证明判定语义正确、不证明误报率可接受、不证明其他门也生效。")
        print("        门的行为随版本/平台/配置变化 —— 改动 hook 后必须重跑。")
        print("=" * 78)

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
