# -*- coding: utf-8 -*-
"""
安装链路测试：经过 .cmd 包装器 + 真实 Claude Code hook payload。

为什么必须单独测这一层：
  自测直接调 python 文件，绕过了 .cmd 包装器和 cmd.exe。
  互审③说得对 —— 「自测通过」不能证明「装上去有效」。
  这一层专门覆盖：.cmd 能否跑、退出码能否透传、路径含空格能否处理。

跑法：python tests/cmd_chain_test.py
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
HOOKS = os.path.join(os.path.dirname(HERE), "adapters", "claude-code", "hooks")
WINSHELL = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"


def _render_fragment():
    """按 install.py 的规则渲染 settings.fragment.json，得到真实注册的命令。"""
    frag = json.load(open(os.path.join(os.path.dirname(HERE),
                                       "adapters", "claude-code",
                                       "settings.fragment.json"), encoding="utf-8"))
    py = sys.executable.replace("\\", "/")
    hooks_fwd = HOOKS.replace("\\", "/")
    txt = json.dumps(frag, ensure_ascii=False)
    txt = txt.replace("{{PYTHON}}", py).replace("{{HOOKS}}", hooks_fwd)
    return json.loads(txt)


def run_registered(script_needle, event, payload, state_dir):
    """执行【真实注册的】那条命令（经 sh —— Claude Code 在 Windows 上用 Git Bash）。

    为什么不测 .cmd 包装器：V2.6 起注册的是 python 直调，.cmd 已是死代码，
    且它们把 Python 路径写死 —— 换台电脑就静默 exit 0。
    测真实注册的命令更接近真实。
    """
    frag = _render_fragment()
    cmd = None
    for e in frag["hooks"].get(event, []):
        for h in e.get("hooks", []):
            if script_needle in h["command"]:
                cmd = h["command"]
    if not cmd:
        return -1, "", "配置里找不到 %s" % script_needle
    env = dict(os.environ)
    env["CLAUDE_BUDGET_STATE_DIR"] = state_dir
    p = subprocess.run(["sh", "-c", cmd],
                       input=json.dumps(payload).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail.replace("\n", " | ")[:400])


def main():
    tmp = tempfile.mkdtemp(prefix="bg-cmdchain-")
    sid = "cmdchain"

    print("\n===== 真实注册的命令：路径与退出码 =====")

    # 1) 注入端
    rc, out, err = run_registered("inject_budget", "UserPromptSubmit",
                                  {"session_id": sid, "prompt": "测试"}, tmp)
    check("注入端可执行 (rc=0)", rc == 0, "rc=%s err=%s" % (rc, err[:200]))
    check("注入端透传了 stdout", "agent_spawns" in out, out[:200])

    # 2) 关键：exit 2 必须能穿透
    rc, out, err = run_registered("budget_gate", "PreToolUse",
                                  {"session_id": sid, "tool_name": "Agent",
                                   "tool_input": {"subagent_type": "Explore",
                                                  "description": "x"}}, tmp)
    check("阻断码 exit 2 能穿透", rc == 2, "rc=%s（应为 2）" % rc)

    # 3) 放行路径
    rc, out, err = run_registered("destructive_gate", "PreToolUse",
                                  {"session_id": sid, "tool_name": "Bash",
                                   "tool_input": {"command": "ls -la"}}, tmp)
    check("放行时 rc=0", rc == 0, "rc=%s" % rc)

    # 4) G5 阻断
    rc, out, err = run_registered("destructive_gate", "PreToolUse",
                                  {"session_id": sid, "tool_name": "Bash",
                                   "tool_input": {"command": 'pkill -f "x"'}}, tmp)
    check("G5 阻断码穿透", rc == 2, "rc=%s" % rc)

    # 5) Stop 门 + schema
    rc, out, err = run_registered("effect_gate", "Stop",
                                  {"session_id": sid, "stop_hook_active": False,
                                   "hook_event_name": "Stop",
                                   "last_assistant_message": "已经修复完成了。"}, tmp)
    check("Stop 门阻断码穿透", rc == 2, "rc=%s" % rc)
    check("Stop 门输出 decision=block", '"decision"' in out and '"block"' in out,
          out[:200])

    print("\n===== 路径含空格 =====")
    spaced = tempfile.mkdtemp(prefix="bg with space ")
    rc, out, err = run_registered("budget_gate", "PreToolUse",
                                  {"session_id": "sp", "tool_name": "Agent",
                                   "tool_input": {"subagent_type": "Explore",
                                                  "description": "x"}}, spaced)
    check("状态目录含空格时不崩", rc in (0, 2), "rc=%s err=%s" % (rc, err[:200]))

    print("\n===== 与已有 cbm hooks 共存 =====")
    frag = json.load(open(os.path.join(os.path.dirname(HERE),
                                       "adapters", "claude-code",
                                       "settings.fragment.json"), encoding="utf-8"))
    pre = frag["hooks"]["PreToolUse"]
    matchers = [h.get("matcher") for h in pre]
    check("片段里 PreToolUse 含 Agent matcher", "Agent" in matchers, str(matchers))
    check("片段里不含会误伤待办工具的 Task.*",
          not any(m and "Task" in m for m in matchers), str(matchers))
    check("Stop 段存在且不带 matcher（Stop 不支持 matcher）",
          all("matcher" not in h for h in frag["hooks"]["Stop"]),
          str(frag["hooks"]["Stop"]))

    print("\n===== 关键：命令里不能有写死的机器专有路径 =====")
    # ⚠️ 这是从【另一台电脑】的真实安装反馈里学到的：
    #    V2.6 的 .cmd 包装器把 Python 路径写死成 C:/Program Files/Python312，
    #    在那台机器（Python 装在 AppData\Local\Programs）上，
    #    .cmd 一跑就打印 "python not found" 然后 exit 0 —— 静默放行。
    #    所以必须【机械断言】配置里不含写死路径。
    fragtxt = json.dumps(frag, ensure_ascii=False)
    check("片段里没有写死的 Python312 路径", "Python312" not in fragtxt)
    check("片段里没有 %USERPROFILE%（Git Bash 不展开）",
          "%USERPROFILE%" not in fragtxt)
    check("片段用占位符（而不是写死路径）",
          "{{PYTHON}}" in fragtxt and "{{HOOKS}}" in fragtxt)
    # 渲染后必须没有残留占位符（这才是会被写进 settings 的形态）
    rendered_obj = _render_fragment()
    rendered = json.dumps(rendered_obj, ensure_ascii=False)
    check("渲染后无未替换的占位符", "{{" not in rendered, rendered[:200])

    # ⚠️ 判据不能是「字符串里没有 Python312」——
    #    本机 Python 恰好就在 C:\Program Files\Python312，
    #    那是【探测结果】，不是硬编码。按字符串判会冤枉它。
    #    真正的判据是：渲染出的解释器路径 == 本机探测到的路径。
    import sys as _s
    expect_py = _s.executable.replace("\\", "/")
    got_cmds = [h["command"] for es in rendered_obj["hooks"].values()
                for e in es for h in e.get("hooks", [])]
    check("渲染出的解释器就是本机探测到的那个",
          all(expect_py in c for c in got_cmds),
          "期望 %s，实际样例 %s" % (expect_py, got_cmds[0] if got_cmds else ""))

    print("\n===== 命令能否被 shell 解析（真实翻车过的一层）=====")
    # 这一节补的是一个【真实翻车】：
    # 曾经 11/11 全绿，而真实注册的 hook 一次都没执行 ——
    # 命令格式错导致 cmd.exe 进交互模式，门看起来装了、实际是死的。
    sample = None
    for ent in frag["hooks"]["PreToolUse"]:
        for h in ent.get("hooks", []):
            if "budget_gate.py" in h["command"]:
                sample = h["command"]
    check("片段里能找到 budget_gate 命令", sample is not None, "")

    if sample:
        import sys as _s
        hooks_fwd = HOOKS.replace("\\", "/")
        py = _s.executable.replace("\\", "/")
        real = sample.replace("{{HOOKS}}", hooks_fwd).replace("{{PYTHON}}", py)
        env2 = dict(os.environ)
        env2["CLAUDE_BUDGET_STATE_DIR"] = tmp
        os.makedirs(tmp, exist_ok=True)

        inj = None
        for ent in frag["hooks"]["UserPromptSubmit"]:
            for h in ent.get("hooks", []):
                inj = h["command"].replace("{{HOOKS}}", hooks_fwd).replace(
                    "{{PYTHON}}", py)

        def run_sh(command, payload):
            return subprocess.run(
                ["sh", "-c", command],
                input=json.dumps(payload).encode(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env2,
                cwd=os.path.dirname(HOOKS))

        if inj:
            run_sh(inj, {"session_id": "e2e", "prompt": "x"})

        p = run_sh(real, {"session_id": "e2e", "tool_name": "Agent",
                          "tool_input": {"subagent_type": "E",
                                         "description": "x"}})
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        check("经 Git Bash 解析后仍能阻断（exit 2）", p.returncode == 2,
              "rc=%s out=%s" % (p.returncode, out[:200]))
        check("没有 cmd.exe 交互横幅（解析失败的特征）",
              "Microsoft Windows" not in out, out[:200])
        check("没有『语法不正确』或 command not found",
              "语法不正确" not in out and "command not found" not in out,
              out[:200])

        pi = run_sh(inj, {"session_id": "e2e2", "prompt": "x"})
        check("注入端经 Git Bash 真执行（写出预算文件）",
              "agent_spawns" in (pi.stdout + pi.stderr).decode("utf-8", "replace"),
              (pi.stdout + pi.stderr).decode("utf-8", "replace")[:200])

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print("安装链路：%d / %d 通过" % (passed, len(results)))
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
