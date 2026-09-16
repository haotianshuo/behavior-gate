# -*- coding: utf-8 -*-
"""Gate 事件记录回归（REAL_REGRESSION）

覆盖第 7 步 observability 的关键不变量：
  1. 每个门拒绝时恰好 1 条事件
  2. 放行不记事件
  3. 一次判定不重复记录
  4. NATURAL / CONTROLLED / 非法值
  5. 隐私：JSONL 不含原始 command / prompt / content
  6. source identity 归因字段
  7. 记录失败不改变 Gate 判定（fail policy）

运行：python tests/gate_events_test.py
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
HOOKS = os.path.join(PKG, "adapters", "claude-code", "hooks")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RM = "rm"
R = "-rf"
_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("  %-6s %s" % ("PASS" if ok else "**FAIL**", name))
    if not ok and detail:
        print("         %s" % detail)


def run(script, payload, state_dir, source_class=None):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "CLAUDE_BUDGET_STATE_DIR": state_dir}
    if source_class:
        env["BEHAVIOR_GATE_SOURCE_CLASS"] = source_class
    p = subprocess.run(
        [sys.executable, os.path.join(HOOKS, script)],
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def events(state_dir):
    p = os.path.join(state_dir, "gate-events.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    for l in open(p, encoding="utf-8").read().strip().splitlines():
        if l.strip():
            out.append(json.loads(l))
    return out


def bash_payload(cmd, sid):
    return {"session_id": sid, "hook_event_name": "PreToolUse",
            "tool_name": "Bash", "tool_input": {"command": cmd}}


def main():
    print("=" * 88)
    print("Gate 事件记录回归")
    print("=" * 88)

    # ---------- 1. G5：拒绝记事件，放行不记 ----------
    print("\n----- 1. G5 恰好 1 条 / 放行 0 条 -----")
    d = tempfile.mkdtemp(prefix="gev-")
    rc, _ = run("destructive_gate.py", bash_payload(RM + " " + R + " /", "s1"), d)
    ev = events(d)
    check("G5 deny → rc=2", rc == 2, "rc=%d" % rc)
    check("G5 deny → 恰好 1 条事件", len(ev) == 1, "实际 %d" % len(ev))
    if ev:
        e = ev[0]
        check("gate_id=G5", e.get("gate_id") == "G5", repr(e.get("gate_id")))
        check("rule_id 正确", e.get("rule_id") == "rm_rf_traversal",
              repr(e.get("rule_id")))
        check("decision=deny", e.get("decision") == "deny")
        check("tool=Bash", e.get("tool") == "Bash")

    d2 = tempfile.mkdtemp(prefix="gev-")
    run("destructive_gate.py", bash_payload(RM + " " + R + " /home/u/data", "s2"), d2)
    check("G5 allow → 0 条事件", len(events(d2)) == 0)

    # ---------- 2. content rule 记的是 content rule ----------
    print("\n----- 2. 写入内容命中 content rule，记的也是 content rule -----")
    d3 = tempfile.mkdtemp(prefix="gev-")
    run("destructive_gate.py",
        {"session_id": "s3", "hook_event_name": "PreToolUse",
         "tool_name": "Write",
         "tool_input": {"file_path": "a.py", "content": RM + " " + R + " /\n"}},
        d3)
    ev3 = events(d3)
    check("Write 内容命中 → 1 条", len(ev3) == 1, "实际 %d" % len(ev3))
    if ev3:
        check("rule_id 是 content 规则",
              ev3[0].get("rule_id") == "rm_rf_in_content",
              repr(ev3[0].get("rule_id")))

    # ---------- 3. G1 ----------
    print("\n----- 3. G1 预算拒绝 -----")
    d4 = tempfile.mkdtemp(prefix="gev-")
    rc, _ = run("budget_gate.py",
                {"session_id": "s4", "hook_event_name": "PreToolUse",
                 "tool_name": "Agent",
                 "tool_input": {"subagent_type": "Explore",
                                "description": "t"}}, d4)
    ev4 = events(d4)
    check("G1 deny → 恰好 1 条", rc == 2 and len(ev4) == 1,
          "rc=%d n=%d" % (rc, len(ev4)))
    if ev4:
        check("gate_id=G1", ev4[0].get("gate_id") == "G1")
        check("rule_id 是 spawn 类",
              ev4[0].get("rule_id", "").startswith("spawn_"),
              repr(ev4[0].get("rule_id")))

    # ---------- 4. G3 ----------
    print("\n----- 4. G3 Stop feedback -----")
    d5 = tempfile.mkdtemp(prefix="gev-")
    rc, _ = run("effect_gate.py",
                {"session_id": "s5", "hook_event_name": "Stop",
                 "stop_hook_active": False, "transcript_path": "",
                 "last_assistant_message": "已经全部修复完成，验证通过。"}, d5)
    ev5 = events(d5)
    check("G3 block → 恰好 1 条", rc == 2 and len(ev5) == 1,
          "rc=%d n=%d" % (rc, len(ev5)))
    if ev5:
        check("gate_id=G3", ev5[0].get("gate_id") == "G3")
        check("decision=stop_feedback",
              ev5[0].get("decision") == "stop_feedback",
              repr(ev5[0].get("decision")))

    # ---------- 5. G7 ----------
    print("\n----- 5. G7 意图拒绝 -----")
    d6 = tempfile.mkdtemp(prefix="gev-")
    # 先让 inject_budget 记下「不要再问」意图
    run("inject_budget.py",
        {"session_id": "s6", "hook_event_name": "UserPromptSubmit",
         "prompt": "不要再问我了，直接执行。"}, d6)
    rc, _ = run("intent_gate.py",
                {"session_id": "s6", "hook_event_name": "PreToolUse",
                 "tool_name": "AskUserQuestion",
                 "tool_input": {"questions": [{"question": "?"}]}}, d6)
    ev6 = events(d6)
    check("G7 deny → 恰好 1 条", rc == 2 and len(ev6) == 1,
          "rc=%d n=%d" % (rc, len(ev6)))
    if ev6:
        check("gate_id=G7", ev6[0].get("gate_id") == "G7")
        check("rule_id=questions_disabled",
              ev6[0].get("rule_id") == "questions_disabled",
              repr(ev6[0].get("rule_id")))

    # ---------- 6. 不重复 ----------
    print("\n----- 6. 一次判定只记一次 -----")
    d7 = tempfile.mkdtemp(prefix="gev-")
    run("destructive_gate.py", bash_payload(RM + " " + R + " /", "s7"), d7)
    ev7 = events(d7)
    check("单次调用不产生重复事件", len(ev7) == 1, "实际 %d 条" % len(ev7))

    # ---------- 7. source_class ----------
    print("\n----- 7. source_class ---------")
    d8 = tempfile.mkdtemp(prefix="gev-")
    run("destructive_gate.py", bash_payload(RM + " " + R + " /", "s8"), d8)
    check("默认 NATURAL", events(d8)[0].get("source_class") == "NATURAL",
          repr(events(d8)[0].get("source_class")))

    d9 = tempfile.mkdtemp(prefix="gev-")
    run("destructive_gate.py", bash_payload(RM + " " + R + " /", "s9"), d9,
        source_class="CONTROLLED")
    check("显式 CONTROLLED",
          events(d9)[0].get("source_class") == "CONTROLLED",
          repr(events(d9)[0].get("source_class")))

    d10 = tempfile.mkdtemp(prefix="gev-")
    run("destructive_gate.py", bash_payload(RM + " " + R + " /", "s10"), d10,
        source_class="FOO")
    check("非法值 → UNKNOWN（不污染统计）",
          events(d10)[0].get("source_class") == "UNKNOWN",
          repr(events(d10)[0].get("source_class")))

    # ---------- 8. 隐私 ----------
    print("\n----- 8. 隐私回归 -----")
    allowed = {"ts", "session", "gate_id", "rule_id", "decision", "tool",
               "source_class", "version", "source_snapshot"}
    all_ev = []
    for dd in (d, d3, d4, d5, d6, d7, d8, d9, d10):
        all_ev += events(dd)
    bad = [set(e.keys()) - allowed for e in all_ev]
    check("事件只含允许字段", not any(bad), str([b for b in bad if b]))
    raw = ""
    for dd in (d, d3, d4, d5, d6):
        p = os.path.join(dd, "gate-events.jsonl")
        if os.path.exists(p):
            raw += open(p, encoding="utf-8").read()
    check("不含原始 command 文本", (RM + " " + R + " /") not in raw)
    check("不含完整 prompt", "不要再问我了" not in raw)
    check("不含 Write 正文", "a.py" not in raw)

    # ---------- 9. source identity 归因 ----------
    print("\n----- 9. 归因字段 -----")
    if all_ev:
        e = all_ev[0]
        vp = os.path.join(HOOKS, "VERSION")
        ver = open(vp, encoding="utf-8").read().strip()
        check("version 与 VERSION 一致", e.get("version") == ver,
              "%r vs %r" % (e.get("version"), ver))
        check("含 source_snapshot 字段", "source_snapshot" in e)

    # ---------- 10. fail policy ----------
    print("\n----- 10. 记录失败不改变判定 -----")
    # 状态目录不可写 → 记录失败，但 deny 判定必须保留
    # （注意：用 Bash 门 —— 它不像 G1 那样在环境坏时降级放行）
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "CLAUDE_BUDGET_STATE_DIR": "Z:\\nonexistent\\dir"}
    p = subprocess.run(
        [sys.executable, os.path.join(HOOKS, "destructive_gate.py")],
        input=json.dumps(bash_payload(RM + " " + R + " /", "s11")).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    check("记录失败时 G5 仍 deny（rc=2）", p.returncode == 2, "rc=%d" % p.returncode)
    check("记录失败不抛异常", "Traceback" not in out)

    # ---------- 11. 记录不阻塞判定（#54）----------
    # ⚠️ 实测缺陷（外部复核发现，本机坐实）：
    #    record_gate_event 原用 _Lock 的默认 timeout=3.0s，
    #    而它在 deny() 里于【决策 JSON 发出之前】调用 ——
    #    于是锁被占时，一次本应立即生效的 deny 会等满 3 秒。
    #    实测：人为持锁时 deny 耗时 3.09s（无竞争 86ms）。
    #    修法：0.05s 非阻塞 —— 拿不到就丢事件。
    print("\n----- 11. 记录不阻塞判定（锁被占时仍要快）-----")
    import time
    d11 = tempfile.mkdtemp(prefix="gev11-")
    # 人为占住事件锁
    os.makedirs(os.path.join(d11, "gate-events.jsonl.lock"), exist_ok=True)
    env11 = {**os.environ, "PYTHONIOENCODING": "utf-8",
             "CLAUDE_BUDGET_STATE_DIR": d11}
    t0 = time.time()
    p11 = subprocess.run(
        [sys.executable, os.path.join(HOOKS, "destructive_gate.py")],
        input=json.dumps(bash_payload(RM + " " + R + " /", "s11b")).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env11, timeout=30)
    el = time.time() - t0
    check("锁被占时 deny 不等待（< 1 秒）", el < 1.0,
          "实际 %.2f 秒（修前是 3.09 秒）" % el)
    check("锁被占时判定不变（rc=2）", p11.returncode == 2,
          "rc=%d" % p11.returncode)
    # 拿不到锁 → 该事件被丢弃（这是设计：观测不值得让门等）
    n11 = len(events(d11))
    check("拿不到锁 → 丢事件（不阻塞、不报错）", n11 == 0,
          "事件数 %d（期望 0 —— 锁被占时丢事件是有意的）" % n11)

    # ---------- 汇总 ----------
    npass = sum(1 for _, ok, _ in _results if ok)
    print("\n" + "=" * 88)
    print("Gate 事件记录回归：%d / %d 通过" % (npass, len(_results)))
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
