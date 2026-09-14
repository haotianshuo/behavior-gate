# -*- coding: utf-8 -*-
"""
Claude Code 行为门 · 共享库

设计约束（全部来自官方 hook 文档实测核对）：
  - 唯一阻断码是 exit 2。exit 1 是【非阻断错误】，工具会照常执行。
  - PreToolUse 在子代理内也会触发，带 agent_id / agent_type。
  - Stop / SubagentStop 带 last_assistant_message 和 stop_hook_active。
  - UserPromptSubmit 的 stdout 明文会被加入上下文。

失败策略：FAIL-OPEN（宁可漏拦，不可把用户会话卡死）。
  但 fail-open 必须【可见】—— 见 warn_inactive()。
  静默失败 = 看起来装了门其实门是假的，这正是本方案要消灭的模式。
"""

import json
import os
import sys
import re
import time as _time
import shutil as _shutil
import tempfile

# ---------------------------------------------------------------- 基础 IO

def read_input():
    """读取 hook stdin 的 JSON。任何异常都返回 {}，绝不抛出。"""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def emit_json(obj):
    """输出 JSON（UTF-8，不转义中文）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def emit_text(text):
    """输出明文。UserPromptSubmit 下会被加入上下文。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.stdout.write(text)
    sys.stdout.flush()


def deny(reason, hook_event="PreToolUse"):
    """阻断【工具调用】（PreToolUse 家族）。用 exit 2。"""
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    emit_json({
        "hookSpecificOutput": {
            "hookEventName": hook_event,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    })
    sys.stderr.write(reason + "\n")
    sys.stderr.flush()
    sys.exit(2)


def deny_stop(reason):
    """阻断【停止】（Stop / SubagentStop）。

    与工具事件是不同的 schema —— 这一点极易写错：
      PreToolUse 家族：{"hookSpecificOutput":{"hookEventName":..,"permissionDecision":"deny",..}}
      Stop 家族：      {"decision":"block","reason":..}

    写错的后果不是"语义不精确"，而是【门失效】：
    官方文档明确说，解析出的对象 schema 校验失败属于【非阻断错误】——
    动作照常进行，Claude 照常停止。也就是这道门看起来装了、其实没装。
    """
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    emit_json({"decision": "block", "reason": reason})
    sys.stderr.write(reason + "\n")
    sys.stderr.flush()
    sys.exit(2)


def warn_inactive(gate_name, why):
    """门未生效时，把原因说给用户听。非阻断。"""
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.stderr.write("[%s] 未生效：%s\n" % (gate_name, why))
    sys.stderr.flush()


def allow():
    sys.exit(0)


# ---------------------------------------------------------------- 状态文件

def state_dir():
    """状态目录：优先 scratchpad，否则 TEMP。不污染用户项目。"""
    d = os.environ.get("CLAUDE_BUDGET_STATE_DIR")
    if d:
        return d
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return os.path.join(base, "claude-behavior-gates")


def state_path(session_id, suffix="budget"):
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")[:120]
    return os.path.join(state_dir(), "%s.%s.json" % (sid, suffix))


def load_state(path):
    """读状态。失败返回 {} —— 但调用方必须能区分「文件不存在」和「读失败」。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_state_strict(path):
    """读状态，把「不存在」和「读失败」分开。

    为什么需要：Windows 上 os.replace 替换文件的瞬间，
    读方可能拿到一个短暂的失败（共享冲突）。若把它当成 {}，
    调用方会误判为「首次运行」→ 计数从 0 重新开始 → 配额被静默重置。
    实测：这个缺陷让 6 进程并发时约 12% 的轮次多放行 1 个。

    返回 (state, ok)：
      ok=False 表示读取失败（不是文件不存在），调用方应保守处理。
    """
    if not os.path.exists(path):
        return {}, True          # 文件不存在 = 首次，合法
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), True
    except Exception:
        return {}, False         # 文件在但读不出来 = 异常，不可当成首次


def save_state(path, data, retries=12):
    """原子写。返回 True/False —— 调用方必须能知道写没写成功。

    ⚠️ Windows 上的关键问题（实测定位）：
        os.replace(tmp, path) 在【另一个进程正打开着 path 读】时会抛
        PermissionError —— Python 的 open() 不带 FILE_SHARE_DELETE，
        Windows 不允许替换一个被打开的句柄。
        并发场景下这是常态，不是异常。

        原实现是 `except Exception: pass`，也就是**静默吞掉这次写入**。
        后果：配额递增丢失 → 下一个进程读到旧计数 → 再次放行。
        实测：6 进程并发时约 12% 的轮次多放行 1 个，
        表现为「放行了 4 次，最终计数只有 3」。

    所以这里必须：重试 + 如实返回结果。
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        return False

    for attempt in range(retries):
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            return True
        except Exception:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
            _time.sleep(0.01 * (attempt + 1))   # 退避：让读方先读完
    return False


# ------------------------------------------------ 临界区（修并发丢失更新）



class _Lock(object):
    """跨进程互斥（本地）—— 用【原子目录创建】实现。

    ⚠️ 范围声明：这是【同一主机、同一文件系统内】的互斥，不是分布式锁。
       它依赖 os.mkdir 的原子性 —— 跨机器、跨文件系统上这个前提不成立。
       同款要求：不得把本地机制说成"强排他 writer lock"。

    为什么必须有这个：官方文档明确说多个匹配的 hook 会【并行执行】。
    "读 used → used+1 → 写回" 不是原子操作。两个 Agent 调用同时触发时：
        Hook A 读到 0，Hook B 读到 0
        Hook A 写 1，Hook B 写 1
    结果放行 2 次、账上只有 1 次 —— 上限被静默绕过。
    原子替换（os.replace）只防半截文件，防不了读-改-写竞争。

    为什么不用 msvcrt.locking：实测在 ThreadPool 并发下仍会漏（放行 3/4/6 次不稳定）。
    os.mkdir 是 POSIX 和 Windows 上都保证原子的 test-and-set，语义最干脆。

    ⚠️ Windows 的坑（实测 8 进程 × 12 秒的异常分布）：
        FileExistsError:183   33,279 次  ← 一般认为只有这个
        PermissionError:5        331 次  ← 【也】会在竞争时抛！
    所以只 catch FileExistsError 是不够的。而如果又写了
        except Exception: return self
    那 331 次 PermissionError 就会掉进去，被当成"未知错误 → 无锁返回"，
    于是【临界区无锁执行】—— 读-改-写竞争，配额上限被静默绕过。
    实测：这个缺陷让"恰好放行 N 次"的用例约 10% 概率失败。

    陈旧锁：持锁进程崩溃会留下目录。超过 stale_after 秒视为陈旧并夺锁 ——
    否则一次崩溃会让后续所有派生永久卡死。

    ⚠️ timeout 必须【短于】本门被赋予的执行预算（settings 里 hook timeout = 5s）。
       实测缺陷（#17）：原来写 8.0s，比 5s 的预算还长 —— 意思是"等到超时"这条路
       永远走不到，进程会先被 harness 杀掉。于是 LOCK_TIMEOUT 那条【保守拒绝】
       分支成了死代码：不是在等锁，是在等死。改成 3.0s，给启动和收尾留余量。

    stale_after 反过来必须【长于】任何合法的持锁时长。持锁者最多活到 hook 预算
       5s，所以 15s 落在安全的一侧 —— 这个方向本来就是对的，不是不自洽。
    """

    def __init__(self, path, timeout=3.0, stale_after=15.0):
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self.held = False
        self.timed_out = False
        self.env_unavailable = False   # 环境坏了 ≠ 锁被占，两者后果不同
        self.unknown_error = False     # 未知异常 → 不确定是否持锁 → 保守拒绝
        # Gate 4：锁必须记【谁在持有】，否则分不清"持有者已死"和"持有者只是慢"。
        self.writer_id = "%d-%d-%s" % (os.getpid(), _time.time_ns(),
                                       os.urandom(4).hex())

    def _owner_path(self):
        return os.path.join(self.path, "owner")

    def still_holds(self):
        """我是否【仍然】持有这把锁（没被更晚的 epoch 夺走）。

        Gate 4 规则 3「旧 epoch 不得重新夺回主文件」在代码里的落点：
            被夺锁后如果还继续写，就会覆盖对方的更新 —— 实测复现过丢更新
            （两个进程都读到 0、都写回 1，最终计数少了 1）。
            写入前必须再确认一次"锁还是我的"。
        """
        if not self.held:
            return False
        if not os.path.isdir(self.path):
            return False        # 锁目录都没了 = 被夺锁
        try:
            with open(self._owner_path(), encoding="utf-8") as f:
                return f.read().strip() == self.writer_id
        except Exception:
            # 目录在、身份读不到 —— 无法确认持有，按"已失去"处理。
            # 不会造成永久卡死：__enter__ 保证拿到锁时身份一定写得下去
            # （写不下去就主动释放并当环境故障处理）。
            return False

    def __enter__(self):
        deadline = _time.time() + self.timeout
        warned = False
        while True:
            try:
                os.mkdir(self.path)
                # 写下持有者身份 —— 这是 fencing 的依据，也是"陈旧锁"能
                # 被区分于"慢锁"的前提。
                try:
                    with open(self._owner_path(), "w", encoding="utf-8") as f:
                        f.write(self.writer_id)
                except Exception:
                    # 身份写不下去 = 这把锁无法被 fencing 保护。
                    # 不持有它，主动释放，并当【环境故障】处理（放行 + 告警）——
                    # 绝不能让"身份写失败"变成"永久拒绝"。
                    _shutil.rmtree(self.path, ignore_errors=True)
                    self.env_unavailable = True
                    return self
                self.held = True
                return self

            except (FileExistsError, PermissionError):
                # PermissionError 在 Windows 上是【竞争】的另一种表现，不是"锁坏了"。
                # 两者必须归入同一个等待分支。
                try:
                    age = _time.time() - os.path.getmtime(self.path)
                    if age > self.stale_after:
                        _shutil.rmtree(self.path, ignore_errors=True)
                        continue
                except Exception:
                    pass
                if _time.time() >= deadline:
                    # ⚠️ 超时【不能降级为无锁执行】。
                    # 实测：无锁降级会让"恰好放行 N 次"约 10% 概率被突破
                    # （6 进程并发时，4/30 轮多放行了 1 个）。
                    # 预算是"不允许超"的语义 —— 拿不到锁时保守拒绝，
                    # 比无锁放行安全得多。调用方会把它当成"配额不可用"处理。
                    self.timed_out = True
                    return self
                _time.sleep(0.01)

            except FileNotFoundError:
                try:
                    os.makedirs(os.path.dirname(self.path), exist_ok=True)
                except Exception:
                    # ⚠️ 这里【不能】设 timed_out=True。
                    # 区别很重要：
                    #   timed_out  = 锁被别人占着（临时状态）→ 保守拒绝是对的
                    #   目录建不出来 = 环境坏了（持久状态）→ 拒绝会永久卡死用户
                    # 实测：状态目录不可写时，保守拒绝会造成「所有派生永久被拒，
                    # 而错误提示说『请稍等片刻重试』—— 重试一万次也不会好」。
                    # 预算是防浪费的，不是防用户的。环境坏掉时放行 + 告警。
                    if not warned:
                        sys.stderr.write(
                            "【G1 预算门】警告：状态目录无法创建，本门已临时停用。\n"
                            "  位置：%s\n"
                            "  影响：无法记录派生配额，本次及后续派生将被放行（不阻断）。\n"
                            "  这是【有意的降级】—— 环境故障不该把用户永久卡死。\n"
                            "  要恢复：确认该路径可写，或设置 CLAUDE_BUDGET_STATE_DIR 指向可写目录。\n"
                            % os.path.dirname(self.path))
                        warned = True
                    self.env_unavailable = True
                    return self

            except Exception as e:
                # 真未知错误（第四个互审发现）：
                # 上一版重试到超时后仍【无锁放行】—— 与"预算门不能静默失效"直接矛盾。
                # 未知异常意味着"我不知道锁到底有没有拿到"，此时放行是不可接受的。
                #
                # 但也不能像环境故障那样直接返回（那会永久卡死）。
                # 正确做法：标记为 UNKNOWN，交给调用方做【保守拒绝 + 明确报警】。
                # 区别对待的三类：
                #   锁竞争超时   → 临时，保守拒绝
                #   环境不可用   → 持久，放行 + 告警（防卡死）
                #   未知异常     → 不确定，保守拒绝 + 告警（防静默失效）
                if _time.time() >= deadline:
                    sys.stderr.write(
                        "【G1 预算门】警告：锁出现未知异常 %r。\n"
                        "  不确定是否持有锁 → 本次派生子代理将被【保守拒绝】。\n"
                        "  这不是能力限制：预算是「不允许超」的语义，\n"
                        "  状态不确定时放行等于放弃计数。\n"
                        "  若频繁出现，检查残留锁目录。\n" % (e,))
                    sys.stderr.flush()
                    self.unknown_error = True
                    return self
                _time.sleep(0.01)

    def __exit__(self, *exc):
        if self.held:
            # ⚠️ 只有【仍然是我的锁】才能删。
            #    被夺锁之后再删，删掉的是别人的锁目录 —— 那会让第三个进程
            #    直接闯进来。释放这一步同样要 fencing。
            #    另外不再用 rmtree 兜底：rmdir 只在目录确实为空时成功，
            #    这个"失败即不动"的性质本身就是一道保护。
            if self.still_holds():
                try:
                    os.remove(self._owner_path())
                except Exception:
                    pass
                try:
                    os.rmdir(self.path)
                except Exception:
                    pass
            self.held = False
        return False


def locked_update(path, fn):
    """在临界区内完成【读 → 判断 → 更新 → 写回】。

    fn(state) 返回 (new_state, result)；result 返回给调用方。

    拿不到锁时【不写】也不【假装成功】，返回 LOCK_TIMEOUT 交给调用方
    做保守决定（G1 把它当"配额不可用 → 拒绝派生"处理）。
    """
    lock = _Lock(path + ".lock")
    with lock:
        # 环境坏了（目录建不出来）≠ 锁被占。
        # 前者是持久故障，拒绝会让用户永久卡死 —— 放行 + 告警（已在 _Lock 里告警）。
        # 后者是临时竞争，保守拒绝是对的。
        if lock.env_unavailable:
            return "ENV_UNAVAILABLE"
        if lock.unknown_error and not lock.held:
            return "LOCK_UNKNOWN"
        if lock.timed_out and not lock.held:
            return "LOCK_TIMEOUT"
        st, ok = load_state_strict(path)
        if not ok:
            # 文件在但读不出来 —— 绝不能当成 {} 让计数从 0 重来
            return "STATE_UNREADABLE"
        new_st, result = fn(st)
        if new_st is not None:
            # ⚠️ 写之前再确认一次「锁还是我的」（Gate 4 fencing）。
            #    实测复现（#15）：持有者超过 stale_after 会被夺锁，
            #    若此时它还照写，就会覆盖对方的更新 ——
            #    两个进程各读到 0、各写回 1，计数静默少 1。
            #    预算是"不允许超"的语义：这种情况下必须保守拒绝，
            #    不能假装写成功。
            if not lock.still_holds():
                return "LOCK_PREEMPTED"
            if not save_state(path, new_st):
                # 状态没写下去 = 计数没递增 → 绝不能放行
                return "STATE_SAVE_FAILED"
        return result


# ---------------------------------------------------------------- 策略加载

DEFAULT_POLICY = {
    "budget": {
        "agent_spawns": 0,
        "agent_depth": 1,
        # ⚠️ 曾经在这里有 webfetch=5 / bash_rounds=40。
        # 它们【从未被执行】—— agent_spawns 有 PreToolUse 强制，
        # 而 Bash/WebFetch 没有对应的拦截器，只是被注入成提示文字。
        # 实测本会话用了 143 次 Bash（声明上限 40，超 3.6 倍）且门不拦。
        #
        # 删掉它们的教训不只是"删一个数字"：
        #   第一次我只删了 policy 文件里的键，但 load_policy() 会 _deep_merge
        #   到 DEFAULT_POLICY —— 删键 = 回退默认值，等于没删。
        #   真正移除必须【同时删掉默认值】。
        #   我又一次证明了"我执行了动作"，但没验证"它生效了"。
    },
    "effect_gate": {
        "enabled": True,
        "min_risk_level": "medium",   # low | medium | high
    },
    "destructive_gate": {
        "enabled": True,
        "deny_patterns": [],
        "warn_patterns": [],
    },
    "closeout": {"enabled": True},
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_policy():
    """策略文件查找顺序：显式环境变量 → 本 hook 自己所在目录 → 用户目录。

    ⚠️ 刻意【不查 cwd/.claude】（V2.6 修正）。
    修前顺序里第二个就是 cwd/.claude/behavior-policy.json，后果有两个：

      1. 全局安装下【读错文件】：hook 运行时 cwd 是用户的项目目录，
         于是每个项目都会用自己的策略覆盖全局策略 ——
         等于全局配置被项目目录里的残留文件悄悄改掉。
      2. 校验脚本从别处运行时读到的是【那个目录】的策略，
         而不是它要校验的那个安装 —— 出现"校验本身看错对象"。

    正确语义：策略应当跟着【hook 脚本自己】走，而不是跟着 cwd 走。
    所以要找的是本文件上级目录（hooks/ 的父目录即 .claude/）。
    """
    candidates = []
    env_p = os.environ.get("CLAUDE_BEHAVIOR_POLICY")
    if env_p:
        candidates.append(env_p)

    # 本 hook 所在的 .claude/ 目录（hooks/ 的父目录）
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), "behavior-policy.json"))

    # 用户目录（全局兜底）
    candidates.append(os.path.join(
        os.environ.get("USERPROFILE") or os.path.expanduser("~"),
        ".claude", "behavior-policy.json"))

    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                return _deep_merge(DEFAULT_POLICY, json.load(f))
        except Exception:
            continue
    return dict(DEFAULT_POLICY)


# ---------------------------------------------------------------- 预算解析

# 用户在 prompt 里显式声明预算的语法。
# 刻意不锚定行首 —— 用户会说「这个任务 agent_spawns: 2 可以派」，
# 预算声明夹在句子里是常态。锚行首会导致声明被静默忽略（实测踩过）。
BUDGET_LINE = re.compile(
    r"\b(agent_spawns|agent_depth)\s*[:=]\s*([0-9]+)",
    re.I)

# 硬上限（BUG-1 修复）。
# 这是最后一道防线：即使用户/模型在 prompt 里写天文数字，也不会突破它。
# 为什么需要：G1 的全部价值是「上限不可绕过」——
# 若上限本身可被一句 prompt 解除，那它就不是上限，只是装饰。
HARD_CAPS = {
    "agent_spawns": 20,    # 审计里极端会话用了 62 个子代理；20 已远超正常需要
    "agent_depth": 1,      # 子代理内禁止再派（设计上就该是 1）
}

# 用户口语里"允许派生"的信号
SPAWN_PERMIT = re.compile(
    r"(可以派\s*[Aa]gent|允许并行|用\s*[Aa]gent|派个子?代理|多个子\s*[Aa]gent|并行调研|allow\s+subagents)",
    re.I)

# 用户口语里"不要派生"的信号
SPAWN_FORBID = re.compile(
    r"(不要派|别派|禁止派|不用\s*[Aa]gent|不要开后台|别开后台|不要用子代理)",
    re.I)


def parse_budget_from_prompt(prompt, policy):
    """从用户 prompt 解析预算。返回 (budget_dict, source_str)。

    ⚠️ BUG-1 修复：所有预算必须有【硬上限】。
    修前：`agent_spawns: 999999999999999999999` 会被原样接受 ——
          等于用一句话把 G1 预算门整个关掉。
          G1 的全部价值是「上限不可绕过」，所以上限本身必须不可绕过。
    """
    budget = dict(policy["budget"])
    source = "policy-default"

    if prompt:
        if SPAWN_FORBID.search(prompt):
            budget["agent_spawns"] = 0
            source = "prompt:forbid"
        elif SPAWN_PERMIT.search(prompt):
            budget["agent_spawns"] = 3   # 显式许可；不是无限，仍封顶
            source = "prompt:permit"

        hits = BUDGET_LINE.findall(prompt)
        if hits:
            for key, val in hits:
                # BUG-2 修复：类型/取值校验。
                # 修前：非数字会在 int() 处抛异常被吞掉，
                #       或状态里存成字符串导致后续静默放行。
                try:
                    n = int(str(val).strip())
                except (TypeError, ValueError):
                    continue
                if n < 0:
                    continue
                cap = HARD_CAPS.get(key.lower())
                if cap is not None and n > cap:
                    n = cap          # 压到硬上限，而不是拒绝（用户意图仍被尊重）
                budget[key.lower()] = n
            source = "prompt:explicit"

    return budget, source


def safe_cap(value, key="agent_spawns"):
    """把预算上限规整成【可信的整数】。

    BUG-2 修复：状态文件可能被外部改动或序列化异常，
    出现字符串 / null / 数组 / 负数。这些情况必须【保守处理】，
    不能 `int()` 崩掉，更不能当成 0 或无穷大静默放行。

    返回 (cap_int, ok)：
      ok=False 表示取值不可信，调用方应保守拒绝（预算是"不允许超"的语义）。
    """
    if isinstance(value, bool):
        # 布尔是 int 的子类，必须最先排除（True 会被当成 1）
        return 0, False
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return 0, False
        n = int(value)
    else:
        # 字符串 / null / 数组 / 对象 —— 一律不可信。
        #
        # 为什么不接受 "5" 这种"看起来是数字"的字符串：
        #   状态文件是【机器写的】。出现字符串只有两种可能 ——
        #   序列化异常，或被人/程序改过。两种情况都不该信任。
        #   宽容地接受它 = 给"篡改状态即可绕过配额"留了路。
        #   预算是"不允许超"的语义，宁可保守拒绝。
        return 0, False

    if n < 0:
        return 0, False
    cap = HARD_CAPS.get(key)
    if cap is not None and n > cap:
        return cap, True         # 压到上限，而不是判为不可信
    return n, True
