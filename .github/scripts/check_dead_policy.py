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
    段名作为字面量出现在 hooks 下的 .py 里（门读它必然要写 policy.get("段名")）。
    实测的三次缺陷都是 0 处引用。

⚠️ 输出只用 ASCII —— Windows runner 控制台是 GBK，
   含中文的 print 在失败路径会抛 UnicodeEncodeError（本项目踩过两次）。
"""

import json
import os
import pathlib
import sys

# 已知【故意保留】的死段（有明确理由说明为什么留着）。
# 目前为空 —— 每一条都要有理由，不能因为"将来可能用"就留。
ALLOWED_DEAD = {
    # "section_name": "为什么留着（必须具体，不能是'以后可能用'）",
}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = pathlib.Path(__file__).resolve().parents[2]
    pol_path = root / "policy" / "behavior-policy.json"
    hooks_dir = root / "adapters" / "claude-code" / "hooks"

    policy = json.loads(pol_path.read_text(encoding="utf-8"))

    # 只统计真正的 hook 脚本
    blob = []
    for name in sorted(os.listdir(hooks_dir)):
        if name.endswith(".py"):
            blob.append((hooks_dir / name).read_text(encoding="utf-8"))
    text = "\n".join(blob)

    checked = 0
    dead = []
    for section in policy:
        if section.startswith("_"):
            continue
        checked += 1
        if ('"%s"' % section) in text or ("'%s'" % section) in text:
            continue
        if section in ALLOWED_DEAD:
            continue
        dead.append(section)

    print("[check_dead_policy] checked %d policy sections" % checked)

    if dead:
        print("[check_dead_policy] FAIL: sections nobody reads")
        for s in dead:
            print("  %s  <- no hook references this section name" % s)
        print("")
        print("  A policy section that no code reads is a fake capability:")
        print("  it declares a switch that will never take effect.")
        print("  Fix: either delete the section, or wire it up.")
        return 1

    print("[check_dead_policy] OK: every section is referenced by a hook")
    return 0


if __name__ == "__main__":
    sys.exit(main())
