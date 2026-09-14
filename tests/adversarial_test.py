# -*- coding: utf-8 -*-
"""
对抗测试：用自测【没有用过】的输入，找误伤和绕过。

与 four_gate_selftest.py 的分工：
  自测  = 证明设计的行为成立
  对抗  = 证明设计之外的输入不会翻车（fail-open 方向对不对、会不会误伤日常）

跑法：python tests/adversarial_test.py
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(os.path.dirname(HERE), "adapters", "claude-code", "hooks")
PY = sys.executable

results = []


def run(script, payload, tmp):
    env = dict(os.environ)
    env["CLAUDE_BUDGET_STATE_DIR"] = tmp
    p = subprocess.run([PY, os.path.join(HOOKS, script)],
                       input=json.dumps(payload).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return p.returncode


def an(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail[:300])


def main():
    tmp = tempfile.mkdtemp(prefix="bg-adv-")
    sid = "adv"

    print("\n===== A. 不能误伤日常操作（fail-open 方向） =====")
    daily = [
        "ls -la app/", "git status --short", "npm test",
        "node --check app.js", "python -m pytest tests/",
        "grep -rn 'TODO' src/", "taskkill /PID 1234 /F",
        "rm -rf ./dist/_build",                 # 具体路径的 rm -rf 是合法的
        "curl -s http://127.0.0.1:8080/api/status",
    ]
    bad = []
    for cmd in daily:
        if run("destructive_gate.py",
               {"session_id": sid, "tool_name": "Bash",
                "tool_input": {"command": cmd}}, tmp) != 0:
            bad.append(cmd)
    an("9 条日常命令全部放行", not bad, "被误伤: %s" % bad)

    print("\n===== B. 写作场景不能误伤（互审发现的真实缺陷） =====")
    docs = [
        ("write", "审计报告.md", "deepseek 跑过 pkill -f \"proxy.mjs\"，侥幸没中招"),
        ("write", "notes.md", "不要用 killall，改用精确 PID"),
        ("write", "rule.md", "禁止 rm -rf / 这类命令"),          # 句子中间，不该拦
        ("write", "note2.md", "不要用 killall 或 pkill，改用 PID"),
        ("edit", "readme.md", "文档里提到 /tmp 的坑"),
    ]
    bad = []
    for tool, fp, txt in docs:
        key = "content" if tool == "write" else "new_string"
        rc = run("destructive_gate.py",
                 {"session_id": sid, "tool_name": "Write" if tool == "write" else "Edit",
                  "tool_input": {"file_path": fp, key: txt}}, tmp)
        if rc != 0:
            bad.append(fp)
    an("4 篇写文档场景全部放行", not bad, "被误伤: %s" % bad)

    print("\n===== C. 含糊回复不该拦（避免净摩擦） =====")
    casual = [
        "好，我看看。",
        "这个文件在哪？",
        "已读取文件，运行了命令。",
        "好的，可以用。",            # 无完成措辞
        "翻译完成度 80%。",
    ]
    bad = []
    for m in casual:
        if run("effect_gate.py",
               {"session_id": sid, "stop_hook_active": False,
                "last_assistant_message": m}, tmp) != 0:
            bad.append(m)
    an("5 条含糊回复全部放行", not bad, "被误伤: %s" % bad)

    print("\n===== D. 绕过尝试（应该被拦） =====")
    bypass = [
        "pkill  -f  x",           # 多空格
        "PKILL -f x",             # 大写
        "pkill\t-f\tx",           # tab
        "cd / && rm -rf *",       # 组合
    ]
    missed = []
    for cmd in bypass:
        if run("destructive_gate.py",
               {"session_id": sid, "tool_name": "Bash",
                "tool_input": {"command": cmd}}, tmp) != 2:
            missed.append(cmd)
    an("4 条绕过变体全部拦住", not missed, "漏过: %s" % missed)

    print("\n===== E. 边界与畸形输入不能崩 =====")
    weird = [
        {"session_id": "x", "tool_name": "Bash", "tool_input": {}},          # 无 command
        {"session_id": "x", "tool_name": "Agent", "tool_input": {}},         # 无参数
        {"session_id": "", "tool_name": "Bash", "tool_input": {"command": ""}},
        {"session_id": "x", "tool_name": "Unknown", "tool_input": {"a": 1}},
    ]
    crashed = []
    for w in weird:
        try:
            rc = run("destructive_gate.py", w, tmp)
            if rc not in (0, 2):
                crashed.append((w, rc))
        except Exception as e:
            crashed.append((w, repr(e)))
    an("4 条畸形输入不崩且给出合法码", not crashed, str(crashed))

    # 完全空的 stdin
    p = subprocess.run([PY, os.path.join(HOOKS, "effect_gate.py")],
                       input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    an("空 stdin 不崩", p.returncode == 0, "rc=%s" % p.returncode)

    # 非法 JSON
    p = subprocess.run([PY, os.path.join(HOOKS, "effect_gate.py")],
                       input=b"{not json", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    an("非法 JSON 不崩（fail-open）", p.returncode == 0, "rc=%s" % p.returncode)

    print("\n===== E2. '完成 + 免责声明' 不能绕过 G3 =====")
    # 第四个互审发现：NOT_MEASURED 短路写得太宽，导致
    # 「已经修复完成，但未验证」被放行 —— 这正是方案点名批评的
    # 「用动词代替状态」：宣布完成，再挂一个免责声明。
    bypass_claims = [
        "已经修复完成，但未验证。",
        "已完成。未实测。",
        "已修复，未做端到端验证。",
        "已部署完成，但没有验证生效。",
    ]
    leaked = []
    for m in bypass_claims:
        rc = run("effect_gate.py",
                 {"session_id": sid, "stop_hook_active": False,
                  "last_assistant_message": m}, tmp)
        if rc != 2:
            leaked.append(m)
    an("4 条『完成+免责声明』全部被拦", not leaked, "绕过了: %s" % leaked)

    # 诚实的措辞必须仍然放行（不能矫枉过正成"惩罚诚实"）
    honest = [
        "已改完，未验证生效（NOT_MEASURED）",
        "已修改，未验证。",
        "改动已落地，NOT_MEASURED。",
    ]
    punished = []
    for m in honest:
        rc = run("effect_gate.py",
                 {"session_id": sid, "stop_hook_active": False,
                  "last_assistant_message": m}, tmp)
        if rc != 0:
            punished.append(m)
    an("3 条诚实措辞仍然放行（不惩罚诚实）", not punished, "被误拦: %s" % punished)

    print("\n===== E3. G3 误报：不同段落的两个清单一不能算矛盾 =====")
    # V2.5 修的真实误报：我在真实会话里被误拦过一次 ——
    # "A 已验证 ✅" 和 "B 未验证 ⬜" 分属不同段落、说不同的事，
    # 旧判定按全文共现，误判为自相矛盾。误报率实测约 1/3。
    multi_section = (
        "## 结果\n"
        "G1 已验证通过，G3 也已验证。\n"
        "\n"                      # ← 空行 = 分段
        "## 未验证项\n"
        "- MCP matcher 未验证\n"
        "- 与 cbm 共存未验证\n"
    )
    rc = run("effect_gate.py",
             {"session_id": sid, "stop_hook_active": False,
              "last_assistant_message": multi_section}, tmp)
    an("分段的『已验证 + 未验证项』不再误拦", rc == 0, "rc=%s" % rc)

    same_para = "已修复，但未验证。"
    rc = run("effect_gate.py",
             {"session_id": sid, "stop_hook_active": False,
              "last_assistant_message": same_para}, tmp)
    an("同段的『已修复 + 未验证』仍然拦下", rc == 2, "rc=%s" % rc)

    print("\n===== E4. G3 use-mention：引用不能当断言 =====")
    # V2.6 修的第二个真实误报：我在【报告门的行为】时引用了测试用例的字面内容，
    # 门在同一段里看到两个词就判自相矛盾。实测三种引用形式全部误报。
    # 这是经典的 use-mention 问题。
    mentions = [
        ("围栏代码块", "**验证：**\n```\n分段的「已验证 + 未验证项」 rc=0\n```"),
        ("行内中文引号", "测试显示「已验证 + 未验证」这一条应该放行。"),
        ("块引用", "> 旧行为：「已修复，但未验证」被放过"),
        ("行内反引号", "条目 `已验证 + 未验证项` 现在是分开的。"),
        ("英文双引号", 'It reports "已修复，但未验证" as a bypass.'),
    ]
    leaked = []
    for label, m in mentions:
        rc = run("effect_gate.py",
                 {"session_id": sid, "stop_hook_active": False,
                  "last_assistant_message": m}, tmp)
        if rc != 0:
            leaked.append(label)
    an("5 种引用形式全部放行（不再是误报）", not leaked, "仍误拦: %s" % leaked)

    # 真实断言必须仍然拦住 —— 不能矫枉过正
    asserts = [
        ("同段", "已修复，但未验证。"),
        ("目标达成", "已完成。未验证。"),
    ]
    missed = []
    for label, m in asserts:
        rc = run("effect_gate.py",
                 {"session_id": sid, "stop_hook_active": False,
                  "last_assistant_message": m}, tmp)
        if rc != 2:
            missed.append(label)
    an("真实断言仍被拦下（未矫枉过正）", not missed, "漏放: %s" % missed)

    print("\n===== F. 锁太严的反面：环境故障不能永久卡死用户 =====")
    # 上一版"保守拒绝"引入的新失败模式：
    # 状态目录不可写时，所有派生被永久拒绝，而提示说"请稍等片刻重试"——
    # 重试一万次也不会好，用户只能卸载。预算是防浪费的，不是防用户的。
    blocker = tempfile.mkdtemp(prefix="bg-blocker-")
    blocker_file = os.path.join(blocker, "ocupado")
    with open(blocker_file, "w") as f:
        f.write("x")
    bad_state = os.path.join(blocker_file, "sub")

    env = dict(os.environ)
    env["CLAUDE_BUDGET_STATE_DIR"] = bad_state
    p = subprocess.run([PY, os.path.join(HOOKS, "budget_gate.py")],
                       input=json.dumps({"session_id": "envfail",
                                         "tool_name": "Agent",
                                         "tool_input": {"subagent_type": "E",
                                                        "description": "x"}}).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    err = p.stderr.decode("utf-8", "replace")
    an("状态目录不可写时【放行】而非永久拒绝", p.returncode == 0,
       "rc=%s (期望 0=放行)" % p.returncode)
    an("环境故障的降级是【可见】的", "已临时停用" in err or "无法创建" in err,
       err[:250])
    an("提示不再误导用户去重试", "请稍等片刻重试" not in err, err[:250])

    print("\n===== G. 锁被占用（临时）仍然保守拒绝 =====")
    # 与 F 相反：锁被别人占着是【临时】状态，保守拒绝是对的。
    # 两者必须区分开，否则要么卡死用户、要么放弃配额。
    import time as _t
    lockdir = os.path.join(tmp, "busy.budget.json.lock")
    os.makedirs(lockdir, exist_ok=True)
    env2 = dict(os.environ)
    env2["CLAUDE_BUDGET_STATE_DIR"] = tmp
    # 让 stale_after 判定不生效：把锁目录 mtime 设为现在（默认 stale_after=15s）
    p = subprocess.run([PY, os.path.join(HOOKS, "budget_gate.py")],
                       input=json.dumps({"session_id": "busy",
                                         "tool_name": "Agent",
                                         "tool_input": {"subagent_type": "E",
                                                        "description": "x"}}).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env2)
    err = p.stderr.decode("utf-8", "replace")
    an("锁超时 → 保守拒绝（不无锁放行）", p.returncode == 2, "rc=%s" % p.returncode)
    an("拒绝理由说明是锁等待", "锁" in err, err[:250])
    try:
        os.rmdir(lockdir)
    except Exception:
        pass

    print("\n===== H. 预算优先级 =====")
    s2 = "adv-prio"
    run("inject_budget.py", {"session_id": s2, "prompt": "可以派 Agent"}, tmp)
    r1 = run("budget_gate.py", {"session_id": s2, "tool_name": "Agent",
                                "tool_input": {"subagent_type": "E", "description": "a"}}, tmp)
    an("口语『可以派 Agent』→ 放行", r1 == 0, "rc=%s" % r1)

    s3 = "adv-prio2"
    run("inject_budget.py",
        {"session_id": s3, "prompt": "可以派 Agent，但 agent_spawns: 0 算了"}, tmp)
    r2 = run("budget_gate.py", {"session_id": s3, "tool_name": "Agent",
                                "tool_input": {"subagent_type": "E", "description": "a"}}, tmp)
    an("显式声明覆盖口语 → 拦", r2 == 2, "rc=%s" % r2)

    s4 = "adv-forbid"
    run("inject_budget.py", {"session_id": s4, "prompt": "不要开后台，直接做"}, tmp)
    r3 = run("budget_gate.py", {"session_id": s4, "tool_name": "Agent",
                                "tool_input": {"subagent_type": "E", "description": "a"}}, tmp)
    an("『不要开后台』→ 强制 0", r3 == 2, "rc=%s" % r3)

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print("对抗测试：%d / %d 通过" % (passed, len(results)))
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
