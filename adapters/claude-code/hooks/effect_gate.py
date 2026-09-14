# -*- coding: utf-8 -*-
"""
G3 生效门 · 执行端（Stop / SubagentStop）

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

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import (  # noqa: E402
    read_input, deny_stop, allow, load_policy, warn_inactive,
)


# 完成类措辞。
# ⚠️ 这张表要覆盖【所有】完成语义，不能只覆盖"修复"。
# 实测漏过一次：「已部署」不在表里 → 高风险任务宣布部署完成时静默放行。
CLAIM_RX = re.compile(
    r"(已修复|已经修复|修复完成|已改好|改好了|已完成|完成了|已解决|解决了"
    r"|验证通过|已验证|测试通过|全部通过|已通过|已跑通"
    r"|已实现|搞定了|没问题了|已生效|现在可以了"
    r"|已部署|部署完成|已上线|上线完成|已发布|已提交|已推送|已合并|已落盘|已写入)"
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

# 证据级别。⚠️ 顺序要紧：L3 必须排在 L1 前面，
# 否则 "L3" 里的字符会被更宽松的规则先吃掉（历史踩坑）。
LEVEL_RX = re.compile(r"\b(L3|L2|L1)\b|NOT_MEASURED", re.I)

# 风险级别声明
RISK_RX = re.compile(r"风险级别\s*\**\s*[:：]\s*\**\s*(低|中|高|低风险|中风险|高风险)", re.I)

# 完成词里【明确表示"还没做到"】的那几个。
# 「已改完」「已修改」「已落地」说的是"动作做完了"，
# 不是"目标达成了" —— 这类才允许和"未验证"共存。
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
    """返回 (claim, level, level_num, risk)"""
    claim = CLAIM_RX.search(msg)
    lvl = LEVEL_RX.search(msg)
    level = lvl.group(0).upper() if lvl else None
    if level == "NOT_MEASURED":
        level_num = 0
    else:
        level_num = LEVEL_NUM.get(level, None)
    risk_m = RISK_RX.search(msg)
    risk = risk_m.group(1).replace("风险", "") if risk_m else None
    return claim, level, level_num, risk


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
    t = re.sub(r"```.*?```", " ", t, flags=re.S)          # 围栏代码块
    t = re.sub(r"`[^`\n]*`", " ", t)                       # 行内代码
    t = re.sub(r"\"[^\"\n]{0,200}\"", " ", t)              # 英文双引号
    t = re.sub(r"[“”][^“”\n]{0,200}[“”]", " ", t)          # 中文双引号
    t = re.sub(r"[「『][^」』\n]{0,200}[」』]", " ", t)      # 中文书名/引号
    t = re.sub(r"^[ \t]*>.*$", " ", t, flags=re.M)         # 块引用行
    return t


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
        return True
    return False


def main():
    data = read_input()

    # 防死循环：官方要求的提前退出
    if data.get("stop_hook_active") is True:
        allow()

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

    claim, level, level_num, risk = analyze(asserted)
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
        goal = None
        for para in re.split(r"\n\s*\n", asserted):
            if not UNVERIFIED_RX.search(para):
                continue
            g = GOAL_CLAIM_RX.search(para)
            if g:
                goal = g
                break

        if not goal:
            # 未验证声明与完成声明不在同一段 —— 不是矛盾
            allow()
        # 同一段里既说"未验证"又宣布【目标达成】 → 自相矛盾，打回
        deny_stop(
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
        )

    # 未声明风险级别 → 按【中】从严（缺省不该等于豁免）
    effective_risk = risk or "中"
    need = REQUIRED.get(effective_risk, 2)

    # --- 判定 ---
    if has_block and level_num is not None and level_num >= need:
        allow()

    # 说"完成了"但根本没写 NOT_MEASURED / 证据
    if not has_block:
        deny_stop(
            "【G3 生效门】你宣布了完成，但本回合没有生效证据块。\n"
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
        )

    # 有证据块，但证据级别不够
    shown = level or "未标注"
    deny_stop(
        "【G3 生效门】证据级别不足以支撑「%s」。\n"
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
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        warn_inactive("G3 生效门", "脚本异常，本次放行：%r" % (e,))
        sys.exit(0)
