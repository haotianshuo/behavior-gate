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

def discover_suites():
    """从磁盘枚举套件 —— **不写死清单**。

    ⚠️ 这个函数的存在理由（来自另一台电脑复核的教训）：
       它用 3.5.10 的清单去跑 3.5.11，报「236/236 全过」——
       而 3.5.11 新增的 4 个套件【一个都没跑】。
       写死清单 = 新增套件时不会自动进检查。

       同一类问题还有一处：AGENTS.md 曾写着「全部测试（167 项）」，
       只列 6 个套件 —— 而 AGENTS.md 是【给 AI 读的项目说明】，
       AI 照着跑就会漏掉一半，然后报"全过"。
       所以清单必须从磁盘派生，且每份列清单的文档都要被检查是否列全。

    排除自己（doc_consistency_test）—— 它递归跑其它套件。
    """
    here = os.path.basename(__file__)[:-3]
    out = []
    for f in sorted(os.listdir(os.path.join(PKG, "tests"))):
        if not f.endswith(".py") or f == "__init__.py":
            continue
        name = f[:-3]
        if name == here:
            continue
        out.append(name)
    return out


SUITES = discover_suites()

# 哪些文档【应当】列全测试清单。
# 判据：文档里出现了 2 个以上套件名 → 它就是在列清单 → 必须列全。
# AGENTS.md 也在内（它是给 AI 读的，漏列会导致 AI 漏跑）。
CANDIDATE_DOCS = ["README.md", "install.md", "安装说明.md",
                  "CONTRIBUTING.md", "AGENTS.md", ".github/workflows/ci.yml"]

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
    DOCS = []
    for f in CANDIDATE_DOCS:
        p = os.path.join(PKG, f)
        if not os.path.isfile(p):
            continue
        s = io.open(p, encoding="utf-8").read()
        listed = set(re.findall(r"([a-z_0-9]+_test|four_gate_selftest)", s))
        # 列了 2 个以上套件名 → 它就是在列测试清单 → 纳入检查
        if len(listed & set(SUITES)) >= 2:
            DOCS.append(f)
        bad = []
        for i, line in enumerate(s.split("\n"), 1):
            for m in PAT.finditer(line):
                n = int(m.group(1))
                # 只关心「测试总数」量级；其他数字（如 53/53 的分项）不在此列
                if 80 <= n <= 999 and n != total:
                    bad.append("行 %d：写着 %d 项" % (i, n))
        check("%s 无过期测试数" % f, not bad,
              "；".join(bad) if bad else "")

    # ---- 列了清单的文档必须【列全】----
    # ⚠️ 这条是新增的，来自真实教训：AGENTS.md 曾只列 6/13 个套件，
    #    而它是给 AI 读的 —— AI 照着跑就漏掉一半，然后报"全过"。
    print("\n----- 列了测试清单的文档必须列全（%d 套）-----" % len(SUITES))
    print("      （检查对象由「文档里出现 2 个以上套件名」自动判定）")
    for f in CANDIDATE_DOCS:
        p = os.path.join(PKG, f)
        if not os.path.isfile(p):
            continue
        s = io.open(p, encoding="utf-8").read()
        listed = set(re.findall(r"([a-z_0-9]+_test|four_gate_selftest)", s))
        listed &= set(SUITES)
        if len(listed) < 2:
            continue                      # 不是清单，跳过
        miss = set(SUITES) - listed
        check("%s 列全了套件" % f, not miss,
              "缺 %d 个：%s" % (len(miss), ", ".join(sorted(miss))))

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
