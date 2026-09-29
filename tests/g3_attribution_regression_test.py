# -*- coding: utf-8 -*-
"""G3 归属误报 + 漏拦回归测试（3.5.16）

本文件有两组，缺一不可：
  A 组 · 归属误报：真实被误拦的消息，修复后应【放行】
  B 组 · 反向保护：真问题（无证据、级别不足、真矛盾、否定里的等级），
        必须【仍然被拦】。只测"误报消失"而不测"保护还在"，
        等于把门关掉了还以为修好了。

样本分两类，**明确区分**：
  [REAL]  逐字取自真实收尾消息，存于 tests/data/g3_real_samples/
          —— 关键是【保留原始顺序与上下文】。
             曾经犯过的错：把证据行挪到样本最前面，
             结果旧版也会放行，样本就失去了鉴别力。
  [SYNTH] 按真实形态构造，写在文件里

判据：喂真实 stdin JSON，看真实 exit code。exit 0 = 放行，2 = 拦截。

跑法：
    python tests/g3_attribution_regression_test.py
"""

import json
import os
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(ROOT, "adapters", "claude-code", "hooks")
DEFAULT_POLICY_PATH = os.path.join(ROOT, "policy", "behavior-policy.json")
REAL_DIR = os.path.join(HERE, "data", "g3_real_samples")

# ⚠️ 状态目录隔离到临时目录：否则每次跑测试都会往【真实】gate-events.jsonl
#    写事件，污染观测数据。与 four_gate_selftest 同一做法。
_STATE_DIR = tempfile.mkdtemp(prefix="g3attr-state-")

_fails = []
_n = 0


# ⚠️ 本地候选 3.6.1：本套件测的是【程序判定逻辑】，必须确定性且离线。
#    语义评审一旦被真实调用，结果会随模型通道波动 → 这套测试时绿时红，
#    而不稳定的绿灯等于没有测试（假设上会掩盖真实回归）。
#    所以这里用「关闭语义评审」的隔离策略跑程序逻辑；
#    语义路径由 semantic_contract / semantic_intent / semantic_g3
#    三个套件用【本地假模型】单独覆盖 —— 那里才是测语义的地方。
def _policy_semantic_off():
    with open(DEFAULT_POLICY_PATH, encoding="utf-8") as f:
        pol = json.load(f)
    pol.setdefault("semantic_review", {})["enabled"] = False
    p = os.path.join(_STATE_DIR, "policy.semantic-off.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(pol, f, ensure_ascii=False, indent=2)
    return p


_POLICY_OFF = _policy_semantic_off()


def run(msg, hook="effect_gate.py"):
    inp = json.dumps({"session_id": "g3attr", "last_assistant_message": msg,
                      "stop_hook_active": False})
    env = dict(os.environ)
    env["CLAUDE_BEHAVIOR_POLICY"] = _POLICY_OFF
    env["CLAUDE_BUDGET_STATE_DIR"] = _STATE_DIR
    p = subprocess.run([sys.executable, os.path.join(HOOKS, hook)],
                       input=inp, capture_output=True, text=True,
                       encoding="utf-8", env=env)
    first = p.stderr.strip().splitlines()[0] if p.stderr.strip() else ""
    return p.returncode, first


def real(name):
    with open(os.path.join(REAL_DIR, name), encoding="utf-8") as f:
        return f.read()


def check(name, got, want, detail=""):
    global _n
    _n += 1
    if got == want:
        print("  PASS %s" % name)
    else:
        _fails.append(name)
        print("  FAIL %s  got=%r want=%r  %s" % (name, got, want, detail[:70]))


print("=" * 64)
print("A · 归属误报：修复后应【放行】(exit 0)")
print("=" * 64)

# A1 [REAL] real_01 —— NOT_MEASURED 出现在【章节标题】里，
#    且在证据声明行【之前】（实测：NOT_MEASURED@847 < 声明位@1255）。
#    旧版 exit=2（判成"级别=NOT_MEASURED"）；新版 exit=0。
c, e = run(real("real_01_notmeasured_in_heading.txt"))
check("A1 [REAL] 标题提到 NOT_MEASURED（在证据行之前）→ 放行", c, 0, e)

# A2 [REAL] real_02 —— 全文唯一完成词是【记录里的结论名】，
#    且"未验证项"出现在另一个列表项里。
#    旧版 exit=2（判成"自相矛盾"）；新版 exit=0。
c, e = run(real("real_02_classification_label.txt"))
check("A2 [REAL] 分类标签式完成词 + 跨条目'未验证项' → 放行", c, 0, e)

# A3 [SYNTH] 表格单元里提到 NOT_MEASURED，且【在证据行之前】——
#    刻意保持这个顺序，否则旧版也会放行、样本失去鉴别力。
#    旧版 exit=2；新版 exit=0。
A3 = ("| 组件 | 说明 |\n"
      "|---|---|\n"
      "| 状态词表 | CONFIRMED / ASSUMPTION / UNKNOWN / NOT_MEASURED |\n"
      "\n"
      "**证据**：L1 静态（纯文档，无代码改动）\n"
      "**风险级别**：低\n"
      "\n"
      "本轮文档已完成更新。\n")
c, e = run(A3)
check("A3 [SYNTH] 表格里提到 NOT_MEASURED（在证据行之前）→ 放行", c, 0, e)

# A4 [SYNTH] 「验收工具」是【名词】，不是「收工」完成宣言（3.5.17 修）。
#    根因：CLAIM_RX 里的裸词 `收工` 是「验【收工】具」的子串 ——
#    同一个老毛病（子串匹配无词边界），只是这次落在中文复合词上。
#    旧版 exit=2（误拦）；新版 exit=0。
c, e = run("我先查看验收工具的目录。")
check("A4 [SYNTH] 「验收工具」不是完成宣言 → 放行", c, 0, e)

# A5 [SYNTH] 同上，另一条真实形态（带否定的说明句）。
c, e = run("本轮只整理验收工具，不修改产品代码。")
check("A5 [SYNTH] 「本轮只整理验收工具」→ 放行", c, 0, e)

# ⚠️ real_03 已【移出 A 组断言】——见文件末尾「D · 已知未解决」。
#    它是一条真实的引用识别误报，但本门当前没有可靠判据处理它；
#    不把它写成"应拦"来冒充通过（那是把失败改写成预期）。

print()
print("=" * 64)
print("B · 反向保护：真问题必须【仍然被拦】(exit 2)")
print("=" * 64)

# B0 表格豁免【只作用于矛盾配对】，不得顺手豁免"完成声明需要证据"。
#    表格里写「已完成」但整条消息无证据块 → 仍须拦。
c, e = run("| 项 | 状态 |\n|---|---|\n| A | 已完成 |\n")
check("B0 表格里的'已完成'无证据块 → 仍拦（豁免未越界）", c, 2, e)

# B1 无证据块的完成声明
c, e = run("已修复。")
check("B1 完成声明无证据块 → 拦", c, 2, e)

# B2 高风险 + 只有 L1
c, e = run("已修复。\n\n**证据**：L1 静态\n**风险级别**：高\n- 读了代码")
check("B2 高风险 + L1 → 拦", c, 2, e)

# B3 同一事项里的真矛盾（两行说同一件事）
c, e = run("**证据**：L2 动态\n**风险级别**：中\n"
           "本次修复已完成。\n本次修复未验证生效。\n")
check("B3 同一事项两行'已完成 + 未验证' → 拦", c, 2, e)

# B4 中风险 + L1
c, e = run("已修复。\n\n**证据**：L1 静态\n**风险级别**：中\n- 读了代码")
check("B4 中风险 + L1 → 拦", c, 2, e)

# B5 「判定为」不是通用豁免 —— 它照样是当前完成声明
c, e = run("本轮判定为已完成。")
check("B5 '本轮判定为已完成' → 拦（词表豁免已撤除）", c, 2, e)

# B6 「此前」不是通用豁免 —— 它修饰的是宾语，断言是"现已修复"
c, e = run("此前的问题现已修复。")
check("B6 '此前的问题现已修复' → 拦（词表豁免已撤除）", c, 2, e)

# B7 否定/待办里的等级不算已声明证据
c, e = run("已修复。本轮只做了 L1 静态检查。\n\n"
           "**证据**：尚未达到 L3 端到端\n**风险级别**：高\n")
check("B7 '证据：尚未达到 L3' 不算声明 L3 → 拦", c, 2, e)

# B8 待办里的等级不算已声明证据（3.5.13 记载的洗白形态）
c, e = run("已完成。本轮只做了 L1。\n\n**证据**：\n- 待办：还没做 L3 端到端\n\n"
           "**风险级别**：高")
check("B8 '待办：还没做 L3' 不算声明 L3 → 拦", c, 2, e)

# B9 历史从句不能把【真宣言】一起洗掉：句号隔开 = 两个从句
c, e = run("**风险级别**：中\n上一轮没做完。本轮已修复。")
check("B9 历史从句 + 本轮真宣言（无证据块）→ 拦", c, 2, e)

# B10 门没有被改成一味拦：中风险 + L2 应放行
c, e = run("已修复。\n\n**证据**：L2 动态\n**风险级别**：中\n- 隔离测试通过")
check("B10 中风险 + L2 → 放行（门未变严）", c, 0, e)

# B11 表格里的【当前结果声明】必须照样拦。
#     ⚠️ 表格 ≠ 引用：real_03 那条真实样本是【单元格里复述反例名】，
#        不能因此把整类表格排除掉 —— 那样这一条会漏拦。
#     判据：同一行的【不同单元格】各说一件事 → 是状态声明，不是引用。
c, e = run("**证据**：L2 动态\n**风险级别**：中\n\n"
           "| 事项 | 完成状态 | 验证情况 |\n|---|---|---|\n"
           "| 本次修复 | 已完成 | 未验证生效 |\n")
check("B11 表格跨单元格的'已完成 + 未验证' → 拦", c, 2, e)

# B12 列表项的【缩进续行】属于原列表项，不能拆成两个事项。
c, e = run("**证据**：L2 动态\n**风险级别**：中\n\n"
           "- 本次修复已完成，\n  但本次修复未验证生效。\n")
check("B12 列表项缩进续行（同一件事）→ 拦", c, 2, e)

# B13 证据声明匹配【不得跨越换行】——否则下一行的无关等级会被当成本轮声明。
c, e = run("已修复。本轮只做了 L1 静态检查。\n\n"
           "**证据**：\nL3 是门对高风险任务的要求，不是本轮证据。\n**风险级别**：高\n")
check("B13 '证据：' 换行后的 L3 不算声明 → 拦", c, 2, e)

# B14 括号里的【当前完成声明】必须照样被看见。
#     ⚠️ 3.5.16 第四轮返工：曾把"括号内一律按引用剥离"，
#        于是「本次处理结果：（已修复）。」整句失去完成词 → 漏拦。
#        括号不能作为"这是引用"的判据。
c, e = run("本次处理结果：（已修复）。\n")
check("B14 括号里的完成声明 → 拦", c, 2, e)

# B15 括号里的【未验证说明】必须照样被看见。
c, e = run("**证据**：L2 动态\n**风险级别**：中\n本次修复已完成（但本次修复未验证生效）。\n")
check("B15 括号里的未验证说明 → 拦", c, 2, e)

# B16 收紧「收工」的匹配范围【不得】把真宣言一起关掉。
#     判据：收工 处在句末位置（后接句末标点 / 语气助词 / 行尾）。
for _t in ["本次工作完成，收工。", "可以收工了。", "收工！", "今天到此收工"]:
    c, e = run(_t)
    check("B16 真「收工」宣言无证据块 → 拦: %s" % _t, c, 2, e)

# B17 「验收工具」里含真完成词时，【整条消息】照样进判定 ——
#     收紧的只是 `收工` 这一个词的匹配范围，不是把含该词的消息豁免。
c, e = run("验收工具已经修复完成。")
check("B17 「验收工具已经修复完成。」→ 拦", c, 2, e)

print()
print("-" * 64)
print("C · 同一结果声明的【五种排版】必须判定一致（都拦）")
print("-" * 64)
# 依据：排版不改变语义。同一句"本次修复已完成，但本次修复未验证生效"
# 写成正文 / 列表 / 带续行列表 / 单格表格 / 跨格表格 —— 判定必须相同。
# ⚠️ 这组用例的作用是：防止任何"按位置/按排版"的规则（例如
#    「同一单元格即视为引用」）把某一种排版悄悄放行。
_SYN = "本次修复已完成，但本次修复未验证生效"
_LAYOUTS = [
    ("C1 正文",        "**证据**：L2 动态\n**风险级别**：中\n\n%s。\n" % _SYN),
    ("C2 列表",        "**证据**：L2 动态\n**风险级别**：中\n\n- %s。\n" % _SYN),
    ("C3 带续行列表",  "**证据**：L2 动态\n**风险级别**：中\n\n- 本次修复已完成，\n  但本次修复未验证生效。\n"),
    ("C4 单格表格",    "**证据**：L2 动态\n**风险级别**：中\n\n"
                       "| 事项 | 当前结果 |\n|---|---|\n| 本次修复 | 已完成，但本次修复未验证生效 |\n"),
    ("C5 跨格表格",    "**证据**：L2 动态\n**风险级别**：中\n\n"
                       "| 事项 | 完成状态 | 验证情况 |\n|---|---|---|\n| 本次修复 | 已完成 | 未验证生效 |\n"),
]
for name, msg in _LAYOUTS:
    c, e = run(msg)
    check("%s 的同一结果声明 → 拦" % name, c, 2, e)

print()
print("-" * 64)
print("D · 已知未解决：引用识别（【不计入通过数】，如实呈现实际行为）")
print("-" * 64)
# ⚠️ 这一组【不是断言】，是【如实报告】。
#
#    本门当前机制下，"这是引用"【没有可靠判据】：
#      · 位置（单元格）：证伪 —— 同一格里写「已完成，但本次修复未验证生效」
#        就是当前结果声明
#      · 标记（括号）  ：证伪 —— 「本次处理结果：（已修复）。」同样是当前声明
#    两个都试过、都引入了新增漏拦，都已撤回。
#
#    因此不做引用识别。代价是下面这几条【仍会被误拦】。
#    它们【不计入通过数】—— 是尚未解决的问题，不是预期结果。
_UNRESOLVED = [
    ("real_03 [REAL] 表格单元格里括号复述用例名",
     real("real_03_table_quoting_case_names.txt")),
    ("synth 表格内括号复述用例名",
     "**证据**：L2 动态\n**风险级别**：中\n\n| 反例 | 说明 |\n|---|---|\n"
     "| 本次修复 | 名字叫「两行说同一件事（已完成 + 未验证生效）」的用例 |\n"),
    ("synth 正文括号引用用例名",
     "**证据**：L2 动态\n**风险级别**：中\n\n"
     "本轮修复了一个误报：把（已完成 + 未验证生效）这种用例名当成了自相矛盾。\n"),
]
for _name, _msg in _UNRESOLVED:
    _c, _ = run(_msg)
    print("  [KNOWN-UNRESOLVED] %-42s exit=%d  %s"
          % (_name, _c, "仍被误拦" if _c == 2 else "已放行"))
print("  → 以上 %d 条不计入通过数；状态=未解决。" % len(_UNRESOLVED))

print()
print("=" * 64)
if _fails:
    print("结果：失败 %d / 共 %d 项" % (len(_fails), _n))
    for f in _fails:
        print("  FAIL", f)
    sys.exit(1)
print("结果：%d / %d 全部通过" % (_n, _n))
