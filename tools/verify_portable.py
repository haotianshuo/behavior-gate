# -*- coding: utf-8 -*-
"""1:1 复刻验证 —— 模拟在一台【全新电脑】上安装。

做什么：
    1. 把整个包复制到一个干净目录（模拟"拷到新电脑"）
    2. 用一个【干净的环境变量】（模拟新机器的 HOME/USERPROFILE 不同）
    3. 跑安装
    4. 验证生成配置里没有任何本机专有的写死值
    5. 用真实注册的命令跑一遍门

这不是"读代码确认" —— 是真复制、真安装、真执行。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

results = []


def ck(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail[:250])


def main():
    stage = tempfile.mkdtemp(prefix="newpc-")
    pkg = os.path.join(stage, "pkg")
    fake_home = os.path.join(stage, "fakehome")
    target = os.path.join(stage, "my project")     # 故意带空格
    os.makedirs(fake_home)

    print("\n===== 1. 模拟拷贝到新电脑 =====")
    shutil.copytree(ROOT, pkg)
    # 清掉可能带过来的状态/缓存
    for junk in ("__pycache__", ".smoke-state"):
        for base, dirs, _f in os.walk(pkg):
            if os.path.basename(base) == junk:
                shutil.rmtree(base, ignore_errors=True)
    ck("包已复制到干净目录", os.path.isdir(pkg))

    print("\n===== 2. 干净环境（模拟新机器）=====")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_BUDGET_STATE_DIR", "CLAUDE_BEHAVIOR_POLICY")}
    env["USERPROFILE"] = fake_home          # 假的家目录
    env["HOME"] = fake_home
    ck("环境已隔离（USERPROFILE 指向假目录）", env["USERPROFILE"] == fake_home)

    print("\n===== 3. 在新电脑上安装 =====")
    r = subprocess.run([PY, os.path.join(pkg, "install.py"),
                        "--project", target, "--apply"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=env, cwd=pkg)
    out = r.stdout.decode("utf-8", "replace")
    ck("安装退出码为 0", r.returncode == 0,
       "rc=%d\n%s" % (r.returncode, out[-500:]))
    ck("安装自检通过", "自检通过" in out, out[-400:])

    print("\n===== 4. 生成的配置不含本机专有值 =====")
    sp = os.path.join(target, ".claude", "settings.local.json")
    if not os.path.isfile(sp):
        ck("settings.local.json 已生成", False, sp)
        return 1
    cfg = json.load(open(sp, encoding="utf-8"))
    blob = json.dumps(cfg, ensure_ascii=False)
    ck("无未替换的占位符", "{{" not in blob, blob[:200])
    ck("无 %USERPROFILE%（Git Bash 不展开）", "%USERPROFILE%" not in blob)
    ck("无 Python312 硬编码以外的怪值", '"project"' not in blob)

    cmds = [h["command"] for es in cfg["hooks"].values()
            for e in es for h in e.get("hooks", [])]
    ck("命令数 > 0", len(cmds) > 0, str(len(cmds)))
    first = cmds[0] if cmds else ""
    ck("命令指向包内 hooks（不是原目录）",
       target.replace("\\", "/") in first or os.path.basename(pkg) in first,
       first)

    print("\n===== 5. 用新电脑上的配置真跑一遍 =====")
    st = os.path.join(stage, "state")
    env2 = dict(env, CLAUDE_BUDGET_STATE_DIR=st)

    def run(ev, needle, payload):
        cmd = None
        for e in cfg["hooks"].get(ev, []):
            for h in e.get("hooks", []):
                if needle in h["command"]:
                    cmd = h["command"]
        if not cmd:
            return -1, "找不到命令"
        pp = subprocess.run(["sh", "-c", cmd],
                            input=json.dumps(payload).encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env2, cwd=target)
        return pp.returncode, (pp.stdout + pp.stderr).decode("utf-8", "replace")

    inj = [h["command"] for e in cfg["hooks"].get("UserPromptSubmit", [])
           for h in e.get("hooks", [])]
    if inj:
        subprocess.run(["sh", "-c", inj[0]],
                       input=json.dumps({"session_id": "np", "prompt": "x"}).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env2, cwd=target)

    rc, o = run("PreToolUse", "budget_gate",
                {"session_id": "np", "tool_name": "Agent",
                 "tool_input": {"subagent_type": "E", "description": "x"}})
    ck("新电脑上 G1 能拦", rc == 2, "rc=%d %s" % (rc, o[:150]))

    rc, o = run("PreToolUse", "destructive_gate",
                {"session_id": "np", "tool_name": "Bash",
                 "tool_input": {"command": "p" + "kill -f x"}})
    ck("新电脑上 G5 能拦", rc == 2, "rc=%d" % rc)

    rc, o = run("Stop", "effect_gate",
                {"session_id": "np", "stop_hook_active": False,
                 "last_assistant_message": "已经修复完成了。"})
    ck("新电脑上 G3 能拦", rc == 2, "rc=%d" % rc)

    rc, o = run("PreToolUse", "destructive_gate",
                {"session_id": "np", "tool_name": "Bash",
                 "tool_input": {"command": "ls -la"}})
    ck("新电脑上放行路径正常", rc == 0, "rc=%d" % rc)

    print("\n" + "=" * 64)
    ok = sum(1 for _, c, _ in results if c)
    print("1:1 复刻验证：%d / %d" % (ok, len(results)))
    print("临时目录：%s（可删）" % stage)
    print("=" * 64)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
