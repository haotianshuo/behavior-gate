# -*- coding: utf-8 -*-
"""
G6 收口门（PreToolUse → Agent | Bash，旁路采集）

与 V1 的差别：V1 让模型自报收口清单 —— 但模型自报的
「进程已停、端口已释放」不可靠，而且【误杀其他会话的进程】风险真实存在
（审计实证：deepseek 说「我看到 17 个子 Agent 在跑，但我只启动了 1 个」）。

V2 改成：宿主侧【自动采集】。
  - 纯记录，不阻断，不猜，不杀任何东西。
  - 只记录本会话实际派生过的 agent_id 和跑过的后台命令 PID。
  - 收尾时由 closeout_report.py（或模型读取状态文件）如实呈现。

刻意不做的事：
  - 不自动 kill 任何进程（归属不明的进程一律不碰）
  - 不解析进程树（尽力而为地记录，不做判断）
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import (  # noqa: E402
    read_input, allow, state_path, locked_update, load_policy, load_state,
)


def _record(st, session_id, tool, ti, agent_id):
    if not st:
        st = {}
    st.setdefault("session_id", session_id)
    # ⚠️ 字段叫什么必须对应实际存了什么。
    # 这里存的是【派生请求】（工具输入里的 subagent_type/description），
    # 不是子代理运行后的真实 agent_id —— 那要等 SubagentStart/Stop 事件才有。
    # 叫它 agent_ids 会误导后续关联（无法区分同类型多个子代理）。
    st.setdefault("agent_requests", [])
    st.setdefault("background_cmds", [])
    st.setdefault("writes", [])
    # Bash/WebFetch 计数（只记录，不拦截 —— 本项目未实现这两个预算的强制）
    st.setdefault("bash_cmds", [])
    st.setdefault("webfetch_count", 0)
    st.setdefault("repeat_suspects", [])

    if tool == "Agent":
        rec = {
            "requesting_agent_id": agent_id,        # 谁发起的（None = 主对话）
            "subagent_type": ti.get("subagent_type") or "?",
            "description": (ti.get("description") or "")[:80],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        st["agent_requests"].append(rec)

    elif tool == "Bash":
        cmd = (ti.get("command") or "")
        # 计数（只记录，不拦截）
        norm = re.sub(r"\s+", " ", cmd.strip())[:200]
        st["bash_cmds"].append(norm)
        if st["bash_cmds"].count(norm) == 3:
            # 同一条命令第 3 次 = G4 循环门的可执行代理指标
            st["repeat_suspects"].append({"cmd": norm,
                                          "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
        if ti.get("run_in_background") or "--bg" in cmd or "start-bg" in cmd:
            st["background_cmds"].append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "cmd": norm,
                "agent_id": agent_id,
            })

    elif tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        fp = ti.get("file_path") or ti.get("notebook_path") or "?"
        st["writes"].append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "tool": tool, "path": fp[:200],
                             "agent_id": agent_id})

    elif tool == "WebFetch":
        st["webfetch_count"] = int(st.get("webfetch_count", 0)) + 1

    # 上限，防止状态文件无限增长
    st["agent_requests"] = st["agent_requests"][-50:]
    st["background_cmds"] = st["background_cmds"][-50:]
    st["writes"] = st["writes"][-200:]
    st["bash_cmds"] = st["bash_cmds"][-400:]
    st["repeat_suspects"] = st["repeat_suspects"][-50:]
    return st, None


def main():
    data = read_input()
    session_id = data.get("session_id") or "nosession"
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    agent_id = data.get("agent_id")

    # ⚠️ 本门读 policy —— 这在 3.5.3 之前是缺的。
    #    修前：policy 写 `closeout.enabled = false`（"默认关闭"），
    #    但本文件完全不读 policy，无条件运行并写盘。
    #    「声明与行为不一致」正是本项目要消灭的模式（同 #2、#21 的形状）。
    #    现在 enabled=false 会真的让本门提前退出。
    policy = load_policy()
    if not (policy.get("closeout") or {}).get("enabled", True):
        allow()

    path = state_path(session_id, "closeout")
    # ⚠️ 必须进临界区：同一批并行工具调用会同时读同一个文件，
    # 各自写回 → 后写的覆盖先写的，收口记录静默丢条目。
    # 这和 G1 的预算是同一类问题（读-改-写竞态），只是后果更隐蔽。
    locked_update(path, lambda st: _record(st, session_id, tool, ti, agent_id))

    # --- 用量警告（3.5.3 新增）---
    #
    # 为什么是警告而不是拦截：
    #   硬拦截需要一个"合理阈值"，而当前【没有实测分布】作为依据。
    #   在没有数据时硬拦 = 拿用户当实验品，正常调研任务会被误伤。
    #   所以先让用量【可见】，跑一段时间拿到真实分布，再决定是否硬拦。
    #
    # 修前的问题不是"没做限额"，而是"计了数但没有任何人看得到"——
    #   那和没计数是一样的，而且 policy 里写个数字不执行更是"假能力"。
    #   现在每次越过阈值都会打印一行，用户和模型都能看到。
    if tool in ("Bash", "WebFetch"):
        budget = policy.get("budget") or {}
        warn_at = budget.get("bash_warn_at" if tool == "Bash" else "webfetch_warn_at")
        if warn_at:
            st = load_state(path) or {}
            used = len(st.get("bash_cmds") or []) if tool == "Bash" \
                else int(st.get("webfetch_count") or 0)
            # 只在【恰好越过】阈值时提醒一次，避免每次调用都刷屏
            if used == int(warn_at):
                sys.stderr.write(
                    "【用量提醒】本次会话 %s 调用已达 %d 次。\n"
                    "  这不是拦截，只是一次可见性提示。\n"
                    "  若这是正常的深度调研，忽略即可；\n"
                    "  若你在反复重试同类命令，考虑先确认方向再继续。\n"
                    % (tool, used))
                sys.stderr.flush()

    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)   # 纯旁路，绝不影响主流程
