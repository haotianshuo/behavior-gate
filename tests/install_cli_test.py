# -*- coding: utf-8 -*-
"""install.py 命令行参数回归（REAL_REGRESSION）

来源：两台电脑的独立复核都报了「README 写的 --global 不生效」，
本机实测坐实，且比记录更严重 ——

    python install.py --apply --global
      → 作用域：项目级
      → 目标：<包自己所在的目录>/.claude/settings.local.json
      → 没有任何提示说 --global 被忽略了

用户以为装到全局了，重启后门完全不生效。

根因是【静默忽略未知参数】—— 不只是 --global 一个参数的问题。

运行：python tests/install_cli_test.py
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
INSTALL = os.path.join(PKG, "install.py")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("  %-6s %s" % ("PASS" if ok else "**FAIL**", name))
    if not ok and detail:
        print("         %s" % detail)


_TMP = tempfile.mkdtemp(prefix="install-cli-")


def run(args, cwd=None):
    """在【隔离临时目录】里跑 —— 绝不往仓库或用户目录写东西。

    ⚠️ 这条纪律是踩出来的：初版默认 cwd=PKG（仓库根），
       结果 `install.py --apply`/`--global` 在仓库里留下了 .claude/
       （策略副本 + hooks + 备份文件），污染了工作区。

       同一个包自己在 CHANGELOG #30 就记过这类问题：
       「测试未隔离环境 → 同一份代码在不同机器上结果不同」。
       这里是它的第二次：不只是结果不稳，还会改仓库。

    HOME/USERPROFILE 也指向临时目录 —— 否则 --global 会碰真实的 ~/.claude。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8",
           "HOME": _TMP, "USERPROFILE": _TMP}
    p = subprocess.run([sys.executable, INSTALL] + args,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       cwd=cwd or _TMP, env=env, timeout=60)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def main():
    print("=" * 88)
    print("install.py 命令行参数回归")
    print("=" * 88)

    # ---------- 未知参数必须报错，不能静默忽略 ----------
    print("\n----- 未知参数：报错，而不是静默忽略 -----")
    for bad in ["--bogus", "--globall", "--scope-global"]:
        rc, out = run(["--apply", bad])
        check("%-16s → rc=2 且提示" % bad,
              rc == 2 and "无法识别" in out,
              "rc=%d 含提示=%s" % (rc, "无法识别" in out))

    # ---------- --global 作为别名必须能工作 ----------
    # ⚠️ 这里只断言【作用域解析】，不断言 rc。
    #    --global 会去写 $HOME/.claude/，而在隔离环境里那个目录不存在，
    #    安装本身会失败（rc=2）—— 那是环境问题，不是参数解析问题。
    #    把两者混在一个断言里，就会像初版那样误报。
    print("\n----- --global 别名（旧 README 的写法） -----")
    rc, out = run(["--global"])
    check("--global → 解析为全局作用域",
          "作用域：全局" in out,
          out.splitlines()[2] if len(out.splitlines()) > 2 else out[:80])

    # ---------- --scope global 仍是正解 ----------
    print("\n----- --scope global（文档正解） -----")
    rc, out = run(["--scope", "global"])
    check("--scope global → 解析为全局作用域",
          "作用域：全局" in out)

    # ---------- 默认仍是项目级 ----------
    print("\n----- 默认行为不变 -----")
    rc, out = run([])
    check("无参数 → 项目级",
          "作用域：项目级" in out)

    # ---------- 已知参数不该被误报 ----------
    print("\n----- 已知参数不该被误报 -----")
    for okarg in ["--apply", "--force", "--rollback"]:
        rc, out = run([okarg, "--help"] if okarg != "--apply" else ["--apply"])
        check("%-12s 不被判为未知" % okarg,
              "无法识别" not in out,
              out[:80])

    # ---------- README 不再写错用法 ----------
    print("\n----- 文档一致性 -----")
    readme = open(os.path.join(PKG, "README.md"), encoding="utf-8").read()
    check("README 不含旧的错误写法 '--apply --global'",
          "--apply --global" not in readme)
    check("README 含正确写法 '--scope global'",
          "--scope global" in readme)

    # ---------- 隔离性：测试不能污染仓库 ----------
    print("\n----- 隔离性：仓库目录不得被测试写入 -----")
    leaked = os.path.join(PKG, ".claude")
    check("仓库里没有 .claude/（测试未污染工作区）",
          not os.path.exists(leaked),
          "存在：%s" % leaked)

    npass = sum(1 for _, ok, _ in _results if ok)
    print("\n" + "=" * 88)
    print("install CLI 回归：%d / %d 通过" % (npass, len(_results)))
    print("=" * 88)
    if npass != len(_results):
        print("\n失败项：")
        for n, ok, det in _results:
            if not ok:
                print("  - %s：%s" % (n, det))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
