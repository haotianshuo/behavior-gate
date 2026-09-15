# -*- coding: utf-8 -*-
"""
四关自测 —— 用真实脚本跑，不是演示。

跑法：
    python tests/four_gate_selftest.py

它会把每个 hook 当子进程启动，喂真实的 stdin JSON，检查真实的 exit code。
exit code 是本方案唯一有强制力的信号，所以必须实测，不能靠读代码推断。
"""

import json
import os
import subprocess
import sys
import tempfile

# --- 控制台编码（可移植性）---
# Windows 控制台默认 GBK(cp936)；print 中文时遇到非 GBK 字符会抛
# UnicodeEncodeError，脚本直接崩。实测：CI 在 windows-latest 上必崩，
# ubuntu/macos 正常 —— 本地 Git Bash 恰好是 UTF-8，所以看不见。
# 详见 lib/console_utf8.py。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
# --- end ---


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(ROOT, "adapters", "claude-code", "hooks")
# 包内策略文件。测试必须【显式指定】它，不能依赖机器上的全局副本 ——
# 修 #29 时踩到：load_policy 的查找顺序里 ~/.claude/behavior-policy.json
# 优先于包内那份，于是同一份代码在不同机器上读到不同策略，
# 测试结果随环境而变（本机上那份是 2.5.0 的旧副本，closeout.enabled=false，
# 导致「收口状态已落盘」这条断言失败）。
# 测试要证明的是【这份代码】的行为，所以必须锁定【这份代码自带的策略】。
DEFAULT_POLICY_PATH = os.path.join(ROOT, "policy", "behavior-policy.json")
PY = sys.executable

results = []


def run(script, payload, state_dir, policy=None):
    """跑一个 hook，返回 (exitcode, stdout, stderr)。

    policy 默认为包内策略 —— 显式隔离，避免读到机器上的全局副本。
    """
    env = dict(os.environ)
    env["CLAUDE_BUDGET_STATE_DIR"] = state_dir
    env["CLAUDE_BEHAVIOR_POLICY"] = policy or DEFAULT_POLICY_PATH
    p = subprocess.run(
        [PY, os.path.join(HOOKS, script)],
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail.replace("\n", "\n       ")[:500])


def main():
    tmp = tempfile.mkdtemp(prefix="bg-selftest-")
    sid = "selftest-session"

    print("\n===== 第 1 关：只读关 —— 第一步是否只读 =====")
    # 用户提交一个"只看不改"的 prompt，注入端应写入预算状态
    code, out, err = run("inject_budget.py",
                         {"session_id": sid, "prompt": "先只读看看这个项目"},
                         tmp)
    check("inject_budget 正常退出", code == 0, "exit=%d stderr=%s" % (code, err))
    check("注入了预算上下文", "agent_spawns" in out, out[:200])
    state_file = os.path.join(tmp, "%s.budget.json" % sid)
    check("预算状态已落盘", os.path.isfile(state_file), state_file)

    print("\n===== 第 2 关：预算关 —— 默认禁止派生 =====")
    code, out, err = run("budget_gate.py",
                         {"session_id": sid, "tool_name": "Agent",
                          "tool_input": {"subagent_type": "Explore",
                                         "description": "调研一下"}},
                         tmp)
    check("默认 agent_spawns=0 时派 Agent 被阻断 (exit 2)", code == 2,
          "exit=%d" % code)
    check("阻断理由说明了几何事实(53%%)", "53%" in err, err[:300])
    check("阻断理由给了两条出路", "自己直接做" in err and "agent_spawns" in err,
          err[:400])

    print("\n===== 第 3 关：预算可放行 —— 用户显式批准后 =====")
    # 用户在 prompt 里放行 2 个
    run("inject_budget.py",
        {"session_id": sid, "prompt": "这个任务 agent_spawns: 2 可以派"},
        tmp)
    c1, o1, e1 = run("budget_gate.py",
                     {"session_id": sid, "tool_name": "Agent",
                      "tool_input": {"subagent_type": "Explore", "description": "A"}},
                     tmp)
    c2, o2, e2 = run("budget_gate.py",
                     {"session_id": sid, "tool_name": "Agent",
                      "tool_input": {"subagent_type": "Explore", "description": "B"}},
                     tmp)
    c3, o3, e3 = run("budget_gate.py",
                     {"session_id": sid, "tool_name": "Agent",
                      "tool_input": {"subagent_type": "Explore", "description": "C"}},
                     tmp)
    check("第 1 次派生放行", c1 == 0, "exit=%d" % c1)
    check("第 2 次派生放行", c2 == 0, "exit=%d" % c2)
    check("第 3 次超预算被阻断", c3 == 2, "exit=%d" % c3)
    check("阻断信息是「预算已用尽」", "预算已用尽" in e3, e3[:200])

    print("\n===== 第 4 关：子代理内禁止再派 =====")
    run("inject_budget.py",
        {"session_id": sid, "prompt": "agent_spawns: 5"},
        tmp)
    c, o, e = run("budget_gate.py",
                  {"session_id": sid, "tool_name": "Agent", "agent_id": "sub-123",
                   "agent_type": "general-purpose",
                   "tool_input": {"subagent_type": "Explore", "description": "嵌套"}},
                  tmp)
    check("子代理内派孙代理被阻断", c == 2, "exit=%d" % c)
    check("阻断理由指出嵌套", "子代理内" in e, e[:200])

    print("\n===== G5：危险门 =====")
    sid2 = "selftest-g5"

    c, o, e = run("destructive_gate.py",
                  {"session_id": sid2, "tool_name": "Bash",
                   "tool_input": {"command": 'pkill -f "proxy.mjs"'}},
                  tmp)
    check("pkill -f 被阻断", c == 2, "exit=%d" % c)
    check("给出了精确替代写法", "taskkill /PID" in e, e[:300])

    c, o, e = run("destructive_gate.py",
                  {"session_id": sid2, "tool_name": "Bash",
                   "tool_input": {"command": "taskkill /PID 15072 /F"}},
                  tmp)
    check("精确 PID 杀进程放行", c == 0, "exit=%d stderr=%s" % (c, e[:200]))

    c, o, e = run("destructive_gate.py",
                  {"session_id": sid2, "tool_name": "Bash",
                   "tool_input": {"command": "find /tmp -name '*.log'"}},
                  tmp)
    check("/tmp 在 Windows 的陷阱被阻断", c == 2, "exit=%d" % c)
    check("解释了 C:\\tmp 的原因", "C:\\tmp" in e, e[:300])

    c, o, e = run("destructive_gate.py",
                  {"session_id": sid2, "tool_name": "Bash",
                   "tool_input": {"command": "ls -la app/"}},
                  tmp)
    check("普通命令放行", c == 0, "exit=%d" % c)

    c, o, e = run("destructive_gate.py",
                  {"session_id": sid2, "tool_name": "Write",
                   "tool_input": {"file_path": "x.py", "content": "rm -rf /"}},
                  tmp)
    check("Write 内容里的危险串也拦", c == 2, "exit=%d" % c)

    print("\n===== G3：生效门 =====")
    sid3 = "selftest-g3"

    c, o, e = run("effect_gate.py",
                  {"session_id": sid3, "stop_hook_active": False,
                   "last_assistant_message": "已经修复了这个问题，全部通过。"},
                  tmp)
    check("无证据宣布完成被阻断", c == 2, "exit=%d" % c)
    check("阻断信息给出证据块模板", "证据块" in e and "L3" in e, e[:300])

    c, o, e = run("effect_gate.py",
                  {"session_id": sid3, "stop_hook_active": False,
                   "last_assistant_message":
                       "已经修复。\n\n**证据**：L3 端到端\n- 下次看到：状态栏显示绿色\n- 在哪看：监控面板首页"},
                  tmp)
    check("带 L3 证据块时放行", c == 0, "exit=%d stderr=%s" % (c, e[:300]))

    c, o, e = run("effect_gate.py",
                  {"session_id": sid3, "stop_hook_active": False,
                   "last_assistant_message": "今天天气不错，我读了三个文件。"},
                  tmp)
    check("没有完成措辞时放行", c == 0, "exit=%d" % c)

    c, o, e = run("effect_gate.py",
                  {"session_id": sid3, "stop_hook_active": True,
                   "last_assistant_message": "已经修复了。"},
                  tmp)
    # ⚠️ 断言已修正（#43）：修前这里断言"无条件放行"，
    #    而那正是「原样重发即可通过」的缺陷。
    #    现在的语义是：重试仍放行（防死循环），但【放行必须可见】。
    check("重试轮次放行，且放行可见",
          c == 0 and "可见的放行" in e,
          "exit=%d stderr=%s" % (c, e[:200]))

    # --- #43 回归用例：G3 提示模板自己要求的字段，不能成为绕过门的钥匙 ---
    #
    # 实测缺陷（外部复核发现）：门被拦时打印的模板要求写
    #     - 未验证项：<明确列出；没有就写「无」>
    # 而用户真的照写「- 未验证项：无」时，它被当成【承认有未验证】，
    # 走进豁免分支 → 风险=高 + 证据=L1 被放行。
    # 换成「- 遗留事项：无」则正常拦截。
    #
    # 为什么原 154 项没抓到：所有 G3 用例的证据块都不含「未验证项」那一行 ——
    # 测试是按门的【规则】写的，不是按门【要求用户照写的模板】写的。
    _tmpl = ("**证据**：%s\n**风险级别**：高\n"
             "- 用户下次会看到什么不同：X\n- 在哪看：Y\n- 我怎么确认它生效了：Z")

    c, o, e = run("effect_gate.py",
                  {"session_id": sid3, "stop_hook_active": False,
                   "last_assistant_message":
                       "已完成修复。\n\n" + _tmpl % "L1 静态" + "\n- 未验证项：无"},
                  tmp)
    check("按模板写『未验证项：无』不能豁免等级检查", c == 2,
          "exit=%d（L1 撑高风险应被拦）" % c)

    c, o, e = run("effect_gate.py",
                  {"session_id": sid3, "stop_hook_active": False,
                   "last_assistant_message":
                       "已改完。\n\n" + _tmpl % "L1 静态" + "\n- 未验证项：MCP matcher 未验证"},
                  tmp)
    check("『未验证项：<具体内容>』仍算诚实声明（不惩罚诚实）", c == 0,
          "exit=%d" % c)

    c, o, e = run("effect_gate.py",
                  {"session_id": sid3, "stop_hook_active": False,
                   "last_assistant_message":
                       "已完成。\n\n" + _tmpl % "L3 端到端" + "\n- 未验证项：无"},
                  tmp)
    check("L3 证据 + 未验证项：无 正常放行", c == 0, "exit=%d" % c)

    print("\n===== G6：收口旁路 =====")
    sid4 = "selftest-g6"
    run("closeout_gate.py",
        {"session_id": sid4, "tool_name": "Agent",
         "tool_input": {"subagent_type": "Explore", "description": "调研"}},
        tmp)
    run("closeout_gate.py",
        {"session_id": sid4, "tool_name": "Bash",
         "tool_input": {"command": "node start-bg", "run_in_background": True}},
        tmp)
    c, o, e = run("closeout_gate.py",
                  {"session_id": sid4, "tool_name": "Edit",
                   "tool_input": {"file_path": "app/x.py"}},
                  tmp)
    check("收口采集不阻断", c == 0, "exit=%d" % c)
    co = os.path.join(tmp, "%s.closeout.json" % sid4)
    ok = os.path.isfile(co)
    check("收口状态已落盘", ok, co)
    if ok:
        st = json.load(open(co, encoding="utf-8"))
        check("记录了 agent 派生", len(st.get("agent_requests", [])) == 1, str(st))
        check("记录了后台命令", len(st.get("background_cmds", [])) == 1, str(st))
        check("记录了写操作", len(st.get("writes", [])) == 1, str(st))

    print("\n===== G3：风险分级（互审发现文档承诺未实现） =====")
    sid5 = "selftest-g3risk"

    c, o, e = run("effect_gate.py",
                  {"session_id": sid5, "stop_hook_active": False,
                   "last_assistant_message":
                       "已完成。\n\n**证据**：L1 静态\n风险级别：低\n- 产物：a.md 已生成"},
                  tmp)
    check("低风险 + L1 证据 → 放行", c == 0, "exit=%d stderr=%s" % (c, e[:250]))

    c, o, e = run("effect_gate.py",
                  {"session_id": sid5, "stop_hook_active": False,
                   "last_assistant_message":
                       "已修复。\n\n**证据**：L1 静态\n风险级别：中\n- 读了代码"},
                  tmp)
    check("中风险 + 只有 L1 → 必须被拦", c == 2, "exit=%d" % c)
    check("拦截理由指出风险等级不够", "风险" in e and "L1" in e, e[:250])

    c, o, e = run("effect_gate.py",
                  {"session_id": sid5, "stop_hook_active": False,
                   "last_assistant_message":
                       "已修复。\n\n**证据**：L2 动态\n风险级别：中\n- 隔离测试通过"},
                  tmp)
    check("中风险 + L2 → 放行", c == 0, "exit=%d stderr=%s" % (c, e[:250]))

    c, o, e = run("effect_gate.py",
                  {"session_id": sid5, "stop_hook_active": False,
                   "last_assistant_message":
                       "已部署。\n\n**证据**：L2 动态\n风险级别：高\n- 跑了单测"},
                  tmp)
    check("高风险 + 只有 L2 → 必须被拦", c == 2, "exit=%d" % c)

    c, o, e = run("effect_gate.py",
                  {"session_id": sid5, "stop_hook_active": False,
                   "last_assistant_message":
                       "已完成。\n\n**证据**：L1 静态\n- 没有写风险级别"},
                  tmp)
    check("未声明风险级别 + L1 → 按中风险从严，拦", c == 2, "exit=%d" % c)

    print("\n===== G3：Stop 输出 schema（必须是 decision=block） =====")
    c, o, e = run("effect_gate.py",
                  {"session_id": sid5, "stop_hook_active": False,
                   "last_assistant_message": "已经修复完成了。"},
                  tmp)
    check("Stop 事件输出 decision=block", '"decision"' in o and '"block"' in o,
          "stdout=%s" % o[:200])
    check("Stop 事件不再输出 permissionDecision",
          "permissionDecision" not in o, "stdout=%s" % o[:200])

    print("\n===== G5：Edit/Write 内容不应套用命令正则 =====")
    sid6 = "selftest-g5c"

    c, o, e = run("destructive_gate.py",
                  {"session_id": sid6, "tool_name": "Write",
                   "tool_input": {"file_path": "报告.md",
                                  "content": "审计发现 deepseek 跑过 pkill -f \"proxy.mjs\""}},
                  tmp)
    check("写含 pkill 字样的文档 → 放行（内容非命令）", c == 0, "exit=%d stderr=%s" % (c, e[:250]))

    c, o, e = run("destructive_gate.py",
                  {"session_id": sid6, "tool_name": "Edit",
                   "tool_input": {"file_path": "notes.md",
                                  "new_string": "文档里提到 killall 的用法"}},
                  tmp)
    check("Edit 含 killall 字样 → 放行", c == 0, "exit=%d" % c)

    c, o, e = run("destructive_gate.py",
                  {"session_id": sid6, "tool_name": "Write",
                   "tool_input": {"file_path": "evil.sh",
                                  "content": "rm -rf / --no-preserve-root"}},
                  tmp)
    check("写含 rm -rf / 的脚本 → 仍拦", c == 2, "exit=%d" % c)

    c, o, e = run("destructive_gate.py",
                  {"session_id": sid6, "tool_name": "Bash",
                   "tool_input": {"command": "echo pkill is a word"}},
                  tmp)
    check("Bash 里出现 pkill 字样 → 仍拦（命令上下文）", c == 2, "exit=%d" % c)

    print("\n===== G5：MCP 工具覆盖 =====")
    c, o, e = run("destructive_gate.py",
                  {"session_id": sid6, "tool_name": "mcp__filesystem__write_file",
                   "tool_input": {"path": "x", "content": "rm -rf /"}},
                  tmp)
    check("MCP 写工具内容含 rm -rf / → 拦", c == 2, "exit=%d" % c)
    frag = open(os.path.join(os.path.dirname(HERE),
                             "adapters", "claude-code", "settings.fragment.json"),
                encoding="utf-8").read()
    check("settings 片段含 mcp 写工具 matcher", "mcp__" in frag, frag[:200])

    print("\n===== G1：并发计数不丢失（30 轮硬门槛）=====")
    # ⚠️ 跑 1 次不算数。这个缺陷约 10% 概率触发，
    # 抽样 3 次全绿的概率约 73% —— 靠"连跑 3 次"证明"稳定"本身就是 P0-1。
    # 所以这里必须循环 30 轮，全绿才算过。
    import concurrent.futures as cf

    race_bad = []
    RACE_ROUNDS = 60
    RACE_N = 6

    for rnd in range(RACE_ROUNDS):
        sid7 = "selftest-race-%d" % rnd
        run("inject_budget.py",
            {"session_id": sid7, "prompt": "agent_spawns: 3"}, tmp)

        def _spawn(i, _sid=sid7):
            return run("budget_gate.py",
                       {"session_id": _sid, "tool_name": "Agent",
                        "tool_input": {"subagent_type": "Explore",
                                       "description": "并发%d" % i}},
                       tmp)[0]

        with cf.ThreadPoolExecutor(max_workers=RACE_N) as ex:
            codes = list(ex.map(_spawn, range(RACE_N)))
        allowed = sum(1 for x in codes if x == 0)
        if allowed != 3:
            race_bad.append("轮%d 放行%d次 %s" % (rnd, allowed, codes))

    check("并发 %d 次派生 × %d 轮，每轮恰好放行 3 次" % (RACE_N, RACE_ROUNDS),
          not race_bad,
          "失败 %d/%d 轮：%s" % (len(race_bad), RACE_ROUNDS, race_bad[:3]))

    print("\n===== G3：NOT_MEASURED 的语义（V2.4 收紧） =====")
    # V2.2 让「任何含"未验证"的消息」直接放行 —— 第四个互审发现这留了后门：
    # 「已经修复完成，但未验证」会被放过，而那正是"用动词代替状态"。
    # V2.4 改成：承认未验证 ≠ 豁免，而是【不许再说目标达成】。
    sid9 = "selftest-notmeasured"
    nm_allow = [
        ("已改完，未验证生效（NOT_MEASURED）", "动作完成 + 未验证"),
        ("已修改，未做端到端验证。", "已修改 + 未验证"),
        ("改动已落地，NOT_MEASURED。", "已落地 + NOT_MEASURED"),
    ]
    nm_deny = [
        ("已完成。**证据**：NOT_MEASURED", "目标达成 + NOT_MEASURED"),
        ("已修复。**证据**：NOT_MEASURED", "已修复 + NOT_MEASURED"),
        ("已经修复完成，但未验证。", "完成 + 免责声明"),
        ("已部署，未验证生效。", "已部署 + 未验证"),
    ]
    nm_bad = []
    for msg, label in nm_allow:
        rc, _o, _e = run("effect_gate.py",
                         {"session_id": sid9, "stop_hook_active": False,
                          "last_assistant_message": msg}, tmp)
        if rc != 0:
            nm_bad.append("误拦 " + label)
    for msg, label in nm_deny:
        rc, _o, _e = run("effect_gate.py",
                         {"session_id": sid9, "stop_hook_active": False,
                          "last_assistant_message": msg}, tmp)
        if rc != 2:
            nm_bad.append("漏放 " + label)
    check("未验证：动作类放行 / 目标达成类拦下", not nm_bad, str(nm_bad))

    rc, _o, _e = run("effect_gate.py",
                     {"session_id": sid9, "stop_hook_active": False,
                      "last_assistant_message": "已经修复完成了。"}, tmp)
    check("对照：纯完成词无证据 → 仍拦", rc == 2, "exit=%d" % rc)

    print("\n===== G6：记录真实 agent_id =====")
    sid8 = "selftest-g6id"
    run("closeout_gate.py",
        {"session_id": sid8, "tool_name": "Agent", "agent_id": "parent-1",
         "tool_input": {"subagent_type": "Explore", "description": "调研A"}},
        tmp)
    co8 = os.path.join(tmp, "%s.closeout.json" % sid8)
    st8 = json.load(open(co8, encoding="utf-8"))
    rec = st8.get("agent_requests") or []
    check("G6 记录的是 agent_requests（含类型+描述）",
          "agent_requests" in st8, str(list(st8.keys())))
    check("G6 记录里带 subagent_type",
          any(r.get("subagent_type") == "Explore" for r in rec), str(rec))
    check("G6 不再用会误导的 agent_ids 命名", "agent_ids" not in st8, str(list(st8.keys())))

    print("\n===== 失败可见性（关键：静默失败=门不存在） =====")
    bad = os.path.join(tmp, "bad-policy.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ this is not json }")
    c, o, e = run("inject_budget.py",
                  {"session_id": "x", "prompt": "hi"}, tmp, policy=bad)
    check("策略文件损坏时不崩（回退默认）", c == 0, "exit=%d" % c)

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("结果：%d / %d 通过" % (passed, total))
    if passed < total:
        print("\n失败项：")
        for n, ok, d in results:
            if not ok:
                print("  - %s\n    %s" % (n, d[:300]))
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
