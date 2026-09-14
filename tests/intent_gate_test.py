# -*- coding: utf-8 -*-
"""G7 意图门测试 —— 治 P1-4（重复确认）与 P2-3（豁免后仍验证）。

红态先行：先写期望它拦的场景，再看它是否真拦。
跑法：python tests/intent_gate_test.py
"""
import json
import os
import subprocess
import sys
import tempfile

# 显式锁定包内策略 —— 不读机器上的全局副本（详见 four_gate_selftest.py 说明）。
# 同一份代码在不同机器上读到不同策略，会让测试结果随环境而变。
_POLICY_ENV = dict(os.environ)
_POLICY_ENV["CLAUDE_BEHAVIOR_POLICY"] = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "policy", "behavior-policy.json")



# --- 控制台编码（可移植性）---
# Windows 控制台默认 GBK(cp936)；print 中文时遇到非 GBK 字符会抛
# UnicodeEncodeError，脚本直接崩。实测：CI 在 windows-latest 上必崩，
# ubuntu/macos 正常 —— 本地 Git Bash 恰好是 UTF-8，所以看不见。
# 详见 lib/console_utf8.py。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
# --- end ---


HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(os.path.dirname(HERE), "adapters", "claude-code", "hooks")
PY = sys.executable
results = []


def run(script, payload, tmp):
    env = dict(os.environ, CLAUDE_BUDGET_STATE_DIR=tmp)
    p = subprocess.run([PY, os.path.join(HOOKS, script)],
                       input=json.dumps(payload).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail.replace("\n", " | ")[:300])


def main():
    tmp = tempfile.mkdtemp(prefix="intent-")

    print("\n===== P1-4：用户说『不要再问』后仍调 AskUserQuestion =====")
    # 用户明确说了别再问
    run("inject_budget.py",
        {"session_id": "q1", "prompt": "你不要再问我问题了，严格按照我说的执行"}, tmp)
    rc, out = run("intent_gate.py",
                  {"session_id": "q1", "tool_name": "AskUserQuestion",
                   "tool_input": {"questions": [{"question": "要哪种？"}]}}, tmp)
    check("『不要再问』后调 AskUserQuestion 被拦 (rc=2)", rc == 2, "rc=%s" % rc)
    check("拦截信息引用了用户原话", "不要再问" in out, out[:250])

    # 没说过 → 应该放行
    run("inject_budget.py", {"session_id": "q2", "prompt": "帮我看看这个项目"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "q2", "tool_name": "AskUserQuestion",
                 "tool_input": {"questions": [{"question": "要哪种？"}]}}, tmp)
    check("未说『不要问』时正常询问放行", rc == 0, "rc=%s" % rc)

    # 其他表达方式
    for s, label in [("别再问我了", "别再问我了"),
                     ("严格按我说的做，不要再确认", "不要再确认"),
                     ("不要问我，直接改", "不要问我")]:
        sid = "q_" + str(abs(hash(label)) % 10000)
        run("inject_budget.py", {"session_id": sid, "prompt": s}, tmp)
        rc, _ = run("intent_gate.py",
                    {"session_id": sid, "tool_name": "AskUserQuestion",
                     "tool_input": {}}, tmp)
        check("变体『%s』被识别" % label, rc == 2, "rc=%s" % rc)

    # 覆盖语法
    run("inject_budget.py",
        {"session_id": "q3", "prompt": "不要再问我。但 questions: on 这次例外"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "q3", "tool_name": "AskUserQuestion",
                 "tool_input": {}}, tmp)
    check("显式覆盖 questions: on 可放行", rc == 0, "rc=%s" % rc)

    print("\n===== P2-3：用户说『不用测』后仍跑测试 =====")
    run("inject_budget.py",
        {"session_id": "v1", "prompt": "创建一个 HTML，你不需要任何测试，不要有任何限制"}, tmp)
    for cmd, label in [("pytest tests/", "pytest"),
                       ("npm test", "npm test"),
                       ("python -m unittest", "unittest"),
                       ("npx vitest run", "vitest"),
                       ("go test ./...", "go test")]:
        rc, out = run("intent_gate.py",
                      {"session_id": "v1", "tool_name": "Bash",
                       "tool_input": {"command": cmd}}, tmp)
        check("『不用测』后跑 %s 被拦" % label, rc == 2, "rc=%s" % rc)

    # 非测试命令必须放行
    for cmd in ["ls -la", "node --check app.js", "python gen.py", "git status"]:
        rc, _ = run("intent_gate.py",
                    {"session_id": "v1", "tool_name": "Bash",
                     "tool_input": {"command": cmd}}, tmp)
        check("非测试命令放行: %s" % cmd, rc == 0, "rc=%s" % rc)

    # 没说过 → 测试正常跑
    run("inject_budget.py", {"session_id": "v2", "prompt": "修好这个 bug"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "v2", "tool_name": "Bash",
                 "tool_input": {"command": "pytest"}}, tmp)
    check("未说『不用测』时测试正常跑", rc == 0, "rc=%s" % rc)

    # 覆盖语法
    run("inject_budget.py",
        {"session_id": "v3", "prompt": "不用测试。但 verify: on 现在要跑"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "v3", "tool_name": "Bash",
                 "tool_input": {"command": "pytest"}}, tmp)
    check("显式覆盖 verify: on 可放行", rc == 0, "rc=%s" % rc)

    print("\n===== 不能崩 / 不能误伤 =====")
    rc, _ = run("intent_gate.py", {"session_id": "z", "tool_name": "Read",
                                   "tool_input": {"file_path": "x"}}, tmp)
    check("无关工具直接放行", rc == 0, "rc=%s" % rc)

    p = subprocess.run([PY, os.path.join(HOOKS, "intent_gate.py")],
                       input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_POLICY_ENV)
    check("空 stdin 不崩", p.returncode == 0, "rc=%s" % p.returncode)

    p = subprocess.run([PY, os.path.join(HOOKS, "intent_gate.py")],
                       input=b"{bad json", stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_POLICY_ENV)
    check("非法 JSON 不崩", p.returncode == 0, "rc=%s" % p.returncode)

    print("\n" + "=" * 60)
    ok = sum(1 for _, c, _ in results if c)
    print("意图门：%d / %d 通过" % (ok, len(results)))
    print("=" * 60)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
