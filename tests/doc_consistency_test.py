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

# ⚠️ 「全部套件」的口径必须只定义一处。
#   SUITES 排除了自己（它递归跑其它套件，不能自我递归）；
#   但文档里列的清单【包含】doc_consistency_test —— 所以对外计数是 +1。
#   实测踩到：标题用 len(SUITES)=12、正文用 13，两处口径打架。
TOTAL_SUITES = len(SUITES) + 1

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
    """跑一遍【除自己外】的全部套件，返回 (合计, 每套明细, 异常表)。

    ⚠️ 为什么【不含自己】—— 这是踩出来的设计：
       本测试不能跑自己（递归），所以它测不出自己的用例数。
       若把「自己」算进总数，总数就依赖一个它测不到的值 ——
       实测踩到：它算 296（不含自己），文档写 296，报"通过"——
       而全部套件的真实合计是 313。**基准错了，所以错的文档也能过。**

       现在改为：总数 = 其余套件的合计（它【能】测到的部分），
       文档写的也必须是这个数；「自己那一套」单独列，不混进总数。
       这样没有自指、没有需要手工维护的常量。

    ⚠️ 异常表 errors —— 来自外部复核实测的两个缺陷：
       1) 原实现【完全不看 p.returncode】：一个套件只要往 stdout 打印
          「53/53 通过」然后 sys.exit(1)，本检查器仍报全绿。
          实测：把 four_gate_selftest 换成 print('53/53 通过'); sys.exit(1)
                → 检查器 17/17 通过 / EXIT 0。
       2) 原实现在 subprocess.run 上给了 timeout=120，但【没有捕获
          TimeoutExpired】。实测：套件超时 → 直接 traceback 崩溃，
          不是一次干净的 FAIL（用户看到的是堆栈，不是"哪个套件超时"）。
       两者都让"跑过一遍"这个前提失效，所以统一记进 errors，
       由 main 里单独一条 check 呈现。
    """
    t = 0
    detail = {}
    errors = {}
    for s in SUITES:
        try:
            p = subprocess.run(
                [sys.executable, os.path.join(PKG, "tests", s + ".py")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        except subprocess.TimeoutExpired:
            detail[s] = 0
            errors[s] = "超时（超过 120s 未结束）"
            continue
        out = p.stdout.decode("utf-8", "replace")
        # 退出码非 0 = 该套件自认失败；即使它打印了"全过"也不算通过
        if p.returncode != 0:
            errors[s] = "退出码 %d" % p.returncode
        m = re.findall(r"(\d+)\s*/\s*(\d+)\s*通过", out)
        n = int(m[-1][1]) if m else 0
        if not m:
            m2 = re.search(r"：\s*(\d+)\s*/\s*(\d+)", out)
            n = int(m2.group(2)) if m2 else 0
        detail[s] = n
        t += n
    return t, detail, errors


def main():
    print("=" * 84)
    print("文档数字一致性回归")
    print("=" * 84)

    total, detail, errors = actual_total()
    print("\n  实际测试总数：%d" % total)
    for k, v in detail.items():
        if v == 0:
            print("    ⚠️ %s 未解析到数字" % k)

    # ⚠️ 这条检查的是「跑过一遍」这个前提本身是否成立。
    #    原实现只 grep stdout 的 "N/M 通过"，既不看 returncode 也不捕获
    #    TimeoutExpired —— 于是「打印全过但实际失败」和「超时崩溃」都能蒙混。
    #    实测：套件换成 print('53/53 通过'); sys.exit(1) → 原实现 17/17 全绿。
    if errors:
        print("\n  套件运行异常：")
        for k, v in errors.items():
            print("    ✗ %s：%s" % (k, v))
    check("全部套件都正常结束（退出码 0 且未超时）",
          not errors,
          "异常：%s" % "；".join("%s=%s" % (k, v) for k, v in errors.items()))

    check("全部套件都跑出数字（无解析失败）",
          all(v > 0 for v in detail.values()),
          "零值项：%s" % [k for k, v in detail.items() if v == 0])

    # ---- 文档里的「N 项」与「N 项真实问题」----
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

    # ---- 覆盖范围自检：措辞重构不该让文档静默逃出检查 ----
    #
    # ⚠️ 补这个盲区（外部复核实测）：上面的纳入判据是「文档里出现 ≥2 个
    #    套件名」—— 一个【内容启发式】，不是文件清单。后果是文档作者
    #    在无意间拥有"关掉检查"的能力，且关掉时没有任何提示：
    #      实测：把 README / install.md / CONTRIBUTING.md 里的套件名
    #            _test.py 改成 _check.py → 检查项从 17 塌缩到 8，
    #            输出仍是「8 / 8 通过」/ EXIT 0。
    #
    #    这条自检不写死文档清单（那会失去自动发现能力），只问一句：
    #    没被纳入检查的候选文档里，有没有「看起来像测试总数」的数字？
    #    有 → 它本该被查却没被查。
    print("\n----- 覆盖范围自检（没有文档因措辞变化静默逃出）-----")
    skipped = []
    for f in CANDIDATE_DOCS:
        if f in DOCS:
            continue
        p = os.path.join(PKG, f)
        if not os.path.isfile(p):
            continue
        s = io.open(p, encoding="utf-8").read()
        hits = sorted({int(m.group(1)) for m in PAT.finditer(s)
                       if 80 <= int(m.group(1)) <= 999})
        if hits:
            skipped.append("%s（未被纳入检查，却含 %s 项）" % (f, hits[:3]))
    check("没有文档因措辞变化静默逃出检查范围", not skipped,
          "；".join(skipped))

    # ---- 列了清单的文档必须【列全】----
    # ⚠️ 这条是新增的，来自真实教训：AGENTS.md 曾只列 6/13 个套件，
    #    而它是给 AI 读的 —— AI 照着跑就漏掉一半，然后报"全过"。
    print("\n----- 列了测试清单的文档必须列全（%d 套）-----" % TOTAL_SUITES)
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

    # ---- 「N 套测试」也必须等于实际套件数 ----
    # ⚠️ 补这个盲区：原来只查「N 项」，漏了「N 套」。
    #    实测踩到：README / install.md 写「十二套」，实际 13 个
    #    （doc_consistency_test 自己没被算进去）。
    print("\n----- 「N 套测试」必须等于实际套件数（%d）-----" % TOTAL_SUITES)
    CN_NUM = {"十二": 12, "十三": 13, "十四": 14, "十五": 15,
              "十六": 16, "十七": 17, "十八": 18, "十九": 19,
              "二十": 20, "十一": 11, "十": 10}
    SUITE_CNT = TOTAL_SUITES
    for f in CANDIDATE_DOCS:
        p = os.path.join(PKG, f)
        if not os.path.isfile(p):
            continue
        s = io.open(p, encoding="utf-8").read()
        bad = []
        for m in re.finditer(r"([一二三四五六七八九十]+)套测试", s):
            n = CN_NUM.get(m.group(1))
            if n is not None and n != SUITE_CNT:
                bad.append("「%s套测试」应为 %d 套" % (m.group(1), SUITE_CNT))
        for m in re.finditer(r"\b(\d+)\s*套测试", s):
            if int(m.group(1)) != SUITE_CNT:
                bad.append("「%s 套测试」应为 %d 套"
                           % (m.group(1), SUITE_CNT))
        if bad:
            check("%s 套件数正确" % f, False, "；".join(bad[:3]))

    # ---- 每套的分项数字也要对 ----
    print("\n----- 各套的分项数字（如 53/53）与实际一致 -----")
    for f in DOCS:
        p = os.path.join(PKG, f)
        if not os.path.isfile(p):
            continue
        s = io.open(p, encoding="utf-8").read()
        bad = []
        for suite, n in detail.items():
            # 形态 A：找形如 "suite.py  # 53/53" 或 "suite.py # 期望 53/53"
            pat = re.compile(re.escape(suite) + r"[^\n]*?(\d+)\s*/\s*(\d+)")
            for m in pat.finditer(s):
                if int(m.group(2)) != n:
                    bad.append("%s 写成 %s（实际 %d）"
                               % (suite, m.group(0).split("/")[-1][:4], n))
            # 形态 B：无分母写法 "suite.py  # 53"
            #
            # ⚠️ 补这个盲区（外部复核实测）：原实现只认【带分母】的 N/M，
            #    而 AGENTS.md 全部 13 处都写成 `# 53`（无分母）——
            #    逐套件命中数【全部为 0】，12 个分项数字一个都没被检查。
            #    实测：把 AGENTS.md 的分项数字改错、保留总数锚，
            #          检查器仍报 17/17 通过 / EXIT 0。
            #    负向前瞻 (?!\s*/) 保证形态 B 不吞掉形态 A 的那一半。
            pat_b = re.compile(
                re.escape(suite) + r"[^\n]*?[#:：]\s*\**\s*(\d{1,4})\b(?!\s*/)")
            for m in pat_b.finditer(s):
                if int(m.group(1)) != n:
                    bad.append("%s 写成 %s（无分母写法，实际 %d）"
                               % (suite, m.group(1), n))
        # ⚠️ 已知盲区（如实声明，不假装能查）：
        #    本测试【查不了自己】的分项数字 —— detail 基于 SUITES，
        #    而 SUITES 刻意排除了自己（否则递归）。
        #    实测踩到：四份文档都写着「doc_consistency_test # 9/9」，
        #    而它已涨到 17 —— 没有任何自动检查发现。
        #    改它的项数时【必须手工同步这四份文档】。
        check("%s 分项数字正确" % f, not bad,
              "；".join(bad[:3]) if bad else "")

    # ---- CHANGELOG 的「N 项真实问题」必须等于实际条目数 ----
    # ⚠️ 补盲区：原来只查「N 项测试」，漏了「N 项真实问题」——
    #    实测踩到：CHANGELOG 实际 56 条，README 里仍写「55 项真实问题」，
    #    而本测试报"通过"。同一类数字在【另一处】复发。
    print("\n----- CHANGELOG 条目数必须与文档引用一致 -----")
    cl = io.open(os.path.join(PKG, "CHANGELOG.md"), encoding="utf-8").read()
    n_issues = 0
    for line in cl.split("\n"):
        mm = re.match(r"\| (\d+) \|", line)
        if mm and int(mm.group(1)) == n_issues + 1:
            n_issues = int(mm.group(1))
    print("      CHANGELOG 实际条目数：%d" % n_issues)
    for f in CANDIDATE_DOCS:
        p2 = os.path.join(PKG, f)
        if not os.path.isfile(p2):
            continue
        s2 = io.open(p2, encoding="utf-8").read()
        bad = []
        for mm in re.finditer(r"(\d+)\s*项真实问题", s2):
            if int(mm.group(1)) != n_issues:
                bad.append("写着 %s 项，实际 %d" % (mm.group(1), n_issues))
        # 只在文档确实引用了这个数时才检查
        if re.search(r"\d+\s*项真实问题", s2):
            check("%s 的 CHANGELOG 项数正确" % f, not bad, "；".join(bad))

    # ---- 文档里的「N 项测试」必须等于【功能测试】合计 ----
    #
    # ⚠️ 口径：总数【只算被检查的 12 套功能测试】（当前 296），
    #    **不含本检查器自己**。
    #
    #    为什么不含自己 —— 这是踩了两次后的结论：
    #      本测试不能跑自己（递归），所以它无法得知自己的用例数。
    #      任何把它算进总数的方案都需要一个「自己有多少项」的已知值：
    #        · 用常量记 → 改本文件就漂（实测漂过两次）
    #        · 用 len(_results) 现算 → 但检查跑完前它还在增长，算不准
    #      两种都失败，因为**总数依赖它自己**，是自指。
    #
    #    不含自己就没有自指：改本文件、加减检查，都不影响文档要写的数。
    #    「296 项功能测试 + 1 个检查器」也比「313 项」更准确 ——
    #    检查器不是功能，它没有「通过/不通过」以外的产品意义。
    print("\n----- 文档中的「N 项」必须等于功能测试合计（%d）-----" % total)
    for f in DOCS:
        p2 = os.path.join(PKG, f)
        s2 = io.open(p2, encoding="utf-8").read()
        bad = []
        for i, line in enumerate(s2.split("\n"), 1):
            for m in PAT.finditer(line):
                n = int(m.group(1))
                if 80 <= n <= 999 and n != total:
                    bad.append("行 %d：写着 %d 项（应为 %d）" % (i, n, total))
        check("%s 无过期测试数" % f, not bad, "；".join(bad))

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
