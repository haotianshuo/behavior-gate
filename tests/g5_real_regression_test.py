# -*- coding: utf-8 -*-
"""G5 真实回归测试（REAL_REGRESSION）

⚠️ 与 synthetic fuzz 的区别 —— 这个区别是本文件存在的理由：
    synthetic  ：为了覆盖规则而构造的用例，写法理想（`rm -rf /`）
    REAL       ：从【真实会话】里挖出来、并已复现的用例

    #48 之所以漏了这么久，正是因为 tests/ 里只有 synthetic 的理想写法：
        `rm -rf /`、`rm -rf ~`、`rm -rf D:/`
    而真实开发者写的是：
        `rm -rf /tmp/v350`        （前缀贪婪 → 误报）
        `rm -r -f /`              （短选项分离 → 漏报）
        `rm -Rf /`                （大写 R   → 漏报）

每条用例都标注来源，便于回溯。

运行：python tests/g5_real_regression_test.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
GATE = os.path.join(PKG, "adapters", "claude-code", "hooks",
                    "destructive_gate.py")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _find_python():
    for c in (sys.executable, r"C:\Program Files\Python312\python.exe",
              "python3", "python"):
        if c and (os.path.exists(c) or c in ("python", "python3")):
            return c
    return "python"


PY = _find_python()
RULE_RX = re.compile(r"已阻断：([^\s\\]+)")

# 本轮修复涉及的规则族
FAMILY = {"rm_rf_traversal", "rm_rf_windows_drive", "rm_rf_windows_env"}

_results = []


def run_gate(tool, tool_input, tmp):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["CLAUDE_BUDGET_STATE_DIR"] = tmp
    p = subprocess.run(
        [PY, GATE],
        input=json.dumps({"session_id": "real-reg",
                          "hook_event_name": "PreToolUse",
                          "tool_name": tool,
                          "tool_input": tool_input}).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    m = RULE_RX.search(out)
    return p.returncode, (m.group(1) if m else None)


def check(name, ok, detail, origin):
    _results.append((name, ok, detail, origin))
    print("  %-6s %s" % ("PASS" if ok else "**FAIL**", name))
    if not ok:
        print("         %s" % detail)
        print("         来源：%s" % origin)


def main():
    tmp = tempfile.mkdtemp(prefix="g5-real-")
    RM = "rm"

    print("=" * 88)
    print("G5 真实回归测试（REAL_REGRESSION）")
    print("=" * 88)

    # ---------- SHOULD_BLOCK：真危险，必须拦 ----------
    print("\n----- SHOULD_BLOCK｜真危险，必须拦住 -----")
    BLOCK = [
        (RM + " -rf /", "根目录",
         "synthetic 基线"),
        (RM + " -rf ~", "家目录",
         "synthetic 基线"),
        (RM + " -rf $HOME", "HOME 变量",
         "synthetic 基线"),
        (RM + " -r -f /", "短选项分离写法",
         "REAL：09-15 检查真实拦截记录时发现旧规则漏拦"),
        (RM + " -f -r /", "短选项分离（逆序）",
         "REAL：同上，选项顺序无关"),
        (RM + " -Rf /", "大写 R 标志",
         "REAL：同上，rm(1) 接受大写 R"),
        (RM + " -rf D:/", "Windows 盘符根",
         "synthetic 基线（#45 引入）"),
        (RM + " -r -f D:/", "Windows + 短选项分离",
         "REAL：同族规则共享缺陷，本轮一并修"),
        (RM + " -Rf D:/", "Windows + 大写 R",
         "REAL：同上"),
        (RM + " -rf $env:TEMP/x", "Windows 环境变量目录",
         "synthetic 基线"),
        (RM + " -Rf $TEMP", "环境变量 + 大写 R",
         "REAL：同族规则共享缺陷，本轮一并修"),
    ]
    for cmd, note, origin in BLOCK:
        rc, rule = run_gate("Bash", {"command": cmd}, tmp)
        ok = (rc == 2 and rule in FAMILY)
        check("应拦 %-26s (%s)" % (cmd, note), ok,
              "rc=%d rule=%s" % (rc, rule), origin)

    # ---------- SHOULD_ALLOW：正常操作，不得误拦 ----------
    print("\n----- SHOULD_ALLOW｜正常操作，不得被 rm_rf 族误拦 -----")
    ALLOW = [
        (RM + " -rf /home/u/data", "根路径下的具体子目录",
         "REAL：本轮发现的前缀贪婪误报"),
        (RM + " -rf /var/log/*", "子目录下的通配",
         "REAL：同上"),
        (RM + " -rf ./build", "项目内构建目录",
         "synthetic 基线"),
        (RM + " -rf D:/proj/build", "Windows 项目路径",
         "synthetic 基线（#45 的明确取舍）"),
        (RM + " -rf node_modules", "相对目录",
         "synthetic 基线"),
        (RM + " -f /", "非递归删除（语义不扩大）",
         "REAL：确认修复未扩大拦截范围"),
        (RM + " -f a.txt", "普通删除",
         "synthetic 基线"),
    ]
    for cmd, note, origin in ALLOW:
        rc, rule = run_gate("Bash", {"command": cmd}, tmp)
        ok = (rule not in FAMILY)
        check("应放 %-26s (%s)" % (cmd, note), ok,
              "rc=%d rule=%s" % (rc, rule), origin)

    # ---------- 真实误报案例（单独列出，因为它引发本轮修复）----------
    print("\n----- 真实误报案例（本轮修复的触发点）-----")
    rc, rule = run_gate("Bash", {"command": RM + " -rf /tmp/v350"}, tmp)
    # ⚠️ 3.5.13 说明更新：posix_tmp_on_windows 已由 DENY 降为 WARN，
    #    所以这条命令现在既不被 rm_rf 族拦、也不被它拦。
    #    断言本身（不属 rm_rf 族）不变 —— 降级只会让它更容易成立。
    check("rm -rf /tmp/v350 不再被 rm_rf 族误拦",
          rule not in FAMILY,
          "rc=%d rule=%s（posix_tmp 已降为 WARN，不再阻断）" % (rc, rule),
          "REAL：09-15 09:51 真实会话中被 rm_rf_traversal 误拦")

    # ---------- 审计新增（2026-09-29）：新加/迁移的拦截能力必须上锁 ----------
    #
    # 背景（ce-code-review 的 testing reviewer #2，独立验证批 confirmed）：
    # 下面这些规则是本轮【新加或改过】的，但 tests/ 下此前零命中 ——
    # 后续任何改动关掉其中一条，425 项仍会全绿。取证形态：以下用例
    # ＝把代码注释里的只读探针输入硬化成断言（"手测生效 ≠ 回归锁"）。
    print("\n----- 审计新增｜新拦截能力的回归锁（每组带反例）-----")
    NEW_BLOCK = [
        ("Bash", {"command": RM + " -rf '/tmp/../'"},
         "rm_rf_dotdot_root", "dotdot 规范化归根（审计新增规则）"),
        ("Bash", {"command": RM + " -rf ~/"},
         None, "尾锚定放宽：波浪号加尾随斜杠"),
        ("Bash", {"command": RM + ' -rf "$HOME"'},
         None, "尾锚定放宽：双引号包裹 HOME"),
        ("Bash", {"command": RM + " -rf ${HOME}"},
         None, "尾锚定放宽：花括号 HOME"),
        ("Bash", {"command": RM + " -rf ${TEMP}"},
         None, "尾锚定放宽：花括号 TEMP"),
        ("Bash", {"command": RM + " -rf %TEMP%"},
         None, "尾锚定放宽：百分号 TEMP"),
        ("Bash", {"command": ":()" + " { :|:& };:"},
         "fork_bomb", "执行侧（此前只挂写入侧）"),
        ("Bash", {"command": "dd if=/dev/zero " + "of=" + "/dev/sda" + " bs=1M"},
         "disk_overwrite", "执行侧（此前只挂写入侧）"),
    ]
    for tool, ti, want_rule, note in NEW_BLOCK:
        rc, rule = run_gate(tool, ti, tmp)
        ok = (rc == 2) and (want_rule is None or rule == want_rule)
        check("审计新增 应拦 %-30s" % note, ok,
              "rc=%d rule=%s（期望 rc=2%s）"
              % (rc, rule, " rule=%s" % want_rule if want_rule else ""),
              "ce-code-review #2：新能力零覆盖")
    # 反例：常规清理不得被新规则误伤（只测一个方向的用例等于没测）
    for cmd, note in ((RM + " -rf /tmp/build", "常规清理"),
                      (RM + " -rf D:/myproj/build", "Windows 项目内清理")):
        rc, rule = run_gate("Bash", {"command": cmd}, tmp)
        check("审计新增 应放 %-30s" % note, rule is None,
              "rc=%d rule=%s" % (rc, rule),
              "ce-code-review #2：反例配对")
    # 写入侧（Write）：同内容须仍被拦（#7 锚定修复后为「独占整行」形态）
    for content, note in (("dd if=/dev/zero " + "of=" + "/dev/sda" + " bs=1M\n", "dd 独占整行"),
                          (":()" + " { :|:& };:" + "\n", "fork 独占整行")):
        rc, rule = run_gate("Write", {"file_path": "x", "content": content}, tmp)
        check("审计新增 应拦(写入侧) %-24s" % note, rc == 2,
              "rc=%d rule=%s" % (rc, rule),
              "ce-code-review #2：写入侧不得回退")

    # ---------- 审计新增（2026-09-29 · 第二轮）：写入侧根等价写法（A1 修复的回归锁）----------
    #
    # 背景：执行侧的"尾随斜杠/引号"容忍此前未同步到写入侧 —— Write/NotebookEdit
    # 内容里的 `rm -rf ~/`、`rm -rf $HOME/` 漏拦（实跑 3 例 rc=0，且回溯确认
    # v3.5.0 起就存在）。修复把『根等价后缀』提为共享判据（见 _ROOT_EQ_TAIL 注释）。
    print("\n----- 审计新增2｜写入侧根等价写法（A1）-----")
    WRITE_POS = [
        (RM + " -rf ~" + "/\n", "尾随斜杠 ~/"),
        (RM + " -rf $" + "HOME" + "/\n", "$HOME 加尾斜杠"),
        (RM + " -rf \"" + "$" + "HOME" + "\"/\n", "引号包裹 + 尾斜杠"),
    ]
    for content, note in WRITE_POS:
        rc, rule = run_gate("Write", {"file_path": "x", "content": content}, tmp)
        check("审计新增2 写入侧应拦 %-22s" % note, rc == 2 and rule == "rm_rf_in_content",
              "rc=%d rule=%s" % (rc, rule),
              "A1：写入侧尾缀未跟上执行侧（第二轮三验发现）")
    # 反例配对：带子路径与句中提及不得被误拦
    for content, note in ((RM + " -rf ~" + "/proj\n", "带子路径 ~/proj"),
                          ("说明：" + RM + " -rf ~" + "/ 是危险写法\n", "句中提及（非行首）")):
        rc, rule = run_gate("Write", {"file_path": "x", "content": content}, tmp)
        check("审计新增2 写入侧应放 %-22s" % note, rule is None,
              "rc=%d rule=%s" % (rc, rule),
              "A1 反例配对（放宽尾缀的误伤检查）")

    # ---------- 字段语义不变量 ----------
    print("\n----- 字段语义不变量（cmd_deny 不作用于写入内容）-----")
    rc, rule = run_gate("mcp__filesystem__write_file",
                        {"path": "x", "content": RM + " -rf /"}, tmp)
    check("MCP 写文件 content 含删根 → 由 content 规则拦",
          rc == 2 and rule == "rm_rf_in_content",
          "rc=%d rule=%s" % (rc, rule),
          "REAL：本轮发现 MCP 通道 rm 保护曾靠前缀贪婪缺陷侥幸生效")

    rc, rule = run_gate("Write",
                        {"file_path": "s.sh", "content": RM + " -rf /"}, tmp)
    check("Write content 含删根 → content 规则拦",
          rc == 2 and rule == "rm_rf_in_content",
          "rc=%d rule=%s" % (rc, rule),
          "synthetic 基线")

    rc, rule = run_gate("mcp__filesystem__write_file",
                        {"path": "x", "content": "pkill -f node"}, tmp)
    check("MCP 写文件 content 含 pkill → 放行（内容不是命令）",
          rule is None,
          "rc=%d rule=%s" % (rc, rule),
          "REAL：跨字段泄漏实测，已按字段语义分离修正")

    rc, rule = run_gate("mcp__desktop__bash",
                        {"command": RM + " -rf /"}, tmp)
    check("MCP 执行命令含删根 → cmd 规则拦",
          rc == 2 and rule in FAMILY,
          "rc=%d rule=%s" % (rc, rule),
          "REAL：MCP 命令字段属可执行上下文")

    # ---------- 已知边界：写入内容的 Windows 盘符覆盖 ----------
    # 这里断言的是【当前已知行为】，不是「理想行为」。
    # 目的是锁住它：如果将来有人改了字段分派或补了盘符覆盖，
    # 这条会红，从而被迫显式更新这条边界，而不是悄悄改变语义。
    print("\n----- 已知边界（有意保留，锁住行为）-----")
    rc, rule = run_gate("mcp__filesystem__write_file",
                        {"path": "x", "content": RM + " -rf D:/"}, tmp)
    check("已知边界：MCP 写文件 content 含盘符删根 → 当前放行",
          rule is None,
          "rc=%d rule=%s（Edit/Write 一直是这个行为，MCP 已对齐）" % (rc, rule),
          "REAL：字段分离的已知代价，有意保留，见 CHANGELOG #48")

    rc, rule = run_gate("Bash", {"command": RM + " -rf D:/"}, tmp)
    check("对照：Bash 命令含盘符删根 → 仍拦（可执行通道覆盖完整）",
          rc == 2 and rule in FAMILY,
          "rc=%d rule=%s" % (rc, rule),
          "REAL：确认盘符保护在【可执行通道】没有丢失")

    # ---------- 汇总 ----------
    npass = sum(1 for r in _results if r[1])
    print("\n" + "=" * 88)
    print("真实回归结果：%d / %d 通过" % (npass, len(_results)))
    print("=" * 88)
    fails = [r for r in _results if not r[1]]
    if fails:
        print("\n失败项：")
        for name, _, detail, origin in fails:
            print("  - %s\n      %s\n      来源：%s" % (name, detail, origin))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
