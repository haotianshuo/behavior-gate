# -*- coding: utf-8 -*-
"""
G1 预算门 · 注入端（UserPromptSubmit）

职责：在用户提交 prompt 时解析一次预算，落盘成会话状态，
      并把预算声明注入上下文（用户零书写）。

这一端解决了 V1 的致命缺口：「budget 从哪来」。
V1 让 PreToolUse 去猜预算，但 PreToolUse 拿不到原始 prompt，
让模型自己写又等于把强制力交给被约束方。

UserPromptSubmit 是唯一能同时看到【用户原话】和【会话id】的时机。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import (  # noqa: E402
    read_input, emit_text, load_policy, parse_budget_from_prompt,
    state_path, save_state, load_state, allow,
)


def main():
    data = read_input()
    session_id = data.get("session_id") or "nosession"
    prompt = data.get("prompt") or ""

    policy = load_policy()
    budget, source = parse_budget_from_prompt(prompt, policy)

    path = state_path(session_id, "budget")

    # 解析【意图信号】并【在同一次写入里】落盘（G7 意图门读它）。
    # 复用同一套状态文件，不为这件事新建一套存储。
    prev = load_state(path) or {}
    no_ask = bool(prev.get("intent_no_ask"))
    no_verify = bool(prev.get("intent_no_verify"))
    try:
        from intent_gate import scan_prompt
        no_ask, no_verify = scan_prompt(prompt, prev)
    except Exception as e:
        # ⚠️ 不能静默吞。上一版这里是 `except: pass`，
        # 结果 NameError 被吃掉、状态文件根本没写出来，
        # 而 rc 仍是 0 —— 看起来成功，实际什么都没发生。
        # 这正是本方案要消灭的模式：**静默失败 = 门不存在**。
        sys.stderr.write("[G7 意图门] 意图解析失败（本次跳过）：%r\n" % (e,))
        sys.stderr.flush()

    saved = save_state(path, {
        "session_id": session_id,
        "budget": budget,
        "source": source,
        "agent_spawns": int(prev.get("agent_spawns", 0)),
        "intent_no_ask": no_ask,
        "intent_no_verify": no_verify,
    })

    # 写不成功必须看得见 —— 否则 G1/G7 两个门都会读到空状态
    if not saved:
        sys.stderr.write(
            "[G1/G7] 警告：预算状态写入失败 -> %s\n"
            "  依赖它的门（预算门/意图门）本次不可靠。\n" % path)
        sys.stderr.flush()

    # 注入上下文 —— 明文 stdout 会被加入 Claude 可见的上下文
    # 刻意只列出【真正会被强制】的预算。
    # 不列 webfetch / bash_rounds —— 它们没有拦截器，列出来等于承诺一个
    # 不存在的约束（"假能力"）。要重新列出，必须先实现拦截。
    lines = [
        "[行为门 · 本次任务预算]",
        "  agent_spawns = %d   （派 Agent 上限；0 = 禁止）" % budget["agent_spawns"],
        "  agent_depth  = %d   （子代理内禁止再派）" % budget["agent_depth"],
        "  来源：%s" % source,
        "",
        "超出预算的唯一合法动作：停下 → 报告 → 问。",
        "要改预算，在 prompt 里写 `agent_spawns: 3` 这样的一行即可。",
    ]
    emit_text("\n".join(lines))
    allow()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # fail-open，但可见
        sys.stderr.write("[budget-inject] 异常，已跳过：%r\n" % (e,))
        sys.exit(0)
