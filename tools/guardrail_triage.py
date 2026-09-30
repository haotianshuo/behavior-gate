#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行为门抽样复核（guardrail triage）—— 让「误报率」从传闻变成可测量的样本估计。

为什么做它（2026-09-29，对照外部同类方案的评估惯例）：
    门禁系统（guardrail）的成熟做法里，误报/漏报不靠感觉，靠【抽样复核】：
    从被拦事件里抽一批，人工判「真拦 / 误报」，再据此算比例。
    行为门此前只有 gate_stats.py（纯计数，明说「不做归因」），
    于是 policy 里那句「G3 实测误报率约 1/3」一直是个传闻，无法更新、无法分解。

它做什么（两阶段，人始终在环上）：
    1) sample —— 从 gate-events.jsonl 的【出手事件】随机抽样 → 生成 triage.csv
       （含身份列与空 verdict 列，人工填 true_block / false_positive / unclear）
    2) score  —— 读回已标注的 csv → 输出按门/按规则分解的样本估计
       （含 Wilson 95% 区间——因为这是【样本估计】，不是全量真值）

它不做什么（边界，如实声明）：
    · 不自动判定任何一条是真是假 —— 判定由人做，工具只做抽样与统计。
    · 不读命令行内容/正文 —— 只用 gate-events 里已有的脱敏字段
      （本工具输出中不含用户原话、命令、文件内容）。
    · 样本估计不能替代全量审计；未标注的条目不参与计算。

用法：
    python tools/guardrail_triage.py sample --n 30 [--seed 7]
    python tools/guardrail_triage.py score [triage.csv]
"""

import argparse
import csv
import json
import math
import os
import pathlib
import random
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 出手事件 = 门真的做了拦截/打回（放行不在内）
HIT_DECISIONS = {"deny", "stop_feedback", "stop_feedback_retry"}


def _events_path():
    env = (os.environ.get("CLAUDE_BUDGET_STATE_DIR") or "").strip()
    if env:
        p = os.path.join(env, "gate-events.jsonl")
        if os.path.isfile(p):
            return p
    return os.path.join(tempfile.gettempdir(), "claude-behavior-gates",
                        "gate-events.jsonl")


def _load_hits(path):
    hits = []
    if not os.path.isfile(path):
        return hits
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if (e.get("gate_id") or "") == "REVIEW":
                continue          # 评审事件不是"出手"，另行统计
            if e.get("decision") in HIT_DECISIONS:
                hits.append(e)
    return hits


def _wilson(k, n, z=1.96):
    """Wilson 95% 区间（k 成功 / n 总数）。样本小的时候比 naive 比例稳。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def cmd_sample(args):
    path = _events_path()
    hits = _load_hits(path)
    if not hits:
        print("[triage] 没有可抽样的出手事件（%s）" % path)
        return 1
    rnd = random.Random(args.seed)
    n = min(args.n, len(hits))
    picked = rnd.sample(hits, n)
    out = pathlib.Path(args.out or "triage.csv").resolve()
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        # verdict 列留空待填：true_block / false_positive / unclear
        # ⚠️ 2026-09-30（评估口径更正后补齐追溯信息）：
        #    · session 用【完整值】（原 [:12]+"…" 无法回溯到具体会话）
        #    · 新增 source_snapshot 列（快照标签；身份可信度继承自部署校验，
        #      未经 verify_deploy 核验时视为「身份未核实」）
        #    · source_class 保留原值但加注：默认即 NATURAL（_lib.py:207），
        #      **不能证明业务用途** —— 用途分类请填 purpose 列并写明依据
        #    · 新增 evidence 列（默认给 ts，供标注时指回原始记录）
        w.writerow(["#", "ts", "gate_id", "rule_id", "tool",
                    "session", "source_snapshot", "source_class_note",
                    "purpose", "evidence", "verdict", "note"])
        for i, e in enumerate(picked, 1):
            w.writerow([i, e.get("ts", ""), e.get("gate_id", ""),
                        e.get("rule_id", ""), e.get("tool", ""),
                        e.get("session") or "",
                        e.get("source_snapshot") or "",
                        (e.get("source_class", "") + "（默认值≠业务用途）"),
                        "", e.get("ts", ""),
                        "", ""])
    print("[triage] 共 %d 条出手事件，随机抽取 %d 条（seed=%d）" %
          (len(hits), n, args.seed))
    print("[triage] 已写出：%s" % out)
    print("[triage] 请为每行填 verdict 列（true_block / false_positive / unclear），")
    print("         然后运行：python tools/guardrail_triage.py score \"%s\"" % out)
    print()
    print("⚠️ 边界：本表来自 gate-events 的脱敏字段——不含用户原话/命令/正文；")
    print("   判定由人做，工具只做抽样与统计（样本估计 ≠ 全量真值）。")
    return 0


def cmd_score(args):
    p = pathlib.Path(args.csv or "triage.csv").resolve()
    if not p.is_file():
        print("[triage] 找不到 %s（先跑 sample）" % p)
        return 1
    rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
    # ⚠️ 2026-09-30 修（评分混快照/混用途）：评分必须按
    #    【cohort = 快照 × 用途】分组 —— 修前只按门/规则汇总，两个快照
    #    （一个全正确、一个全误报）会被合成一个 50% 的精确率数字，而它
    #    不代表其中任何一个版本。按门/规则的输出保留，但标为「历史混合汇总」。
    by_gate = {}
    by_rule = {}
    by_cohort = {}
    for r in rows:
        v = (r.get("verdict") or "").strip().lower()
        snap = (r.get("source_snapshot") or "<缺失>")[:12]
        purp = (r.get("purpose") or "").strip() or "<用途未填>"
        for bucket, key in ((by_gate, r.get("gate_id") or "?"),
                            (by_rule, r.get("rule_id") or "?"),
                            (by_cohort, "%s | %s" % (snap, purp))):
            b = bucket.setdefault(key, {"true": 0, "false": 0, "unclear": 0})
            if v == "true_block":
                b["true"] += 1
            elif v == "false_positive":
                b["false"] += 1
            else:
                b["unclear"] += 1

    def report(title, bucket):
        print("─" * 60)
        print("[%s]" % title)
        for key in sorted(bucket):
            b = bucket[key]
            n = b["true"] + b["false"]
            if n == 0:
                print("  %-34s 已标注 0 条（unclear/未填 %d）" % (key, b["unclear"]))
                continue
            lo, hi = _wilson(b["true"], n)
            print("  %-34s 真拦 %2d / 误报 %2d → 精确率≈%.0f%%（95%% 区间 %.0f–%.0f%%；未标注 %d）"
                  % (key, b["true"], b["false"], 100 * b["true"] / n,
                     100 * lo, 100 * hi, b["unclear"]))

    total = len(rows)
    judged = sum(1 for r in rows
                 if (r.get("verdict") or "").strip().lower() in
                 ("true_block", "false_positive"))
    print("=" * 60)
    print("抽样复核结果（样本 %d 条，已标注 %d 条，未标注 %d 条）"
          % (total, judged, total - judged))
    # ⚠️ 2026-09-30 第二轮修（cohort 缺时间窗）：同一快照 + 同一用途的
    #    不同时段此前会合并 —— 结果不得直接称为「当前表现」。
    #    最小修法：显式记录样本时间范围并提示边界（不改抽样口径）。
    _tss = sorted((r.get("ts") or "") for r in rows if (r.get("ts") or ""))
    _span = ("%s → %s" % (_tss[0][:16], _tss[-1][:16])) if _tss else "(无时间戳)"
    print("样本时间范围: %s" % _span)
    print("⚠️ 以上数字只限该时间窗内；不得当作任意时段或「当前表现」引用。")
    print("=" * 60)
    report("按 cohort（快照 × 用途）—— 判定效果的有效分组", by_cohort)
    n_snap = len({(r.get("source_snapshot") or "<缺失>")[:12] for r in rows})
    if n_snap > 1:
        note = ("（⚠️ 跨 %d 个快照 → 下方按门/按规则是【历史混合汇总】，"
                "不代表任何单个版本；结论请以上方 cohort 分组为准）" % n_snap)
    else:
        note = ""
    report("按门（历史混合汇总，仅参考）" + note, by_gate)
    report("按规则（历史混合汇总，仅参考）", by_rule)
    print("─" * 60)
    print("⚠️ 这是【样本估计】——区间随样本量收窄；要更紧的区间就多抽一些（sample 再跑大 n）。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="行为门抽样复核（triage）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="随机抽样出手事件 → triage.csv")
    s.add_argument("--n", type=int, default=30)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_sample)
    c = sub.add_parser("score", help="读回已标注的 csv → 样本估计")
    c.add_argument("csv", nargs="?", default=None)
    c.set_defaults(fn=cmd_score)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
