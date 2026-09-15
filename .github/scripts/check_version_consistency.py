#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验版本身份一致性 —— 「单一版本源」的自动检查。

背景：本项目历史上出现过「文档写 V2.4、实际 V3.0」这类漂移。
      VERSION 文件是机器可读的唯一声明源，其余位置必须与它一致。

规则：
    VERSION        = 权威来源（adapters/claude-code/hooks/VERSION）
    policy._version  必须相同
    README.md / install.md / 安装说明.md 里标注的版本行必须相同

⚠️ 输出只用 ASCII。
    本脚本曾内联在 workflow 里，含中文 print，在 windows-latest 上
    因控制台 GBK 编码抛 UnicodeEncodeError。抽成独立脚本并把输出
    改成纯 ASCII，是这类问题的一次性解法 —— 详见 check_no_deps.py
    的文件头说明。
"""

import json
import pathlib
import re
import sys

# 各文档里版本号的呈现形式（正则，第一个捕获组是版本号）
DOC_PATTERNS = [
    ("README.md", r"当前版本：\*\*(V?[\d.]+)\*\*"),
    ("install.md", r"对应版本：\*\*(V?[\d.]+)\*\*"),
    ("安装说明.md", r"版本 (V?[\d.]+)"),
]


def read_version(root):
    p = root / "adapters" / "claude-code" / "hooks" / "VERSION"
    return p.read_text(encoding="utf-8").strip()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = pathlib.Path(__file__).resolve().parents[2]
    problems = []

    ver = read_version(root)
    print("[check_version] VERSION = %s" % ver)

    # policy
    pol_path = root / "policy" / "behavior-policy.json"
    pol_ver = json.loads(pol_path.read_text(encoding="utf-8"))["_version"]
    print("[check_version] policy._version = %s" % pol_ver)
    if pol_ver != ver:
        problems.append("policy/behavior-policy.json: %s != %s" % (pol_ver, ver))

    # docs
    for rel, pat in DOC_PATTERNS:
        path = root / rel
        if not path.exists():
            problems.append("%s: file missing" % rel)
            continue
        text = path.read_text(encoding="utf-8")
        m = re.search(pat, text)
        if not m:
            problems.append("%s: version line not found (pattern=%s)" % (rel, pat))
            continue
        found = m.group(1).lstrip("Vv")
        print("[check_version] %s = %s" % (rel, found))
        if found != ver:
            problems.append("%s: %s != %s" % (rel, found, ver))

    # --- 变更记录项数一致性（3.5.7 新增，见 #40）---
    # 为什么加：README 里「N 项真实问题」这个数字【手工维护】，
    # 已经过期两次（#32 修过一次，3.5.6 又过期）。
    # 手工维护的数字必然复发 —— 所以让它自动核对。
    changelog = root / "CHANGELOG.md"
    if changelog.exists():
        text = changelog.read_text(encoding="utf-8")
        nums = [int(x) for x in re.findall(r"^\| (\d+) \|", text, re.M)]
        if nums:
            actual = max(nums)
            print("[check_version] CHANGELOG max issue number = %d" % actual)
            for rel, pat in [
                ("README.md", r"变更记录（(\d+) 项"),
                ("CHANGELOG.md", r"## 全部问题与修复（(\d+) 项"),
                # 「**统计**：共 N 项」也是手工维护的（#40 同类）。
                # 3.5.9 补：修 #40 时只加了上面那条，漏了这一条 ——
                # 于是它每次都会绿着放过一个过期数字。
                ("CHANGELOG.md", r"\*\*统计\*\*：共 (\d+) 项"),
            ]:
                f = root / rel
                if not f.exists():
                    continue
                m = re.search(pat, f.read_text(encoding="utf-8"))
                if not m:
                    continue
                if int(m.group(1)) != actual:
                    problems.append(
                        "%s: 声称 %s 项，实际 %d 项" % (rel, m.group(1), actual))

    if problems:
        print("[check_version] FAIL: version identity inconsistent")
        for p in problems:
            print("  %s" % p)
        return 1

    print("[check_version] OK: all version declarations agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
