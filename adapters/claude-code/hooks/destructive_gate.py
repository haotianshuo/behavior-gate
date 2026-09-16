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

# ⚠️ 共享的 rm 选项解析片段（3.5.10 修复，见 #48）。
#
#   问题（由真实会话 + 真实子进程发现）：
#     三条同族规则各自抄了一份选项解析 ——
#         rm_rf_traversal / rm_rf_windows_drive / rm_rf_windows_env
#     抄的是同一个有缺陷的写法：
#         (-[a-zA-Z]*\s+)*-?[a-zA-Z]*r[a-zA-Z]*f?\s+
#     它有三个洞：
#       ① 没有结尾锚定 → `rm -rf /tmp/v350` 被前缀贪婪匹配成 `rm -rf /`
#          （真实误报：本机 09-15 09:51 拦过一次正常的临时目录清理）
#       ② 不认短选项分离 → `rm -r -f /` 放行（真删根目录）
#       ③ 不认大写 R    → `rm -Rf /`  放行（同上）
#
#   取舍：抽成共享片段，而不是在三条规则里各抄一份新正则。
#   依据本项目 #19 / #38 的教训 —— 同一条规则只在一侧修好，另一侧漏掉，
#   是这个项目反复出现的失败形状。
#
#   语义（保持与原规则一致，不扩大范围）：
#     只有当【选项序列里出现过含 r/R 的选项】且【目标恰好是根】时才命中。
#     `rm -f /`（非递归）依旧放行 —— 那不是递归删除。
#
#   两个 lookahead 的分工：
#     (?=...)  确认选项序列里确实有 r/R（否则 rm -f / 会被误拦）
#     (?:...)  吃掉完整的选项序列，让后面的路径锚定生效
_RM_OPTS = (r"(?=(?:-{1,2}\S+\s+)*-\S*[rR]\S*\s)"   # 必须含递归标志
            r"(?:-{1,2}\S+\s+)+")                    # 吃掉全部选项
_RM_ROOT_TAIL = r"(\s|$|\*)"                         # 结尾锚定：恰好是根


# ---- MCP 字段名 → 规则集 的显式名单（3.5.10 / #53）----
#
# ⚠️ 为什么是【精确名单】而不是子串匹配：
#    修前用 `"content" in name or "text" in name or "body" in name`，
#    而 `"text" in "context"` == True —— 字段名叫 context 的被误判成内容字段。
#    这与 #3（`bg-` 子串误配）是同一形状：**用子串当身份判据**。
#    那边的 _is_ours 已改成精确判定，这里没跟上。
#
# 名单外的字段不猜 —— 走「只过 content_rules」的 FAIL-OPEN 分支
# （见 MCP 分派处的注释）。
#
# ⚠️ 只保留【有明确命令语义】的字段名。
#    刻意【不】包含 data / input / payload 这类【通用容器名】——
#    它们语义不明，与"名单外字段"是同一类，不该享受"假设是命令"的待遇。
#    实测：这三个字段名在真实 MCP 调用里一次都没出现过（550 次调用）。
#    留着它们反而会让"拿它们装自由文本"的 server 过拦。
#
#    ⚠️ 但 code **保留** —— 它与上面三个不同：
#       code 有明确的"要执行的代码"语义（MCP 常见 mcp__repl__execute
#       这类工具的入参就叫 code），与 script 同类。
#       分清"通用容器名"与"有明确执行语义的名字"是这个名单的关键。
MCP_CMD_FIELDS = {
    # 会被执行的命令 / 脚本。这些字段的值可能真的进 shell。
    "command", "cmd", "script", "shell", "exec", "argv", "args",
    "code", "stdin",
}


# ⚠️ 不变量（3.5.10 / #48 确立）：**每条规则只作用于语义对应的字段**。
#
#   字段语义矩阵 —— 改规则前先看这张表，别把规则放进不对的字段：
#
#     可执行上下文（cmd_deny，真会被执行）
#       Bash.command
#       MCP.*.command / MCP.*.path / 其他非内容字段
#
#     写入内容（content_deny，只是写下来）
#       Edit / Write / MultiEdit 的 content / new_string
#       MCP.*.content / MCP.*.text / MCP.*.body
#
#   判据：`content` 字段 = 写入的内容，不是正在执行的命令。
#   写文件时提到 `pkill` 是「写入内容」，不是「执行命令」——
#   所以它只过 content_deny。
#
#   为什么把这条写进代码而不只写进测试：
#     3.5.10 之前 MCP 通道用 `cmd_rules + content_rules` 混扫 repr(ti)，
#     导致 cmd 类规则在 content 字段命中（实测：MCP 写文件含 pkill 被
#     fuzzy_kill 拦住）。这类「规则放错字段」的问题不会让任何现有测试变红，
#     只有把不变量写在规则定义旁边，才可能在 review 时被看见。
#
#   ⚠️ 已知边界（3.5.10 / #48 实测确认，有意保留）：
#     字段分离后，【写入内容】里的 Windows 盘符删根命令不再被拦：
#         MCP 写文件 content = "rm -rf D:/"      → 放行
#         MCP 写文件 content = "rm -rf C:/Users" → 放行
#     原因：content 字段只过 CONTENT_DENY，而 rm_rf_in_content 只覆盖
#           (/|~|$HOME)，不含盘符；rm_rf_windows_drive 是 cmd 规则。
#     注意 Edit / Write 工具【一直】就是这个行为 —— 不是本轮新造的不一致，
#     而是把 MCP 对齐到了 Edit/Write 的既有语义。
#     取舍：写文件的内容不直接执行；要拦「真会执行的命令」，
#           那是 Bash / MCP.command 通道的职责，那里盘符覆盖是完整的。
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
        # 3.5.10 修复（#48）：补结尾锚定 + 认短选项分离 + 认大写 R。
        # 修前：`rm -rf /tmp/v350` 误拦（前缀贪婪），`rm -r -f /` 与 `rm -Rf /` 漏拦。
        "pattern": r"\brm\s+" + _RM_OPTS + r"(/|~|\$HOME|\*)" + _RM_ROOT_TAIL,
        "why": "递归强删根目录/家目录/通配。\n"
               "  只匹配【目标恰好是根】的情况 ——\n"
               "  `rm -rf /tmp/build` 这类正常清理不会被拦。",
        "instead": "写出完整、具体的路径，并先 `ls` 确认目标。",
    },
    {
        # 3.5.9 新增（#44）。外部复核实测：本机是 Windows 11，
        # 而上面那条规则只认 POSIX 根（/ ~ $HOME *），于是
        #     rm -rf D:/   /  rm -rf C:/   /  rm -rf C:/Users/<用户>
        # 全部【放行】—— 在 Windows 上等于没有保护。
        # 被 /d/... 拦住是巧合（它以 / 开头），不是设计了 Windows 支持。
        "id": "rm_rf_windows_drive",
        # 3.5.10 修复（#48）：与 rm_rf_traversal 共用选项解析片段。
        # 修前同样漏 `rm -r -f D:/` 与 `rm -Rf D:/`。
        #
        # 3.5.10 修复（#51）：第三分支补尾锚定。
        #   修前：`[A-Za-z]:[\\/](Users|Windows|Program)` 无收尾 ——
        #         于是 `C:/Users/me/任意/更深/路径` 全被当成「删用户目录」。
        #   实测（另一台复核报的，本机复现）：4 个用例误报 ——
        #         C:/Users/me/proj/node_modules、.cache/pip、
        #         AppData/Local/Temp/b、Downloads/tmp 全部被拦。
        #   而这条规则的注释【自己写着】「更深的项目路径不拦」——
        #   注释与实际不符，这正是本项目反复出现的形状。
        #   修法：补上结尾锚定，只在目标是 profile 根本身时才拦。
        "pattern": r"\brm\s+" + _RM_OPTS +
                   r"([A-Za-z]:[\\/]?(\s|$|\*)|"
                   r"[A-Za-z]:[\\/][^\\/\s]+[\\/]?\s*$|"
                   r"[A-Za-z]:[\\/](Users|Windows|Program)"
                   r"([\\/][^\\/\s]+)?[\\/]?" + _RM_ROOT_TAIL + r")",
        "why": "递归强删 Windows 盘符根、盘符下第一层目录、或系统目录。\n"
               "  实测：`rm -rf D:/` 与 `rm -rf C:/Users/<用户>` 此前全部放行 ——\n"
               "  规则只认 POSIX 根路径，在本机（Windows 11）等于没有保护。\n"
               "  取舍：只拦到【盘符 + 第一层】（D:/important），\n"
               "  以及系统目录（C:/Users、C:/Users/<用户>、C:/Windows）。\n"
               "  D:/myproj/build、C:/Users/me/proj/node_modules 这类更深的路径不拦\n"
               "  —— 那是常规清理，机械规则区分不了「重要目录」与「项目内构建目录」。",
        "instead": "写出完整的具体路径（如 D:/myproj/build），并先 `ls` 确认目标。",
    },
    {
        "id": "rm_rf_windows_env",
        # 3.5.10 修复（#48）：与 rm_rf_traversal 共用选项解析片段。
        # 修前同样漏 `rm -r -f $env:TEMP/x` 与 `rm -Rf $TEMP`。
        "pattern": r"\brm\s+" + _RM_OPTS +
                   r"(\$env:(TEMP|TMP|USERPROFILE)|\$TEMP|\$TMP|\$USERPROFILE|\$HOME)"
                   r"([\\/](\s|$|\*))?",
        "why": "递归强删 Windows 环境变量指向的目录（临时目录/用户目录）。\n"
               "  实测 `rm -rf $env:TEMP/x` 与 `rm -rf $TEMP/x` 此前放行。",
        "instead": "写出完整的具体路径，并先 `ls` 确认目标。",
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
        # 3.5.10 修复（#48）：与 CMD_DENY 的三条 rm 规则共用同一个选项解析
        # 片段 _RM_OPTS，而不是各写各的。
        # 修前同样漏 `rm -r -f /` 与 `rm -Rf /`（实测坐实）——
        # 与 rm_rf_traversal 是同一个缺陷形状，只是住在另一套规则里。
        "pattern": (r"^[ \t]*\brm\s+" + _RM_OPTS +
                    r"(/|~|\$HOME)(\s|$|--no-preserve-root)"),
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

    # 按工具决定用哪套规则。
    #
    # ⚠️ 不变量（3.5.10 / #48 确立）：**每条规则只作用于语义对应的字段**。
    #     完整矩阵见模块头 CMD_DENY / CONTENT_DENY 的定义处。
    #
    #     content 字段 = 写入的内容，不是正在执行的命令 ——
    #     写文件时提到 `pkill`/`rm` 是「写入内容」，不是「执行命令」。
    #     所以 MCP 写文件的 content 只过 content_rules，不过 cmd_rules。
    #
    #     ⚠️ 3.5.10 之前这里只扫 repr(ti)，带来两个叠加问题：
    #        ① repr 给每个值加一层引号，末尾变成 "'}"。带结尾锚定的规则
    #           （rm_rf_traversal / rm_rf_in_content / rm_rf_windows_env）
    #           在 MCP 通道【永远匹配不到】。MCP 的 rm 保护实际是靠
    #           rm_rf_traversal 的【前缀贪婪缺陷】侥幸生效的。
    #        ② cmd_rules + content_rules 混用 → 跨字段泄漏：
    #           MCP 写文件的 content 字段被 cmd 类规则拦（实测坐实）。
    #     现在改为逐字段 + 按字段语义分派规则。
    if tool == "Bash":
        targets = [("command", ti.get("command") or "", cmd_rules)]
    elif tool.startswith("mcp__"):
        # 逐字段取真实值，而不是 repr(整体) —— 让锚定在 MCP 通道同样成立。
        # 但【不】把两套规则混用：命令类字段 → cmd_rules，
        # 内容类字段 → content_rules。
        #
        # ⚠️ 3.5.10 修复（#53）：分类判据从【裸子串】改成【显式名单】。
        #
        #    修前是：
        #        if "content" in name or "text" in name or "body" in name:
        #   而 `"text" in "context"` == True —— 于是字段名叫 `context` /
        #   `context_id` / `subtext` 的会被当成【内容字段】，分到 content_rules。
        #   而 cmd 类规则（rm_rf_windows_drive 等 5 条）只在 cmd_rules 里 ——
        #   两边都够不着，等于【静默漏拦】。
        #
        #   同一处判据还造成【反方向】的错：
        #       自由文本字段（prompt / description）不含 content/text/body
        #       → 落进 else → 被当成命令字段 → 套 cmd_rules
        #       → 写一句「分析一下 pkill 为什么危险」就被 fuzzy_kill 拦。
        #   实测（本机真实工具 mcp__scheduled-tasks__*）：
        #       {"prompt": "分析一下 pkill 这类命令为什么危险"} → 拦 ✗
        #
        #   根因：**字段名判不出字段语义**，而裸子串让名字判断更不可靠。
        #   这与 #3（`bg-` 子串误配）是同一形状 —— 那边的 _is_ours 后来
        #   改成了按 basename 精确判定，这里没跟上。
        #
        #   修法：用显式名单。名单外的字段【不猜】—— 走下面的保守分支。
        items = []
        for k, v in ti.items():
            field = "input." + str(k)
            name = str(k).lower()
            if name in MCP_CMD_FIELDS:
                # 只对【已知会执行】的字段套命令规则。
                items.append((field, str(v), cmd_rules))
            else:
                # 已知内容字段 + 【名单外字段】→ 都只过 content_rules。
                #
                # ⚠️ 为什么名单外字段【不】套 cmd_rules（取舍说明）：
                #    本项目 README 明写「失败策略是 FAIL-OPEN —— 宁可漏拦，
                #    不可把用户会话卡死」。字段名判不出语义时，两个方向的
                #    代价并不对等：
                #
                #      误拦（套 cmd）：写一句「分析一下 pkill 为什么危险」
                #                    放进 prompt → 被拦 → 一次完整往返。
                #                    实测坐实（本机 mcp__scheduled-tasks__*）。
                #      漏拦（不套 cmd）：名单外字段里的危险串不会被拦。
                #                    代价取决于那个字段会不会被执行。
                #
                #    实测依据（不是推断）：本机【当前可达】的 3 个 MCP 工具
                #    （mcp__scheduled-tasks__*）的全部字段是
                #    taskId / prompt / description / cronExpression / fireAt
                #    / enabled / notifyOnCompletion —— 语义是 ID / 文本 /
                #    时间 / 布尔，没有一个承载 shell 命令。
                #
                #    ⚠️ 这条证据只覆盖【当前可达】的工具。它【不证明】
                #    "文本字段永远不会被执行"（那取决于各 server 的实现，
                #    不在本门可观测范围）。所以：
                #      若将来装了 filesystem / shell / 数据库类 MCP server，
                #      且其命令字段名不在 MCP_CMD_FIELDS 里 → 会漏拦。
                #    这是刻意取舍，与「门是防误操作层，不是安全边界」一致。
                #    见 install.md 的「已知边界」。
                items.append((field, str(v), content_rules))
        targets = items
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
                   rule["why"], rule["instead"]),
                gate_id="G5", rule_id=rule["id"], tool=tool,
                session_id=data.get("session_id"),
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
