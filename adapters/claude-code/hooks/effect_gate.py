# -*- coding: utf-8 -*-
"""
G3 生效门 · 执行端（Stop；SubagentStop【未注册】）

⚠️ N1（审计 2026-09-29，如实标注）：本文件头此前写「Stop / SubagentStop」，
   但 settings.json 的注册里【只有 Stop】—— 子代理结束时本门并不运行
   （实测注册表无 SubagentStop 条目）。这是本项目最反对的
   「声明与行为不一致」形状。本次【不改注册】—— 是否要对子代理收尾
   也做证据检查是行为决策（涉及每个子代理都要跑一次评审），需单独评估；
   先让注释与事实一致。要启用：在 settings 的 hooks 里加 SubagentStop 段。

⚠️ 时序说明（必须知道）：这是【后置校验】，不是前置门。
   AI 生成回复 → 说出"完成了" → 本 hook 触发 → exit 2 打回 → AI 重新生成。
   它不能"阻止它说完成"，只能"说了之后打回重说"。
   平台目前没有 PreClaim 这类前置 hook，这是 G3 能达到的最强形态。

⚠️ schema 陷阱：Stop 家族用 {"decision":"block","reason":...}，
   不是 PreToolUse 的 permissionDecision。写错 = 门失效（见 _lib.deny_stop 注释）。

⚠️ 能力的诚实边界：本门能检测"有没有证据块"，
   无法检测"证据指向的对象是否正确"。
   而审计里 deepseek 的核心毛病恰恰是后者（验证了"我执行了"而非"它生效了"）。
   所以这道门降低误报率，但不消灭它。真正兜底的是 Stop hook 的 8 次封顶之后的
   ——说白了，人还是最后一道防线。
"""

import bisect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import (  # noqa: E402
    read_input, deny_stop, allow, load_policy, warn_inactive,
    record_gate_event,
)


# 完成类措辞。
# ⚠️ 这张表要覆盖【所有】完成语义，不能只覆盖"修复"。
# 实测漏过一次：「已部署」不在表里 → 高风险任务宣布部署完成时静默放行。
#
# ⚠️ 词表的固有上限（3.5.9 补充说明，见 #45）：
#    外部复核实测 10 条常见收尾，其中 6 条漏网（「全部搞定，收工。」「任务结束。」
#    「搞好了。」「弄好了。」「写好了。」「做好了。」等）。已补进下表。
#    但要诚实：中文的表达空间是开放的，【词表永远列不全】。
#    这条边界必须在文档里说清，而不是靠不断加词来假装解决。
#    真正能兜住的是：证据块要求（结构性）+ 人（最后一道防线）。
#
# ⚠️ 两个方向的误判都实测确认（3.5.13 外部复核 + 本机坐实）：
#
#    漏报 80%（12/15）：表里【没有】这些常见完成语义 ——
#        「已经完成 / 全部完成 / 已经解决 / 已经通过 / 上线了 /
#          搞定 / 任务完成 / 修复完毕 / 都做完了 / 已确认生效」
#      → 走 :261 `if not claim: allow()` 【静默放行，无任何痕迹】
#
#    误报 59%（17/29）：纯子串匹配无否定处理 ——
#        「我不认为它已修复」「不要写已完成这种话」「计划是修复完成后再通知」
#        「已修复 9 项」（在报统计数字）
#      → 被当成完成宣言拦下（本会话真实发生 3 次）
#
#    ★ 关键不对称：误报会打回、可见；漏报是静默的。
#      所以这里的取舍应当是【宁可多拦一点】，而不是逐词打补丁。
#      真正的解法在结构（证据块），不在词表 —— 词表只负责"要不要启动这套判定"。
#
# 已知的语义混用（V1 复核指出，尚未修，登记于此）：
#    CLAIM_RX（本表，主触发）与 GOAL_CLAIM_RX（矛盾检查）是【两份清单】，
#    有 10 个词只在 CLAIM_RX 里（已落盘/弄好了/写好了/收工/一切正常…），
#    导致「已落盘，未验证。」逃过矛盾检查。
#    且「已写入 / 已提交」同时在 ACTION_ONLY_CLAIM_RX 与 CLAIM_RX 里 ——
#    但 ACTION_ONLY_CLAIM_RX 是【未接线】的（见其定义处的说明），
#    所以"允许与未验证共存"这条声明对那两个词并未生效，区分结果自相矛盾。
#    这条需要先定"门槛判据到底是哪张表"，不宜在本次顺带改。
# ⚠️ 3.5.17：「收工」必须处在【句末位置】才算完成宣言。
#
#    实测误报（本机复现，用户报的三处之三）：
#        「我先查看验收工具的目录。」           → 「验【收工】具」被判为完成宣言
#        「本轮只整理验收工具，不修改产品代码。」 → 同上
#    后果：整条消息被要求提供完成证据，而它一个字都没宣布完成 ——
#    谈的是【验收工具】这个名词（一个待整理的东西），不是"我收工了"。
#
#    真宣言与复合词的区别在【右边】：
#      真宣言：「…，收工。」「可以收工了」「收工！」「今天到此收工」
#      复合词：验收工具 / 验收工作 / 回收工艺 / 税收工作 / 接收工作
#    判据：收工 后面必须是【句末标点 / 常见语气助词 / 收尾符号 / 行尾】。
#
#    ⚠️ 刻意【不】在左边做文章 —— 「验收工作」前面是「验」，
#       而「今天到此收工」前面也是实词，左边分不出来。
#    ⚠️ 也刻意【不】写成"含验收工具的整条消息一律豁免" ——
#       「验收工具已经修复完成。」仍须正常拦（回归 B17 守住）。
_CLAIM_END_AFTER_RX = (
    r"(?=[。.！!？?，,；;、：:…~」』\"'*）)\s]|了|啦|吧|咯|喽|哟|喔|嘞|$)")

CLAIM_RX = re.compile(
    r"(已修复|已经修复|修复完成|已改好|改好了|修好了|已完成|完成了|写好了|做好了"
    r"|已解决|解决了|问题没有了|没问题了|已弄好|弄好了|弄完了|弄完"
    r"|验证通过|已验证|测试通过|全部通过|已通过|已跑通|跑过了"
    r"|已实现|搞定了|全部搞定|搞好了|已生效|现在可以了"
    r"|已部署|部署完成|已上线|上线完成|已发布|已提交|已推送|已合并|已落盘|已写入"
    r"|收工" + _CLAIM_END_AFTER_RX + r"|任务结束|工作结束|到此结束|可以了"
    r"|一切正常|一切就绪|都正常|全部正常"
    # --- 3.5.13 补漏报（外部复核实测的 10 条兄弟形式）---
    # 这些都是上表已有词的【常见变体】，此前一个都没收。
    #
    # ⚠️ 刻意【不】收「完成」「完毕」「搞定」这类【超短/单字】形式：
    #    它们会匹配「还没完成」「无法完成」「未完毕」——
    #    那正是本表 B2 一侧要防的误报，加进来等于用一个新误报换一个旧漏报。
    #    要加就加【自带主语/时态、语义无歧义】的完整形式。
    r"|已经完成|已经全部完成|全部完成|已经解决|已经通过|已经验证|已经实现"
    # ⚠️ 审计修复（2026-09-29）：「准备就绪」从本表【移除】。
    #    实测误拦（本会话真实发生，已最小化复现）：进度汇报
    #        「3 路子代理并行审计中…准备就绪，等它们的报告。」
    #    被当完成宣言打回（rc=2）。该词的字面语义是【准备工作完成】（过程），
    #    不是【目标达成】—— 按本表自己的准入标准（"自带主语/时态、
    #    语义无歧义"）它不达标。保留同族「已就绪 / 一切就绪」（更接近真宣言）。
    r"|上线了|部署完成并上线|修复完毕|已确认生效|已就绪"
    r"|都做完了|已经做完|可以交付了|不报错了|问题已处理)"
)

# 证据块标题。必须容忍 markdown 粗体 —— `**证据**：` 是最常见的写法。
EVIDENCE_RX = re.compile(
    r"(证据|证据级别|生效验证|EVIDENCE)\s*\**\s*[:：]", re.I)

# 「未验证」类声明的词表。只有这一处定义，判定与分支共用 ——
# 两处各写一份正则，历史上就出现过"改了一处漏了另一处"。
UNVERIFIED_RX = re.compile(
    r"\bNOT_MEASURED\b|\bNOT_VERIFIED\b|未验证|未实测|未做验证|没有验证", re.I)

# ⚠️ 光秃秃的「未验证」不算声明（实测缺陷：一个词就能通关）。
#    原判定只要消息里任何地方出现"未验证"就进豁免分支，于是
#        「已完成。」 + 空行 + 「未验证。」
#    能直接绕过本门 —— 两段各自都不同时含两个词，判定就放行。
#    而「## 未验证项」「- MCP matcher 未验证」这类【带主语】的声明是有效信息，必须保留。
#    判据：整行除了标记本身只剩列表/标题符号和句末标点 = 无主语 = 免责声明，不算声明。
BARE_UNVERIFIED_RX = re.compile(
    r"^[\s\-*•>#]*(\bNOT_MEASURED\b|\bNOT_VERIFIED\b|未验证|未实测|未做验证|没有验证)"
    r"[。.；;，,、!！\s]*$", re.I)

# ⚠️ 「未验证项：无」是【否定】不是【声明】（3.5.9 新增，见 #43）。
#
#    实测缺陷（外部复核发现，坐实）：
#        G3 被拦时打印的模板要求写这一行 ——
#            - 未验证项：<明确列出；没有就写「无」>
#        而当用户真的照写「- 未验证项：无」时，它被当成【承认有未验证】，
#        走进 declared_unverified 豁免分支，于是：
#             风险=高 + 证据=L1（按门规则应当拦）
#             → 只要写「- 未验证项：无」就【放行】
#        换成「- 遗留事项：无」则正常拦截。
#
#    即：门自己要求的字段名，成了绕过门检查的钥匙。
#    这比"漏判"更糟 —— 它奖励照做的人，惩罚不照做的人。
#
#    判据：字段名 + 冒号 + 空值（无 / 没有 / 无此项 / none / - / 空）
#          = 该行陈述的是「没有未验证项」，不构成"承认有未验证"。
NEGATED_UNVERIFIED_RX = re.compile(
    # ⚠️ 审计修复（2026-09-29，五维验证 X8）：去掉【行首锚定】。
    #    修前 `^[\s\-*•>#]*` 要求字段名在行首（允许列表/标题符号前缀），
    #    于是「已完成，**未验证项**：无」（字段在【行中】）失配 →
    #    被判"承认有未验证" → 与完成宣言判成自相矛盾 → 误拦（实测 rc=2；
    #    同内容把字段挪到独占一行 → rc=0）—— 判定随排版而变。
    #    修法：行内匹配（调用处 .match → .search）。
    #    BARE_UNVERIFIED_RX 保持"独占行"语义不变（两处职责分离）。
    r"[\s\-*•>#]*(\b未验证项\b|\b未验证内容\b|\b未验证的?\b|未实测项|"
    r"unverified(\s+items?)?|not\s+verified\s+items?)"
    # ⚠️ 审计修复（2026-09-29）：字段名后必须容忍 markdown 粗体闭合（`**`）。
    #    修前 `\s*[:：]` 直接跟在字段名后，而「**未验证项**：无」在
    #    「未验证项」与「：」之间夹一个 `**` → 失配 → 这一整行被当成
    #    「承认有未验证」→ 与完成宣言判成自相矛盾 → 误拦（实测 rc=2；
    #    同内容列表项写法 `- 未验证项：无` rc=0 —— 判定随排版而变）。
    #    本门模板自己推广的写法就是粗体（`**未验证项**`），照写的用户被罚。
    # ⚠️ 审计修复（2026-09-29，批 2 三验 FAIL 3/4）：容忍【句末标点】。
    #    修前 `…(无|没有|…|\s)*$` 不认「：无。」—— 句号让 $ 失配 →
    #    该行被判"承认有未验证" → 与完成宣言判成自相矛盾 → 误拦。
    #    实测（旧版/新版一致，属既有缺口）：`**未验证项**：无。` rc=2，
    #    去掉句号 rc=0 —— 判定随句末标点而变（#43/X8 同族：排版敏感）。
    #    正负向裁定（技术总监，2026-09-29）：**正向** —— 消除误拦；
    #    误伤面核查：`未验证项：没有测试`（"没有"后接实词）/`未验证项：无需处理`
    #    仍失配（`$` 前有实词）→ 不会把真值误判成"无"。
    r"[ \t]*\**[ \t]*[:：][ \t]*\**[ \t]*(无|没有|无此项|暂无|none|n/?a|-|—|\s)*"
    r"[。.；;!！，,]*$",
    re.I)

# 证据级别。
#
# ⚠️ 不能用 `\b` 做边界（3.5.13 修，外部复核 + 本机实测坐实）。
#
#    实测缺陷：
#        '证据：L3 端到端'  -> L3      可识别
#        '证据：L3端到端'   -> ★None   失配（合规写法被判"未标注级别"）
#        '正文L3检查'       -> ★None   失配
#
#    根因：Python3 的 \w 含 CJK，所以「L3端」里 3 和 端 之间没有词边界。
#    失配面是【L3 紧贴 \w 字符（CJK/字母/数字）】这一种；
#    标点邻接（`L3。` `（L3）` `L3,`）仍能匹配。
#
#    后果比"误拦"更坏：门报的是「你给的证据级别：未标注」——
#    而模型明明写了 L3。错误信息指向错误原因，模型会去补一个
#    已经写好的标签 → 很可能原样再被拦 → 撞到平台 8 次封顶。
#
#    修法：边界改成【排除 ASCII 标识符字符】的否定断言，
#    与 _lib.py 的 BUDGET_LINE 同根因、同修法（那边先修的）。
#    ⚠️ 测试为什么全绿：four_gate_selftest 的 _tmpl 与所有调用点、
#    adversarial_test 的用例，全部用【带空格】写法（`L3 端到端`）→ 零覆盖。
# ⚠️ 3.5.16：下面这个 LEVEL_RX 【已被取代】，保留说明以防历史变不可见。
#
#    它的问题：NOT_MEASURED 那一支【既无边界约束、也无位置约束】，
#    而 analyze() 用 search() 取全文第一个命中 ——
#    于是"提到 NOT_MEASURED"（而不是"声明级别是 NOT_MEASURED"）也会被当级别。
#
#    实测两个真实误报（本机 2026-09-26 两条真实收尾消息，已存为回归样本）：
#      ① 状态词枚举的表格单元：
#         | 语义翻译任务合同 + 状态词表（…/SUPERSEDED/NOT_MEASURED） | …
#      ② 章节标题：## 四、仍标 NOT_MEASURED
#      两条消息都明确写了 **证据**：L1 / L2，却被读成"级别 = NOT_MEASURED"→ 打回。
#
#    取代它的是 EVIDENCE_LEVEL_RX + LEVEL_FALLBACK_RX（见下），
#    由 extract_level() 按【声明位优先】取级别。
# LEVEL_RX = re.compile(
#     r"(?<![A-Za-z0-9_])(L3|L2|L1)(?![A-Za-z0-9_])|NOT_MEASURED", re.I)

# 级别【声明位】：同一行内「证据 / 证据级别 / EVIDENCE + 分隔符 + 级别」。
# 这正是本门模板自己要求的写法（**证据**：L3 端到端），所以按它取最准。
EVIDENCE_LEVEL_RX = re.compile(
    r"(证据|证据级别|EVIDENCE)[ \t]*\**[ \t]*[:：][ \t]*\**[ \t]*"
    r"(L3|L2|L1|NOT_MEASURED)(?![A-Za-z0-9_])", re.I)

# 兜底：只认 L1/L2/L3。NOT_MEASURED 【不参与兜底】——
# 它只有出现在声明位上才算"本轮声明的级别"，否则只是"被提到"。
LEVEL_FALLBACK_RX = re.compile(
    r"(?<![A-Za-z0-9_])(L3|L2|L1)(?![A-Za-z0-9_])", re.I)


def _lowest_asserted_level(text):
    """取【不在否定/待办/条件语境】里的所有 L1/L2/L3 中【最低】的一个。

    ⚠️ 为什么兜底也要过滤（用户独立复验发现，本机实测复现）：
        已修复。本轮只做了 L1 静态检查。

        **证据**：尚未达到 L3 端到端
        **风险级别**：高
      旧版取全文第一个 L -> L1 -> 高风险需 L3 -> 正确拦下(2)。
      若声明位规则只看"行内有 L"而不要求紧邻，"尚未达到 L3"会被当成
      已声明 L3 -> 放行(0) —— 漏拦。
      故：① 声明位要求级别【紧跟在分隔符之后】；
          ② 兜底路径也要跳过否定/待办语境里的等级（如「还没做 L3」）。
      这两条一起，才堵住"把否定/待办里的等级当成有效证据"。

    ⚠️ 审计修复（2026-09-29）：兜底从【取第一个】改为【取最低】。
        实测漏拦（B 组审计 t11 用例，本机独立复现）：
            已完成。门要求高风险要 L3，本轮只做了 L1。
            **证据**：
            - …（声明位无紧邻级别）
            **风险级别**：高
        → 取第一个 L 得 L3（那句"门要求…L3"）→ 高风险放行(0)。
        同内容把 L3 句挪到后面 → 拦(2)：即【语序决定结论】，
        且"复述门自己的规则文本"即可把级别洗白到 L3。
        取最低后：{L3, L1} -> L1 -> 拦；洗白方向被消除
        （与 :332 引用的既定取舍"宁可多拦"一致；声明位路径不受影响 ——
        按模板写 `**证据**：L3` 的收尾仍走声明位取 L3）。
        代价（如实登记）：无声明位时，"真做了 L3 但文中也提到 L1"
        会被按 L1 从严 —— 属误报侧、可见、可改为按模板写声明位。
    """
    best = None
    bounds = _clause_bounds(text)
    neg_spans = _negation_spans(NEGATED_LEVEL_CTX, text)
    for m in LEVEL_FALLBACK_RX.finditer(text):
        seg_start = _clause_start(bounds, m.start())
        # ⚠️ X9（批 2 三验）：从"逐次线性 search/切片"改为"预计算 + 二分"。
        #    曾试过 search(text, pos, endpos)（避免切片）——切片没了，但每次
        #    搜索仍从 pos 扫到 endpos，Θ(n²) 不变（实测 19.57s → 19.55s）。
        if _last_negation_in(neg_spans, seg_start, m.start()) is not None:
            continue
        lv = m.group(1).upper()
        if best is None or LEVEL_NUM[lv] < LEVEL_NUM[best]:
            best = lv
    return best


def extract_level(msg):
    """取【声明】的证据级别。返回 "L1"/"L2"/"L3"/"NOT_MEASURED"/None。

    判据（3.5.16，经一轮返工后定稿）：
      ① 声明位：`证据/证据级别/EVIDENCE + 分隔符` 之后【紧接着】就是级别。
         这正是本门模板自己要求的写法（**证据**：L3 端到端）。
         —— 要求"紧接着"而不是"行内 20 字内"，是为了不把
            「**证据**：尚未达到 L3 端到端」这种【否定里的等级】当成声明。
      ② 兜底：全文第一个【非否定/待办语境】的 L1/L2/L3。
         NOT_MEASURED 不参与兜底（它只有出现在声明位上才算声明）。

    ⚠️ 为什么不是「取证据块【之后】的第一个 L 标签」（3.5.13 试过并回退）：
       那条规则会扫到声明【之后】的任意文本，于是
           「**证据**：\n- 待办：还没做 L3 端到端」
       被从 L1 洗白成 L3 —— 复述门自己的规则文本即可通关。
       本修法只看【声明行本身】+ 兜底过否定，那个绕过不成立。

    ⚠️ 已知边界（如实登记，不隐藏）：
       若模型在声明位【紧邻处】写一个高于实际的级别，本门无法识别 ——
       这与"门不能校验证据真伪"是同一条既有边界，不是本次引入的。
    """
    m = EVIDENCE_LEVEL_RX.search(msg)
    if m:
        return m.group(2).upper()
    return _lowest_asserted_level(msg)


# ⚠️ 3.5.16 追加修正（用户独立复验发现，本机复现）：声明位匹配【不得跨越换行】。
#
#    实测漏拦：
#        已修复。本轮只做了 L1 静态检查。
#
#        **证据**：
#        L3 是门对高风险任务的要求，不是本轮证据。
#        **风险级别**：高
#    修前 EVIDENCE_LEVEL_RX 在 `：` 之后用 `\s*`，而 `\s` 含换行 →
#    把下一行的「L3 是门对高风险任务的要求」读成了【本轮声明 L3】→ 高风险放行(0)。
#    旧版取全文第一个 L 得 L1 → 正确拦下(2)。
#
#    修法：`：` 两侧的空隙改用 `[ \t]*`（只吃空格与制表符，不吃换行）。
#
#    ⚠️ 同时核过【兜底路径】不会再捡起下一行的无关等级：
#      该输入里兜底取到的是「本轮只做了 L1 静态检查」的 L1（非否定语境）→ 仍需 L3 → 拦。
#      即声明位被修掉之后，兜底并没有把那一行的 L3 捡回来。

# 风险级别声明
#
# ⚠️ 审计修复（2026-09-29）：空隙改 [ \t]*（只吃空格与制表符，不吃换行）。
#    与 EVIDENCE_LEVEL_RX 在 3.5.16 的修法对齐 —— 当时只修了级别那一处，
#    本条漏改（同形状缺陷只修一侧，本项目 #19/#38 的反复形状）。
#    实测：「**风险级别**：\n低于预期…」被读成"低"→ 高风险漏拦(0)；
#    倒过来「…\n高风险这一点…」被读成"高"→ 误拦(2)。
RISK_RX = re.compile(
    r"风险级别[ \t]*\**[ \t]*[:：][ \t]*\**[ \t]*(低|中|高|低风险|中风险|高风险)", re.I)

# 完成词里【明确表示"还没做到"】的那几个。
# 「已改完」「已修改」「已落地」说的是"动作做完了"，
# 不是"目标达成了" —— 这类才允许和"未验证"共存。
#
# ⚠️⚠️ 3.5.14 如实声明：本常量【定义了但未接线】，全仓零引用。
#
#    它的设计意图（动作类 vs 目标达成）是真的，但**从未实现**：
#      · `main()` 的主触发用的是 CLAIM_RX，不是本表
#      · 「已改完 / 已修改」当前能放行，靠的是它们【恰好不在
#        CLAIM_RX 里】—— 而非按本表的设计区分
#      · 反例：`已写入` / `已提交` 同时在本表与 CLAIM_RX 里，
#        于是「已写入，未验证生效。」被当【目标达成】拦下，
#        与本表声明的"允许与未验证共存"**直接矛盾**
#
#    为什么本轮【不接线】（外部复核 V1 的意见，已采纳）：
#      接线需要先决定一个更大的问题 —— 门槛判据到底用 CLAIM_RX
#      还是 GOAL_CLAIM_RX（两者是两份清单，10 个词只在前者里）。
#      在没定这件事之前接线，等于把设计问题掩盖成一个补丁。
#      这正是本项目反复强调的：先定判据，再改实现。
#
#    所以这里保留定义 + 如实标注状态，而不是删掉 —— 删掉会让
#    "这个未解决的设计问题"变成不可见（下一位改这里的人只会
#    看到一个空缺，不知道曾经有过这个意图）。
ACTION_ONLY_CLAIM_RX = re.compile(r"(已改完|已修改|已改动|已落地|已写入|已提交|已保存|已生成|已创建)")

# 「已完成/已完成部署/已发布」这类说的是【目标达成】，与"未验证"互斥。
GOAL_CLAIM_RX = re.compile(
    r"(已修复|已经修复|修复完成|已改好|改好了|已完成|完成了|已解决|解决了"
    r"|验证通过|已验证|测试通过|全部通过|已通过|已跑通"
    r"|已实现|搞定了|没问题了|已生效|现在可以了"
    r"|已部署|部署完成|已上线|上线完成|已发布|已合并|已推送)")

# 各风险等级要求的最低证据级别
REQUIRED = {
    "低": 1,
    "中": 2,
    "高": 3,
}
LEVEL_NUM = {"L1": 1, "L2": 2, "L3": 3}


def analyze(msg):
    """返回 (claim, level, level_num, risk)

    ⚠️ 级别取值（3.5.16 起）：【声明位优先】——
       ① 先在「证据/证据级别/EVIDENCE + 分隔符」的同一行内取级别
       ② 取不到时，兜底取全文第一个 L1/L2/L3（NOT_MEASURED 不参与兜底）
       见 extract_level()。此前的行为是"全文第一个 L 标签（含 NOT_MEASURED）"，
       它在两个真实场景里把"提到 NOT_MEASURED"误读成"声明级别"。

    【已尝试并回退的改动，3.5.13】
      曾改成「优先取证据块【之后】的第一个 L 标签」，想修这个真实问题：
        "已完成。过程中先做了 L1 静态检查，后来补了 L3。\\n\\n**证据**：L3 端到端"
        -> 取到 L1，被判"级别不足"（诚实描述过程反被罚）

      **回退了**，因为独立核验实测发现它引入了一个更严重的绕过：
        只要证据块【本体不含 L 标签】，而证据块【之后】出现任意一个
        语义无关的 "L3"，级别就被从 L1 洗白到 L3 ——
            "已完成。本轮只做了 L1。\\n\\n**证据**：\\n- 待办：还没做 L3 端到端\\n\\n**风险级别**：高"
            -> 旧版拦（正确）；改后【放行】（错误）
            "…**证据**：\\n- 门规定高风险要 L3 端到端"  -> 同样放行
        即【复述门自己的规则文本】即可通关。

      按本文件 CLAIM_RX 注释自己写明的取舍：
      「误报会打回、可见；漏报是静默的。所以这里的取舍应当是【宁可多拦一点】」
      —— 用 8 次过度拦截换 5 条静默漏报，方向与本项目既定原则相反。

      **所以这里维持原状（取全文第一个 L）。** 原缺陷（诚实描述过程被罚）
      保留为已知边界 —— 它是"误报"侧，可见、可绕过（把过程描述放到证据块
      之后或省略），代价低于它引入的静默漏报。
      真正的解法需要"哪个 L 才是声明级别"的语义判据，不是挪边界。
    """
    claim = CLAIM_RX.search(msg)
    level = extract_level(msg)      # 3.5.16：声明位优先，见 extract_level()
    if level == "NOT_MEASURED":
        level_num = 0
    else:
        level_num = LEVEL_NUM.get(level, None)
    risk_m = RISK_RX.search(msg)
    risk = risk_m.group(1).replace("风险", "") if risk_m else None
    return claim, level, level_num, risk


# 否定 / 未来 / 条件语境（3.5.13 补，B2 误报一侧）。
#
# 实测误报（三源确认，本会话真实发生 3 次）：
#     「我不认为它已修复」          ← 明确否定
#     「不要写已完成这种话」         ← 禁止语境
#     「计划是修复完成后再通知」      ← 未来计划
#     「已修复 9 项」（在报统计数字）  ← 陈述统计，非宣布完成
#
# 判据：完成词【前面 12 字内】出现否定/未来/条件词 → 不算"宣布完成"。
#
# ⚠️ 刻意【不】复用 _lib 的 DISCUSSION_CTX：
#    它含「报告 / 文档 / 说明 / 参考 / 建议」——那些在【报告工作】时
#    会合法出现（"我写了报告，已完成"），用在 G3 会把真宣言也剥掉。
#    G1 用它是对的（预算声明确实不该出现在"文档/说明"里），场景不同。
# ⚠️ 审计修复（2026-09-29，批 2 三验新发现，技术总监裁定：正向修复）：
#    「计划」本意是标记【未来语态】("计划是修复完成后再通知")，但它是
#    兼类词 —— 作【名词主语】时是宣布，不是未来语境：
#        「还没看，但计划已完成了。」
#    （3.5.13 列出的 5 个误放行案例之一，当年未修干净；实测旧版/新版一致 rc=0）
#    修法：只保留未来语态用法 ——「计划」后必须紧跟 是/要/将/会。
#    代价（如实登记）："计划完成后通知"这类省略句会从"静默漏拦"转为
#    "可见误拦"——方向与项目「宁可多拦（可见）/不可静默漏报」的取舍一致。
_NEG_BASE = (r"(不认为|不觉得|算不上|谈不上|不要|别写|别说|不能写|"
             r"计划(?=[是要将会])|打算|将要|即将|如果|假如|一旦|"
             r"尚未|还没|没有|无法|未能|不应该|不用)")

# 声明丢弃路径专用（_drop_negated_claims）：基础词表 —— 刻意【不含】「未/没+动词」。
NEGATED_CLAIM_CTX = re.compile(_NEG_BASE)

# ⚠️ 审计修复（2026-09-29，三遍交叉验证后实施）：级别判定路径专用（_lowest_asserted_level）。
#
#    背景：B 组审计为修「否认达到 L3 反被读成声明了 L3」，把「未/没 + 动词」
#    整类加进了 NEGATED_CLAIM_CTX —— 但该常量有【两个】消费者：
#      · _lowest_asserted_level()（:251 附近）—— 级别判定的语境过滤
#        （修法目标，需要新词：「未达到 L3」里的 L3 不算声明级别）
#      · _drop_negated_claims()（:499 附近）—— 决定"这条完成宣言算不算宣布完成"，
#        返回空即 allow()，无 stderr、无 gate 事件
#    实测漏拦（correctness reviewer，独立验证批 confirmed）：
#        「未通过的用例已修复。」「之前未完成的验收已全部通过。」「没做的两项已经修复。」
#        → rc=0（应拦）——中文「未X的Y已Z」是同小句内的常规句式（否定修饰宾语、
#        断言在后），G3 的证据块/级别/矛盾三条判定全部被跳过。
#    修法：拆成两份常量各司其职（基础版=声明丢弃；扩展版=级别判定）。
#    双向验证（v7 三验脚本）：E2 目标（未达到/未做/没做/不到 L3）仍 rc=2；
#    「未X的Y已Z」恢复 rc=2；合规收尾不受影响。
NEGATED_LEVEL_CTX = re.compile(
    _NEG_BASE[:-1] + r"|未达到|未做|未完成|未实现|未通过|未覆盖|未接入|不具备|"
    r"达不到|没做|没达到|没完成|没能|"
    r"(达到|验证|做|测|跑|检查|看|找)不到)")


# ⚠️⚠️ 3.5.16 一轮【已撤回】的尝试（保留说明，避免同一个坑再踩一次）
#
#    曾在此加过一个 NON_ASSERTIVE_CLAIM_CTX 词表，想把
#    「历史陈述 / 分类标签」从完成宣言里剔除。**已整体撤除。**
#
#    撤除原因（用户独立复验发现，本机坐实，两条都实测复现）：
#        「本轮判定为已完成。」   -> 旧版拦(2)，加了词表后【放行(0)】  ✗漏拦
#        「此前的问题现已修复。」 -> 旧版拦(2)，加了词表后【放行(0)】  ✗漏拦
#      根因：这些词【既能修饰完成声明本身，也能修饰声明的宾语】——
#        「此前的问题」是宾语，「现已修复」才是断言。词表分不出来。
#      结论：**词表在这个位置无法可靠工作**，任何"通用豁免"都会开出一个漏拦口。
#
#    那 C-01 那个真实误报（「M3 拆成**功能验证通过**与**协议执行失败**两个结论」
#    被当成完成宣言）怎么办？——**不需要词表**。
#    它真正的成因是【矛盾判定把两个不同的列表项当成了一件事】，
#    已由下面的 _iter_items() 按【事项】切分解决。修对了根因，就不需要补丁。


# 从句边界：否定词的作用域【止于】这些标点/连词。
#
# ⚠️ 为什么必须有它（3.5.13 修，独立核验实测坐实）：
#    初版用「完成词前 12 字内」做判据，没有从句概念 —— 实测 5/9 误放行：
#        「不用管了，任务完成了。」      before 含"不用" -> 误剔 -> 静默放行
#        「没有异常，一切正常。」        "没有异常"是在报【好消息】
#        「无法复现，已解决。」          否定在前半句
#        「还没看，但计划已完成了。」
#        「如果没有其他问题的话，已完成。」
#    这些否定词的作用域【止于逗号】，与后半句的完成宣言毫无关系。
#    12 是魔法常数、无语言学依据，且「没有/无法/还没」在中文里
#    高频用于【非否定】语境（"没有异常"）。
#    误放行的门 = 门不存在，比误拦更危险（见 CLAIM_RX 注释的取舍原则）。
CLAUSE_BOUNDARY = re.compile(r"[，,。.；;：:！!？?、\n]|但|不过|然而|可是|所以|因此")


def _clause_bounds(text):
    """预计算子句边界终点列表（升序）。一次 O(n)。

    ⚠️ 审计修复（2026-09-29，五维验证 X9）：原 `_clause_start(text,pos)` 每次
    都从 0 扫到 pos，而 `_drop_negated_claims`（每个完成词命中一次）与
    `_lowest_asserted_level`（每个级别命中一次）都要查 —— 合计 Θ(n²)。
    实测 padding 攻击（红队）：40000 个「已完成。」> 60s 被子进程杀死；
    宿主 Stop timeout 30s → 杀 hook = 无 deny、无 stderr = **门静默不执行**
    （本项目定义的最严重缺陷形状）。修法：一次建表 + 二分查询。
    """
    return [b.end() for b in CLAUSE_BOUNDARY.finditer(text)]


def _negation_spans(rx, text):
    """预计算某否定词表在全文的匹配区间（升序）。一次 O(n)。

    ⚠️ X9 第三轮（批 2 三验）：原逐次 `rx.search(text, seg_start, m.start())`
    在【无子句边界的长文本】里每次从 seg_start 扫到 endpos → 仍是 Θ(n²)。
    Profile 实测（12000×「L1 」，36000 字符）：12004 次 search 占 19.33s，
    其余全部函数合计 <0.2s —— 瓶颈就在这里。
    """
    return [(x.start(), x.end()) for x in rx.finditer(text)]


def _last_negation_in(spans, start, end):
    """spans 中落在 [start, end) 内的最后一个；没有则 None。O(log n)。

    "最后一个" = 离目标词最近的那个（_drop_negated_claims 的既有语义）。
    """
    j = bisect.bisect_left(spans, (end,)) - 1
    if j >= 0 and spans[j][0] >= start:
        return spans[j]
    return None


def _clause_start(bounds, pos):
    """pos 之前最后一个子句边界的终点；没有边界则 0。O(log n)。

    bounds 必须由 _clause_bounds(text) 预计算（升序、同一次文本）。
    语义与旧版一致：返回 end() 最大且 <= pos 的边界终点。
    """
    i = bisect.bisect_right(bounds, pos) - 1
    return bounds[i] if i >= 0 else 0


# 引用性定语（3.5.14 补，本机实测坐实 —— BG06 的第三个现场）。
#
# ⚠️ 问题：完成词被当【定语】修饰名词时，是在引用/对比，不是宣布达成。
#    实测四条全部误判为完成宣言：
#        「和我们已验证的「写在正文里照样复现」冲突」   ← 现场样本
#        「这与我们已验证的结论冲突」
#        「前面已验证过的那条规则，这里不适用」
#        「和已验证的行为对比」
#    `_strip_quoted_context` 挡不住 —— 这些完成词【没有任何引号/代码块标记】。
#
# 判据：完成词【紧接着】一个定语标记（的/过/了之后接名词性成分）→ 是定语用法。
#    具体看完成词【后面】紧跟的字符：
#      · "的" + 名词   → 定语（已验证的结论）      → 剔除
#      · "过" + "的"   → 定语（已验证过的规则）      → 剔除
#    而真断言通常是「已验证。」「已验证，」「已验证」句末 → 保留。
_ATTR_AFTER_RX = re.compile(r"\s*(过\s*的|的(?![，,。.；;：:！!？?]|$))")


# ---- 否定作用域的「封闭标记」（2026-09-29 五维验证 X7）----
#
# 借鉴 NegEx/ConText（临床 NLP 的否定检测）的结构：否定词的作用域应由
# 【触发词 + 作用域终止符】界定，而不是"所在小句内出现否定词就整条否定"。
# NegEx 的 pseudo_negation（假触发词）概念正对本处漏拦：
#     「无法复现的那条已修复」——「无法」修饰宾语「复现的那条」，
#     不作用于后面的断言「已修复」，但旧判据整条剔除 → 静默放行（rc=0）。
# 机械判据（不需要句法分析）：
#   · 否定词与完成词之间出现「的」→ 否定被锁在定语里（修饰宾语），
#     断言在后 → 不剔除；
#   · 否定词是条件词（如果/假如/…）且其后出现主句起首标记（那/则）
#     → 条件从句结束、主句开始 → 不剔除。
# 代价（如实登记）：含「的」的真否定句（「我不认为它的问题已修复」）
#   会被判成"不剔除"→ 误拦（可见、可纠正）；方向与项目
#   「宁可多拦（误报可见）/不可静默漏报」的既定取舍一致。
_SCOPE_CLOSER_RX = re.compile(r"的")
_COND_WORDS = ("如果", "假如", "一旦", "若", "要是")
_MAIN_CLAUSE_RX = re.compile(r"那|则")


def _negation_scope_closed(span, text, end):
    """否定词 span 的作用域是否在 [span.end, end) 之间被封闭。

    返回 True = 否定不作用于其后的完成词（否定修饰的是宾语/从句）。
    span = (start, end)（来自 _negation_spans）。
    ⚠️ 用 search(text, pos, endpos) 而不是先切片（见 X9 说明）。
    """
    s = span[1]
    if _SCOPE_CLOSER_RX.search(text, s, end):
        return True
    if text[span[0]:span[1]] in _COND_WORDS and _MAIN_CLAUSE_RX.search(text, s, end):
        return True
    return False


def _drop_negated_claims(text, rx):
    """剔除【处在否定/未来/条件语境】或【作定语引用】的完成词匹配。

    只保留"真的在宣布完成"的那些。若全被剔除 → 本门不适用（放行）。
    这是机械匹配的固有上限的缓解，不是根治 —— 根治靠证据块要求。

    判据（3.5.13 修正）：否定词必须与完成词【同处一个从句】才有效 ——
    即两者之间【没有】从句边界。这样「没有异常，一切正常」不会被误剔
    （逗号隔断了"没有"的作用域），而「我不认为它已修复」仍被正确剔除。

    判据（3.5.14 补充）：完成词后紧跟"的/过的"且后面还有实词 → 定语引用，
    不是断言（见 _ATTR_AFTER_RX 的说明）。

    判据（X7 修正，2026-09-29 五维验证）：同小句还不够 —— 否定词与完成词
    之间若出现作用域封闭标记（见 _negation_scope_closed），说明否定修饰的
    是宾语/从句，不作用于完成词。取【最后一个】否定词判断（最近的才相关）。
    """
    out = []
    bounds = _clause_bounds(text)
    neg_spans = _negation_spans(NEGATED_CLAIM_CTX, text)
    for m in rx.finditer(text):
        # 往前看一个从句（到最近的边界为止），在其中找否定词
        seg_start = _clause_start(bounds, m.start())
        neg = _last_negation_in(neg_spans, seg_start, m.start())
        if neg is not None and not _negation_scope_closed(neg, text, m.start()):
            continue
        # 往后看：紧跟定语标记且其后还有实词 → 可能是定语，不算宣布
        #
        # ⚠️⚠️ 3.5.14 第二轮修正（外部核验发现方向性错误）：
        #    只看后缀是【错的判据】—— 它分不清：
        #      定语引用："和已验证的结论冲突"        ← 该剔除
        #      名词化主语的真宣言："已修复的问题不会再出现了" ← 该保留！
        #    第一版按后缀一律剔除 → 实测 12/12 真宣言被【静默放行】
        #    （走 allow()，无证据块、无痕迹）。
        #
        #    而本文件 CLAIM_RX 注释自己写明：
        #      「误报会打回、可见；漏报是静默的 → 取舍应当【宁可多拦一点】」
        #    用静默漏报换可见误报 = 方向相反。
        #
        #    修法（外部复核建议）：改成【双条件】——
        #    必须【前面】有引用/对比标记（和/与/参考/按/对比/这与/前面…），
        #    才认为它是定语引用。纯后缀不足以判定。
        #
        #    ⚠️ 窗口取 40 而非 12：实测「的 + 多个空格 + 结论」会把
        #    12 字窗口填满，让 `的(?!...|$)` 的 `$` 误命中而整体失配。
        _REF_CTX = re.compile(
            r"(和|与|跟|同|参考|参照|按|根据|对比|比对|如同|类似|"
            r"这与|那与|前面|先前|此前|之前|上述|前述|已知)")
        after = text[m.end():m.end() + 40]
        am = _ATTR_AFTER_RX.match(after)
        # ⚠️ X9 第二轮：原 `before[-16:]` 须先构造 before（大切片）——
        #    改为带 pos/endpos 的 search，语义等价（before 的最后 16 字符）。
        if am and _REF_CTX.search(text, max(seg_start, m.start() - 16), m.start()):
            # 定语标记后面还有内容，说明它在修饰某物：
            #   · 实词（汉字/字母/数字）→ "已验证的结论"
            #   · 引号/书名号开头的引用    → "已验证的「写在正文里照样复现」"
            #     （本机实测的首个现场样本就是这个形态）
            #   ⚠️ 必须先跳过空白 —— 剥离器把「…」删成空格后，会留下
            #      "已验证的 冲突" 这种形态（实测踩到：不跳空白就漏判）。
            rest = after[am.end():].lstrip()
            if rest:
                tail = rest[0]
                if re.match(r"[\w一-龥]", tail) or tail in "「『“\"'（(":
                    continue
        out.append(m)
    return out


def _strip_quoted_context(text):
    """剔除【被提到】的文本，只留【被断言】的文本。

    为什么需要：门按字符串匹配，分不清 use 和 mention。
    我在报告里引用测试用例的字面内容（"分段的「已验证 + 未验证项」"），
    门看到两个词就判自相矛盾 —— 实测三种引用形式全部误报。

    剔除范围：
      - ``` 围栏代码块
      - `行内代码`
      - "双引号" / “中文双引号”
      - 「」『』/ 『』中文引号
      - > 块引用行

    刻意【不】剔除单引号 ' —— 它太常见（it's / 变量名），会误伤正文。
    """
    if not text:
        return ""
    t = text
    # ⚠️ 审计修复（2026-09-29，五维验证 X10）：与 _lib.strip_referenced_text
    #    同步扩范围（两份同形实现必须一起改，否则又是"只修一侧"）：
    #    `~~~` 围栏（CommonMark 合法围栏字符）/ HTML 注释 / 4 空格缩进代码块 /
    #    长引号去 200 上限。实测（红队）：`~~~\n已完成\n~~~`、`<!-- 已完成 -->`、
    #    210 字引号此前 rc=2 误拦（同内容放反引号里 rc=0）。
    t = re.sub(r"```.*?```", " ", t, flags=re.S)          # 围栏代码块
    t = re.sub(r"~~~.*?~~~", " ", t, flags=re.S)           # 波浪号围栏（X10）
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)          # HTML 注释（X10）
    t = re.sub(r"^[ \t]{4,}.*$", " ", t, flags=re.M)       # 缩进代码块（X10）
    t = re.sub(r"`[^`\n]*`", " ", t)                       # 行内代码
    t = re.sub(r"\"[^\"\n]*\"", " ", t)                    # 英文双引号（X10：去上限）
    t = re.sub(r"[“”][^“”\n]*[“”]", " ", t)                # 中文双引号（X10：去上限）
    t = re.sub(r"[「『][^」』\n]*[」』]", " ", t)            # 中文书名/引号（X10：去上限）
    # ⚠️ 3.5.16 第四轮：【曾加过"括号内一律按引用剥离"，已撤销】。
    #
    #    那一版的意图是让 real_03 的「（已完成 + 未验证生效）」被当成用例名。
    #    但它是一条【通用豁免】，代价是新增两条漏拦（用户独立复验，本机复现，
    #    旧版 exit=2 / 那一版 exit=0）：
    #        本次处理结果：（已修复）。
    #        本次修复已完成（但本次修复未验证生效）。
    #    两条都是【当前声明】，却因为落在括号里而被整段看不见。
    #
    #    → 结论：**括号同样不能作为"这是引用"的判据**，
    #      与"单元格位置不能作为判据"是同一个错误的两副面孔。
    #      「引用识别」在本门当前机制下【没有可靠判据】，因此不做 ——
    #      它的代价（real_03 仍会被误拦）如实登记为【已知未解决】，
    #      不用"新增漏拦"去换那个误报变绿。
    t = re.sub(r"^[ \t]*>.*$", " ", t, flags=re.M)         # 块引用行
    return t


_LIST_ITEM_RX = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")

# markdown 表格行（结构化数据，不是散文断言）
_TABLE_ROW_RX = re.compile(r"^\s*\|.*\|\s*$")


def _iter_items(text):
    """把消息切成【事项】，用于矛盾判定。

    为什么需要它（3.5.16 经一轮返工后定稿）：

      初版按【同段】判矛盾 —— 但 markdown 的连续列表项属于同一段，
      于是两个【不同事项】的条目被拼成"自相矛盾"：
          - §3：M3 拆成 功能验证通过 与 协议执行失败 两个结论
          - G3-FP-001：…未改 G3，未隐去未验证项——…
      这是真实误报（C-01，已存为回归样本）。

      第二版改按【同行】—— 又走过头，漏拦：
          「本次修复已完成。」
          「本次修复未验证生效。」
      这两行说的是【同一件事】，却被当成两个事项 → 放行。
      这是用户独立复验发现的漏拦（c1，已存为回归样本）。

    正确粒度 = 【事项】：
      · 列表项（- / * / + / 数字.）各自成一个事项
      · 列表项的【缩进续行】归属该列表项 —— 它们是同一件事的续写
      · 非列表的连续行合成一个事项（它们属于同一段话）

    于是：上面两行仍属同一事项（仍判矛盾）；
          C-01 的两个 `- ` 条目分属不同事项（不再误判）。

    ⚠️ 3.5.16 追加修正（用户独立复验发现，本机复现）：缩进续行必须归位。
        实测漏拦：
            - 本次修复已完成，
              但本次修复未验证生效。
        修前把第二行（缩进 2 空格）当成了【新事项】→ 两件事被拆开 → 放行(0)。
        旧版按同段判定 → 拦(2)。
        判据：markdown 里列表项的续行是【缩进】的；缩进即归属上一列表项。
    """
    for para in re.split(r"\n\s*\n", text):
        items = []                      # 每项是 list[str]
        for line in para.splitlines():
            if not line.strip():
                continue
            if _LIST_ITEM_RX.match(line):
                items.append([line])
            elif items and line[:1] in (" ", "\t") and _LIST_ITEM_RX.match(items[-1][0]):
                items[-1].append(line)      # 缩进续行 → 归属上一个列表项
            elif items and not _LIST_ITEM_RX.match(items[-1][0]):
                items[-1].append(line)      # 非列表段落的续行 → 合为一项
            else:
                items.append([line])
        for it in items:
            yield "\n".join(it)


def _pair_units(item):
    """把【事项】切成矛盾配对的判定单元：表格【每行】一个单元，其余整体一个单元。

    ⚠️ 表格在这里返工了三次，每次都是"用位置/排版当语义"，每次都错：

      第一版：表格行【整类排除】。
        —— 错。表格不等于引用。漏拦：
             | 本次修复 | 已完成 | 未验证生效 |

      第二版：按【单元格】判断，同格共现即视为复述而跳过。
        —— 也错。单元格位置同样证明不了引用。漏拦：
             | 本次修复 | 已完成，但本次修复未验证生效 |

      第三版：改用【括号】当引用标记（在 _strip_quoted_context 里剥离）。
        —— 仍然错，而且代价更大：新增两条漏拦
            本次处理结果：（已修复）。
            本次修复已完成（但本次修复未验证生效）。
        括号同样只是排版特征，不是语义判据。

      第四版（现在）：**不做引用识别**。本函数只负责切单元：
        表格每行一个单元、行内不按单元格切、非表格内容整体一个单元。

    ⚠️ 结论（如实登记，不用它换"全绿"）：
        「引用识别」在本门当前机制下【没有可靠判据】——
        位置（单元格）、标记（括号）都被证明不可靠。
        因此不做，代价是已存档的 real_03（表格里括号复述用例名）
        **仍会被误拦**，状态为【已知未解决】。
        不用"新增漏拦"去换那个误报变绿。
    """
    lines = [l for l in item.splitlines() if l.strip()]
    if not lines:
        return
    buf = []
    for l in lines:
        if _TABLE_ROW_RX.match(l):
            if buf:
                yield "\n".join(buf)
                buf = []
            # 每行一个判定单元 —— 【不按单元格切、也不因同格共现而跳过】。
            # 单元格位置证明不了内容是引用（见函数说明）。
            yield l
        else:
            buf.append(l)
    if buf:
        yield "\n".join(buf)


def _has_binding_unverified(text):
    """消息里是否存在【带主语】的未验证声明。

    为什么要单独判"带不带主语"：
        原判定只要消息里出现"未验证"就进豁免分支。于是
            「已完成。」 + 空行 + 「未验证。」
        绕过了本门 —— 两段各自都不同时含两个词，判定直接放行。
        而"## 未验证项""- MCP matcher 未验证"这类声明是要保留的：它说了是哪个东西没验证。

    判据：整行除了标记本身，只剩列表/标题符号和句末标点 = 无主语 = 免责声明。
    """
    for line in text.splitlines():
        if not UNVERIFIED_RX.search(line):
            continue
        if BARE_UNVERIFIED_RX.match(line.strip()):
            continue        # 光秃秃的免责声明，不构成声明
        if NEGATED_UNVERIFIED_RX.search(line.strip()):
            continue        # 「未验证项：无」= 否定，不是"承认有未验证"（#43）
                            # X8：search（行内位置）而非 match（行首）
        return True
    return False


# ---------------------------------------------------------------- 语义归属（本地候选 3.6.0）
#
# G3 的「理解层」接入点：**只做一件事** —— 判断含完成词的那一行是
# 【说话人自己的声明】还是【引用 / 用例名 / 反例名】。
#
# 为什么必须由模型做（记录在案的返工史）：
#   real_03 场景 —— 表格里
#       | 两行说同一件事（已完成 + 未验证生效） | 2 | 0 |
#   是那条【反例的名字】，不是收尾声明。为了把它和"真声明"分开，
#   这个门先后试过三种机械判据，全部被实测证伪：
#       ① 表格整类排除   → 漏拦跨单元格的状态声明
#       ② 按单元格判断   → 漏拦同单元格的状态声明
#       ③ 按括号剥离     → 漏拦括号里的当前声明
#   结论（记录原话）：**位置与标记都不是「引用」的可靠判据**。
#   这不是字符串匹配问题。
#
# 设计纪律：
#   · 程序只做【定位】（找出含完成词的行）—— 不判归属
#   · 模型只做【归属】—— 不做格式/级别判定（那是程序的事）
#   · 判为「引用」的行被剔除；判为「真声明」立即停止检查（要拦）
#   · 评审不可用 / 失败 → 一个字都不改，走原有逻辑（保守），并留痕
def _review_deadline(policy):
    """本次 Stop 事件用于【全部】语义评审调用的总时间预算，返回截止时间戳。

    用户要求：「对齐整次 G3 的总时间预算、单次请求预算和宿主 timeout。
    不只把所有超时调大，也不让超时变成静默放行。」
      · 总预算 = policy.semantic_review.total_budget_s（默认 20s）
      · 单次请求预算 = 总预算扣掉已用时间后的剩余（且不超过 timeout_s）
      · 宿主 timeout 必须 > 总预算（见 settings.fragment.json 的 Stop）
      · 预算耗尽 → 该次判断标为未裁决并【留痕】，不是静默放行
    """
    import time as _time
    sr = (policy or {}).get("semantic_review") or {}
    try:
        total = float(sr.get("total_budget_s") or 20.0)
    except Exception:
        total = 20.0
    return _time.time() + total


def _stmt_budget(policy):
    """单次评审请求预算（policy.semantic_review.timeout_s）。坏值回落 15.0。

    ⚠️ 审计修复（2026-09-29，#12）+ 简化审查提取：两个评审入口此前把 15.0
    硬编码在调用点，policy.semantic_review.timeout_s 在 G3 侧不生效 ——
    review_unit 只在调用方不传 timeout_s 时才回落到 policy（review_unit.py:481）。
    类型防御口径同 review_unit._call_and_parse（坏值不抛、回落默认）。
    """
    try:
        return float((policy.get("semantic_review") or {}).get("timeout_s") or 15.0)
    except (TypeError, ValueError):
        return 15.0


def _turn_had_tool_activity(transcript_path, max_lines=400):
    """本回合（最后一条【用户输入】之后）是否有工具调用。

    这是「任务状态」判据（用户要求用它来限定评审范围）：
    有过实质工作的回合才值得问模型"你是不是在宣布完成"；
    纯对话回合不评审，省掉一次调用。

    返回 True / False / None（None = 读不到 transcript，无法判断）。
    """
    import json as _json
    if not transcript_path:
        return None
    try:
        # ⚠️ 审计修复（2026-09-29，三遍交叉验证后实施）：只读文件尾部，
        #    不再 readlines() 整份 transcript（reliability reviewer #18）。
        #    修前：本函数在词表零命中时由入口 2 调用，调用点在 available()/
        #    deadline 判定之前（enabled=false 也照样整读）；长会话 transcript
        #    可达数十 MB —— 一次 Stop 付出秒级 IO + 大内存峰值，而 Stop 预算
        #    30s 中 20s 已留给语义评审，挤掉余量即被宿主杀掉（静默失效）。
        #    修法：seek 尾部定长（256KB）按行切、丢弃首个残行 —— O(400 行)；
        #    在尾部 256KB ≥ max_lines 行时与 readlines()[-max_lines:] 语义等价。
        _TAIL_BYTES = 262144
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            _size = f.tell()
            f.seek(max(0, _size - _TAIL_BYTES))
            _data = f.read()
        _text = _data.decode("utf-8", "replace")
        lines = _text.splitlines()
        if _size > _TAIL_BYTES and lines:
            lines = lines[1:]
        lines = lines[-max_lines:]
    except Exception:
        return None
    seen_tool = False
    for ln in reversed(lines):
        try:
            d = _json.loads(ln)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = msg.get("content")
        kinds = [b.get("type") for b in content if isinstance(b, dict)] \
            if isinstance(content, list) else []
        if t == "assistant" and "tool_use" in kinds:
            seen_tool = True
        elif t == "user":
            if "tool_result" in kinds:
                continue              # 工具结果回传，不是新的用户输入
            return seen_tool          # 到达本回合起点
    return seen_tool


class _SemanticClaim(object):
    """语义评审找到的完成声明，包装成与 re.Match 同形的对象。

    下游提示里用 claim.group(1) 取触发文本 —— 这里只需支持 group()。
    """

    def __init__(self, text):
        self._text = text

    def group(self, n=0):
        return self._text


def _semantic_filter_references(asserted, policy, session_id,
                                max_checks=2, deadline=None):
    """入口 1（聚焦式）：程序已定位到含完成词的行，只判这些行的归属。

    返回 (过滤后的文本, note)。
    note 为 None 表示本次没有语义参与（不可用 / 无命中 / 全部判为真声明）。
    """
    try:
        from review_unit import review_claim, available
    except Exception as e:
        return asserted, "评审模块导入失败：%r" % (e,)

    ok_avail, why = available(policy)
    if not ok_avail:
        return asserted, "语义评审不可用（%s）" % why

    lines = asserted.split("\n")
    hits = [i for i, ln in enumerate(lines) if CLAIM_RX.search(ln)]
    if not hits:
        return asserted, None

    import time as _time
    if deadline is None:
        deadline = _review_deadline(policy)
    # ⚠️ 审计修复（2026-09-29，#12）：单次预算读 policy（不再硬编码 15.0）。
    _tmax = _stmt_budget(policy)
    checked = 0
    dropped = []
    skipped_for_budget = 0
    skipped_for_cap = 0
    for i in hits:
        remaining = deadline - _time.time()
        # ⚠️ 审计修复（2026-09-29，#19）：两个中断条件分开计数 ——
        #    修前共用 skipped_for_budget，cap 中断也被文案报成"总预算耗尽"。
        if checked >= max_checks:
            skipped_for_cap = len(hits) - checked
            break
        if remaining <= 1.0:
            skipped_for_budget = len(hits) - checked
            break
        seg = lines[i].strip()
        if not seg:
            continue
        where = "表格的某一行" if seg.startswith("|") else "正文的某一行"
        r = review_claim(seg, "这是 AI 助手收尾陈述里%s（不是用户在说话）" % where,
                         policy=policy, session_id=session_id,
                         timeout_s=min(remaining, _tmax))
        checked += 1
        if r.get("ok") and r.get("verdict") == "allow":
            # 评审结论：引用/命名/复述 —— 不算本回合的完成声明
            dropped.append(i)
        elif r.get("ok") and r.get("verdict") == "block":
            # 找到一处真声明 → 本门适用，不必再查其余行
            break
        # insufficient（失败/超时）→ 不剔除，走原逻辑保守判定

    notes = []
    if dropped:
        notes.append("语义评审剔除 %d 处引用性行（非本回合声明）" % len(dropped))
    if skipped_for_budget:
        # ⚠️ 预算耗尽必须留痕 —— 不许变成"看起来都查过了"
        notes.append("语义评审总预算耗尽，%d 行未做归属判断（按原逻辑处理）"
                     % skipped_for_budget)
    if skipped_for_cap:
        # ⚠️ 审计修复（2026-09-29，#19）：cap 中断与预算耗尽分开报。
        #    修前两者共用一个计数器、文案恒称"总预算耗尽"——当命中行数 >
        #    max_checks 而预算充裕时，用户看到的是关于系统自身行为的错误陈述。
        notes.append("命中行数超过单次上限（%d），%d 行未做归属判断（按原逻辑处理）"
                     % (max_checks, skipped_for_cap))
    return ("\n".join(ln for i, ln in enumerate(lines) if i not in dropped) if dropped
            else asserted), ("；".join(notes) if notes else None)


def _semantic_find_claims(asserted, policy, session_id, deadline):
    """入口 2（整段式）：找【词表没覆盖】的完成表达。

    为什么需要它（用户明确要求）：
      「不能让未被词库覆盖的完成表达永远进不了模型」——
      例如「这件事办妥了，你直接用就行」，CLAIM_RX 一个词都匹配不到，
      旧设计下永远不会被评审看见。

    返回 (claims 列表, note)。
    """
    try:
        from review_unit import review_claims, available
    except Exception as e:
        return [], "评审模块导入失败：%r" % (e,)

    ok_avail, why = available(policy)
    if not ok_avail:
        return [], "语义评审不可用（%s）" % why

    import time as _time
    remaining = deadline - _time.time()
    if remaining <= 1.0:
        return [], "语义评审总预算已耗尽，本次未做整段评审"

    # ⚠️ 审计修复（2026-09-29，#12）：单次预算读 policy（同入口 1）。
    _tmax = _stmt_budget(policy)
    r = review_claims(asserted, "这是 AI 助手对用户的收尾陈述",
                      policy=policy, session_id=session_id,
                      timeout_s=min(remaining, _tmax))
    if r.get("ok") and r.get("verdict") == "block":
        return (r.get("detail") or {}).get("claims") or [], None
    if not r.get("ok"):
        return [], "整段语义评审未完成：%s" % str(r.get("reason"))[:80]
    # ⚠️ 审计修复（2026-09-29，C7）：ok=True + insufficient 也要留痕。
    #    修前这里直接落到 `return [], None` —— 而 review_unit 对"跳过"（too_long）
    #    与"评审过、无发现"都用 ok=True 表达，只有 meta.skip_reason 区分。
    #    于是收尾 >4000 字时，"整段评审被跳过"对调用方【不可见】（静默），
    #    与本项目「静默失败 = 门不存在」的原则相反。
    #    修法：把 skip_reason 显式带出为 note（消费处会打印给用户）。
    if r.get("verdict") == "insufficient":
        _sr = (r.get("meta") or {}).get("skip_reason")
        if _sr:
            return [], "整段语义评审已跳过（%s）：%s" % (
                _sr, str(r.get("reason"))[:80])
    return [], None


def main():
    data = read_input()

    # ⚠️ stop_hook_active 的正确用法（3.5.9 修正，见 #43）。
    #
    #    官方文档原文（hooks.md）：
    #      "The stop_hook_active field is true when Claude Code is already
    #       continuing as a result of a stop hook. Check this value or
    #       process the transcript to avoid blocking on a condition that
    #       will never resolve. Claude Code overrides the hook and ends the
    #       turn after 8 consecutive blocks."
    #
    #    修前：`if stop_hook_active: allow()` —— 无条件放行，不看内容。
    #    后果（外部复核实测，坐实）：模型被打回后【原样重发同一句话】
    #    即可通过。门的强制力从"最多 8 次"实际降到 1 次。
    #
    #    修法：仍然判定内容 —— 只把【本次的拒绝】从"阻断"降级为"放行 + 告警"。
    #    这样：
    #      · 内容不合规时，第一次仍会拦（强制力保留）
    #      · 不会无限循环（第二次必放行，不为难用户）
    #      · 但【放行这件事是可见的】—— 用户能看到"门放行了不合规的收尾"
    #
    #    刻意不做的：不解析 transcript 去比较"是否原样重发"。
    #    那需要读 transcript 文件、引入 IO 与解析失败面，
    #    而这个门的设计原则是 fail-open 且不能成为新的崩溃源。
    #    用"拦一次"换取"简单且不会坏"，是这里值得的取舍。
    retry = data.get("stop_hook_active") is True

    policy = load_policy()
    if not policy.get("effect_gate", {}).get("enabled", True):
        allow()

    msg = data.get("last_assistant_message") or data.get("last_message") or ""
    if not msg:
        allow()

    # ⚠️ 判定必须做在【被断言】的文本上，不能做在【被提到】的文本上（use-mention）。
    #
    #    实测缺陷（主判定路径）：_strip_quoted_context() 原先只在下面的
    #    declared_unverified 分支里调用 —— 主路径上等于死代码。
    #    后果：任何【引用】了触发词的文本都会被拦。实测五种写法全部误报：
    #    中文双引号、英文双引号、中文书名号、围栏代码块、行内反引号。
    #    连"我在批评这个门的触发词表"都被拦（本会话实测两次）。
    #
    #    这正是 V2.6 想修的那类误报，只是当时漏修了主路径 ——
    #    测试里 E3/E4 的用例恰好每条都含"未验证"，全部走了那条被修过的分支，
    #    所以测试全绿而缺陷仍在。
    #
    #    修法：在入口处统一剥一次，之后所有判定都只看"断言"。
    asserted = _strip_quoted_context(msg)

    # 不合规时的统一出口（#43）。
    # 三处 deny_stop 都改走这里 —— 「拦一次」的语义只有一处实现，
    # 避免"改了两处漏了第三处"（本项目 #19 的老毛病）。
    def _reject(reason, rule_id="unspecified"):
        """首次阻断；重试轮次放行，但【放行必须可见】。

        rule_id 供 gate-events 记录用 —— 它区分「哪条 G3 判据拦的」，
        而 reason 文本不适合做统计键（会变）。
        """
        if retry:
            sys.stderr.write(
                "【G3 生效门】本轮为重试轮次 → 已放行（不再次阻断）。\n"
                "  原因：平台在连续阻断 8 次后会强制结束回合，重复阻断无意义。\n"
                "  ⚠️ 但本次收尾仍【不合规】，这是【可见的放行】，不是通过：\n"
                "  %s\n"
                % reason.splitlines()[0])
            sys.stderr.flush()
            # 重试轮次的「可见放行」也算一次真实干预 —— 记下来，
            # 否则统计里会看不到「G3 打回了但没拦住」这一类的真实发生率。
            record_gate_event("G3", rule_id, "stop_feedback_retry",
                              tool="Stop", session_id=data.get("session_id"))
            allow()
        deny_stop(reason, gate_id="G3", rule_id=rule_id, tool="Stop",
                  session_id=data.get("session_id"))

    # --- 语义介入（先于一切机械判定）---
    #
    # ⚠️ 评审不可用时本行什么都不改 —— 下面的全部逻辑与 3.5.17 完全一致。
    _deadline = _review_deadline(policy)

    # 入口 1（聚焦式）：词表命中 → 判断这些命中是"声明"还是"引用"
    if CLAIM_RX.search(asserted):
        asserted, _sem_note = _semantic_filter_references(
            asserted, policy, data.get("session_id"), deadline=_deadline)
        if _sem_note:
            if str(_sem_note).startswith("语义评审剔除"):
                record_gate_event("G3", "semantic_reference_filter",
                                  "review_filtered", tool="Stop",
                                  session_id=data.get("session_id"))
            sys.stderr.write("【G3 生效门】%s\n" % _sem_note)
            sys.stderr.flush()

    claim, level, level_num, risk = analyze(asserted)

    # 入口 2（整段式）：词表【没】命中时，仍然给未见表达一条进模型的路。
    #
    # 用户要求原话：「不能让未被词库覆盖的完成表达永远进不了模型」。
    # 范围用【事件类型（Stop）+ 任务状态（本回合有无工具活动）】限定：
    #   · 有工具活动 → 评审（这回合可能真的做完了什么）
    #   · 明确没有   → 不评审（纯对话，省一次调用）
    #   · 读不到 transcript（None）→ 仍评审：宁可多花一次调用，
    #     也不让这条路径静默断掉 —— 断了就等于回到"词表守门"。
    claim_from_semantic = False
    if not claim:
        _act = _turn_had_tool_activity(data.get("transcript_path"))
        if _act is not False:
            _found, _note2 = _semantic_find_claims(
                asserted, policy, data.get("session_id"), _deadline)
            if _found:
                claim = _SemanticClaim(_found[0])
                claim_from_semantic = True
                record_gate_event("G3", "semantic_claim_uncovered",
                                  "review_found_claim", tool="Stop",
                                  session_id=data.get("session_id"))
            if _note2:
                sys.stderr.write("【G3 生效门】%s\n" % _note2)
                sys.stderr.flush()

    # ⚠️ B2 误报一侧（3.5.13）：把处在【否定/未来/条件语境】里的完成词剔除。
    #
    #    实测误报（三源确认，本会话真实发生 3 次）：
    #        「我不认为它已修复」/「不要写已完成这种话」/「计划是修复完成后再通知」/
    #        「已修复 9 项」（在报统计）
    #    全部被当成"宣布完成"拦下。
    #
    #    若剔除后【一个都不剩】→ 本回合没有真的宣布完成 → 本门不适用，放行。
    #
    # ⚠️ 本地候选 3.6.2（用户独立核验发现）：这条剔除【只对词表找到的 claim 有效】。
    #    语义入口找到的 claim 本来就是词表一个字都不命中的表达
    #    （「这件事办妥了，你直接用就行」），拿 CLAIM_RX 去剔除它，
    #    结果必然是空 → 命中下面这行 allow() → 语义结论被旧分支覆盖、白拦不住。
    #    实测：修复前该输入 rc=0（期望 2），事件却已经写了 review_found_claim。
    #    修法：语义来源的 claim 跳过这条词表剔除，直接进入证据检查；
    #    引用/否定/假设的排除由评审层负责（它的提示词里已明确要求），
    #    而"已验证的声明"仍由 has_block + level 检查保护。
    if claim and not claim_from_semantic \
            and not _drop_negated_claims(asserted, CLAIM_RX):
        # ⚠️ X7（2026-09-29 五维验证）：剔除导致放行【也必须留痕】——
        #    红队实测这类静默放行在事后统计里完全不可见（无 stderr、无事件），
        #    违反本项目「静默失败 = 门不存在」的原则。事件与
        #    stop_feedback_retry 同模式：记录"门检查了但放行了"，
        #    便于事后按 rule_id 统计"否定剔除导致放行"的发生率。
        record_gate_event("G3", "claim_dropped_negated",
                          "allow_by_negation_filter",
                          tool="Stop", session_id=data.get("session_id"))
        allow()

    if not claim:
        allow()

    has_block = bool(EVIDENCE_RX.search(asserted))

    # --- NOT_MEASURED / 未验证：不是"豁免"，是"把级别钉死在 0" ---
    #
    # 为什么不能直接 allow()（V2.2 的写法，第四个互审发现问题）：
    #   那会让「已经修复完成，但未验证」直接放行 ——
    #   而这正是方案点名批评的「用动词代替状态」：
    #   宣布完成，再挂一个免责声明。实测确认可绕过。
    #
    # 正确的语义：
    #   承认"未验证" → 就不许再说任何【目标达成】类的话。
    #   "已改完、未验证" 可以（说的是动作做完）；
    #   "已修复完成、未验证" 不行（说的是目标达成，又自认没验证 = 自相矛盾）。
    #
    # 这样既不惩罚诚实（不否决），又不留后门（级别钉死 0，且禁目标词）。
    # 「未验证」声明必须【带主语】才算数 —— 光秃秃的免责声明不算。
    # 依据见 BARE_UNVERIFIED_RX 的注释（实测：一个换行就能通关）。
    declared_unverified = _has_binding_unverified(asserted)

    if declared_unverified:
        # ⚠️ 判定粒度：必须【同段】共现才算自相矛盾。
        #
        # 误报 1（V2.5）："G1 已验证 ✅" 和 "MCP 未验证 ⬜" 分属两段、说两件事，
        #   旧判定按【全文共现】→ 误拦。修法：改为【同段】。这条保留。
        #
        # 误报 2（V2.6）：我在报告里【引用】测试用例的字面内容，被误判成矛盾。
        #   当时的修法是"判定前剔除引用"，但只加在了这条分支里 ——
        #   主路径仍是裸匹配，所以误报并没真正消失（见 asserted 处的注释）。
        #   本轮统一到入口处理。
        #
        # 本轮补的第三个洞：光秃秃的"未验证"不构成声明。
        #   修前「已完成。\n\n未验证。」靠一个空行就能绕过本门。
        # ⚠️ 3.5.16（返工定稿）：判定粒度 = 【事项】，见 _iter_items()。
        #
        #    一轮返工记录（两版都不对，第三版才对）：
        #      同段  → 把两个不同列表项拼成矛盾（真实误报 C-01）
        #      同行  → 把同一件事的两行拆开（漏拦 c1，用户复验发现）
        #      事项  → 列表项各自成项、非列表连续行合为一项 ✓
        #
        #    实质保护始终由【证据块 + 级别】承担；矛盾判定只负责
        #    "同一事项里同时宣称达成与未验证"这一种自相矛盾形态。
        #    ⚠️ 3.5.16 追加：表格按【单元格】判断，见 _pair_units()。
        #       跨单元格 = 状态声明（要配对）；同单元格 = 复述/描述（不配对）。
        #       ⚠️ 无论哪种，都只影响【矛盾配对】——「完成声明需要证据块」
        #          那条要求不受影响，表格里写"已完成"照样进入证据判定（用例 B0 守住）。
        goal = None
        for item in _iter_items(asserted):
            for unit in _pair_units(item):
                if not UNVERIFIED_RX.search(unit):
                    continue
                g = GOAL_CLAIM_RX.search(unit)
                if g:
                    goal = g
                    break
            if goal:
                break

        # ⚠️ 这里【不能】有 `if not goal: allow()`（3.5.13 修，外部复核实测）。
        #
        #    修前：未验证声明与完成声明不同段时，直接 allow() 提前退出 ——
        #    于是后续三条判定全部被跳过：
        #        · 同段矛盾判定（本处）
        #        · has_block 检查（缺证据块）
        #        · level_num >= need 判定（级别不够）
        #    后果：一段免责声明就能关掉整个 G3。
        #    实测：
        #        已完成。证据：L1                      -> 拦
        #        已完成。证据：L1 + 「## 未验证项」    -> 放行  ← 绕过
        #        已完成 +「未验证项：什么都没做」       -> 放行  ← 连证据块都不要
        #
        #    修法：不是矛盾的（goal 为 None）就【自然落到主判定链】，
        #    由它按正常规则评（该拦就拦、该放就放），而不是直接放行。
        #    这样"承认未验证"不再是一张免死金牌。
        if goal:
            # 同一段里既说"未验证"又宣布【目标达成】 → 自相矛盾，打回
            _reject(
                "【G3 生效门】自相矛盾的收尾：一边说「未验证」，一边宣布「%s」。\n"
                "\n"
                "  「未验证」和「已完成/已修复」不能同时成立 ——\n"
                "  前者是【我没确认它生效】，后者是【我确认目标达成了】。\n"
                "  把两者写在一起 = 用一个免责声明包住一句完成宣言。\n"
                "\n"
                "  实测：这句话在上一版能直接绕过本门。\n"
                "\n"
                "  请二选一：\n"
                "    · 改成「已改完，未验证生效（NOT_MEASURED）」——说的是【动作做完】\n"
                "    · 或者补上证据块，把「%s」撑起来\n"
                "\n"
                "  注意：「已改完 / 已修改 / 已落地」是允许的（动作完成），\n"
                "       「已修复 / 已完成 / 已部署」不允许（目标达成）。"
                % (goal.group(0), goal.group(0))
,
                "contradictory_evidence"
            )

    # 未声明风险级别 → 按【中】从严（缺省不该等于豁免）
    effective_risk = risk or "中"
    need = REQUIRED.get(effective_risk, 2)

    # --- 判定 ---
    if has_block and level_num is not None and level_num >= need:
        allow()

    # 说"完成了"但根本没写 NOT_MEASURED / 证据
    if not has_block:
        _reject(
            "【G3 生效门】你宣布了完成，但本回合没有生效证据块。\n"
            "⚠️ 若因同一原因反复被打回：先说明卡点、换一种验证思路，或把选择交回用户。\n"
            "\n"
            "  触发词：「%s」\n"
            "\n"
            "  审计实证：这是最高频的失败模式。三次典型翻车——\n"
            "    · 宣布「输入输出有了」→ 流式实际 0 条，值来自非流式请求\n"
            "    · 宣布「加 loadAll() 就好了」→ 状态根本不看测试结果\n"
            "    · 宣布「浅色零回归，通过验收」→ 检查恰恰证明什么都没变\n"
            "  共同点：证明的是「我执行了动作」，不是「结果在用户路径上可见」。\n"
            "\n"
            "  请补上证据块后重新收尾（风险级别不写缺省按【中】处理）：\n"
            "\n"
            "    **证据**：L3 端到端\n"
            "    **风险级别**：高\n"
            "    - 用户下次会看到什么不同：<具体到界面元素/命令输出/数值>\n"
            "    - 在哪看：<具体到进程/端口/页面/文件>\n"
            "    - 我怎么确认它生效了：<指向用户路径的证据>\n"
            "    - 未验证项：<明确列出；没有就写「无」>\n"
            "\n"
            "  风险级别 → 最低证据（必须真的达标，不只是写个 level 标签）：\n"
            "    低（Markdown/纯文案/一次性 HTML） → L1 静态\n"
            "    中（普通代码修改）                 → L2 动态\n"
            "    高（进程/数据库/部署/生产环境）    → L3 端到端\n"
            "\n"
            "  如果确实做不到，就把措辞改成「未验证 / NOT_MEASURED」——\n"
            "  诚实地说没验证，比不验证就说完成，代价小得多。"
            % claim.group(1)
,
            "missing_effect_evidence"
        )

    # 有证据块，但证据级别不够
    #
    # ⚠️ 本出口必须走 _reject()，不能裸调 deny_stop()。
    #    外部复核实测：_reject() 里有「重试轮次降级为可见放行」的逻辑
    #    （:243-255），而这里原来是裸调 deny_stop，绕过了它 —— 后果是
    #    这条判据在重试轮次【仍然阻断】，一路撞到平台的 8 次封顶，
    #    与设计意图「第二次必放行，不为难用户」不符。
    #    实测：缺证据块/自相矛盾在重试轮次都放行，唯独级别不够仍拦。
    #    _reject 的 docstring 本来就写着「三处 deny_stop 都改走这里」——
    #    注释承诺了，实现漏了第三处，这里补上。
    shown = level or "未标注"
    _reject(
        "【G3 生效门】证据级别不足以支撑「%s」。\n"
        "⚠️ 若因同一原因反复被打回：先说明卡点、换一种验证思路，或把选择交回用户。\n"
        "\n"
        "  风险级别：%s（%s）\n"
        "  你给的证据级别：%s\n"
        "  这个风险级别要求：L%d\n"
        "\n"
        "  为什么不够：\n"
        "    L1 静态   —— 读了代码 / 语法检查通过  → 只能说「已改，未验证」\n"
        "    L2 动态   —— 单元测试 / 隔离实例跑通  → 只能说「逻辑通过，未验证生效」\n"
        "    L3 端到端 —— 在用户真实路径上肉眼可见 → 才能说「完成」\n"
        "\n"
        "  审计实证：三次翻车全部是「用 L1/L2 的证据支撑 L3 的结论」——\n"
        "    查了遥测字段，没核对是哪个进程在写（应 L3）\n"
        "    改了读数函数，没核对状态列由谁算（应 L3）\n"
        "    跑了浅色对比，没意识到用户就是浅色模式（应 L3）\n"
        "\n"
        "  请二选一：\n"
        "  1. 补到 L%d 再宣布完成\n"
        "  2. 把措辞降级为「已改，未验证生效（NOT_MEASURED）」\n"
        % (claim.group(1), effective_risk,
           "声明" if risk else "未声明，按中风险从严",
           shown, need, need)
,
        "insufficient_evidence_level"
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        warn_inactive("G3 生效门", "脚本异常，本次放行：%r" % (e,))
        sys.exit(0)
