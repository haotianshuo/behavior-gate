# -*- coding: utf-8 -*-
"""G3 / G5 稳定化修复的双向回归（2026-10-27）。

覆盖本轮五个根因的「应识别」与「不应误伤」两侧，并覆盖实际 hook 入口、
最终 JSON 与事件记录。

运行：python tests/g3_g5_stabilization_test.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
HOOKS = os.path.join(PKG, "adapters", "claude-code", "hooks")
sys.path.insert(0, HOOKS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import effect_gate as EG          # noqa: E402
import result_scan as RS          # noqa: E402
import destructive_gate as DG     # noqa: E402

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("  %-6s %s" % ("PASS" if ok else "**FAIL**", name))
    if not ok and detail:
        print("         %s" % detail)


def mk(sig, err=False, aw=False, state=None):
    r = {"tool_use_id": "", "tool": "Bash", "is_error": err,
         "signals": list(sig), "chars": 10, "digest": "x",
         "after_write": aw, "writes_after": []}
    if aw:
        r["writes_after"] = [{"targets": ["app.py"],
                              "state": state or "confirmed"}]
    return r


def scan(rows, truncated=False):
    return {"ok": True, "truncated": truncated, "errors":
            sum(1 for r in rows if r.get("is_error")), "results": rows}


def backing(text, rows, truncated=False):
    return EG._backing_state(text, scan(rows, truncated), RS.obs_tokens)


def policy_semantic_off():
    src = os.path.join(PKG, "policy", "behavior-policy.json")
    with open(src, encoding="utf-8") as f:
        pol = json.load(f)
    pol.setdefault("semantic_review", {})["enabled"] = False
    p = os.path.join(tempfile.mkdtemp(prefix="stab-policy-"), "policy.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(pol, f, ensure_ascii=False, indent=2)
    return p


_POLICY = policy_semantic_off()


def run_hook(script, payload, state_dir):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "CLAUDE_BEHAVIOR_POLICY": _POLICY,
           "CLAUDE_BUDGET_STATE_DIR": state_dir}
    p = subprocess.run([sys.executable, os.path.join(HOOKS, script)],
                       input=json.dumps(payload).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=env, timeout=60)
    return (p.returncode, p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def events(state_dir):
    p = os.path.join(state_dir, "gate-events.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8").read().splitlines()
            if l.strip()]


def transcript(rows):
    """rows: [(type, payload)] → transcript 文件路径。"""
    d = tempfile.mkdtemp(prefix="stab-tx-")
    p = os.path.join(d, "t.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for blocks in rows:
            f.write(json.dumps({"message": {"content": blocks}},
                               ensure_ascii=False) + "\n")
    return p


def main():
    print("=" * 78)
    print("G3 / G5 稳定化双回归")
    print("=" * 78)

    # ---------------- A. 否定作用域 ----------------
    print("\n----- A. 否定作用域 -----")
    u = EG._claim_units("没有异常，上传已修复。")
    check("A1 「没有异常，上传已修复」保留上传声明",
          len(u) == 1 and u[0]["kind"] == "success", str(u))
    _has_claim = lambda us: any(x["kind"] != "evidence" for x in us)
    u = EG._claim_units("尚未修复。")
    check("A2 「尚未修复」不算当前声明", not _has_claim(u), str(u))
    u = EG._claim_units("如果修复完成，那就可以验收。")
    check("A3 条件句里的完成词不算当前声明", not _has_claim(u), str(u))
    u = EG._claim_units("和已验证的结论冲突。")
    check("A4 定语引用不算当前声明", not _has_claim(u), str(u))
    u = EG._claim_units("上传已修复，下载尚未修复。")
    check("A5 同句多个完成词逐个判断",
          len(u) == 1 and len(u[0]["claims"]) == 1, str(u))

    # ---------------- B. 跨句关联 ----------------
    print("\n----- B. 同事项跨句关联 -----")
    rows = [mk(["exit code 0"])]
    b = backing("上传已完成。\n它的退出码是 0。", rows)
    check("B1 明确回指 → 同事项合并，得到支持",
          b["kind"] == "matched", str(b["kind"]))
    b = backing("上传已完成。\n退出码是 0。", rows)
    check("B2 无回指 → 不自动关联，保持未确认",
          b["kind"] not in ("matched",), str(b["kind"]))
    b = backing("登录已完成，exit code 0。支付已完成。", rows)
    check("B3 登录有证据、支付无证据 → partial，支付不被忽略",
          b["kind"] == "partial" and b["units_total"] == 2
          and b["units_matched"] == 1, str(b))
    b = backing("上传已完成。\n其结果返回 200。", [mk(["HTTP/1.1 200"])])
    check("B4 回指句带观测 → 合并后可支持",
          b["kind"] == "matched", str(b["kind"]))

    # ---------------- C. 复现 / 成功分类 ----------------
    print("\n----- C. 复现与成功分类 -----")
    err1 = [mk(["exit code 1"], err=True)]
    b = backing("原故障已复现，exit code 1。", err1)
    check("C1 纯复现有依据 → repro", b["kind"] == "repro", str(b["kind"]))
    b = backing("原故障已复现，exit code 1。修复已完成，HTTP 200。", err1)
    check("C2 复现有依据、修复无依据 → partial",
          b["kind"] == "partial" and b["units_repro"] == 1
          and b["units_success"] == 0, str(b))
    both = err1 + [mk(["HTTP/1.1 200"])]
    b = backing("原故障已复现，exit code 1。修复已完成，HTTP 200。", both)
    check("C3 两类事项分别有依据 → matched 且类型分开记",
          b["kind"] == "matched" and b["units_success"] == 1
          and b["units_repro"] == 1, str(b))
    b = backing("已复现并已修复。", both)
    check("C4 同句复现+成功无法拆分 → 未确认",
          b["kind"] not in ("matched", "repro"), str(b["kind"]))
    b = backing("原故障已复现，HTTP 500。", [mk(["HTTP/1.1 200"])])
    check("C5 复现声明找不到对应失败 → 未确认",
          b["kind"] == "repro_unconfirmed", str(b["kind"]))

    # ---------------- D. 截断优先级 ----------------
    print("\n----- D. 截断优先级 -----")
    rows = [mk(["exit code 0"])]
    b = backing("上传已完成，exit code 0。", rows, truncated=True)
    check("D1 截断 + 匹配 → truncated（不记已验证）",
          b["kind"] == "truncated", str(b["kind"]))
    b = backing("上传已完成，exit code 0。", [], truncated=True)
    check("D2 截断 + 无结果 → truncated", b["kind"] == "truncated",
          str(b["kind"]))
    b = backing("上传已完成，exit code 0。", rows, truncated=False)
    check("D3 未截断 + 匹配 → matched", b["kind"] == "matched",
          str(b["kind"]))

    # ---------------- E. 写入三态 ----------------
    print("\n----- E. 写入三态 -----")
    # 先有结果，之后才是失败的写请求 —— 该结果必须被标记为「后面可能有写」
    tx = transcript([
        [{"type": "tool_result", "tool_use_id": "r1",
          "content": "exit code 0", "is_error": False}],
        [{"type": "tool_use", "id": "w1", "name": "Write",
          "input": {"file_path": "a.py", "content": "x"}}],
        [{"type": "tool_result", "tool_use_id": "w1",
          "content": "boom", "is_error": True}],
    ])
    s = RS.scan_session(tx)
    check("E1 失败写请求保留为 possible",
          any(w.get("state") == "possible"
              for r in s["results"] for w in (r.get("writes_after") or [])),
          str(s["results"]))
    b = backing("上传已完成，exit code 0。", s["results"])
    check("E2 可能写入 → 不记已验证",
          b["kind"] in ("write_unconfirmed", "truncated", "unmatched"),
          str(b["kind"]))
    tx2 = transcript([
        [{"type": "tool_use", "id": "w2", "name": "Write",
          "input": {"file_path": "a.py", "content": "x"}}],
        [{"type": "tool_result", "tool_use_id": "w2",
          "content": "ok", "is_error": False}],
        [{"type": "tool_result", "tool_use_id": "r2",
          "content": "exit code 0", "is_error": False}],
        [{"type": "tool_use", "id": "w3", "name": "Edit",
          "input": {"file_path": "a.py", "old_string": "x", "new_string": "y"}}],
        [{"type": "tool_result", "tool_use_id": "w3",
          "content": "ok", "is_error": False}],
        [{"type": "tool_result", "tool_use_id": "r3",
          "content": "exit code 0", "is_error": False}],
    ])
    s2 = RS.scan_session(tx2)
    check("E3 相关成功写入之后，旧结果标记为受影响",
          any(r.get("after_write_confirmed") for r in s2["results"]),
          str(s2["results"]))
    b = backing("上传已完成，exit code 0。", s2["results"])
    check("E4 重新取得的新结果可替代旧结果",
          b["kind"] == "matched", str(b["kind"]))

    # ---------------- F. heredoc 文档豁免 ----------------
    print("\n----- F. heredoc 文档豁免 -----")
    doc = ("cat > notes.md <<'EOF'\n"
           "这里提到 rm -rf / 但只是文案\nEOF\n")
    check("F1 纯文档写入保持豁免",
          bool(DG._heredoc_doc_ranges(doc)), str(DG._heredoc_doc_ranges(doc)))
    doc2 = ("mkdir -p x && cat > notes.md <<'EOF'\n"
            "这里提到 rm -rf / 但只是文案\nEOF\n")
    check("F2 mkdir && cat 正常处理",
          bool(DG._heredoc_doc_ranges(doc2)), str(DG._heredoc_doc_ranges(doc2)))
    bad1 = "cat <<'EOF' | sh\nrm -rf /\nEOF\n"
    check("F3 cat <<EOF | sh 不豁免", not DG._heredoc_doc_ranges(bad1))
    bad2 = "sh <<'EOF' | cat\nrm -rf /\nEOF\n"
    check("F4 sh <<EOF | cat 不豁免", not DG._heredoc_doc_ranges(bad2))
    bad3 = "sh -s 'label; cat' <<'EOF'\nrm -rf /\nEOF\n"
    check("F5 引号内分号不误切，sh -s 不豁免",
          not DG._heredoc_doc_ranges(bad3))
    bad4 = "cat | sh <<'EOF'\nrm -rf /\nEOF\n"
    check("F6 cat | sh 不豁免", not DG._heredoc_doc_ranges(bad4))

    # ---------------- G. hook 入口 ----------------
    print("\n----- G. hook 入口与事件 -----")
    d = tempfile.mkdtemp(prefix="stab-run-")
    tx_empty = transcript([])
    rc, out, err = run_hook("effect_gate.py", {
        "session_id": "g1", "hook_event_name": "Stop",
        "stop_hook_active": False, "transcript_path": tx_empty,
        "last_assistant_message": "上传已完成。",
    }, d)
    check("G1 完整无结果 + 完成声明 → 打回", rc == 2, "rc=%s" % rc)
    ev = [e for e in events(d) if e.get("session") == "g1"]
    check("G1b 事件为 stop_feedback",
          any(e.get("decision") == "stop_feedback" for e in ev), str(ev))

    d2 = tempfile.mkdtemp(prefix="stab-run-")
    tx_ok = transcript([
        [{"type": "tool_use", "id": "u1", "name": "Bash",
          "input": {"command": "true"}}],
        [{"type": "tool_result", "tool_use_id": "u1",
          "content": "exit code 0", "is_error": False}],
    ])
    rc, out, err = run_hook("effect_gate.py", {
        "session_id": "g2", "hook_event_name": "Stop",
        "stop_hook_active": False, "transcript_path": tx_ok,
        "last_assistant_message": "上传已完成，exit code 0。",
    }, d2)
    ev2 = [e for e in events(d2) if e.get("session") == "g2"]
    check("G2 无证据块 + 行内观测匹配 → inline_evidence_corroborated",
          rc == 0 and any(e.get("rule_id") == "inline_evidence_corroborated"
                          for e in ev2), "rc=%s ev=%s" % (rc, ev2))
    check("G2b 普通放行不输出 permissionDecision",
          "permissionDecision" not in out, out[:200])

    d2b = tempfile.mkdtemp(prefix="stab-run-")
    rc, out, err = run_hook("effect_gate.py", {
        "session_id": "g2b", "hook_event_name": "Stop",
        "stop_hook_active": False, "transcript_path": tx_ok,
        "last_assistant_message":
            "上传已完成，exit code 0。\n\n**证据**：L2 动态\n"
            "**风险级别**：中\n- 在哪看：x\n- 未验证项：无",
    }, d2b)
    ev2b = [e for e in events(d2b) if e.get("session") == "g2b"]
    check("G2c 证据块 + 真实结果匹配 → verified_by_observed_results",
          rc == 0 and any(e.get("rule_id") == "verified_by_observed_results"
                          for e in ev2b), "rc=%s ev=%s" % (rc, ev2b))

    d3 = tempfile.mkdtemp(prefix="stab-run-")
    rc, out, err = run_hook("effect_gate.py", {
        "session_id": "g3", "hook_event_name": "Stop",
        "stop_hook_active": True, "transcript_path": tx_empty,
        "last_assistant_message": "上传已完成。",
    }, d3)
    ev3 = [e for e in events(d3) if e.get("session") == "g3"]
    check("G3 重试轮次不再阻断", rc == 0, "rc=%s" % rc)
    check("G3b 重试轮次记 stop_feedback_retry",
          any(e.get("decision") == "stop_feedback_retry" for e in ev3),
          str(ev3))
    check("G3c 重试轮次不追加模型上下文",
          "additionalContext" not in out, out[:200])

    d4 = tempfile.mkdtemp(prefix="stab-run-")
    rc, out, err = run_hook("destructive_gate.py", {
        "session_id": "g4", "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cat <<'EOF' | sh\nrm -rf /\nEOF\n"},
    }, d4)
    check("G4 管道执行式 heredoc 被拦", rc == 2, "rc=%s" % rc)
    check("G4b 阻断 JSON 含 permissionDecision=deny",
          '"permissionDecision": "deny"' in out, out[:200])

    d5 = tempfile.mkdtemp(prefix="stab-run-")
    rc, out, err = run_hook("destructive_gate.py", {
        "session_id": "g5", "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command":
                       "cat > notes.md <<'EOF'\n"
                       "这里提到 rm -rf / 但只是文案\nEOF\n"},
    }, d5)
    check("G5 纯文档 heredoc 放行", rc == 0, "rc=%s" % rc)

    # ---------------- 汇总 ----------------
    npass = sum(1 for _, ok, _ in _results if ok)
    print("\n" + "=" * 78)
    print("G3/G5 稳定化双回归：%d / %d 通过" % (npass, len(_results)))
    print("=" * 78)
    if npass != len(_results):
        print("\n失败项：")
        for n, ok, det in _results:
            if not ok:
                print("  - %s：%s" % (n, det))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
