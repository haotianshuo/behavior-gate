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
    state_path, save_state, load_state, locked_update, allow,
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
    # ⚠️ 审计修复（2026-09-29，C5）：继承条件从 `source == "policy-default"`
    #    放宽为「本轮没有对 agent_spawns 的显式表态」。
    #    实测漏拦（C 组审计 c7-B，本机独立复现）：
    #        上一轮 prompt:permit(3) + 本轮「agent_depth: 0 …」
    #        → source=prompt:explicit:agent_depth ≠ policy-default
    #        → 不进入继承 → cap 回落 0：上一轮的授权被一条
    #          与派生数无关的预算声明打断。
    #    本轮显式声明 agent_spawns（prompt:explicit）/ 禁令（prompt:forbid）/
    #    许可（prompt:permit）时【不】继承 —— 原语义不变。
    _spawns_declared = source in ("prompt:forbid", "prompt:permit",
                                  "prompt:explicit")
    if not _spawns_declared:
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
    #
    # ⚠️ 本地候选 3.6.0：意图理解改由【语义评审单元】完成（review_unit），
    #    词表降级为「语义不可用时的回退」。理由见 intent_gate.decide_intent
    #    的注释：词表在「分别测试」「验收工具」这类真实表达上会把意图读反。
    #
    #    ⚠️ 数据边界（如实声明）：评审会把【用户本轮原话】发给
    #       Claude Code 自己正在用的模型通道（环境变量 ANTHROPIC_*）。
    #       不新增供应商、不发会话历史、不发文件内容。
    #       关闭方式：policy.semantic_review.enabled = false。
    review = None
    try:
        from review_unit import review_intent, available
        _ok, _why = available(policy)
        if _ok:
            review = review_intent(prompt, policy, session_id)
        else:
            # 不可用（用户关闭 / 通道缺失）—— 如实记原因，走词表回退
            review = {"ok": False, "verdict": "insufficient",
                      "detail": {}, "reason": _why, "meta": {}}
    except Exception as e:
        # 评审模块故障不能拖垮注入端 —— 但必须留痕，不许静默
        review = {"ok": False, "verdict": "insufficient", "detail": {},
                  "reason": "评审模块异常：%r" % (e,), "meta": {}}

    intent_state = None
    try:
        from intent_gate import decide_intent
        intent_state = decide_intent(prompt, prev, review)
    except Exception as e:
        # ⚠️ 不能静默吞。上一版这里是 `except: pass`，
        # 结果 NameError 被吃掉、状态文件根本没写出来，
        # 而 rc 仍是 0 —— 看起来成功，实际什么都没发生。
        # 这正是本方案要消灭的模式：**静默失败 = 门不存在**。
        sys.stderr.write("[G7 意图门] 意图解析失败（本次跳过）：%r\n" % (e,))
        sys.stderr.flush()

    if intent_state is None:
        # 决策层故障：保持上一轮状态不动（绝不因故障而放宽既有约束）
        intent_state = {
            "intent_no_ask": bool(prev.get("intent_no_ask")),
            "intent_no_verify": bool(prev.get("intent_no_verify")),
            "intent_ask_excerpt": prev.get("intent_ask_excerpt"),
            "intent_verify_excerpt": prev.get("intent_verify_excerpt"),
            "intent_source": "decision-failed",
            "intent_degraded": "决策层异常，保持上一轮状态",
        }
    no_ask = intent_state["intent_no_ask"]
    no_verify = intent_state["intent_no_verify"]
    ask_frag = intent_state["intent_ask_excerpt"]
    verify_frag = intent_state["intent_verify_excerpt"]

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

    # ⚠️ 审计修复（2026-09-29，C1 丢更新）：原实现是 save_state(整份快照) ——
    #    快照在【语义评审之前】读取（prev），而评审耗时 3~15s。
    #    若评审窗口内有 Agent PreToolUse 落地（budget_gate 在锁内递增 used），
    #    本次写回会把它盖回旧值 —— 实测 A/B 对照确认丢更新：
    #        used 被盖回 0、last_spawn_at 被抹掉（配额可被静默绕过）。
    #    修法：改 locked_update —— 在锁内读【最新状态】，只更新本端负责的
    #    字段（预算声明 / 意图 / 引用片段）。used 与 last_spawn_* 一律保留
    #    锁内现值（那是 budget_gate 的字段，由它自己在锁内维护）。
    #    顺带修掉：原 `int(prev.get("agent_spawns", 0))` 遇损坏值会抛
    #    TypeError → 整个注入端跳过落盘（A 组审计登记的线索）。
    def _merge_inject(st):
        if not st:
            st = {}
        st["session_id"] = session_id
        st["budget"] = budget
        st["source"] = source
        st["intent_no_ask"] = no_ask
        st["intent_no_verify"] = no_verify
        st["intent_ask_excerpt"] = ask_frag
        st["intent_verify_excerpt"] = verify_frag
        # 本次意图是怎么来的（semantic / regex-fallback / override …）
        # 与降级原因。统计与拒绝提示都要用到；没有它就无法区分
        # 「语义判断」和「词表回退」——那正是本项目最反对的混淆。
        st["intent_source"] = intent_state.get("intent_source")
        st["intent_degraded"] = intent_state.get("intent_degraded")
        st["intent_review_ok"] = bool(review and review.get("ok"))
        st["intent_review_model"] = ((review or {}).get("meta") or {}).get("model") or None
        st["intent_review_ms"] = ((review or {}).get("meta") or {}).get("elapsed_ms") or 0
        st["forbid_excerpt"] = _forbid_frag
        # ⚠️ 刻意【不】写 st["agent_spawns"] —— 那是 used 计数（budget_gate 维护）；
        #    本端只写 cap（st["budget"]["agent_spawns"]）。
        return st, None

    _res = locked_update(path, _merge_inject)
    saved = not isinstance(_res, str)

    # 写不成功必须看得见 —— 否则 G1/G7 两个门都会读到空状态
    if not saved:
        sys.stderr.write(
            "[G1/G7] 警告：预算状态写入失败（%s）-> %s\n"
            "  依赖它的门（预算门/意图门）本次不可靠。\n" % (_res, path))
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

    # --- 意图理解状态（本地候选 3.6.0）---
    #
    # 安静原则：没有需要告诉用户的变化时【一行都不显示】。
    # 两种情况必须显示：
    #   1) 本会话建立了对模型的约束（用户该知道自己的话被记成了什么）
    #   2) 语义评审降级成了关键词匹配（用户必须能发现"这次没用上理解能力"）
    intent_lines = []
    _forbids = []
    if no_verify:
        _forbids.append("不要运行测试" + ("（依据原话「%s」）" % verify_frag
                                          if verify_frag else "（无来源片段）"))
    if no_ask:
        _forbids.append("不要向你提问" + ("（依据原话「%s」）" % ask_frag
                                          if ask_frag else "（无来源片段）"))
    if _forbids:
        intent_lines.append("  已记录本会话要求：%s" % "；".join(_forbids))
    # ⚠️ 「关闭」与「故障」分开说 —— 用户要能分清"我关的"和"它坏了"。
    _src = str(intent_state.get("intent_source") or "")
    if intent_state.get("intent_degraded"):
        _why = str(intent_state["intent_degraded"])[:80]
        # ⚠️ 三种「没做语义判断」必须让用户分得清（用户 本地候选 3.6.2 明确要求）：
        #    · 原话过长未判断（信息缺失）—— 不是"用户没提"
        #    · 用户自己关了语义评审
        #    · 调用故障
        if _src == "hold-too-long":
            intent_lines.append(
                "  · 原话过长，本轮未做语义判断 —— "
                "既有约束保持不变，未据片段新增或解除")
        elif _src.startswith("regex-fallback(disabled)") or \
                not (policy.get("semantic_review") or {}).get("enabled", True):
            # ⚠️ 审计修复（2026-09-29，C4）：判据补上"直接读配置"这一支。
            #    实测（C 组审计 c6，本机独立复现）：用户关闭语义评审且
            #    状态干净时，intent_source 写的是 'inherit'（不是
            #    'regex-fallback(disabled)'）→ 落到 else 分支 →
            #    「用户自己关的」被显示成「本轮语义评审未完成」。
            #    直接以配置为准：enabled=false ⇒ 无论 source 是什么，
            #    都按"已关闭"如实说明。
            intent_lines.append("  · 语义评审已由配置关闭，本轮按关键词匹配处理")
        else:
            intent_lines.append(
                "  ⚠️ 本轮语义评审未完成（原因：%s）——"
                "已保留既有约束，未用关键词新增禁令" % _why)
    if intent_lines:
        lines += ["", "[行为门 · 意图理解]"] + intent_lines

    # --- 工作协议卡片（3.5.16 新增）---
    #
    # 目的：把「任务理解 / 逐项验证」这两条协议，从【需要用户反复手动粘贴的
    #      卡片】变成【每次提交都自动出现在上下文里的工作指导】。
    #
    # ⚠️⚠️ 边界必须写清（否则这一节就是本文件最反对的那种"假能力"）：
    #   · 这一节是【给模型的工作指导】，程序【不检查】它是否被遵守、
    #     也无法检查"用户到底被理解对了没有"。
    #   · 因此它【不列进 README 的门表】，也【不声明任何强制力】。
    #   · 真正由程序实际检查的只有四样，已在卡片最后一行如实列出。
    #   · 绝不可写成"程序保证理解自然语言" —— 做不到，这就是过度承诺。
    #
    # 关闭方式：策略里把 protocol_card.enabled 设为 false。
    # （该键【确实被本处读取】—— 不是只声明不执行的空开关。）
    proto = policy.get("protocol_card") or {}
    if proto.get("enabled", True):
        lines += [
            "",
            "[行为门 · 工作协议（模型指导；程序不检查是否遵守）]",
            "  非平凡任务先交任务合同再动手：目标 / 明确要求 / 明确不做 / 假设 / 验收标准",
            "  补执行条件，不补业务规则；低风险假设声明后继续，只有高影响未知才暂停相关分支",
            "  逐项闭环：先验证存在 → 最小改动 → 用同一路径复验；复现不出记 NOT_REPRODUCED（≠不存在）",
            "  收尾三报分开：协议执行 / 机械检查 / 未测量项（一律写 NOT_MEASURED）",
            "  ⚠️ 程序实际检查的只有：预算(G1) · 证据块与级别(G3) · 危险命令(G5) · 意图(G7)",
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
