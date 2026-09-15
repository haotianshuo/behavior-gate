#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 policy 里有没有【没有任何 hook 读】的段（死段）。

背景 —— 这个形状的缺陷在本项目出现过三次：

    bash_rounds / webfetch   声明了上限，但没有拦截器（假能力）
    closeout.enabled         写了 false，但门不读它、无条件运行（#31）
    gateway_budget           整段零引用（#36）

三次都是靠人肉扫描发现的。而项目自己的注释里早就写着一句话：

    「装一个不生效的开关比没有开关更糟」

概念早就有，只是缺一个机械检查。人扫一次能发现问题，但挡不住下一次。

判定方法：
    段名作为【访问表达式】出现在 hooks 下的 .py 里，
    即 policy.get("段名") / policy["段名"] / 变量.get("段名")。

⚠️ 为什么不是「任意字面量」（3.5.7 修正，见 #38）：
    初版规则是「段名作为任意带引号的字面量出现在任一 .py 里即算被读」。
    实测证明它【结构性地抓不到 closeout 那类死段】，原因：

        _lib.py 的 DEFAULT_POLICY 为【每一个段】都写了一遍段名。
        → 一个段只要被接进 DEFAULT_POLICY，段名就永远躺在 .py 里
        → 规则永远判它活着，无论它死得多彻底

    决定性实测（在 v3.5.0 上跑，那一版 closeout 与 gateway_budget 都真死）：

        budget              原规则判活   收紧后判活
        effect_gate         原规则判活   收紧后判活
        destructive_gate    原规则判活   收紧后判活
        closeout            原规则判活 ✗  收紧后判死 ✓   <- 漏了
        gateway_budget      原规则判死 ✓  收紧后判死 ✓

    漏判的直接帮凶还有一处：closeout_gate.py 里的
    state_path(session_id, "closeout") —— 那是【状态文件名】，
    不是 policy 读取，但字面量一模一样。收紧后不再误救。

    讽刺的是：让"假能力键"危险的东西（有默认值），
    正是让初版检测器瞎掉的东西 —— _lib.py 自己的注释早写过这个陷阱
    （"删键 = 回退默认值，等于没删"）。

⚠️ 输出只用 ASCII —— Windows runner 控制台是 GBK，
   含中文的 print 在失败路径会抛 UnicodeEncodeError（本项目踩过两次）。
"""

import json
import os
import pathlib
import re
import sys

# 已知【故意保留】的死段（有明确理由说明为什么留着）。
# 目前为空 —— 每一条都要有理由，不能因为"将来可能用"就留。
ALLOWED_DEAD = {
    # "section_name": "为什么留着（必须具体，不能是'以后可能用'）",
}

# 访问表达式：.get("段名") 或 ["段名"]
# 覆盖 policy.get("x") / d.get('x') / obj["x"] 三种写法。
_ACCESS = r'(\.get\(\s*["\']%s["\']|\[\s*["\']%s["\']\s*\])'


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = pathlib.Path(__file__).resolve().parents[2]
    pol_path = root / "policy" / "behavior-policy.json"
    hooks_dir = root / "adapters" / "claude-code" / "hooks"

    # ⚠️ 检查【生效策略】而不只是 policy 文件（3.5.7 修正，见 #39）。
    #    修前只读 policy/behavior-policy.json，于是漏掉了只在
    #    _lib.py 的 DEFAULT_POLICY 里存在的键 ——
    #    min_risk_level / deny_patterns 就是这样藏了很久：
    #    policy 文件里根本没有它们，用户改 policy 时甚至看不见，
    #    但 DEFAULT_POLICY 会把它们 merge 进生效策略。
    #    （_lib.py 自己的注释早写过这个陷阱："删键 = 回退默认值，等于没删"）
    sys.path.insert(0, str(hooks_dir))
    from _lib import DEFAULT_POLICY, _deep_merge  # noqa: E402
    policy = _deep_merge(
        DEFAULT_POLICY,
        json.loads(pol_path.read_text(encoding="utf-8")))

    # 只统计真正的 hook 脚本
    blob = []
    for name in sorted(os.listdir(hooks_dir)):
        if name.endswith(".py"):
            blob.append((hooks_dir / name).read_text(encoding="utf-8"))
    text = "\n".join(blob)

    checked = 0
    dead = []
    dead_keys = []
    for section, value in policy.items():
        if section.startswith("_"):
            continue
        checked += 1
        if not re.search(_ACCESS % (section, section), text):
            if section not in ALLOWED_DEAD:
                dead.append(section)
            # 段都死了，里面的键不必再单独报
            continue

        # 段内键（3.5.7 新增，见 #38）
        # 为什么需要：初版只看顶层段，而 #2 那个形状
        # （bash_rounds / webfetch）是 budget 【段里的键】——
        # budget 有真读取 → 判活 → 段内死键结构上不可见。
        if isinstance(value, dict):
            for key in value:
                if key.startswith("_"):
                    continue
                if not re.search(_ACCESS % (key, key), text):
                    dead_keys.append("%s.%s" % (section, key))

    print("[check_dead_policy] checked %d policy sections" % checked)

    if dead or dead_keys:
        print("[check_dead_policy] FAIL: declared but never read")
        for s in dead:
            print("  %s  <- no hook references this section" % s)
        for s in dead_keys:
            print("  %s  <- no hook references this key" % s)
        print("")
        print("  A policy entry that no code reads is a fake capability:")
        print("  it declares a switch that will never take effect.")
        print("  Fix: either delete it, or wire it up.")
        return 1

    print("[check_dead_policy] OK: every section and key is read by a hook")
    return 0


if __name__ == "__main__":
    sys.exit(main())
