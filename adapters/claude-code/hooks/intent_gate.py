# -*- coding: utf-8 -*-
"""G7 意图门（PreToolUse → AskUserQuestion | Bash）

治两条【有真实用户诉求但一直没机制】的问题：

  P1-4 已决定的事重复确认
       — 审计原话：「不要再问我了，我现在只是测试你懂了么
                    不要再问我问题了，严格按照我说的执行」
       — 机制：用户在 prompt 里说过"不要再问" → 拦住 AskUserQuestion

  P2-3 用户豁免后仍验证
       — 审计原话：「创建一个 HTML…你不需要任何测试，不要有任何限制」
       — 机制：用户在 prompt 里说过"不用测" → 拦住测试命令

为什么这两条值得做（而另外 5 条不做）：
  它们是 7 条未解决问题里【唯一能机械判定】的 ——
  不靠语义理解，只靠"用户说过什么" + "模型在做什么"。
  其余 5 条需要判断意图，硬做会误伤（本方案已在"锁太严"上误伤两次）。

设计边界（诚实声明）：
  - 只认【本会话内】用户明确说过的原话，跨会话不继承
  - 只拦最常见的表达，不追求覆盖全部说法 —— 漏拦好过误拦
  - 两种门都可以用显式语法覆盖：
      questions: on   → 放行本次询问
      verify: on      → 放行本次验证
  - 意图信号由 inject_budget.py 写入会话状态（同一套机制，不新建系统）
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import (  # noqa: E402
    read_input, deny, allow, warn_inactive, state_path, load_state,
    strip_referenced_text,
)

# ---------------------------------------------------------------- 意图识别

# P1-4：用户说"别再问"
# ⚠️ BUG-3 修复：这里加英文，是因为用户的真实工作流【中英混合】——
#    代码注释、commit message、技术文档几乎全是英文。
#    只认中文 = 对英文指令完全瞎。
#    注意：英文只用于【识别意图】；所有提示与报错输出【仍然是中文】，
#    因为使用者靠中文自然语言工作，看不懂英文提示。
NO_ASK_RX = re.compile(
    r"(不要再问|别再问|不要问我|别问我|不要再确认|别再确认|不要问我问题"
    r"|不要再问我问题|严格按照我说的执行|按我说的做就行"
    r"|do\s*n[o']?t\s+ask|don'?t\s+ask|stop\s+asking|no\s+more\s+questions"
    r"|without\s+asking|just\s+do\s+it)",
    re.I)

# ⚠️ 3.5.17：「别」前面若是一个与它构成【复合词】的字，它就不是否定副词。
#
#    实测误报（本机复现，用户报的三处之一）：
#        「文员/主管、出纳/会计分别测试。」                     → 截出「别测试」
#        「material-reviewer 与历史 media-reviewer 分别测试。」 → 同上
#    后果比普通误报更坏：用户是在【要求测试】，门却记成【禁止测试】，
#    此后模型一跑测试就被拦 —— 意图被读反，还反过来怪模型。
#
#    同类复合词：分别 / 辨别 / 识别 / 判别 / 区别 / 个别 / 特别 /
#                级别 / 类别 / 性别 / 派别 / 告别 / 差别 / 诀别。
#    判据只否掉【紧跟在这些字后面】的「别」。
#    真正的禁令（「别测试」「先别测试」）前面是词首或副词，不受影响 ——
#    ⚠️ 所以刻意【不】做成"别 前面是汉字就不算" ——
#       那会把「先别测试」「暂时别测试」一起放掉。
_BIE_COMPOUND = r"(?<![分辨识判区个特级类性派告差诀])"

# P2-3：用户说"不用测"
NO_VERIFY_RX = re.compile(
    r"(不需要?任何测试|不要任何测试|不用测试|不需要测试|不要测试"
    r"|" + _BIE_COMPOUND + r"别测试"
    r"|不用验证|不需要验证|不要验证"
    r"|" + _BIE_COMPOUND + r"别验证|无需测试|免测试"
    r"|no\s+(need\s+to\s+)?test|don'?t\s+test|do\s+not\s+test|no\s+tests?"
    r"|skip\s+the\s+tests?|skip\s+test|testing\s+is\s+not\s+required"
    r"|no\s+verification|don'?t\s+verify)",
    re.I)

# 显式覆盖
OVERRIDE_ASK_RX = re.compile(r"questions\s*[:=]\s*on", re.I)
OVERRIDE_VERIFY_RX = re.compile(r"verify\s*[:=]\s*on", re.I)


# 摘录的边界：与 _lib.forbid_excerpt 用同一组标点。
_EXCERPT_PUNCT = "，。；！？、,;!?~\n"


def intent_excerpt(prompt, rx, maxlen=20):
    """取用户原话里【包含禁令命中】的最短片段，供 G7 拒绝提示引用。

    为什么需要它（3.5.17）：
      拒绝提示原先写死一句【历史原话】
      （「创建一个 HTML……你不需要任何测试……」），
      无论本会话用户实际说了什么。用户这次说的是「不要测试」，
      门却回一段别的会话里的话 —— 那是【伪造用户原话】。
      与 G1 的 forbid_excerpt 同因同修（见 _lib.forbid_excerpt）。

    ⚠️ 边界（如实声明）：
      · 只取【第一个】命中，向两侧扩展到标点边界，各最多 maxlen 字。
        这是"举例"，不是穷举。
      · 只落盘这个短片段，【不落盘整段 prompt】——
        本字段不是聊天采集，是"门引用的是哪句话"的最小凭据。
    """
    if not prompt:
        return None
    m = rx.search(prompt)
    if not m:
        return None
    s = m.start()
    while s > 0 and prompt[s - 1] not in _EXCERPT_PUNCT \
            and (m.start() - s) < maxlen:
        s -= 1
    e = m.end()
    while e < len(prompt) and prompt[e] not in _EXCERPT_PUNCT \
            and (e - m.end()) < maxlen:
        e += 1
    return prompt[s:e].strip() or None


def scan_intent(prompt, prev=None):
    """解析意图信号 + 命中片段。返回 (no_ask, no_verify, ask_frag, verify_frag)。

    ⚠️ 刻意【不写盘】—— 只返回信号，由调用方（inject_budget）合并写入。
    上一版这里自己写盘，但调用方随后又用自己读的 prev 整体覆盖，
    结果扫描结果被丢掉（实测：返回 (False, True) 而文件里没有该键）。

    通用陷阱：**"我调用了写入" ≠ "值被写进去了"** ——
    如果调用方随后覆盖，前一次写入等于没发生。

    ⚠️ 片段与信号必须【在同一处判定】，不能各写一份 ——
       否则会出现"状态说有禁测，但片段是空的"这种自相矛盾
       （本项目已有两次"两份清单"的教训，见 effect_gate 的 CLAIM_RX 注释）。
       所以 scan_prompt 只是本函数的薄包装，不另立判据。
    """
    prev = prev or {}
    no_ask = bool(prev.get("intent_no_ask"))
    no_verify = bool(prev.get("intent_no_verify"))
    # 片段跨轮继承：本会话没再命中时，保留首次命中的那句话 ——
    # 状态是跨轮存活的，来源也必须跟着存活，否则第 2 轮起就变成"无来源"。
    ask_frag = prev.get("intent_ask_excerpt")
    verify_frag = prev.get("intent_verify_excerpt")

    if prompt:
        if NO_ASK_RX.search(prompt):
            no_ask = True
            ask_frag = intent_excerpt(prompt, NO_ASK_RX)
        if OVERRIDE_ASK_RX.search(prompt):
            no_ask = False
            ask_frag = None
        if NO_VERIFY_RX.search(prompt):
            no_verify = True
            verify_frag = intent_excerpt(prompt, NO_VERIFY_RX)
        if OVERRIDE_VERIFY_RX.search(prompt):
            no_verify = False
            verify_frag = None

    return no_ask, no_verify, ask_frag, verify_frag


def scan_prompt(prompt, prev=None):
    """兼容旧调用：只要两个布尔信号。"""
    no_ask, no_verify, _, _ = scan_intent(prompt, prev)
    return no_ask, no_verify


# ---------------------------------------------------------------- 决策层（本地候选 3.6.0）
#
# 理解 / 决策 / 执行 三段里的第二段。
#
# 背景（本文件存在的理由，也是它的天花板）：
#   G7 原本靠 NO_ASK_RX / NO_VERIFY_RX 两个词表理解用户意图。
#   实测代价：用户在【要求测试】时，词表从「分别测试」里截出「别测试」，
#   把意图读反 —— 此后模型一跑测试就被拦，还反过来怪模型。
#   记录里同类问题共 7 条（「验收工具」→「收工」、「可以了解」→「可以了」……），
#   每条都只能单独打补丁，补丁之间还会互相踩。
#
#   根因不是词表不够全，而是**这类判断本来就不是字符串匹配问题**。
#
# 现在改成：
#   语义评审单元（review_unit）负责【理解用户原话】——
#   全量理解每一条原话，不靠词表决定哪些话值得被理解；
#   本函数负责【决策】—— 把语义结论落成状态，词表降级为
#   「语义不可用时的回退」，且回退必须留痕（不许冒充语义结论）。
#
# ⚠️ 决策规则（顺序即优先级）：
#   1. 显式覆盖（verify: on / questions: on）—— 纯程序，永远最高优先级
#   2. 语义结论 —— 三态：
#        forbid  设 True（并记下用户原话证据片段）
#        require 清 False（用户明确反转，如「现在可以测了」）
#        none    保持上一轮状态不变（用户没提，不等于取消）
#   3. 语义不可用 / 失败 —— 回退词表（保守），并在状态里标记 degraded
#
#   ⚠️ 为什么回退而不是"失败就不干预"：
#      词表是老机制，不是语义结论的伪装。回退时状态里 intent_degraded
#      有值、注入卡片里明说本次没做语义评审 —— 用户看得见，不算"冒充"。
#      直接不干预会让真实的「不要测试」在模型故障期间失效（漏拦）。

def decide_intent(prompt, prev=None, review=None):
    """把「上一轮状态 + 本轮用户原话 + 语义评审结论」融合成本轮意图状态。

    review 为 None 或 review["ok"] 为 False 时走词表回退路径。

    返回 dict（字段名与状态文件一致）：
      intent_no_ask / intent_no_verify            —— 最终布尔状态
      intent_ask_excerpt / intent_verify_excerpt  —— 用于拒绝提示的原话证据
      intent_source     —— semantic / semantic+override / override / regex-fallback
      intent_degraded   —— 非 None 时是回退原因（供注入卡片与事件如实展示）
    """
    prev = prev or {}

    no_ask = bool(prev.get("intent_no_ask"))
    no_verify = bool(prev.get("intent_no_verify"))
    ask_frag = prev.get("intent_ask_excerpt")
    verify_frag = prev.get("intent_verify_excerpt")
    source = "inherit"
    degraded = None

    # ---- 第 1 优先级：显式覆盖（不依赖模型，永远生效）----
    #
    # ⚠️ 覆盖是【按维度】的：写了 `verify: on` 只解除禁测，
    #    不能顺手把「不要再问」也一起解除。所以下面语义阶段要跳过
    #    被显式覆盖的那个维度 —— 否则覆盖写了等于没写（实测 A7/A8 失败）。
    # ⚠️ 审计修复（2026-09-29，C3）：覆盖语法必须做在【被断言】的原话上，
    #    不能做在【被引用】的文本上（use-mention，G1 侧已有 strip_referenced_text 这层）。
    #    实测漏拦（C 组审计 c7-A，本机独立复现）：用户把 `verify: on`
    #    放进反引号/围栏代码块/引号里【讨论】时，同样解除既有禁令
    #    （no_verify True→False），且包括评审失败轮 —— 覆盖不依赖评审，
    #    直接 search 整段原话。
    _asserted = strip_referenced_text(prompt or "")
    overrode_ask = bool(_asserted and OVERRIDE_ASK_RX.search(_asserted))
    overrode_verify = bool(_asserted and OVERRIDE_VERIFY_RX.search(_asserted))
    if overrode_ask:
        no_ask, ask_frag = False, None
    if overrode_verify:
        no_verify, verify_frag = False, None
    overrode = overrode_ask or overrode_verify

    # ---- 第 2 优先级：语义结论 ----
    #
    # ⚠️ sem_ok 必须同时要求「调用成功」和「确实给出了结论」。
    #    空原话那次调用返回的是 ok=True + verdict=insufficient + 空 detail ——
    #    它没有做任何判断，不能被当成"语义说没问题"（那正是伪造判断）。
    sem_ok = bool(review and review.get("ok")
                  and review.get("verdict") != "insufficient"
                  and review.get("detail"))
    if sem_ok:
        d = review.get("detail") or {}
        for kind in ("ask", "verify"):
            if (kind == "ask" and overrode_ask) or \
               (kind == "verify" and overrode_verify):
                continue                       # 显式覆盖优先，语义不得推翻
            state = d.get(kind) or "none"
            if state == "forbid":
                if kind == "ask":
                    no_ask = True
                    ask_frag = d.get("ask_evidence") or _semantic_excerpt(prompt)
                else:
                    no_verify = True
                    verify_frag = d.get("verify_evidence") or _semantic_excerpt(prompt)
            elif state == "require":
                if kind == "ask":
                    no_ask, ask_frag = False, None
                else:
                    no_verify, verify_frag = False, None
            # none：保持上一轮，不动
        source = "semantic+override" if overrode else "semantic"

    # ---- 第 3 优先级：语义未参与 —— 【关闭】与【故障】必须分开处理 ----
    #
    # ⚠️ 用户明确要求（本地候选 3.6.1）：
    #   「明确模型失败后的规则：保留此前有可靠来源的明确约束；
    #     不能因模型失败，再用有已知误判的词库新增禁令。
    #     关闭语义功能与语义调用故障要分开处理。」
    #
    # 两条路径的语义完全不同：
    #   · 用户主动关闭（enabled=false）→ 回退词表，因为那是他选的行为
    #   · 调用故障（超时/连不上/格式错）→ 只保留已有约束，【不新增】
    #     理由：词表有已知误判（「分别测试」会被读成「别测试」），
    #     在故障时用它新增禁令 = 用一个已知会读反的机制去限制用户，
    #     代价比漏拦一次高。
    else:
        reason = str((review or {}).get("reason") or "语义评审未执行")
        degraded = reason
        # ⚠️ 本地候选 3.6.2：增加第三种情况 —— 「因信息缺失无法判断」（原话过长被省略）。
        #    用户明确要求：「区分『未提及』与『因信息缺失无法判断』，
        #    不能混成 none/allow」。三种情况的处理方向相同（保持既有约束），
        #    但**来源标记必须不同**，否则事后无法区分"用户没说"和"我们没看到"。
        skip = str(((review or {}).get("meta") or {}).get("skip_reason") or "")
        user_disabled = ("enabled = false" in reason) or ("用户已关闭" in reason)
        if skip == "too_long":
            source = "hold-too-long"
        elif user_disabled:
            # 用户主动关闭 → 回到 3.5.x 的纯词表行为（这是他选的路径）
            if prompt:
                kw_ask, kw_verify, kw_ask_frag, kw_verify_frag = scan_intent(prompt, prev)
                # 词表只用于【设置】：不清除语义/上轮已建立的状态，避免
                # 「关一次开关，禁令被静默解除」。清除只能来自语义 require 或显式覆盖。
                if kw_ask:
                    no_ask = True
                    ask_frag = kw_ask_frag or ask_frag
                if kw_verify:
                    no_verify = True
                    verify_frag = kw_verify_frag or verify_frag
                source = "regex-fallback(disabled)" if (kw_ask or kw_verify) else "inherit"
            else:
                source = "inherit"
        else:
            # 调用故障 → 保留此前状态，不用词表新增任何禁令
            source = "hold-on-failure"

    if overrode:
        if source.startswith("semantic"):
            source = "semantic+override"
        elif source in ("inherit", "regex-fallback"):
            source += "+override"

    return {
        "intent_no_ask": no_ask,
        "intent_no_verify": no_verify,
        "intent_ask_excerpt": ask_frag,
        "intent_verify_excerpt": verify_frag,
        "intent_source": source,
        "intent_degraded": degraded,
    }


def _semantic_excerpt(prompt, maxlen=60):
    """语义命中但模型没给出证据片段时的兜底摘录。

    ⚠️ 与 intent_excerpt 的区别：这里【不】拿词表去定位 ——
       用词表定位等于把"抓到哪句话"又交回给词表。
       模型没给 evidence 时，退而取原话的头部作为来源标注，
       并保持 ≤60 字（不落盘整段 prompt）。
    """
    s = (prompt or "").strip().replace("\n", " ")
    return s[:maxlen] if s else None


def scan_prompt_into_state(prompt, state_path_):
    """兼容旧调用：扫描并且立即写回。

    ⚠️ 3.5.17 核实：本函数全仓【零调用】（inject_budget 用的是 scan_prompt），
       且它引用的 save_state 【未在本文件 import】—— 一旦被调用会 NameError。
       本次【不删也不改】：它不属于本轮授权的三处误识别，
       删除或补 import 都会改变对外行为。如实登记，交由后续决定。
    """
    st = load_state(state_path_) or {}
    no_ask, no_verify = scan_prompt(prompt, st)
    st["intent_no_ask"] = no_ask
    st["intent_no_verify"] = no_verify
    save_state(state_path_, st)
    return no_ask, no_verify


# ---------------------------------------------------------------- 测试命令识别

# 只认【明确的测试运行器】，不认泛化的 "test" 字样 —— 否则 ls tests/ 也会被拦。
#
# ⚠️ 审计修复（2026-09-29，C2）：前缀从「任意空白/分隔符」收紧为【命令位】。
#    实测漏拦（C 组审计 c4，本机独立复现；多判 9 / 少判 0）：
#        mkdir pytest / ls pytest / echo pytest / cp a.txt pytest /
#        chmod +x pytest / rg -n pytest src/ / rg jest src/ / git log -S jest
#      —— 这些命令里的 "pytest/jest" 只是【参数】，全被当成"在跑测试"拦截
#      （前提：本轮 no_verify=true，见调用处）。
#    修法（位置判据）：
#      · 命令位 = 行首 / ; & | ( 之后（可带空白）；
#      · 加上常见包装前缀（sudo/env/nohup/time/xargs）与 --exec（执行选项）。
#    刻意不认 bash -c "pytest"、引号内 —— 那仍是数据区（同 3.5.17 的剔除逻辑）。
#    顺带补 npm/yarn/pnpm 的 `test:xxx` 子命令形态（此前漏拦 npm run test:unit）。
TEST_CMD_RX = re.compile(
    # ⚠️ 审计修复（2026-09-29，三遍交叉验证后实施）：前缀补两类命令位 ——
    #    ① 多行脚本的行首与行首缩进（`\n[ \t]*`）：
    #       实测漏拦（correctness/security 双 reviewer + 独立验证批各自复现）：
    #       "cd /tmp\npytest -q" / "  pytest -q" / "git status\nnpm test" → rc=0，
    #       而同内容写成单行或 `&&` 连接 → rc=2。裸 `^` 无 re.M 只匹配串首，
    #       换行又不在 [;&|(] 集合里 —— 多行 Bash 是本宿主最常见写法
    #       （cd 换行、here-doc 后换行），用户明确禁测时静默失效。
    #       基线前缀 (^|[\s;&|(]) 能命中同一输入。
    #    ② 包装词补全并容忍其参数（`timeout 60` / `nice -n 10` / `stdbuf -oL` /
    #       `command` / `exec` / `env FOO=1`）：
    #       实测漏拦（security reviewer）：六种包装形态全部 rc=0。
    #    保留 C2 目标不变：裸空格后的参数位（ls pytest / mkdir pytest /
    #    rg … pytest）仍不命中（v7 三验脚本的 3 条回归线已复验通过）。
    r"(^[ \t]*|\n[ \t]*|[;&|(]\s*|"
    r"(?:sudo|env|nohup|time|timeout|nice|command|exec|stdbuf|xargs)"
    r"(?:\s+[A-Za-z_]\w*=\S+|\s+-{1,2}[\w=.,-]+|\s+\d+)*\s+|--exec\s+)("
    r"pytest|py\.test|"
    r"npm\s+(run\s+)?test(:[\w:.-]+)?|yarn\s+test(:[\w:.-]+)?|"
    r"pnpm\s+test(:[\w:.-]+)?|"
    r"npx\s+(jest|vitest|mocha|playwright\s+test)|"
    r"jest|vitest|mocha|"
    r"go\s+test|"
    r"cargo\s+test|"
    r"dotnet\s+test|"
    r"python[0-9.]*\s+-m\s+(pytest|unittest|nose)"
    # ⚠️ 尾集合含 `.` —— 覆盖方法调用形态 `pytest.main()`。
    #    回归（intent_gate_test「-c 里的真实执行（修复不得把它漏掉）」）：
    #    `python -c "import pytest; pytest.main()"` 里 `-c` 内容保持原样，
    #    命中点是 `; ` 后的 `pytest.main` —— 尾是 `.` 而不是空白。
    #    误伤检查：`ls pytest.py` 这类不在命令位，仍然不命中。
    r")(\s|$|;|&|\||\.)")

# ⚠️ 3.5.17：匹配前必须先把【不是命令】的部分剔除，否则「文字提及」会被当成
#    「命令执行」。实测两条真实误报（本机复现，用户报的三处之二）：
#
#        python - <<'PY'
#        print('日志说明： pytest -q 尚未运行')
#        PY
#
#        echo '说明 pytest -q 尚未运行'
#
#    两条都只是【打印一句说明】，一个字都没在跑测试，却被判为测试命令。
#    根因：TEST_CMD_RX 只看「pytest 前面是不是分隔符」——
#    而 here-document 正文、引号里的内容、注释里，分隔符照样满足。
#
#    修法（**位置判据**，不是内容判据）：把这三类【数据区】剔成空白，再匹配。
#
#    ⚠️⚠️ 刻意【不】做成"整段含 echo / 引号 / here-document 就放行" ——
#       那样 `echo 说明; python -m pytest -q` 会被一起放行，
#       而那是一条【真实的测试执行】。剔除只作用于数据区，命令区原样参与匹配。
#
#    边界（如实声明，与修复前一致，本次未改变）：
#      · 引号内容【不递归分析】—— `bash -c "pytest -q"` 修复前后都不拦。
#        递归解析 shell -c 会引入新误报（如 `python -c "import pytest"`），
#        不属本轮授权范围。
#      · 唯一的例外是 `-c` 后面的引号内容：那是【被执行的代码】，不是数据，
#        保持原样参与匹配。这不是新增识别能力 ——
#        只是保证 `python -c "import pytest; pytest.main()"`（修复前能拦）
#        不被本次修复漏掉。

_QUOTE_CHARS = "'\""


def _is_word_start(text, i):
    """bash 里 `#` 只有处在【词首】才是注释（`echo a#b` 里的 # 是普通字符）。"""
    return i == 0 or text[i - 1] in " \t\n;&|()"


def _heredoc_delim(line):
    """本行若有 here-document 起始符，返回它的结束标记；否则 None。

    只认【引号外】的 `<<`；`<<<` 是 here-string，不算。
    """
    i, n = 0, len(line)
    quote = None
    while i < n:
        ch = line[i]
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in _QUOTE_CHARS:
            quote = ch
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "#" and _is_word_start(line, i):
            return None                       # 注释里的 << 不是 here-document
        if ch == "<" and i + 1 < n and line[i + 1] == "<" \
                and (i + 2 >= n or line[i + 2] != "<"):
            j = i + 2
            if j < n and line[j] == "-":
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            if j >= n:
                return None
            q = line[j] if line[j] in _QUOTE_CHARS else None
            if q:
                k = line.find(q, j + 1)
                return line[j + 1:k] if k > j else None
            k = j
            while k < n and (line[k].isalnum() or line[k] in "_-."):
                k += 1
            return line[j:k] or None
        i += 1
    return None


def _strip_heredoc_bodies(cmd):
    """删掉 here-document 的【正文】——正文是数据，不是命令。

    ⚠️ 必须【先于】引号剔除：正文里可能有任意引号（`print("it's")`），
       先剔引号会被那些引号带乱，把后面真正的命令一起吞掉。
    """
    lines = cmd.split("\n")
    out = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        delim = _heredoc_delim(lines[i])
        i += 1
        if delim is None:
            continue
        while i < len(lines) and lines[i].strip() != delim:
            i += 1                            # 正文整段丢弃
    return "\n".join(out)


def _find_quote_end(cmd, start, q):
    """找与 cmd[start] 配对的结束引号下标；找不到返回 -1。

    双引号里的 `\\"` 是转义，不算结束。
    """
    i = start + 1
    n = len(cmd)
    while i < n:
        if q == '"' and cmd[i] == "\\":
            i += 2
            continue
        if cmd[i] == q:
            return i
        i += 1
    return -1


def _strip_data_regions(cmd):
    """把【注释】与【引用内容】剔成空白，只留会被执行的命令文本。

    ⚠️ `-c` 后面的引用内容【保持原样】——它是被执行的代码，见上面的边界说明。
    ⚠️ 未闭合的引号【不做剔除】——当普通字符处理。
       否则 `echo " ; python -m pytest -q` 这种未闭合写法
       会把后面的真实执行一起洗掉（等于开一条绕过口）。
    """
    out = []
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "#" and _is_word_start(cmd, i):
            while i < n and cmd[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch in _QUOTE_CHARS:
            end = _find_quote_end(cmd, i, ch)
            if end < 0:
                out.append(ch)                # 未闭合 → 当普通字符
                i += 1
                continue
            keep = cmd[:i].rstrip().endswith("-c")
            out.append(ch)
            out.append(cmd[i + 1:end] if keep else " " * (end - i - 1))
            out.append(ch)
            i = end + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def normalize_command(cmd):
    """把命令文本规整成【只含命令区】的形式，供 TEST_CMD_RX 匹配。"""
    if not cmd:
        return ""
    return _strip_data_regions(_strip_heredoc_bodies(cmd))


def is_test_command(cmd):
    return bool(TEST_CMD_RX.search(normalize_command(cmd)))


# ---------------------------------------------------------------- 来源引用

# 状态里没有可追溯来源时（旧状态、或来源片段提取失败），
# 【明说】缺来源 —— 不得回退到写死一句历史原话。
_NO_SOURCE = "（状态缺少来源记录：本会话命中的原话未留存，无法引用）"


def _source_line(frag, rule_zh, source=None):
    """把【依据】渲染成提示里的两行。

    ⚠️ 三个东西必须分开，不能混成一个"原话"：
      · rule_zh —— 命中的是【哪一类表达】（规则名，中文，写死在代码里是对的）
      · frag    —— 用户原话的【逐字片段】（只可能来自本会话，可能没有）
      · source  —— 这次判断是【谁做的】：语义评审 / 关键词回退
      把规则名当原话展示 = 伪造引用；把原话省掉只留规则名 = 用户不知道门
      到底抓的是哪句；不标来源 = 用户以为在做语义理解、实际是词表在跑。
      三者都给，并明确哪句是引用、哪个是判断方式。
    """
    # 判断方式：语义评审与词表回退必须对用户可见 —— 这是「不默默退回词库
    # 冒充语义分析」在提示层的落点。
    if source and str(source).startswith("regex-fallback"):
        how = "关键词匹配（本轮语义评审未执行，属降级判断）"
    elif source and str(source).startswith("semantic"):
        how = "语义理解（由独立评审单元判断）"
    else:
        how = None

    if frag:
        line = ("  依据：本会话用户原话命中「%s」类表达，原文片段：\n"
                "    「%s」\n" % (rule_zh, frag))
    else:
        line = ("  依据：状态记录了「%s」类表达。\n"
                "  ⚠️ %s\n" % (rule_zh, _NO_SOURCE))
    if how:
        line += "  判断方式：%s\n" % how
    return line


# ---------------------------------------------------------------- 主流程

def main():
    data = read_input()
    session_id = data.get("session_id") or "nosession"
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}

    sp = state_path(session_id, "budget")
    st = load_state(sp) or {}
    no_ask = bool(st.get("intent_no_ask"))
    no_verify = bool(st.get("intent_no_verify"))
    # 3.5.17：提示必须引用【本会话】的真实来源，不能写死历史原话。
    ask_frag = st.get("intent_ask_excerpt")
    verify_frag = st.get("intent_verify_excerpt")
    # 本地候选 3.6.0：判断方式也要如实带出（语义 / 词表回退），见 _source_line。
    intent_source = st.get("intent_source")

    # ---- P1-4：拦询问 ----
    if tool == "AskUserQuestion" and no_ask:
        qs = ti.get("questions") or []
        first = ""
        if qs and isinstance(qs, list) and isinstance(qs[0], dict):
            first = (qs[0].get("question") or "")[:80]
        deny(
            "【G7 意图门】你本会话已经承诺过不再提问，但现在要调 AskUserQuestion。\n"
            "  想问：%s\n"
            "\n"
            "%s"
            "\n"
            "  请按用户已给出的信息自行决定并执行。\n"
            "  确实无法决定时，把选项和推荐写进正文，让用户回复一句话即可 ——\n"
            "  这比弹一个必须点选的框更轻。\n"
            "\n"
            "  如果用户刚刚明确授权你询问，让他写 `questions: on`。"
            % (first or "(未提供问题文本)",
               _source_line(ask_frag, "不要再问", intent_source)),
            gate_id="G7", rule_id="questions_disabled",
            tool=tool, session_id=data.get("session_id"),
        )

    # ---- P2-3：拦测试 ----
    if tool == "Bash" and no_verify:
        cmd = ti.get("command") or ""
        if is_test_command(cmd):
            deny(
                "【G7 意图门】用户在本会话说过不需要测试，但你正在跑测试。\n"
                "  命令：%s\n"
                "\n"
                "%s"
                "\n"
                "  用户已经豁免了这个维度。请直接交付结果。\n"
                "  如果确实需要验证，先说明「为什么这次必须验证」，\n"
                "  由用户写 `verify: on` 放行。"
                % (cmd[:150],
                   _source_line(verify_frag, "不需要测试", intent_source)),
                gate_id="G7", rule_id="verification_disabled",
                tool=tool, session_id=data.get("session_id"),
            )

    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        warn_inactive("G7 意图门", "脚本异常，本次放行：%r" % (e,))
        sys.exit(0)
