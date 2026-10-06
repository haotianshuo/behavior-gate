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
import datetime
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


# 与 guardrail_triage.py 的 HIT_DECISIONS 保持同一事实口径：
# 只有这些 decision 才表示门实际拦截/打回（评审事件另行排除）。
HIT_DECISIONS = frozenset({
    "deny",
    "stop_feedback",
    "stop_feedback_retry",
})


def _decision_class(decision):
    """block / review / unknown —— 不对未知类型猜测。"""
    if isinstance(decision, str):
        if decision in HIT_DECISIONS:
            return "block"
        if decision.startswith("review_"):
            return "review"
    return "unknown"


def _decision_label(decision):
    """把任意 decision 归一为稳定、可 JSON 序列化的字符串标签。

    空值与空串统一记 <empty>；非字符串值用紧凑 JSON 表示
    （ensure_ascii=False + sort_keys 保证同值同标签）。
    """
    if isinstance(decision, str):
        return decision if decision else "<empty>"
    if decision is None:
        return "<empty>"
    try:
        return json.dumps(decision, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(decision)


def analyze(recs):
    a = {
        "total": len(recs),
        "by_gate": collections.Counter(r.get("gate_id") for r in recs),
        "by_rule": collections.Counter(r.get("rule_id") for r in recs),
        "by_decision": collections.Counter(_decision_label(r.get("decision"))
                                           for r in recs),
        "by_day": collections.Counter((r.get("ts") or "")[:10] for r in recs),
    }
    # A. 同一 session 内同一规则重复触发 —— 只算门真实出手；
    #    REVIEW 评审与未知 decision 不进入（见 _decision_class）。
    unknown = collections.Counter()
    grp = collections.defaultdict(list)
    for r in recs:
        decision = r.get("decision")
        cls = _decision_class(decision)
        if cls == "unknown":
            unknown[_decision_label(decision)] += 1
            continue
        if cls != "block":
            continue
        if (r.get("gate_id") or "") == "REVIEW":
            continue
        grp[(r.get("session"), r.get("rule_id"))].append(r.get("ts") or "")
    a["unknown_decisions"] = unknown
    a["repeats"] = sorted(
        [(len(v), k[0], k[1]) for k, v in grp.items() if len(v) >= 3],
        reverse=True)
    # B. 重试仍不合规
    a["retry"] = collections.Counter(
        r.get("rule_id") for r in recs
        if r.get("decision") == "stop_feedback_retry")
    return a


# ---------------------------------------------------------------- cohort 视角
# （2026-09-30 增补，依据：评估口径错配的教训 + 朋友方复核）
#
# ⚠️ 为什么需要：跨多个 source_snapshot 累积后，任何按 version 分组的分析
#    都会把不同代码的行为混成一锅（实测：VERSION 冻结在 3.5.16 的内部至少有
#    8 个快照，行为差异显著）。此外 source_class 默认即 NATURAL
#    （_lib.py:207），**不能证明业务用途**。
#
# 三条硬规则：
#   ① 用途分类必须由【外部依据】注入（--purpose-map 文件），未列入的会话
#      一律标「用途未知」—— 未知不得悄悄变成业务数据；
#   ② source_snapshot 只是标签，其可信度继承自部署校验（verify_deploy）——
#      未校验时输出里标「身份未核实」；
#   ③ 数量核对：纳入 + 排除 = 总数，任一项都要打印；对不上直接报错。
#
# 指标口径：所有耗时/失败率一律写「每次调用」，样本不足以支撑「每轮」时不写。

_FAIL_KINDS = (
    ("超时", ("Timeout", "timed out")),
    ("连接失败", ("urlopen", "URLError", "拒绝", "Connection")),
    ("空响应", ("output_tokens", "响应文本为空")),
    ("鉴权/环境缺失", ("ANTHROPIC_API_KEY", "模型标识")),
)


def classify_failure(err):
    """把 review_error 归入有限几类；无法归类返回「其他」。"""
    e = str(err or "")
    if not e:
        return None
    for kind, keys in _FAIL_KINDS:
        if any(k in e for k in keys):
            return kind
    return "其他"


def _parse_ts(s):
    """解析事件时间戳为 aware datetime；失败返回 None。

    ⚠️ 2026-09-30 修：原实现用【字符串比较】—— `2026-09-30T01:00Z` 与
    `2026-09-30T08:30+08:00` 会被按字符序错排（后者实际早 30 分钟）。
    现在解析 + 统一转 UTC 比较；无法解析的样本【排除并计数】，不静默混入。
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def cohort_summary(recs, snapshot=None, until=None, purpose_map=None):
    """cohort 口径统计。purposes 未提供的会话 = 用途未知（不得猜）。"""
    pm = purpose_map or {}
    classes = pm.get("classes") or {}
    basis = (pm.get("basis") or "").strip()
    # ⚠️ 2026-09-30 修（自相矛盾）：依据缺失或 classes 非法时【拒绝分类】——
    #    修前：basis 为空走 or 分支显示"全部未知"，但 classes 仍被使用
    #    → 同一次输出里既说"全部未知"又给出"已确认业务"分类。
    if not basis or not isinstance(classes, dict):
        classes = {}
        basis = "(未提供用途依据 → 全部为「用途未知」)"
    else:
        # ⚠️ 2026-09-30 第二轮修：值必须是【会话 ID 列表】——
        #    修前只检查 classes 是 dict；若某值是字符串，`sid in ids` 会退化为
        #    【子串包含判断】（"sA" in "prefix-sA-suffix" → True）→ 把无关会话
        #    误分类成业务。格式非法一律拒绝分类，不猜。
        _bad = [k for k, v in classes.items()
                if not isinstance(v, list)
                or any((not isinstance(x, str)) or (not x) for x in v)]
        if _bad:
            classes = {}
            basis = ("(用途映射格式非法：%s 的值不是会话 ID 列表 → 全部为「用途未知」)"
                     % "、".join(map(str, _bad)))
    # 会话分类冲突（同一 sid 出现在多个类）→ 该会话保持未知，不猜
    _seen, conflicts = {}, set()
    for label, ids in classes.items():
        for sid in (ids or []):
            if _seen.get(sid, label) != label:
                conflicts.add(sid)
            _seen[sid] = label

    until_dt = _parse_ts(until) if until else None
    if until and until_dt is None:
        raise ValueError("--until 无法解析为时间：%r" % until)

    total = len(recs)
    in_scope, out_scope, unparsable = [], [], []
    for r in recs:
        if until_dt is not None:
            dt = _parse_ts(r.get("ts"))
            if dt is None:
                unparsable.append(r)      # 时间无法解析 → 不纳入，单独计数
                continue
            if dt > until_dt:             # 含边界（<= until 纳入）
                out_scope.append(r)
                continue
        if snapshot and not str(r.get("source_snapshot") or "").startswith(snapshot):
            out_scope.append(r)
            continue
        in_scope.append(r)

    def purpose_of(r):
        sid = str(r.get("session") or "")
        if sid in conflicts:
            return "分类冲突(保持未知)"
        for label, ids in classes.items():
            if sid in (ids or []):
                return label
        return "用途未知"

    by_purpose = collections.Counter(purpose_of(r) for r in in_scope)
    by_snap = collections.Counter(str(r.get("source_snapshot") or "<缺失>")
                                  for r in in_scope)
    rev = [r for r in in_scope if r.get("gate_id") == "REVIEW"]
    fails = collections.Counter(classify_failure(r.get("review_error"))
                                for r in rev if r.get("review_error"))
    return {
        "total": total, "in_scope": len(in_scope), "out_scope": len(out_scope),
        "unparsable_ts": len(unparsable),
        "snapshot": snapshot, "until": until, "basis": basis,
        "by_purpose": dict(by_purpose), "by_snapshot": dict(by_snap),
        "review_calls": len(rev), "failures": dict(fails),
    }


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
    ap.add_argument("--cohort", action="store_true",
                    help="cohort 口径输出（快照/截止时间/用途依据/数量核对）")
    ap.add_argument("--snapshot", default=None, help="只看该快照前缀（配合 --cohort）")
    ap.add_argument("--until", default=None, help="截止时间（ISO 前缀比较，配合 --cohort）")
    ap.add_argument("--purpose-map", default=None,
                    help='用途依据 JSON：{"basis": "...", "classes": {"显式测试": ["sid", ...]}}')
    ap.add_argument("--legacy-metrics", action="store_true",
                    help="附带旧混合口径 repeats_all_decisions（仅与 --json 同用）")
    args = ap.parse_args()
    if args.legacy_metrics and not args.json:
        ap.error("--legacy-metrics requires --json")

    path = args.path or events_path()
    recs = load(path, args.since)

    if args.cohort:
        pm = None
        if args.purpose_map:
            with open(args.purpose_map, encoding="utf-8") as f:
                pm = json.load(f)
        c = cohort_summary(recs, snapshot=args.snapshot, until=args.until, purpose_map=pm)
        ok = (c["in_scope"] + c["out_scope"] + c["unparsable_ts"] == c["total"])
        if args.json:
            c["count_check"] = "OK" if ok else "FAIL"
            print(json.dumps(c, ensure_ascii=False, indent=2))
            return 0
        print("=" * 78)
        print("cohort 口径统计（固定分析范围 + 数量核对）")
        print("=" * 78)
        print("  文件    : %s" % path)
        print("  快照过滤: %s" % (c["snapshot"] or "(未过滤)"))
        if c["snapshot"]:
            print("            ⚠️ 快照是标签，身份是否可信继承自部署校验（verify_deploy）")
        print("  截止时间: %s（含边界；按解析后的 UTC 时间比较）" % (c["until"] or "(未设)"))
        print("  用途依据: %s" % c["basis"])
        print()
        print("  总数 %d = 纳入 %d + 排除 %d + 时间无法解析 %d   核对: %s" % (
            c["total"], c["in_scope"], c["out_scope"], c["unparsable_ts"],
            "OK" if ok else "FAIL"))
        print()
        print("[纳入样本 · 按用途]")
        for k, v in sorted(c["by_purpose"].items(), key=lambda kv: -kv[1]):
            print("  %-14s %5d" % (k, v))
        print()
        print("[纳入样本 · 按快照]")
        for k, v in sorted(c["by_snapshot"].items(), key=lambda kv: -kv[1]):
            print("  %-20s %5d" % (k[:20], v))
        print()
        print("[评审调用] %d 次（以下指标均为【每次调用】口径）" % c["review_calls"])
        if c["failures"]:
            for k, v in sorted(c["failures"].items(), key=lambda kv: -kv[1]):
                print("  未裁决·%s: %d 次" % (k, v))
        else:
            print("  无失败记录")
        print()
        print("  边界：只做计数与分组；未提供用途依据的会话一律「用途未知」，")
        print("        不得据本表宣称业务效果；快照未经部署校验时视为身份未核实。")
        return 0

    a = analyze(recs)

    if args.json:
        out = {
            "path": path, "since": args.since, "total": a["total"],
            "by_gate": dict(a["by_gate"]), "by_rule": dict(a["by_rule"]),
            "by_decision": dict(a["by_decision"]), "by_day": dict(a["by_day"]),
            "repeats": a["repeats"][:20], "retry": dict(a["retry"]),
            "unknown_decisions": dict(a.get("unknown_decisions") or {}),
        }
        if args.legacy_metrics:
            # 旧混合口径：不按 gate/decision 过滤，仅显式请求时输出对照
            legacy = collections.defaultdict(list)
            for r in recs:
                legacy[(r.get("session"), r.get("rule_id"))].append(
                    r.get("ts") or "")
            out["repeats_all_decisions"] = sorted(
                [(len(v), k[0], k[1]) for k, v in legacy.items()
                 if len(v) >= 3], reverse=True)[:20]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("门事件统计（gate-events.jsonl 的只读消费者）")
    print("=" * 78)
    print("  文件: %s" % path)
    if args.since:
        print("  范围: >= %s" % args.since)
    print("  记录: %d 条" % a["total"])
    print("  重复触发口径：仅统计 deny / stop_feedback / stop_feedback_retry；"
          "REVIEW 评审调用不计入。")
    unknown = a.get("unknown_decisions") or {}
    if unknown:
        print("  ⚠️ 未知 decision 类型（未计入重复触发）：%s" %
              ", ".join("%s=%d" % (k, v) for k, v in sorted(unknown.items())))
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
