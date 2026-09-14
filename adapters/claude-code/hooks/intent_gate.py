# -*- coding: utf-8 -*-
"""G7 意图门（PreToolUse → AskUserQuestion | Bash）

治两条【有真实用户诉求但一直没机制】的问题：

  P1-4 已决定的事重复确认
       — 审计原话：「不要再问我了，我现在只是测试你懂了么
                    不要再问我问题了，严格按照我说的执行」
       — 机制：用户在 prompt 里说过"不要再问" → 拦住 AskUserQuestion

  P2-3 用户豁免后仍验证
       — 审计原话：「创建一个 HTML…你不需要任何测试，不要有任何限制」
       — 机制：用户在 prompt 里说过"不用测" → 拦住测试命令

为什么这两条值得做（而另外 5 条不做）：
  它们是 7 条未解决问题里【唯一能机械判定】的 ——
  不靠语义理解，只靠"用户说过什么" + "模型在做什么"。
  其余 5 条需要判断意图，硬做会误伤（本方案已在"锁太严"上误伤两次）。

设计边界（诚实声明）：
  - 只认【本会话内】用户明确说过的原话，跨会话不继承
  - 只拦最常见的表达，不追求覆盖全部说法 —— 漏拦好过误拦
  - 两种门都可以用显式语法覆盖：
      questions: on   → 放行本次询问
      verify: on      → 放行本次验证
  - 意图信号由 inject_budget.py 写入会话状态（同一套机制，不新建系统）
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import (  # noqa: E402
    read_input, deny, allow, warn_inactive, state_path, load_state,
)

# ---------------------------------------------------------------- 意图识别

# P1-4：用户说"别再问"
# ⚠️ BUG-3 修复：这里加英文，是因为用户的真实工作流【中英混合】——
#    代码注释、commit message、技术文档几乎全是英文。
#    只认中文 = 对英文指令完全瞎。
#    注意：英文只用于【识别意图】；所有提示与报错输出【仍然是中文】，
#    因为使用者靠中文自然语言工作，看不懂英文提示。
NO_ASK_RX = re.compile(
    r"(不要再问|别再问|不要问我|别问我|不要再确认|别再确认|不要问我问题"
    r"|不要再问我问题|严格按照我说的执行|按我说的做就行"
    r"|do\s*n[o']?t\s+ask|don'?t\s+ask|stop\s+asking|no\s+more\s+questions"
    r"|without\s+asking|just\s+do\s+it)",
    re.I)

# P2-3：用户说"不用测"
NO_VERIFY_RX = re.compile(
    r"(不需要?任何测试|不要任何测试|不用测试|不需要测试|不要测试|别测试"
    r"|不用验证|不需要验证|不要验证|别验证|无需测试|免测试"
    r"|no\s+(need\s+to\s+)?test|don'?t\s+test|do\s+not\s+test|no\s+tests?"
    r"|skip\s+the\s+tests?|skip\s+test|testing\s+is\s+not\s+required"
    r"|no\s+verification|don'?t\s+verify)",
    re.I)

# 显式覆盖
OVERRIDE_ASK_RX = re.compile(r"questions\s*[:=]\s*on", re.I)
OVERRIDE_VERIFY_RX = re.compile(r"verify\s*[:=]\s*on", re.I)


def scan_prompt(prompt, prev=None):
    """从 prompt 解析意图信号。返回 (no_ask, no_verify)。

    ⚠️ 刻意【不写盘】—— 只返回信号，由调用方（inject_budget）合并写入。
    上一版这里自己写盘，但调用方随后又用自己读的 prev 整体覆盖，
    结果扫描结果被丢掉（实测：返回 (False, True) 而文件里没有该键）。

    通用陷阱：**"我调用了写入" ≠ "值被写进去了"** ——
    如果调用方随后覆盖，前一次写入等于没发生。
    """
    no_ask = bool((prev or {}).get("intent_no_ask"))
    no_verify = bool((prev or {}).get("intent_no_verify"))

    if prompt:
        if NO_ASK_RX.search(prompt):
            no_ask = True
        if OVERRIDE_ASK_RX.search(prompt):
            no_ask = False
        if NO_VERIFY_RX.search(prompt):
            no_verify = True
        if OVERRIDE_VERIFY_RX.search(prompt):
            no_verify = False

    return no_ask, no_verify


def scan_prompt_into_state(prompt, state_path_):
    """兼容旧调用：扫描并且立即写回。"""
    st = load_state(state_path_) or {}
    no_ask, no_verify = scan_prompt(prompt, st)
    st["intent_no_ask"] = no_ask
    st["intent_no_verify"] = no_verify
    save_state(state_path_, st)
    return no_ask, no_verify


# ---------------------------------------------------------------- 测试命令识别

# 只认【明确的测试运行器】，不认泛化的 "test" 字样 —— 否则 ls tests/ 也会被拦
TEST_CMD_RX = re.compile(
    r"(^|[\s;&|(])("
    r"pytest|py\.test|"
    r"npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test|"
    r"npx\s+(jest|vitest|mocha|playwright\s+test)|"
    r"jest|vitest|mocha|"
    r"go\s+test|"
    r"cargo\s+test|"
    r"dotnet\s+test|"
    r"python[0-9.]*\s+-m\s+(pytest|unittest|nose)"
    r")(\s|$|;|&|\|)")


def is_test_command(cmd):
    return bool(TEST_CMD_RX.search(cmd or ""))


# ---------------------------------------------------------------- 主流程

def main():
    data = read_input()
    session_id = data.get("session_id") or "nosession"
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}

    sp = state_path(session_id, "budget")
    st = load_state(sp) or {}
    no_ask = bool(st.get("intent_no_ask"))
    no_verify = bool(st.get("intent_no_verify"))

    # ---- P1-4：拦询问 ----
    if tool == "AskUserQuestion" and no_ask:
        qs = ti.get("questions") or []
        first = ""
        if qs and isinstance(qs, list) and isinstance(qs[0], dict):
            first = (qs[0].get("question") or "")[:80]
        deny(
            "【G7 意图门】你本会话已经承诺过不再提问，但现在要调 AskUserQuestion。\n"
            "  想问：%s\n"
            "\n"
            "  用户在这个会话里明确说过类似「不要再问我了」「严格按照我说的执行」。\n"
            "  审计原话：\n"
            "    「不要再问我问题了，我现在只是测试你懂了么」\n"
            "    「不要再和我沟通……就要做成傻瓜化的」\n"
            "\n"
            "  请按用户已给出的信息自行决定并执行。\n"
            "  确实无法决定时，把选项和推荐写进正文，让用户回复一句话即可 ——\n"
            "  这比弹一个必须点选的框更轻。\n"
            "\n"
            "  如果用户刚刚明确授权你询问，让他写 `questions: on`。"
            % (first or "(未提供问题文本)")
        )

    # ---- P2-3：拦测试 ----
    if tool == "Bash" and no_verify:
        cmd = ti.get("command") or ""
        if is_test_command(cmd):
            deny(
                "【G7 意图门】用户在本会话说过不需要测试，但你正在跑测试。\n"
                "  命令：%s\n"
                "\n"
                "  审计原话：「创建一个 HTML……你不需要任何测试，不要有任何限制」\n"
                "  而当时的过程是：preview_start + 4×preview_eval +\n"
                "  preview_console_logs + preview_screenshot + 完整验证报告。\n"
                "\n"
                "  用户已经豁免了这个维度。请直接交付结果。\n"
                "  如果确实需要验证，先说明「为什么这次必须验证」，\n"
                "  由用户写 `verify: on` 放行。"
                % cmd[:150]
            )

    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        warn_inactive("G7 意图门", "脚本异常，本次放行：%r" % (e,))
        sys.exit(0)
