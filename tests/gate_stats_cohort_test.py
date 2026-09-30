#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计与抽样工具的 cohort 口径回归（2026-09-30）。

## 它守什么

评估口径曾出过两类错误，这两条规则防止复发：

  1. **分混合算**：跨多个 source_snapshot 的行为被合成一个数字（实测：
     两个快照一全对、一全误报 → 合成 50%，两者都不代表）。工具必须能按
     cohort（快照 × 用途）分组，并把按门/按规则的输出标注为"历史混合汇总"。
  2. **未知不得变业务**：source_class 默认即 NATURAL（不能证明用途），
     用途分类只能来自【外部依据】；依据缺失、格式非法、会话跨类冲突时
     必须拒绝分类或保持未知，绝不猜。

附带守两条实现细节：截止时间必须按解析后的时间比较（字符串比较会错排
跨时区样本）；无法解析的时间戳单独计数，不静默混入。

## 边界

  证明    : 上述规则在构造样本上的行为
  不证明  : 真实数据的用途分类正确（那取决于外部依据本身的真实性，
            工具不判断依据真假 —— 但不得自相矛盾）
"""
import csv
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
FAILED = []


def check(label, ok, detail=""):
    print("  %s %s%s" % ("PASS" if ok else "FAIL", label,
                         ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILED.append(label)


def rec(ts, sid, snap, gate="G5", rule="r1"):
    return {"ts": ts, "session": sid, "gate_id": gate, "rule_id": rule,
            "source_snapshot": snap}


def main():
    rows = [rec("2026-09-30T01:00+0800", "sA", "AAAA1111"),
            rec("2026-09-30T01:01+0800", "sB", "BBBB2222")]

    print("=" * 62)
    print("数量核对：纳入 + 排除 + 时间无法解析 = 总数")
    print("=" * 62)
    c = gs.cohort_summary(rows, snapshot="AAAA1111", until=None, purpose_map=None)
    check("快照过滤后核对成立",
          c["in_scope"] + c["out_scope"] + c["unparsable_ts"] == c["total"],
          "in=%s out=%s unparsable=%s total=%s" % (
              c["in_scope"], c["out_scope"], c["unparsable_ts"], c["total"]))
    check("快照过滤生效", c["in_scope"] == 1, "in=%s" % c["in_scope"])

    print()
    print("=" * 62)
    print("用途分类：未知不得变业务")
    print("=" * 62)
    c = gs.cohort_summary(rows, purpose_map=None)
    check("无依据 → 全部未知", c["by_purpose"] == {"用途未知": 2}, str(c["by_purpose"]))
    c = gs.cohort_summary(rows, purpose_map={"basis": "", "classes": {"已确认业务": ["sA"]}})
    check("依据为空 → 拒绝分类", c["by_purpose"] == {"用途未知": 2}, str(c["by_purpose"]))
    c = gs.cohort_summary(rows, purpose_map={"basis": "x", "classes": {"已确认业务": "prefix-sA-suffix"}})
    check("值是字符串 → 拒绝分类（防子串误判）",
          c["by_purpose"] == {"用途未知": 2}, str(c["by_purpose"]))
    c = gs.cohort_summary(rows, purpose_map={"basis": "x", "classes": {"已确认业务": ["sA", 123]}})
    check("列表含非字符串 → 拒绝分类", c["by_purpose"] == {"用途未知": 2}, str(c["by_purpose"]))
    c = gs.cohort_summary([rows[0]], purpose_map={"basis": "x", "classes": {"A": ["sA"], "B": ["sA"]}})
    check("会话跨类冲突 → 保持未知",
          list(c["by_purpose"].keys()) == ["分类冲突(保持未知)"], str(c["by_purpose"]))
    c = gs.cohort_summary(rows, purpose_map={"basis": "用户陈述 2026-09-30",
                                             "classes": {"已确认业务": ["sA"]}})
    check("合法依据+列表 → 正常分类（不误伤）",
          c["by_purpose"] == {"已确认业务": 1, "用途未知": 1}, str(c["by_purpose"]))

    print()
    print("=" * 62)
    print("截止时间：按解析后的时间比较（含边界）；不可解析单独计数")
    print("=" * 62)
    r1 = rec("2026-09-30T08:30+08:00", "s1", "AAAA")   # = 00:30Z
    r2 = rec("2026-09-30T01:00Z", "s2", "AAAA")        # = 01:00Z
    c = gs.cohort_summary([r1, r2], until="2026-09-30T09:00+08:00")   # = 01:00Z
    check("含边界：两条都纳入", c["in_scope"] == 2, "in=%s" % c["in_scope"])
    c = gs.cohort_summary([r1, r2], until="2026-09-30T08:45+08:00")   # = 00:45Z
    check("跨时区按真实时间比较（只纳入 00:30Z 那条）",
          c["in_scope"] == 1, "in=%s" % c["in_scope"])
    c = gs.cohort_summary([rec("不是时间", "s3", "AAAA"), r1],
                          until="2026-09-30T09:00+08:00")
    check("不可解析 → 单独计数且核对成立",
          c["unparsable_ts"] == 1
          and c["in_scope"] + c["out_scope"] + c["unparsable_ts"] == c["total"],
          "unparsable=%s" % c["unparsable_ts"])

    print()
    print("=" * 62)
    print("triage score：按 cohort 分组 + 标注样本时间范围")
    print("=" * 62)
    tmp = tempfile.mkdtemp(prefix="gscohort-")
    csvp = os.path.join(tmp, "t.csv")
    with open(csvp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["#", "ts", "gate_id", "rule_id", "tool", "session",
                    "source_snapshot", "source_class_note", "purpose", "evidence",
                    "verdict", "note"])
        for i in range(3):   # 快照 A 全对
            w.writerow([i + 1, "2026-09-30T01:0%d+0800" % i, "G5", "r1", "Bash",
                        "s%d" % i, "AAAA1111deadbeef", "NATURAL（默认值≠业务用途）",
                        "", "", "true_block", ""])
        for i in range(3):   # 快照 B 全误报
            w.writerow([i + 4, "2026-09-30T02:0%d+0800" % i, "G5", "r1", "Bash",
                        "t%d" % i, "BBBB2222cafebabe", "NATURAL（默认值≠业务用途）",
                        "", "", "false_positive", ""])
    p = subprocess.run([PY, os.path.join(TOOLS, "guardrail_triage.py"), "score", csvp],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    out = p.stdout.decode("utf-8", "replace")
    check("按 cohort 分组（两个快照各自成行）",
          "AAAA1111dead" in out and "BBBB2222cafe" in out)
    check("未被合成单一精确率（分别 100% / 0%）",
          "精确率≈100%" in out and "精确率≈0%" in out)
    check("按门/按规则标注为历史混合汇总", "历史混合汇总" in out)
    check("记录样本时间范围", "样本时间范围" in out)
    check("提示不得当作任意时段引用", "不得当作任意时段" in out)

    print()
    if FAILED:
        print("失败 %d 项：" % len(FAILED))
        for x in FAILED:
            print("  - %s" % x)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
