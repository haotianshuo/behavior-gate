# -*- coding: utf-8 -*-
"""BUG-1 / BUG-2 / BUG-3 回归测试。

BUG-1 预算无上界      —— agent_spawns 可设天文数字，等于关掉 G1
BUG-2 类型不校验      —— 上限值为字符串时静默放行
BUG-3 只认中文        —— 英文 prompt 完全不被识别（用户工作流以英文为主）
"""
import json, os, subprocess, sys, tempfile

H = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "adapters", "claude-code", "hooks")
PY = sys.executable
T = tempfile.mkdtemp(prefix="bugfix-")

# ⚠️ 状态目录必须通过环境变量传给子进程，且【不能】在本进程 import _lib 之后再设 ——
# _lib 会在模块级/首次调用时读环境变量并缓存。之前的版本因此把状态写到了
# claude-behavior-gates 下的真实会话文件，而 hook 读的是临时目录 → 永远对不上。
os.environ["CLAUDE_BUDGET_STATE_DIR"] = T
E = dict(os.environ, CLAUDE_BUDGET_STATE_DIR=T)
res = []


def call(script, payload):
    p = subprocess.run([PY, os.path.join(H, script)],
                       input=json.dumps(payload).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=E)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def check(name, cond, detail=""):
    res.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail[:300])


def budget_of(sid):
    f = os.path.join(T, sid + ".budget.json")
    if not os.path.isfile(f):
        return None
    return json.load(open(f, encoding="utf-8"))


print("\n===== BUG-1：预算必须有上界 =====")
cases = [
    ("999999", 20, "天文数字应被压到硬上限"),
    ("999999999999999999999", 20, "超大整数同样压上限"),
    ("100", 20, "超过硬上限就压"),
    ("10", 10, "正常值原样保留"),
    ("0", 0, "0 合法"),
]
for val, want_max, why in cases:
    call("inject_budget.py", {"session_id": "cap", "prompt": "agent_spawns: %s" % val})
    st = budget_of("cap") or {}
    got = st.get("budget", {}).get("agent_spawns")
    ok = (got == want_max) if val in ("10", "0") else (got == want_max)
    check("agent_spawns: %-22s -> %s（%s）" % (val[:22], got, why), ok,
          "期望 %s，实际 %s" % (want_max, got))

print("\n===== BUG-2：类型不合法时保守处理 =====")
f = os.path.join(T, "typ.budget.json")
os.makedirs(os.path.dirname(f), exist_ok=True)

for raw, expect_rc, why in [
    ('{"budget":{"agent_spawns":"5"}}', 2, "字符串类型 -> 保守拒绝"),
    ('{"budget":{"agent_spawns":null}}', 2, "null -> 保守拒绝"),
    ('{"budget":{"agent_spawns":[1,2]}}', 2, "数组 -> 保守拒绝"),
    ('{"budget":{"agent_spawns":3}}', 0, "合法数字 -> 放行"),
]:
    open(f, "w", encoding="utf-8").write(raw)
    rc, out = call("budget_gate.py", {"session_id": "typ", "tool_name": "Agent",
                                      "tool_input": {"subagent_type": "E", "description": "x"}})
    check("状态 %-36s rc=%d（%s）" % (raw[:36], rc, why), rc == expect_rc,
          "期望 rc=%d，实际 %d" % (expect_rc, rc))

print("\n===== BUG-3：英文 prompt 也能识别（输出仍为中文）=====")
en_ask = ["do not ask me again", "don't ask me", "stop asking me",
          "no more questions", "just do it, don't ask"]
for p in en_ask:
    call("inject_budget.py", {"session_id": "en1", "prompt": p})
    st = budget_of("en1") or {}
    check("英文『别问』: %-26s" % repr(p[:26]), st.get("intent_no_ask") is True,
          "no_ask=%s" % st.get("intent_no_ask"))

en_ver = ["no need to test", "don't test", "no tests required",
          "skip the tests", "testing is not required"]
for p in en_ver:
    call("inject_budget.py", {"session_id": "en2", "prompt": p})
    st = budget_of("en2") or {}
    check("英文『不用测』: %-24s" % repr(p[:24]), st.get("intent_no_verify") is True,
          "no_verify=%s" % st.get("intent_no_verify"))

print("\n===== BUG-3 附：拦截信息必须仍是中文 =====")
call("inject_budget.py", {"session_id": "en3", "prompt": "do not ask me again"})
rc, out = call("intent_gate.py", {"session_id": "en3", "tool_name": "AskUserQuestion",
                                  "tool_input": {"questions": [{"question": "q"}]}})
check("英文触发时仍拦下", rc == 2, "rc=%d" % rc)
check("拦截信息是中文（用户看不懂英文）", "【G7 意图门】" in out and "不要再问" in out,
      out[:200])

print("\n===== 回归：中文仍然工作 =====")
for p, key in [("不要再问我了", "intent_no_ask"),
               ("你不需要任何测试", "intent_no_verify")]:
    call("inject_budget.py", {"session_id": "cn", "prompt": p})
    st = budget_of("cn") or {}
    check("中文『%s』仍识别" % p, st.get(key) is True, "%s=%s" % (key, st.get(key)))

print("\n" + "=" * 62)
ok = sum(1 for _, c, _ in res if c)
print("预算安全测试：%d / %d 通过" % (ok, len(res)))
print("=" * 62)
sys.exit(0 if ok == len(res) else 1)
