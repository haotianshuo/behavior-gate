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

    # ⚠️ 3.5.15：用户本轮【没写】预算声明时，继承上一轮的【用户显式授权】。
    #
    #   修前缺陷（实测坐实，用户痛点二的另一半）：
    #       第1轮 用户写「用三个子代理」  → cap=3  ✅
    #       第2轮 用户写「继续」          → cap=0  ← 授权凭空消失
    #       第3轮 用户写「再帮我看看」     → cap=0
    #       于是用户【每轮】都要重写一遍授权，否则门会 deny 并告诉他
    #       「本次任务禁止派生子代理」—— 那是伪造用户意图。
    #
    #   修法边界（重要，不要放宽）：
    #     · 只继承【用户显式给出过的值】（source 以 prompt: 开头）。
    #       从未声明过 → 仍为 policy 默认 0，不放宽默认值。
    #     · 用户本轮说了「不要派」→ 是显式禁令，覆盖继承，写 0。
    #     · 用户本轮写了显式数字 → 覆盖继承。
    #   这与 V1.5 §9「根任务预算跨重派/拆分/换模型/主代理接手累计，
    #   禁止重置预算续命」同向：换轮次不得重置预算。
    #   不违反 V2 §3「不自动放宽现有行为门」—— 那说的是【系统自行调高默认值】，
    #   不是【延续用户已给出的授权】。
    prev = load_state(path) or {}
    if source == "policy-default":
        _prev_src = str(prev.get("source") or "")
        _prev_cap = (prev.get("budget") or {}).get("agent_spawns")
        # ⚠️ 必须同时接受 `inherit:` 前缀 —— 否则链只延续一轮。
        #    实测踩到：第2轮写成 inherit:prompt:permit，第3轮判断
        #    startswith("prompt:") 不成立 → 继承断链 → 上限又掉回 0。
        _chainable = (_prev_src.startswith("prompt:") or
                      _prev_src.startswith("inherit:"))
        # 仅当上一轮是【用户显式授权】且值 > 0 时才继承
        if (_chainable and _prev_src != "prompt:forbid"
                and isinstance(_prev_cap, int) and not isinstance(_prev_cap, bool)
                and _prev_cap > 0):
            budget["agent_spawns"] = _prev_cap
            # ⚠️ source 不能无限拼接 —— 实测踩到：
            #    第 N 轮会写成 "inherit:inherit:...:prompt:permit"，
            #    长会话里字符串无界增长，且 budget_gate 的分支判断
            #    只需知道【是不是继承来的】，不需要知道继承了几层。
            #    故只保留一层标记。
            source = "inherit:" + _prev_src.split(":", 1)[-1] \
                if _prev_src.startswith("inherit:") else "inherit:" + _prev_src

    # 解析【意图信号】并【在同一次写入里】落盘（G7 意图门读它）。
    # 复用同一套状态文件，不为这件事新建一套存储。
    # ⚠️ prev 已在上面的「继承上一轮授权」处读取，此处不再重复读。
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

    # ⚠️ 3.5.15：把【禁令命中的用户原话片段】落盘，供 G1 报错时引用。
    #
    #   修前：budget_gate 的文案写死「你在 prompt 里说了不要派子代理」，
    #        无论用户实际说的是什么。用户说「不要开后台」，
    #        门却回「你说了不要派子代理」—— 伪造用户原话。
    #
    #   只取【禁令】的片段：授权/显式声明不需要引用（它们有明确数字）。
    _forbid_frag = None
    if source == "prompt:forbid":
        try:
            from _lib import forbid_excerpt
            _forbid_frag, _ = forbid_excerpt(prompt)
        except Exception as e:
            # 不能静默吞 —— 否则文案会退回"写死"的假引用，且没人知道。
            sys.stderr.write("[G1 预算门] 禁令片段提取失败（文案将不含引用）：%r\n" % (e,))
            sys.stderr.flush()

    saved = save_state(path, {
        "session_id": session_id,
        "budget": budget,
        "source": source,
        "agent_spawns": int(prev.get("agent_spawns", 0)),
        "intent_no_ask": no_ask,
        "intent_no_verify": no_verify,
        "forbid_excerpt": _forbid_frag,
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
