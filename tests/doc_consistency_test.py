# -*- coding: utf-8 -*-
"""文档数字一致性回归（REAL_REGRESSION）

为什么需要这个测试：
    「文档里的测试数过期」在本项目已发生过 **5 次**：

        #40   README 的「N 项真实问题」过期（#32 修过一次，同类复发）
        本轮    README  写 291，实际 293
        本轮    install.md 写 292，实际 293
        本轮    CONTRIBUTING 写 265，实际 293（且同文件里另一处已改成 293）

    靠人眼查这个不可靠 —— 它每次都在「同一份文档的另一处」复发。
    所以把它变成机械检查：**跑一遍测试，把总数与文档里的每个
    「N 项」比对，不一致就红。**

运行：python tests/doc_consistency_test.py
"""

import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SUITES = [
    "four_gate_selftest", "cmd_chain_test", "adversarial_test",
    "budget_safety_test", "intent_gate_test", "upgrade_path_test",
    "g5_real_regression_test", "source_identity_consistency_test",
    "gate_events_test", "g5_windows_path_test", "install_cli_test",
    "mcp_field_dispatch_test",
]

DOCS = ["README.md", "install.md", "安装说明.md", "CONTRIBUTING.md"]

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("  %-6s %s" % ("PASS" if ok else "**FAIL**", name))
    if not ok and detail:
        print("         %s" % detail)


def actual_total():
    """跑一遍全部套件，返回 (总数, 每套明细)。"""
    t = 0
    detail = {}
    for s in SUITES:
        p = subprocess.run(
            [sys.executable, os.path.join(PKG, "tests", s + ".py")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        m = re.findall(r"(\d+)\s*/\s*(\d+)\s*通过", out)
        n = int(m[-1][1]) if m else 0
        if not m:
            m2 = re.search(r"：\s*(\d+)\s*/\s*(\d+)", out)
            n = int(m2.group(2)) if m2 else 0
        detail[s] = n
        t += n
    return t, detail


def main():
    print("=" * 84)
    print("文档数字一致性回归")
    print("=" * 84)

    total, detail = actual_total()
    print("\n  实际测试总数：%d" % total)
    for k, v in detail.items():
        if v == 0:
            print("    ⚠️ %s 未解析到数字" % k)

    check("全部套件都跑出数字（无解析失败）",
          all(v > 0 for v in detail.values()),
          "零值项：%s" % [k for k, v in detail.items() if v == 0])

    # ---- 文档里的「N 项」必须都等于实际总数 ----
    print("\n----- 文档中的「N 项」必须与实际一致 -----")
    PAT = re.compile(r"(\d{2,4})\s*项")
    for f in DOCS:
        p = os.path.join(PKG, f)
        if not os.path.isfile(p):
            check("%s 存在" % f, False, "文件缺失")
            continue
        s = io.open(p, encoding="utf-8").read()
        bad = []
        for i, line in enumerate(s.split("\n"), 1):
            for m in PAT.finditer(line):
                n = int(m.group(1))
                # 只关心「测试总数」量级；其他数字（如 53/53 的分项）不在此列
                if 80 <= n <= 999 and n != total:
                    bad.append("行 %d：写着 %d 项" % (i, n))
        check("%s 无过期测试数" % f, not bad,
              "；".join(bad) if bad else "")

    # ---- 每套的分项数字也要对 ----
    print("\n----- 各套的分项数字（如 53/53）与实际一致 -----")
    for f in DOCS:
        p = os.path.join(PKG, f)
        if not os.path.isfile(p):
            continue
        s = io.open(p, encoding="utf-8").read()
        bad = []
        for suite, n in detail.items():
            # 找形如 "suite.py  # 53/53" 或 "suite.py # 期望 53/53"
            pat = re.compile(re.escape(suite) + r"[^\n]*?(\d+)\s*/\s*(\d+)")
            for m in pat.finditer(s):
                if int(m.group(2)) != n:
                    bad.append("%s 写成 %s（实际 %d）"
                               % (suite, m.group(0).split("/")[-1][:4], n))
        check("%s 分项数字正确" % f, not bad,
              "；".join(bad[:3]) if bad else "")

    npass = sum(1 for _, ok, _ in _results if ok)
    print("\n" + "=" * 84)
    print("文档一致性回归：%d / %d 通过" % (npass, len(_results)))
    print("=" * 84)
    if npass != len(_results):
        print("\n失败项：")
        for n, ok, det in _results:
            if not ok:
                print("  - %s：%s" % (n, det))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
