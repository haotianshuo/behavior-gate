#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重复触发口径回归（REAL_REGRESSION）。

## 它守什么

`gate_stats.py` 的「重复触发」曾把 REVIEW 评审调用也算进来 ——
同一 session 提交 30 次 prompt 就是 30 条 review_allow，
榜单上看起来像"门反复拦截、模型打回后没改"，实际只是用户话多。

默认 repeats 必须只统计门真实出手（deny / stop_feedback /
stop_feedback_retry），并排除 gate_id == "REVIEW"；
未知 decision 类型必须可见，不得静默丢弃，也不得让工具崩溃。

## 边界

  证明    : 构造样本上的过滤、分组与未知值处置行为
  不证明  : 真实数据里的决定对错 —— 计数由人解读
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)
import gate_stats as gs  # noqa: E402

PY = sys.executable
PASSED = []
FAILED = []

_BASE = datetime.datetime(2026, 10, 7, 0, 0, 0,
                          tzinfo=datetime.timezone(
                              datetime.timedelta(hours=8)))


def check(label, ok, detail=""):
    print("  %s %s%s" % ("PASS" if ok else "FAIL", label,
                         ("  " + str(detail)) if detail else ""))
    (PASSED if ok else FAILED).append(label)


def event(session, rule, decision, gate="REVIEW", seq=0):
    """构造一条事件；ts 用真实递增时间，不用非法的 00:60。"""
    ts = (_BASE + datetime.timedelta(seconds=seq)).isoformat()
    return {"ts": ts, "session": session, "rule_id": rule,
            "decision": decision, "gate_id": gate}


def test_repeats_ignore_review():
    rows = []
    for i in range(20):
        rows.append(event("s-review", "semantic_intent", "review_allow", seq=i))
    for i in range(10):
        rows.append(event("s-review", "semantic_intent",
                          "review_insufficient", seq=20 + i))
    for i in range(2):
        rows.append(event("s-block", "missing_effect_evidence", "deny",
                          gate="G3", seq=40 + i))
    for i in range(3):
        rows.append(event("s-block", "missing_effect_evidence", "stop_feedback",
                          gate="G3", seq=50 + i))
    for i in range(2):
        rows.append(event("s-block", "missing_effect_evidence",
                          "stop_feedback_retry", gate="G3", seq=60 + i))
    # 异常组合：REVIEW 门 + block 类 decision —— 仍不得进入默认 repeats
    rows.append(event("s-review2", "weird_rule", "deny", gate="REVIEW", seq=70))
    # 非 REVIEW 门 + review_* decision（如 G3 的 review_found_claim）——
    # 同族真实数据存在，且同样不得进入默认 repeats
    for i in range(3):
        rows.append(event("s-mixed", "g3_review_rule", "review_found_claim",
                          gate="G3", seq=71 + i))

    result = gs.analyze(rows)
    repeats = [tuple(x) for x in result["repeats"]]
    check("REVIEW 高频不进入 repeats，只留真实拦截组",
          repeats == [(7, "s-block", "missing_effect_evidence")],
          "repeats=%s" % repeats)
    check("retry 仍按 stop_feedback_retry 精确统计",
          dict(result["retry"]) == {"missing_effect_evidence": 2},
          "retry=%s" % dict(result["retry"]))


def test_unknown_decisions_are_visible():
    rows = [
        event("s-unknown", "future_rule", "hard_deny", gate="G9", seq=1),
        event("s-unknown", "future_rule", "hard_deny", gate="G9", seq=2),
        event("s-unknown", "future_rule", "hard_deny", gate="G9", seq=3),
        event("s-unknown2", "r-empty", "", gate="G9", seq=4),
        event("s-unknown3", "r-none", None, gate="G9", seq=5),
        event("s-unknown4", "r-zero", 0, gate="G9", seq=6),
        event("s-unknown5", "r-list", ["future"], gate="G9", seq=7),
        event("s-unknown6", "r-dict", {"kind": "future"}, gate="G9", seq=8),
    ]
    result = gs.analyze(rows)
    check("未知类型不得混入 repeats（即使同组 >=3 条）",
          list(result["repeats"]) == [],
          "repeats=%s" % result["repeats"])
    unknown = dict(result.get("unknown_decisions") or {})
    check("未知类型计数可见且标签稳定",
          unknown == {"hard_deny": 3, "<empty>": 2, "0": 1,
                      '["future"]': 1, '{"kind":"future"}': 1},
          "unknown=%s" % unknown)
    try:
        json.dumps(dict(result["by_decision"]), ensure_ascii=False)
        serializable = True
    except TypeError:
        serializable = False
    check("by_decision 对非字符串 decision 仍可 JSON 序列化",
          serializable)


def _run_cli(args):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [PY, os.path.join(TOOLS, "gate_stats.py")] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, env=env)


def _write_fixture(tmpdir):
    """CLI 用的混合事件文件：REVIEW 高频 + 真实拦截 + 未知值。"""
    rows = []
    for i in range(20):
        rows.append(event("s-rv", "semantic_intent", "review_allow", seq=i))
    for i in range(10):
        rows.append(event("s-rv", "semantic_intent",
                          "review_insufficient", seq=20 + i))
    for i in range(2):
        rows.append(event("s-block", "missing_effect_evidence", "deny",
                          gate="G3", seq=40 + i))
    for i in range(3):
        rows.append(event("s-block", "missing_effect_evidence", "stop_feedback",
                          gate="G3", seq=50 + i))
    for i in range(2):
        rows.append(event("s-block", "missing_effect_evidence",
                          "stop_feedback_retry", gate="G3", seq=60 + i))
    rows.append(event("s-rv2", "weird_rule", "deny", gate="REVIEW", seq=70))
    rows.append(event("s-unk", "future_rule", "hard_deny", gate="G9", seq=71))
    rows.append(event("s-unk2", "r-none", None, gate="G9", seq=72))
    path = os.path.join(tmpdir, "evt.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def test_cli_contracts():
    with tempfile.TemporaryDirectory(prefix="gsrepeats-") as tmp:
        path = _write_fixture(tmp)

        p = _run_cli(["--path", path, "--json"])
        d = None
        try:
            d = json.loads(p.stdout.decode("utf-8", "replace"))
        except ValueError:
            pass
        check("默认 --json 输出可 json.loads 且 rc=0",
              d is not None and p.returncode == 0,
              "rc=%s stdout首行=%r" % (p.returncode, p.stdout[:60]))

        if d is None:
            d = {}
        rep = [tuple(x) for x in d.get("repeats", [])]
        check("默认 JSON：repeats 已过滤、含 unknown_decisions、无 legacy 字段",
              rep == [(7, "s-block", "missing_effect_evidence")]
              and d.get("unknown_decisions") == {"hard_deny": 1, "<empty>": 1}
              and "repeats_all_decisions" not in d,
              "repeats=%s unknown=%s legacy键=%s" % (
                  rep, d.get("unknown_decisions"),
                  "repeats_all_decisions" in d))

        p2 = _run_cli(["--path", path, "--json", "--legacy-metrics"])
        d2 = None
        try:
            d2 = json.loads(p2.stdout.decode("utf-8", "replace"))
        except ValueError:
            pass
        legacy = [tuple(x) for x in (d2 or {}).get("repeats_all_decisions", [])]
        check("--json --legacy-metrics 可解析且旧口径包含 REVIEW 组",
              d2 is not None and p2.returncode == 0
              and legacy == [(30, "s-rv", "semantic_intent"),
                             (7, "s-block", "missing_effect_evidence")],
              "rc=%s legacy=%s" % (p2.returncode, legacy))

        p3 = _run_cli(["--path", path, "--legacy-metrics"])
        check("单独 --legacy-metrics 非零退出（参数组合错误）",
              p3.returncode != 0, "rc=%s" % p3.returncode)

        p4 = _run_cli(["--path", path])
        out = p4.stdout.decode("utf-8", "replace")
        check("人类输出含口径说明与未知告警",
              "重复触发口径：仅统计 deny / stop_feedback / stop_feedback_retry；"
              "REVIEW 评审调用不计入。" in out
              and "未知 decision" in out and "hard_deny=1" in out,
              "输出摘要=%r" % out[:160])

        seg = out.split("[A] 重复触发")[1].split("[B]")[0] \
            if "[A] 重复触发" in out else ""
        check("人类输出重复榜不含 REVIEW 组、保留真实拦截组",
              "s-rv" not in seg and "semantic_intent" not in seg
              and "missing_effect_evidence" in seg,
              "A段=%r" % seg[:200])


def main():
    test_repeats_ignore_review()
    test_unknown_decisions_are_visible()
    test_cli_contracts()

    total = len(PASSED) + len(FAILED)
    print()
    print("gate_stats repeats 回归：%d / %d 通过" % (len(PASSED), total))
    if FAILED:
        print("失败项：")
        for x in FAILED:
            print("  - %s" % x)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
