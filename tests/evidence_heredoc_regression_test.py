# -*- coding: utf-8 -*-
"""证据关联与 heredoc 接收者回归（离线构造用例）。

只把合成 transcript 和宿主 payload 交给实际 hook 子进程，核对退出码、
输出协议和事件分类。样例中的 Shell 命令从不交给 Shell 执行。
每条用例使用独立临时状态，语义评审关闭；不安装 hook，不访问模型通道。

本套件证明程序在这些输入上的行为，不证明真实宿主送达或业务正确性。
运行：python tests/evidence_heredoc_regression_test.py
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOKS = ROOT / "adapters" / "claude-code" / "hooks"
_RESULTS = []

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def tool_use(tid, name, tool_input):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": tool_input}
    ]}}


def tool_result(tid, text, is_error=False):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text,
         "is_error": is_error}
    ]}}


def observed(tid="check", text="HTTP/1.1 200\nexit code 0", is_error=False):
    # command 是 transcript 数据；不会执行这个字符串。
    return [tool_use(tid, "Bash", {"command": "test-only-observation"}),
            tool_result(tid, text, is_error)]


def evidence_block(body):
    return "**证据**：L2 动态\n**风险级别**：中\n\n" + body


def event_is(outcome, rule=None, decision=None, **fields):
    for ev in outcome.get("events", []):
        if rule is not None and ev.get("rule_id") != rule:
            continue
        if decision is not None and ev.get("decision") != decision:
            continue
        if all(ev.get(k) == v for k, v in fields.items()):
            return True
    return False


def no_verified_success(outcome):
    return not any(ev.get("rule_id") in {
        "verified_by_observed_results", "inline_evidence_corroborated"
    } for ev in outcome.get("events", []))


def no_reproduction_claim(outcome):
    return no_verified_success(outcome) and not any(ev.get("rule_id") in {
        "verified_by_expected_failure", "inline_evidence_repro"
    } for ev in outcome.get("events", []))


def run_gate(tmp, key, script, payload, transcript=()):
    """只启动 hook 的 Python 进程；所有状态与 transcript 均为临时文件。"""
    case_dir = pathlib.Path(tmp) / key
    state_dir = case_dir / "state"
    state_dir.mkdir(parents=True)
    policy = json.loads((ROOT / "policy" / "behavior-policy.json").read_text(
        encoding="utf-8"))
    policy.setdefault("semantic_review", {})["enabled"] = False
    policy_path = case_dir / "policy.semantic-off.json"
    policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    transcript_path = case_dir / "transcript.jsonl"
    transcript_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                       for row in transcript), encoding="utf-8")
    data = dict(payload)
    data["session_id"] = "evidence-heredoc-" + key
    data["transcript_path"] = str(transcript_path)

    # 双重隔离：关闭语义策略，并不把模型认证/通道传给测试子进程。
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ANTHROPIC_")
           and k not in {"CLAUDE_CODE_OAUTH_TOKEN", "BEHAVIOR_GATE_ONLINE"}}
    env.update({
        "CLAUDE_BEHAVIOR_POLICY": str(policy_path),
        "CLAUDE_BUDGET_STATE_DIR": str(state_dir),
        "BEHAVIOR_GATE_SOURCE_CLASS": "CONTROLLED",
        "BEHAVIOR_GATE_ONLINE": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    try:
        proc = subprocess.run(
            [sys.executable, str(HOOKS / script)],
            input=json.dumps(data, ensure_ascii=False), capture_output=True,
            text=True, encoding="utf-8", env=env, timeout=45,
        )
    except subprocess.TimeoutExpired:
        return {"rc": "TIMEOUT", "healthy": False, "events": [],
                "detail": "hook 子进程超过 45 秒"}

    output_ok = True
    try:
        response = json.loads(proc.stdout) if proc.stdout.strip() else {}
        if not isinstance(response, dict):
            output_ok = False
        elif "hookSpecificOutput" in response:
            hso = response["hookSpecificOutput"]
            output_ok = (isinstance(hso, dict)
                         and hso.get("hookEventName") == data["hook_event_name"])
    except (ValueError, TypeError):
        output_ok = False

    events = []
    event_path = state_dir / "gate-events.jsonl"
    try:
        if event_path.exists():
            events = [json.loads(line) for line in event_path.read_text(
                encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        output_ok = False
    inactive = any(ev.get("decision") == "inactive" for ev in events)
    healthy = output_ok and not inactive and "Traceback" not in proc.stderr
    details = {"rc": proc.returncode,
               "events": [(ev.get("rule_id"), ev.get("decision")) for ev in events],
               "stdout": proc.stdout[:400], "stderr": proc.stderr[:400]}
    return {"rc": proc.returncode, "healthy": healthy, "events": events,
            "detail": json.dumps(details, ensure_ascii=False)}


def stop_case(tmp, key, body, transcript=()):
    return run_gate(tmp, key, "effect_gate.py", {
        "hook_event_name": "Stop", "stop_hook_active": False,
        "last_assistant_message": body,
    }, transcript)


def check(name, outcome, expected):
    ok = bool(outcome.get("healthy") and expected(outcome))
    _RESULTS.append((name, ok))
    print("  %s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("       %s" % outcome.get("detail", "缺少运行结果"))


def main():
    print("证据关联与 heredoc 接收者回归（离线，Shell 样例仅作数据）")
    with tempfile.TemporaryDirectory(prefix="evidence-heredoc-") as tmp:
        success = observed()
        failure = observed("repro", "exit code 1", True)

        # A：原始上下文顺序必须保留，不能删掉开头的否定短语让用例变简单。
        negated_prefix = ("登录已修复，HTTP 200。\n"
                          "没有异常，上传已修复，HTTP 201。\n"
                          "证据：L2\n风险级别：中")
        out = stop_case(tmp, "negation-comma", negated_prefix, success)
        check("A1 否定短语后的第二项成功声明仍需证据", out,
              lambda o: o["rc"] == 0 and no_verified_success(o) and event_is(
                  o, "report_partially_supported", "allow_unconfirmed",
                  units_total=2, units_matched=1))

        out = stop_case(tmp, "negation-only", "不能说登录已修复。")
        check("A2 纯否定表达不被反向当作完成声明", out,
              lambda o: o["rc"] == 0 and no_verified_success(o)
              and not event_is(o, decision="stop_feedback"))

        # B：同一事项的直接观测续行可关联；相邻独立事项不自动借用证据。
        multiline = "服务已修复。\n检查结果：HTTP 200，exit code 0。"
        out = stop_case(tmp, "multiline-block", evidence_block(multiline), success)
        check("B1 证据块路径接受同一事项的分行观测", out,
              lambda o: o["rc"] == 0 and event_is(
                  o, "verified_by_observed_results", "allow_with_observed_results"))

        out = stop_case(tmp, "multiline-inline", multiline, success)
        check("B2 行内路径接受同一事项的分行观测", out,
              lambda o: o["rc"] == 0 and event_is(
                  o, "inline_evidence_corroborated", "allow_by_inline_evidence"))

        independent = "1. 登录接口已修复。\n2. 上传接口检查结果：HTTP 200。"
        for suffix, message in (("block", evidence_block(independent)),
                                ("inline", independent)):
            out = stop_case(tmp, "independent-" + suffix, message, success)
            check("B%s 独立列表事项不借用相邻项观测（%s）"
                  % ("3" if suffix == "block" else "4", suffix), out,
                  lambda o: o["rc"] == 0 and no_verified_success(o)
                  and event_is(o, decision="allow_unconfirmed"))

        # 保留最初审查报告的实际行序：证据级别位于结论与观测之间。
        original_multiline = ("登录已修复。\n证据：L2\n"
                              "检查结果：HTTP 200，exit code 0。\n风险级别：中")
        out = stop_case(tmp, "original-multiline", original_multiline, success)
        check("B5 原顺序的分行证据能关联到唯一结论", out,
              lambda o: o["rc"] == 0 and event_is(
                  o, "verified_by_observed_results", "allow_with_observed_results"))

        shared_observation = ("登录已修复。\n上传已修复。\n"
                              "检查结果：HTTP 200。")
        out = stop_case(tmp, "ambiguous-shared-observation",
                        evidence_block(shared_observation), success)
        check("B6 多个连续声明后的公共观测不自动归给任一项", out,
              lambda o: o["rc"] == 0 and no_verified_success(o) and event_is(
                  o, decision="allow_unconfirmed", units_total=2, units_matched=0))

        # C：超过结果数量上限，但尾部仍有匹配结果，不能被成功分支抢先放行。
        many_results = []
        for n in range(41):
            many_results.extend(observed("check-%d" % n))
        for suffix, message in (("block", evidence_block("服务已修复，HTTP 200。")),
                                ("inline", "服务已修复，HTTP 200。")):
            out = stop_case(tmp, "truncated-" + suffix, message, many_results)
            check("C%s 截断且存在匹配结果仍记未确认（%s）"
                  % ("1" if suffix == "block" else "2", suffix), out,
                  lambda o: o["rc"] == 0 and no_reproduction_claim(o)
                  and event_is(o, decision="allow_unconfirmed", truncated=True))

        # D：复现证据只证明错误已复现，不计为成功结果验证；混合报告分别判断。
        reproduction = "错误已复现，exit code 1。"
        out = stop_case(tmp, "repro-block", evidence_block(reproduction), failure)
        check("D1 纯复现证据块使用预期失败专用分类", out,
              lambda o: o["rc"] == 0 and no_verified_success(o) and event_is(
                  o, "verified_by_expected_failure", "allow_expected_failure"))

        out = stop_case(tmp, "repro-inline", reproduction, failure)
        check("D2 纯复现普通表达使用复现专用分类", out,
              lambda o: o["rc"] == 0 and no_verified_success(o) and event_is(
                  o, "inline_evidence_repro", "allow_by_inline_evidence"))

        mixed = reproduction + "\n服务已修复，HTTP 200。"
        out = stop_case(tmp, "partial-block", evidence_block(mixed), failure)
        check("D3 失败证据不能替混合报告中的修复声明背书", out,
              lambda o: o["rc"] == 0 and no_verified_success(o) and event_is(
                  o, "report_partially_supported", "allow_unconfirmed",
                  units_total=2, units_matched=1))

        out = stop_case(tmp, "partial-inline", mixed, failure)
        check("D4 行内混合报告同样保留部分未确认", out,
              lambda o: o["rc"] == 0 and no_verified_success(o) and event_is(
                  o, "inline_evidence_unconfirmed", "allow_unconfirmed",
                  backing="partial", units_total=2, units_matched=1))

        out = stop_case(tmp, "repro-negated", "不能说错误已复现，exit code 1。", failure)
        check("D5 否定复现表达不激活复现成功分类", out,
              lambda o: o["rc"] == 0 and no_reproduction_claim(o)
              and not event_is(o, decision="stop_feedback"))

        out = stop_case(tmp, "repro-quoted", "用户原话：“错误已复现，exit code 1。”", failure)
        check("D6 引用中的复现表达不变成本轮声明", out,
              lambda o: o["rc"] == 0 and no_reproduction_claim(o)
              and not event_is(o, decision="stop_feedback"))

        # E：保持已有三态修补。失败/未返回的写入不能被当成“肯定没改过”。
        write_request = tool_use("edit", "Write", {
            "file_path": "app.py", "content": "# test-only transcript data\n"})
        current = evidence_block("app.py 已修复，HTTP 200。")
        confirmed = success + [write_request, tool_result("edit", "write completed")]
        out = stop_case(tmp, "write-confirmed", current, confirmed)
        check("E1 相关对象确认写入后，旧验证记过期", out,
              lambda o: o["rc"] == 0 and no_verified_success(o) and event_is(
                  o, "results_superseded_by_later_edit", "allow_unconfirmed"))

        possible = success + [write_request, tool_result("edit", "exit code 1", True)]
        out = stop_case(tmp, "write-possible", current, possible)
        check("E2 写入请求失败后，旧验证保留未确认", out,
              lambda o: o["rc"] == 0 and no_verified_success(o) and event_is(
                  o, "results_write_impact_unconfirmed", "allow_unconfirmed"))

        out = stop_case(tmp, "write-pending", current, success + [write_request])
        check("E3 写入请求缺少结果时，旧验证保留未确认", out,
              lambda o: o["rc"] == 0 and no_verified_success(o) and event_is(
                  o, "results_write_impact_unconfirmed", "allow_unconfirmed"))

        fresh = possible + observed("recheck")
        out = stop_case(tmp, "fresh-after-write", current, fresh)
        check("E4 写入之后的新验证仍可支持当前声明", out,
              lambda o: o["rc"] == 0 and event_is(
                  o, "verified_by_observed_results", "allow_with_observed_results"))

        # F：反向对照，防止把“所有观测均不确认”当成修复。
        for suffix, message in (("block", evidence_block("服务已修复，HTTP 200。")),
                                ("inline", "服务已修复，HTTP 200。")):
            out = stop_case(tmp, "ordinary-" + suffix, message, success)
            rule = ("verified_by_observed_results" if suffix == "block"
                    else "inline_evidence_corroborated")
            check("F%s 完整未截断的匹配观测仍可正常对照（%s）"
                  % ("1" if suffix == "block" else "2", suffix), out,
                  lambda o, wanted=rule: o["rc"] == 0 and event_is(o, wanted))

        # G：Shell 字符串仅作为 gate 的 tool_input；下方绝不使用 shell=True。
        danger = "pkill -f TEST_ONLY_PLACEHOLDER"
        prose = "文档说明：不要用 pkill 按名称结束进程。"
        heredocs = [
            ("pipe-shell", "原管道的正文由 sh 接收", 2,
             "cat | sh <<'EOF'\n" + danger + "\nEOF\n"),
            ("quoted-semicolon", "引号参数里的分号不改变 sh 接收者", 2,
             "sh -s 'label; cat' <<'EOF'\n" + danger + "\nEOF\n"),
            ("plain-document", "正常 cat 文档正文不按命令执行", 0,
             "cat > notes.md <<'EOF'\n" + prose + "\nEOF\n"),
            ("quoted-filename", "带引号分号文件名仍是正常文档重定向", 0,
             "cat > 'notes; draft.md' <<'EOF'\n" + prose + "\nEOF\n"),
            ("escaped-filename", "转义分号文件名仍是正常文档重定向", 0,
             "cat > notes\\;draft.md <<'EOF'\n" + prose + "\nEOF\n"),
            ("expanded-body", "未引用正文中的命令替换不得文档豁免", 2,
             "cat > notes.md <<EOF\n$(" + danger + ")\nEOF\n"),
        ]
        for n, (key, label, expected_rc, command) in enumerate(heredocs, 1):
            out = run_gate(tmp, key, "destructive_gate.py", {
                "hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_use_id": "heredoc-" + key, "tool_input": {"command": command},
            })
            check("G%d %s" % (n, label), out,
                  lambda o, wanted=expected_rc: o["rc"] == wanted
                  and (event_is(o, decision="deny") if wanted == 2
                       else not event_is(o, decision="deny")))

    passed = sum(1 for _, ok in _RESULTS if ok)
    print("证据与 heredoc 回归：%d / %d 通过" % (passed, len(_RESULTS)))
    return 0 if passed == len(_RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
