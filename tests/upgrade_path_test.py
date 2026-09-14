# -*- coding: utf-8 -*-
"""升级路径测试 —— 真实模拟 V2.6 → V2.7 的升级。

为什么单独有这个文件（来自另一台电脑的真实反馈）：
    那个报告找出两个只有【升级路径】才会暴露的 bug：
      BUG-A  清理逻辑排在漂移检查【之后】→ 永远轮不到执行 → 安装中止
      BUG-B  _is_ours 漏认 intent_gate + "bg-" 子串误匹配 → 误删第三方 / 留死条目
    这两个在"全新安装"和"单元测试"里都测不出来 —— 只有真升级才会踩到。

本文件做真事：造一个含【历史残留】的已安装目录，然后跑真 install.py。
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
res = []


def ck(name, cond, detail=""):
    res.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail[:300])


def setup_old_install(proj, extra_files=(), extra_hooks=()):
    """造一个"V2.6 时代装过"的目录：含废弃 bg-*.cmd、含第三方文件。"""
    cd = os.path.join(proj, ".claude")
    hd = os.path.join(cd, "hooks")
    os.makedirs(hd, exist_ok=True)

    # 历史残留：5 个已废弃的 .cmd
    for n in ("bg-budget-gate.cmd", "bg-effect-gate.cmd"):
        open(os.path.join(hd, n), "w").write("@echo off\r\nexit /b 0\r\n")

    # 第三方文件（绝不能被删）
    for n in ("cbm-guard.cmd", "someone-elses.js"):
        open(os.path.join(hd, n), "w").write("third party\n")
    for n in extra_files:
        open(os.path.join(hd, n), "w").write("x\n")

    # 历史配置：含指向【失效解释器】的自家条目 + 第三方条目
    hooks = {
        "PreToolUse": [
            {"matcher": "Agent", "hooks": [
                {"type": "command",
                 "command": '"D:/Old/Python311/python.exe" '
                            '"C:/x/.claude/hooks/budget_gate.py"'}]},
            # 这条是第三方的，路径里故意含 bg-
            {"matcher": "Bash", "hooks": [
                {"type": "command",
                 "command": '"C:/tools/node.exe" '
                            '"C:/Users/x/bg-project/.claude/hooks/their-linter.js"'}]},
        ],
        "Stop": [
            {"hooks": [{"type": "command",
                        "command": '"D:/Old/Python311/python.exe" '
                                   '"C:/x/.claude/hooks/intent_gate.py"'}]},
        ],
    }
    for h in extra_hooks:
        hooks.setdefault("PreToolUse", []).append(h)
    json.dump({"hooks": hooks, "permissions": {"allow": ["Bash(ls:*)"]}},
              open(os.path.join(cd, "settings.local.json"), "w",
                   encoding="utf-8"), ensure_ascii=False)
    return cd, hd


def main():
    stage = tempfile.mkdtemp(prefix="upg-")
    proj = os.path.join(stage, "proj")

    print("\n===== 场景：V2.6 残留 → 升级到当前版 =====")
    cd, hd = setup_old_install(proj)
    ck("已造出含残留的旧安装",
       os.path.isfile(os.path.join(hd, "bg-budget-gate.cmd")))

    print("\n----- 关键：不加 --force，应该能自己过去 -----")
    r = subprocess.run([PY, os.path.join(ROOT, "install.py"),
                        "--project", proj, "--apply"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
    out = r.stdout.decode("utf-8", "replace")
    ck("升级未被漂移检查挡下（退出码 0）", r.returncode == 0,
       "rc=%d\n%s" % (r.returncode, out[-600:]))
    ck("废弃文件被自动清理", "已清理" in out, out[-400:])

    print("\n----- 文件层面 -----")
    files = set(os.listdir(hd)) if os.path.isdir(hd) else set()
    ck("废弃的 bg-*.cmd 已删",
       not any(f.startswith("bg-") for f in files), str(sorted(files)))
    ck("第三方 cbm-guard.cmd 保留", "cbm-guard.cmd" in files)
    ck("第三方 someone-elses.js 保留", "someone-elses.js" in files)
    ck("自家 intent_gate.py 已安装", "intent_gate.py" in files)

    print("\n----- 配置层面 -----")
    cfg = json.load(open(os.path.join(cd, "settings.local.json"), encoding="utf-8"))
    cmds = [h["command"] for es in cfg.get("hooks", {}).values()
            for e in es for h in e.get("hooks", [])]
    blob = "\n".join(cmds)
    ck("指向失效解释器的旧条目已清",
       "D:/Old/Python311" not in blob, blob[:300])
    ck("第三方 linter 条目【未被误删】",
       "their-linter.js" in blob, blob[:300])
    ck("permissions 保留", len(cfg.get("permissions", {}).get("allow", [])) == 1)
    ck("无重复的 intent_gate 条目",
       sum(1 for c in cmds if "intent_gate" in c) == 1,
       str([c for c in cmds if "intent_gate" in c]))

    print("\n===== 场景：幂等（再装一次不应变化）=====")
    before = sorted(json.dumps(cfg, sort_keys=True))
    subprocess.run([PY, os.path.join(ROOT, "install.py"),
                    "--project", proj, "--apply", "--force"],
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
    cfg2 = json.load(open(os.path.join(cd, "settings.local.json"), encoding="utf-8"))
    ck("二次安装后条目数不变",
       len([h for es in cfg2["hooks"].values() for e in es for h in e.get("hooks", [])])
       == len(cmds), "before=%d" % len(cmds))

    print("\n===== 场景：自家文件被外部改坏 → 应该报警 =====")
    # 把已安装的某个自家脚本改坏（模拟"有人手改了"）
    p = os.path.join(hd, "budget_gate.py")
    if os.path.isfile(p):
        open(p, "w", encoding="utf-8").write("# tampered\n")
    r = subprocess.run([PY, os.path.join(ROOT, "install.py"),
                        "--project", proj, "--apply"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
    out = r.stdout.decode("utf-8", "replace")
    ck("被改坏的自家文件触发漂移告警", r.returncode != 0 or "漂移" in out,
       "rc=%d %s" % (r.returncode, out[-300:]))

    print("\n===== 场景：.bak 清理不能删【用户自己的】备份 =====")
    # ⚠️ 这是新增的破坏性操作，必须验证边界。
    #    实测踩到：原来只按 `.bak-<时间戳>` 匹配，
    #    用户自己的 my-own-notes.bak-20250202-020202 被当成我们的删掉。
    proj2 = os.path.join(stage, "bakproj")
    cd2 = os.path.join(proj2, ".claude")
    os.makedirs(cd2, exist_ok=True)
    old = 1700000000
    for n in ("my-own-notes.bak-20250101-010101",
              "another-tool.bak-20250202-020202"):
        p2 = os.path.join(cd2, n)
        open(p2, "w").write("user data")
        os.utime(p2, (old, old))
    for i in range(5):
        p2 = os.path.join(cd2, "settings.local.json.bak-2025010%d-010101" % i)
        open(p2, "w").write("ours")
        os.utime(p2, (old + i, old + i))
    json.dump({"permissions": {"allow": ["Bash(ls:*)"]}},
              open(os.path.join(cd2, "settings.local.json"), "w",
                   encoding="utf-8"))
    for _ in range(2):
        subprocess.run([PY, os.path.join(ROOT, "install.py"),
                        "--project", proj2, "--apply"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
    left = sorted(os.listdir(cd2))
    ck("用户的备份未被删（my-own-notes）",
       any("my-own-notes" in f for f in left),
       str([f for f in left if ".bak-" in f]))
    ck("用户的备份未被删（another-tool）",
       any("another-tool" in f for f in left),
       str([f for f in left if ".bak-" in f]))
    ours = [f for f in left if f.startswith("settings.local.json.bak-")]
    ck("本方案备份被限制在 5 个以内", len(ours) <= 5, str(ours))

    print("\n===== 场景：hooks 下的子目录必须能被安装 =====")
    # ⚠️ 与 .json 的扩展名不对称同类，只是换了"递归 vs 顶层"这根轴。
    subdir = os.path.join(ROOT, "adapters", "claude-code", "hooks", "subprobe")
    os.makedirs(subdir, exist_ok=True)
    open(os.path.join(subdir, "helper_probe.py"), "w").write("# probe\n")
    try:
        proj3 = os.path.join(stage, "subproj")
        r = subprocess.run([PY, os.path.join(ROOT, "install.py"),
                            "--project", proj3, "--apply"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT)
        out = r.stdout.decode("utf-8", "replace")
        ck("含子目录时安装通过", r.returncode == 0,
           "rc=%d %s" % (r.returncode, out[-300:]))
        ck("子目录内文件已安装",
           os.path.isfile(os.path.join(proj3, ".claude", "hooks", "subprobe",
                                       "helper_probe.py")),
           out[-200:])
    finally:
        shutil.rmtree(subdir, ignore_errors=True)

    print("\n" + "=" * 64)
    ok = sum(1 for _, c, _ in res if c)
    print("升级路径测试：%d / %d" % (ok, len(res)))
    print("临时目录：%s" % stage)
    print("=" * 64)
    return 0 if ok == len(res) else 1


if __name__ == "__main__":
    sys.exit(main())
