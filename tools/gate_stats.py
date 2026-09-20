#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门事件统计 —— 给 gate-events.jsonl 一个消费者。

## 为什么需要这个

`_lib.record_gate_event()` 一直在写 `gate-events.jsonl`，但**没有任何程序读它**。
后果是本项目最重要的几个问题**无法回答**：

  · 每个门出手多少次？        → 只能手数，实测手数错过两次
  · 哪个门从没出手过？        → 死门（"看起来装了门、其实门是假的"）无法发现
  · 门打回之后，模型改了吗？   → 误报率 / 有效性 全部 NOT_MEASURED
  · 摩擦在上升还是下降？      → V1.5 §19「删除无收益检查」无法执行

V1.5 §19 要求「按证据调并发和预算」「删除无收益检查」——
**没有读取器，这条规则等于没写。**

## 它不做什么

  · 不写任何文件（只读）
  · 不改门的行为
  · 不做归因判断 —— 只呈现计数，结论由人下

## 四个指标

  A. 重复触发  同一 session 内同一规则反复命中 → 打回后没改（摩擦信号）
  B. 重试仍不合规  decision=stop_feedback_retry → 打回后重试仍不合规
  C. 死门探测   已实现但从未出手的规则 → 需要区分「不该出手」与「该出手没出手」
  D. 按天分布   摩擦趋势

## 边界（必须如实声明）

    证明    : 门记录了自己做出的决定（这是记录层的事实）
    不证明  : 门的判定是对的 —— 出手次数多可能是真问题，也可能是误报
    不证明  : 日志不可篡改 / 攻击者不能删改（T2 NOT_PROTECTED）
    时效性  : 读的是累计文件，含历史；要看趋势请按日期分段

用法：
    python tools/gate_stats.py
    python tools/gate_stats.py --json
    python tools/gate_stats.py --since 2026-09-20
"""

import argparse
import collections
import json
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def events_path():
    d = os.environ.get("CLAUDE_BUDGET_STATE_DIR")
    if not d:
        base = (os.environ.get("TEMP") or os.environ.get("TMP")
                or tempfile.gettempdir())
        d = os.path.join(base, "claude-behavior-gates")
    return os.path.join(d, "gate-events.jsonl")


def load(path, since=None):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if since and (r.get("ts") or "")[:10] < since:
                continue
            out.append(r)
    return out


def analyze(recs):
    a = {
        "total": len(recs),
        "by_gate": collections.Counter(r.get("gate_id") for r in recs),
        "by_rule": collections.Counter(r.get("rule_id") for r in recs),
        "by_decision": collections.Counter(r.get("decision") for r in recs),
        "by_day": collections.Counter((r.get("ts") or "")[:10] for r in recs),
    }
    # A. 同一 session 内同一规则重复触发
    grp = collections.defaultdict(list)
    for r in recs:
        grp[(r.get("session"), r.get("rule_id"))].append(r.get("ts") or "")
    a["repeats"] = sorted(
        [(len(v), k[0], k[1]) for k, v in grp.items() if len(v) >= 3],
        reverse=True)
    # B. 重试仍不合规
    a["retry"] = collections.Counter(
        r.get("rule_id") for r in recs
        if r.get("decision") == "stop_feedback_retry")
    return a


# 已知的门编号与其【是否应当出手】—— 用于死门探测
# 这张表必须与 README 的门清单保持一致（改 README 时同步改这里）
KNOWN_GATES = {
    "G1": "预算门（应出手）",
    "G2": "范围门（未实现 —— 不应出手）",
    "G3": "生效门（应出手）",
    "G4": "（未实现 —— 不应出手）",
    "G5": "危险门（应出手）",
    "G6": "收口（仅记录，不是拦截门）",
    "G7": "意图门（应出手）",
}


def main():
    ap = argparse.ArgumentParser(description="门事件统计（gate-events 的只读消费者）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--since", default=None, help="只统计该日期之后（YYYY-MM-DD）")
    ap.add_argument("--path", default=None, help="gate-events.jsonl 路径")
    args = ap.parse_args()

    path = args.path or events_path()
    recs = load(path, args.since)
    a = analyze(recs)

    if args.json:
        print(json.dumps({
            "path": path, "since": args.since, "total": a["total"],
            "by_gate": dict(a["by_gate"]), "by_rule": dict(a["by_rule"]),
            "by_decision": dict(a["by_decision"]), "by_day": dict(a["by_day"]),
            "repeats": a["repeats"][:20], "retry": dict(a["retry"]),
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("门事件统计（gate-events.jsonl 的只读消费者）")
    print("=" * 78)
    print("  文件: %s" % path)
    if args.since:
        print("  范围: >= %s" % args.since)
    print("  记录: %d 条" % a["total"])
    if not a["total"]:
        print()
        print("  ⚠️ 无记录。可能原因：门从未出手 / 状态目录不对 / 文件被删。")
        print("     → 先跑 tools/verify_gates_live.py 确认门能出手。")
        return 0

    print()
    print("─" * 78)
    print("[按门]")
    print("─" * 78)
    for k, v in a["by_gate"].most_common():
        print("  %-6s %5d" % (k, v))

    print()
    print("─" * 78)
    print("[按规则]  （rule_id = 哪条判据拦的）")
    print("─" * 78)
    for k, v in a["by_rule"].most_common(15):
        print("  %-32s %5d" % (str(k), v))

    print()
    print("─" * 78)
    print("[A] 重复触发 —— 同一 session 内同一规则 >=3 次（打回后没改）")
    print("─" * 78)
    if not a["repeats"]:
        print("  无")
    for n, sess, rid in a["repeats"][:10]:
        print("  %3d 次  %-30s session=%s" % (n, str(rid), str(sess)[:18]))

    print()
    print("─" * 78)
    print("[B] 重试仍不合规（decision=stop_feedback_retry）")
    print("─" * 78)
    if not a["retry"]:
        print("  无")
    for k, v in a["retry"].most_common():
        print("  %-32s %5d" % (str(k), v))

    print()
    print("─" * 78)
    print("[C] 死门探测 —— 已实现但从未出手")
    print("─" * 78)
    seen = set(a["by_gate"])
    never = [g for g in KNOWN_GATES if g not in seen]
    if not never:
        print("  无（所有已登记的门都出手过）")
    for g in never:
        print("  %-6s %s" % (g, KNOWN_GATES[g]))
    print()
    print("  ⚠️ 「从未出手」不等于「坏了」—— 要区分：")
    print("     · 未实现的门     → 不该出手，正常")
    print("     · 仅记录的门     → 不拦，正常")
    print("     · 应出手却为零   → ★ 疑似死门，需查（matcher 写错？事件名错？）")

    print()
    print("─" * 78)
    print("[D] 按天分布")
    print("─" * 78)
    mx = max(a["by_day"].values()) if a["by_day"] else 1
    for k, v in sorted(a["by_day"].items()):
        bar = "█" * max(1, int(v * 50 / mx))
        print("  %s  %5d  %s" % (k, v, bar))

    print()
    print("=" * 78)
    print("  边界: 只呈现计数，不做归因。")
    print("        出手多可能是真问题，也可能是误报 —— 本工具不区分。")
    print("        记录层不防篡改（T2 NOT_PROTECTED），不能当作不可抵赖的证据。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
