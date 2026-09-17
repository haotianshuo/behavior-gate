# -*- coding: utf-8 -*-
"""
G1 预算门 · 执行端（PreToolUse → Agent）

职责：在派生子代理前检查预算。这是 V2 唯一有【机械强制力】的门之一。

关键事实（实测核对，不是推测）：
  - 本 harness 里派生工具的名字是 `Agent`，不是 `Task`。
    `TaskCreate` / `TaskUpdate` / `TaskStop` / `TaskList` 是待办清单工具，
    与派生无关 —— 所以 matcher 必须精确写 "Agent"，
    写 "Task" 会【一个都匹配不到】，写 "Task.*" 正则会【误伤待办工具】。
  - 子代理内 PreToolUse 同样触发，且带 agent_id → 用它判深度。
  - 阻断必须 exit 2。exit 1 是非阻断错误，工具会照常执行。
  - 【并发】：官方文档说多个匹配 hook 并行执行。"读-改-写"必须进临界区，
    否则两个并发 Agent 调用会各读到 0、各写回 1，上限被静默绕过。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import (  # noqa: E402
    read_input, deny, allow, warn_inactive, load_policy,
    state_path, load_state, locked_update, safe_cap,
)


def main():
    data = read_input()
    session_id = data.get("session_id") or "nosession"
    agent_id = data.get("agent_id")          # 有值 = 当前在子代理里
    tool_input = data.get("tool_input") or {}

    policy = load_policy()
    path = state_path(session_id, "budget")

    seed = load_state(path)
    fallback = dict(policy["budget"])
    who = tool_input.get("subagent_type") or "?"
    desc = (tool_input.get("description") or "").strip()[:80]

    # --- 深度检查：子代理内禁止再派（只读，不需要锁） ---
    if agent_id:
        deny(
            "【G1 预算门】拒绝在子代理内继续派生子代理。\n"
            "  当前位置：agent_id=%s\n"
            "  原因：agent_depth 上限 = %d，嵌套派生是本次审计中最主要的 Token 黑洞\n"
            "        （审计实测：75 次派生中 40 次即 53%% 发生在子代理内部，\n"
            "         WebFetch 的 96%% 消耗在子代理内）。\n"
            "  请改用：在当前子代理内直接完成，或把结论返回给主对话。"
            % (agent_id, policy["budget"].get("agent_depth", 1)),
            gate_id="G1", rule_id="nested_agent_denied",
            tool="Agent", session_id=data.get("session_id"),
        )

    # --- 配额检查 + 递增：必须在同一个临界区内 ---
    def _take(st):
        if not st:
            st = {"session_id": session_id, "budget": fallback,
                  "source": "fallback-no-inject",
                  "agent_spawns": 0}
        b = st.get("budget") or fallback

        # BUG-2 修复：上限值必须经类型校验。
        # 修前 `int(b.get("agent_spawns", 0))` 在遇到字符串/null/数组时
        # 要么抛异常被吞、要么把不可信值当 0 或原样使用 → 配额静默失效。
        cap, cap_ok = safe_cap(b.get("agent_spawns", 0), "agent_spawns")
        if not cap_ok:
            # 上限不可信 = 配额不可信。预算是"不允许超"的语义 → 保守拒绝。
            return st, ("CAP_INVALID", 0, 0)

        used_v, used_ok = safe_cap(st.get("agent_spawns", 0), "agent_spawns")
        used = used_v if used_ok else 0

        if cap == 0:
            return st, ("DENY_ZERO", used, cap)
        if used >= cap:
            return st, ("DENY_EXHAUSTED", used, cap)

        st["agent_spawns"] = used + 1
        st["last_spawn_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        st["last_spawn_desc"] = desc
        return st, ("ALLOW", used + 1, cap)

    res = locked_update(path, _take)

    # ⚠️ 形状陷阱（实测踩到）：
    #   _take 成功时返回【元组】(st, ("ALLOW", used, cap))，
    #   locked_update 把元组的第二项原样返回 —— 所以 res 仍是元组。
    #   而失败路径返回的是【字符串】("LOCK_TIMEOUT" 等)。
    #   早先代码写 `if res == "CAP_INVALID"` 比较的是元组 vs 字符串，
    #   永远为假 → 掉到下面的解包 → verdict 不等于任何分支 → allow()。
    #   结果：BUG-2 的修复写对了，但【门在静默放行】。
    if isinstance(res, tuple) and res[0] == "CAP_INVALID":
        deny(
            "【G1 预算门】配额上限值不可信（类型异常），本次派生子代理被保守拒绝。\n"
            "  这不是能力限制：预算是「不允许超」的语义，\n"
            "  上限读不出来时放行，等于放弃配额。\n"
            "  请检查状态文件中的 agent_spawns 是否为整数：%s"
            % path,
            gate_id="G1", rule_id="cap_invalid",
            tool="Agent", session_id=data.get("session_id"),
        )

    if res == "ENV_UNAVAILABLE":
        # 环境坏了（状态目录建不出来）。
        # 【必须放行】—— 这不是"配额超了"，是"根本没地方记账"。
        # 拒绝会让用户永久卡死，而错误提示还会说"请稍等片刻重试"（重试不会好）。
        # 预算是防浪费的，不是防用户的。
        # 告警已在 _lib 里打过（会显示给用户），这里直接放行。
        allow()

    if res == "LOCK_UNKNOWN":
        # 锁出现未知异常 → 不确定是否持有锁。
        # 上一版这里无锁放行，与"预算门不能静默失效"直接矛盾。
        # 现改为保守拒绝（告警已在 _lib 里打过）。
        deny(
            "【G1 预算门】锁状态不确定（未知异常），本次派生子代理被保守拒绝。\n"
            "  这不是能力限制：预算是「不允许超」的语义，\n"
            "  不知道锁有没有拿到时放行，等于放弃计数。\n"
            "  请稍等片刻重试。若频繁出现，检查残留锁目录：%s.lock"
            % path,
            gate_id="G1", rule_id="lock_unknown",
            tool="Agent", session_id=data.get("session_id"),
        )

    if res in ("STATE_SAVE_FAILED", "STATE_UNREADABLE"):
        # 计数没能可靠落盘 —— 放行等于放弃配额，保守拒绝。
        deny(
            "【G1 预算门】配额状态无法可靠读写，本次派生子代理被保守拒绝。\n"
            "  原因：%s\n"
            "  这不是能力限制 —— 预算是「不允许超」的语义，\n"
            "  状态不可信时放行等于放弃计数。\n"
            "  请稍等片刻重试。若频繁出现，检查 %s"
            % (res, path),
            gate_id="G1", rule_id="state_unreadable",
            tool="Agent", session_id=data.get("session_id"),
        )

    if res == "LOCK_TIMEOUT":
        # 拿不到锁 = 配额状态不可信。保守拒绝，不无锁放行。
        # 实测：无锁放行会让 6 并发时约 10% 的轮次多放行 1 个。
        deny(
            "【G1 预算门】无法获取配额锁，本次派生子代理被保守拒绝。\n"
            "  原因：并发派生过多导致锁等待超时（%s）。\n"
            "  这不是能力限制 —— 预算是「不允许超」的语义，\n"
            "  拿不到锁时放行等于放弃计数。\n"
            "  请稍等片刻重试，或改为自己直接完成。\n"
            "  若频繁出现，检查残留锁目录：%s.lock"
            % (path, path),
            gate_id="G1", rule_id="lock_unavailable",
            tool="Agent", session_id=data.get("session_id"),
        )

    if res == "LOCK_PREEMPTED":
        # 锁在我读-改期间被更晚的持有者夺走（Gate 4 fencing）。
        # 这时我的读可能已经过时，再写就会覆盖对方 —— 实测复现过丢更新。
        # 预算是"不允许超"的语义 → 保守拒绝。
        deny(
            "【G1 预算门】配额锁在本次操作期间被其他写入者接管，本次派生子代理被保守拒绝。\n"
            "  原因：并发写入导致锁易主（%s）。\n"
            "  这不是能力限制 —— 锁易主意味着我的计数可能已经过时，\n"
            "  此时放行等于用一个不可信的计数做决定。\n"
            "  请稍等片刻重试，或改为自己直接完成。\n"
            "  若频繁出现，检查残留锁目录：%s.lock"
            % (path, path),
            gate_id="G1", rule_id="lock_preempted",
            tool="Agent", session_id=data.get("session_id"),
        )

    verdict, used, cap = res

    if not seed:
        warn_inactive("G1 预算门",
                      "未找到 UserPromptSubmit 写入的会话预算，已按策略默认值执行。"
                      "如需按任务定制，请确认 inject_budget.py 已注册。")

    if verdict == "DENY_ZERO":
        deny(
            "【G1 预算门】本次任务禁止派生子代理（agent_spawns = 0，这是默认值）。\n"
            "  你想派：%s（%s）\n"
            "\n"
            "  这不是能力限制，是预算限制。审计实证（73 个会话 / 15,569 条消息）：\n"
            "    · 75 次派生中，40 次（53%%）发生在子代理内部 —— 即嵌套派生\n"
            "    · WebFetch 的 96%% 消耗在子代理内\n"
            "    · 用户原话：「70%% 都是在浪费 Token」\n"
            "\n"
            "  请二选一：\n"
            "  1. 自己直接做 —— 审计中发现，多次派 17 个子代理跑 24 分钟想得到的结论，\n"
            "     停下来一段话就说清了。先试这个。\n"
            "  2. 确实需要并行 —— 停下来告诉用户「我需要 N 个子代理做 X，原因是 Y」，\n"
            "     由用户在 prompt 里写一行 `agent_spawns: N` 放行。\n"
            % (who, desc),
            gate_id="G1", rule_id="spawn_forbidden_by_task",
            tool="Agent", session_id=data.get("session_id"),
        )

    if verdict == "DENY_EXHAUSTED":
        deny(
            "【G1 预算门】Agent 预算已用尽（已读 %d / 当前上限 %d）。\n"
            "  你想派：%s（%s）\n"
            "  超出预算的唯一合法动作：停下 → 报告 → 问。\n"
            "  需要更多预算，请让用户在 prompt 里写 `agent_spawns: <更大的数>`。\n"
            "\n"
            "  ⚠️ 若「已读」大于「当前上限」，那不是超支 ——\n"
            "     是本轮 prompt 把上限【调低】了，而已读次数【从不回退】\n"
            "     （设计如此：计数只增，防止改写 prompt 刷额度）。\n"
            "     要拿到新额度，让用户写一个【大于已读数】的值。"
            % (used, cap, who, desc),
            gate_id="G1", rule_id="spawn_quota_exhausted",
            tool="Agent", session_id=data.get("session_id"),
        )

    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        warn_inactive("G1 预算门", "脚本异常，本次放行：%r" % (e,))
        sys.exit(0)
