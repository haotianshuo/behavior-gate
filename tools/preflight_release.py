#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前预检（preflight）——把「发布前该跑的检查」变成可执行的一步。

为什么需要它（2026-09-29 复盘，v3.5.16 发布事故）：
    发布前本机跑了 17 套件 450 项、部署校验、冒烟 —— 全绿；
    但【没跑】CI 里的两个检查脚本（check_no_deps / check_dead_policy）。
    结果 check_no_deps 的白名单没跟上新增的 review_unit 模块，
    CI 六矩阵全红、后续 16 个测试套件全被 SKIPPED（发布证据链断裂）。

    教训：检查清单写在人脑里就会漏；写进代码里才可靠。
    本脚本 = 把 ci.yml 的【全部本机可跑步骤】收进一处，一条命令跑完。

用法：
    python tools/preflight_release.py      # 全绿 exit 0；任何一步失败 exit 1

边界（如实声明）：
    · 它证明"本机能跑的检查都绿了"，不替代 CI 的跨平台矩阵
      （三个 OS × 两个 Python 版本上的真实运行仍需 CI）；
    · 它不检查"版本号该不该升"——那是人的决定。
"""

import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _run(label, argv, tail_lines=4, timeout=600):
    """跑一个检查；返回 (ok, 一行摘要)。"""
    t0 = time.time()
    try:
        p = subprocess.run(argv, cwd=str(ROOT), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
        rc = p.returncode
        out = p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        rc, out = "TIMEOUT", ""
    dt = time.time() - t0
    ok = (rc == 0)
    print("  [%s] %-46s rc=%s  %.1fs" % ("PASS" if ok else "FAIL", label, rc, dt))
    if not ok and out:
        for ln in out.strip().splitlines()[-tail_lines:]:
            print("         %s" % ln[:150])
    return ok, label


def main():
    print("=" * 78)
    print("发布前预检（preflight）—— ci.yml 的本机等价步骤 + 全套件")
    print("=" * 78)
    results = []

    # ---- 1. CI 里的检查脚本（三平台都会跑的同款）----
    results.append(_run("确认零第三方依赖 (check_no_deps)",
                        [PY, str(HERE.parent / ".github" / "scripts" / "check_no_deps.py")]))
    results.append(_run("版本一致性 (check_version_consistency)",
                        [PY, str(HERE.parent / ".github" / "scripts" / "check_version_consistency.py")]))
    results.append(_run("死段检测 (check_dead_policy)",
                        [PY, str(HERE.parent / ".github" / "scripts" / "check_dead_policy.py")]))
    results.append(_run("源身份只读校验 (source_identity)",
                        [PY, str(HERE / "source_identity.py")]))

    # ---- 2. 全套件（与 CI 同款跑法：逐个脚本、只看退出码）----
    tests_dir = ROOT / "tests"
    suites = sorted(p.name for p in tests_dir.glob("*_test.py"))
    suites.append("four_gate_selftest.py")   # 不含 _test 后缀，单独带上
    for name in suites:
        results.append(_run("套件 %s" % name, [PY, str(tests_dir / name)], tail_lines=3))

    # ---- 总结 ----
    bad = [label for ok, label in results if not ok]
    print("=" * 78)
    print("预检结果：%d/%d 步通过" % (len(results) - len(bad), len(results)))
    if bad:
        print("失败项：")
        for label in bad:
            print("  - %s" % label)
        print("→ 不要发布：先修到全绿。")
        return 1
    print("全部通过 —— 本机检查面已绿。")
    print("提醒：这只是本机；跨平台矩阵仍以 CI 为准（push 后看 Actions）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
