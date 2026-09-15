# -*- coding: utf-8 -*-
"""
G5 危险门（PreToolUse → Bash | Edit | Write | MultiEdit | NotebookEdit | MCP）

定位声明（必须写在最前面）：
    这是【防误操作层】，不是【安全边界】。
    危险动作可以藏在 Python / Node / Shell / MCP / 数据库 / GUI 里，
    字符串匹配永远拦不全。真正的安全边界是沙箱、精确 PID/端口、
    临时副本、操作系统权限。本门只负责挡住【已知会翻车的常见写法】。

⚠️ 范畴区分（互审发现的真实误伤）：
    治「执行危险命令」和治「写入危险内容」是【两个不同的问题】。
    把它们套同一条正则会导致：写一份提到 `pkill` 的审计报告被自己的门拦住
    —— 而本方案自己的文档就含这个字符串。

    所以规则分三类：
      cmd_deny      只作用于 Bash 的 command / MCP 的输入（真会被执行的上下文）
      content_deny  作用于 Edit/Write 的内容（只有真能造成损害的模式才拦）
      warn          只提示，不阻断
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import read_input, deny, allow, load_policy, warn_inactive  # noqa: E402


# ---- 只在【会被执行】的上下文里拦（Bash command / MCP input）----
CMD_DENY = [
    {
        "id": "fuzzy_kill",
        "pattern": r"\b(pkill|killall)\b",
        "why": "模糊匹配进程名，可能误杀用户正在使用的生产进程。\n"
               "  审计实证：deepseek 跑 `pkill -f \"proxy.mjs\"`，用户的代理因为跑在\n"
               "  app/runtime/node.exe 下才侥幸没中招。事后它自定规则：「只按端口/PID 精确操作」。",
        "instead": "改用 taskkill /PID <精确PID> /F，或先 `netstat -ano | findstr :8787` 查到 PID 再杀。",
    },
    {
        "id": "rm_rf_traversal",
        "pattern": r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME|\*)",
        "why": "递归强删根目录/家目录/通配。",
        "instead": "写出完整、具体的路径，并先 `ls` 确认目标。",
    },
    {
        "id": "posix_tmp_on_windows",
        "pattern": r"\b(find|ls|rm|cat|du|grep|chmod|chown)\b[^\n|;&]*\s/tmp(/|\s|$)",
        "why": "Git Bash on Windows 下 /tmp 会解析到 C:\\tmp。\n"
               "  审计实证：那条命令「把 C:\\tmp 全扫了一遍」。",
        "instead": "用显式路径（$TEMP 或具体目录），不要依赖 POSIX 约定。",
    },
]

# ---- 只在【写入内容】里拦：必须是真能造成损害的模式 ----
#
# ⚠️ 两个反复踩到的坑（都由对抗测试发现，不是推测）：
#   1. 刻意【不】包含 fuzzy_kill —— 写一份提到 `pkill` 的审计报告不该被拦。
#      （本方案自己的文档就含这个字符串。）
#   2. 内容规则必须【行首锚定】—— 否则「禁止 rm -rf / 这类命令」这种
#      规则文档也会被拦，因为句子里出现了那个模式。
#      行首锚定 = 只在它作为一条命令出现时才拦，作为被讨论的文本时放行。
#      代价：文档里整行代码块含 `rm -rf /` 仍会被拦（可接受的保守）。
CONTENT_DENY = [
    {
        "id": "rm_rf_in_content",
        "pattern": r"^[ \t]*\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME)(\s|$|--no-preserve-root)",
        "line_anchored": True,
        "why": "写入的内容里，有一【整行】是递归强删根目录——这段内容一旦被当成脚本执行就会出事。",
        "instead": "如果这是在写文档/注释，把该模式放在句子中间或加转义（如「禁止 rm -rf / 这类命令」），避免独占一行。",
    },
    {
        "id": "fork_bomb",
        "pattern": r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        "why": "Fork bomb。",
        "instead": "不要写这个。",
    },
    {
        "id": "disk_overwrite",
        "pattern": r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd)",
        "why": "直接覆写块设备。",
        "instead": "如果是文档示例，标注清楚并改用占位路径。",
    },
]

# ---- 只提示，不阻断 ----
WARN = [
    {
        "id": "app_db_touch",
        "pattern": r"app/data/app\.db",
        "why": "可能触及正式数据库。审计实证：曾有 +13 行未预判的审计日志写入。",
    },
]


def _compile(rules, where="?"):
    """编译规则表。**失败必须可见**（3.5.8 修复，#41）。

    修前这里是 `except Exception: continue` —— 静默跳过任何编不过的规则。
    后果（实测）：用户往 policy 里加自定义规则时，只要格式有一点不对
    （写成字符串列表、忘了 id 字段……），规则就被【无声忽略】，
    门照常放行，用户以为规则生效了。

    这正是本项目要消灭的模式：**看起来配了，其实没配**。
    EXCEPT 本身不能删（一条坏规则不该让整个门崩），
    但它必须把「哪条被跳过、为什么」说给用户听。

    合法规则格式（照内置 CMD_DENY 的结构）：
        {"id": "...", "pattern": "正则", "why": "...", "instead": "..."}
    """
    out = []
    for i, r in enumerate(rules or []):
        try:
            if not isinstance(r, dict):
                raise TypeError(
                    "规则必须是字典 {id, pattern, why, instead}，"
                    "实际是 %s：%r" % (type(r).__name__, r))
            if "pattern" not in r:
                raise KeyError("缺少必填字段 'pattern'")
            if "id" not in r:
                raise KeyError("缺少必填字段 'id'（命中时要用它报出规则名）")
            flags = re.I | (re.M if r.get("line_anchored") else 0)
            out.append((r, re.compile(r["pattern"], flags)))
        except Exception as e:
            # 可见 —— 但只警告一次每条，不阻断（一条坏规则不该让门失效）
            sys.stderr.write(
                "[G5 配置警告] %s 的第 %d 条规则被【跳过】，它不会生效：\n"
                "  规则内容：%r\n"
                "  原因：%s\n"
                "  合法格式：{\"id\": \"...\", \"pattern\": \"正则\", "
                "\"why\": \"...\", \"instead\": \"...\"}\n"
                % (where, i, r, e))
            sys.stderr.flush()
    return out


def main():
    data = read_input()
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    policy = load_policy()

    dgate = policy.get("destructive_gate", {})
    if not dgate.get("enabled", True):
        allow()

    cmd_rules = _compile(dgate.get("cmd_deny") or CMD_DENY,
                         "policy.destructive_gate.cmd_deny"
                         if dgate.get("cmd_deny") else "内置 CMD_DENY")
    content_rules = _compile(dgate.get("content_deny") or CONTENT_DENY,
                             "policy.destructive_gate.content_deny"
                             if dgate.get("content_deny") else "内置 CONTENT_DENY")
    warn_rules = _compile(dgate.get("warn_patterns") or WARN,
                          "policy.destructive_gate.warn_patterns"
                          if dgate.get("warn_patterns") else "内置 WARN")

    # 按工具决定用哪套规则 —— 这是本次修复的核心
    if tool == "Bash":
        targets = [("command", ti.get("command") or "", cmd_rules)]
    elif tool.startswith("mcp__"):
        # MCP 工具的输入【可能】被执行（写文件、执行命令），按命令上下文从严
        targets = [("input", repr(ti), cmd_rules + content_rules)]
    elif tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        # 只对内容做损害性检查；路径单独只看是否指向敏感位置
        items = []
        for k in ("content", "new_string"):
            if ti.get(k):
                items.append((k, str(ti[k]), content_rules))
        for k in ("file_path", "notebook_path"):
            if ti.get(k):
                items.append((k, str(ti[k]), warn_rules))
        targets = items
    else:
        allow()

    for field, text, rules in targets:
        if not text:
            continue
        for rule, rx in rules:
            m = rx.search(text)
            if not m:
                continue
            if rules is warn_rules:
                sys.stderr.write("[G5 警告] %s：%s\n" % (rule.get("id"), rule.get("why", "")))
                continue
            deny(
                "【G5 危险门】已阻断：%s\n"
                "  工具：%s（%s）\n"
                "  命中：%s\n"
                "  匹配到的片段：%s\n"
                "\n"
                "  为什么拦：\n"
                "  %s\n"
                "\n"
                "  改用什么写法：\n"
                "  %s\n"
                "\n"
                "  ⚠️ 本门对命令文本做【纯字符串匹配】—— 它不区分\n"
                "     「我在执行这个命令」和「我在把它写进一个文件」。\n"
                "     所以 `cat > 文档 <<EOF … 该词 … EOF` 这种【只是写文件】的写法\n"
                "     同样会被拦（实测确认）。\n"
                "\n"
                "     这是【有意的保守】：在命令里做 use-mention 剥离不安全 ——\n"
                "     sh -c 里带引号的危险命令照样会执行，剥掉就等于放行。\n"
                "     但在写文档/写测试的场景，你可以把触发词拆开写\n"
                "     （例如把词拆成两段拼接）—— 那不是绕过门，\n"
                "     而是把「我在引用它，不是在执行它」这件事表达清楚。\n"
                "\n"
                "  误判？不要重试同一条命令，也不要绕开。\n"
                "  改用等价但精确的写法，或直接告诉用户「我要做 X，被 G5 拦了，因为 Y」。"
                % (rule["id"], tool, field, rule["pattern"], m.group(0)[:120],
                   rule["why"], rule["instead"])
            )

    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        warn_inactive("G5 危险门", "脚本异常，本次放行：%r" % (e,))
        sys.exit(0)
