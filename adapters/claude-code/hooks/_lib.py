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


# ---------------------------------------------------------------- 控制台编码
#
# Windows 控制台默认编码是 GBK(cp936)。门的所有告警都是中文，如果 stderr
# 没被设成 UTF-8，分两种情况：
#   1. 输出全是 GBK 不可编码的字符 → UnicodeEncodeError → 门直接崩；
#   2. 能编码 → 字节流是 GBK，而消费方（测试、CI、用户终端）按 UTF-8 读
#      → 看到【乱码告警】。
#
# 实测（#28）：budget_gate 的「状态目录无法创建」告警在 Windows 上输出为
#   `��G1 Ԥ���š�...` —— 用户看到的是乱码，等于告警没说清楚。
#   deny() / warn_inactive() 各自有 reconfigure，但 _Lock 里的两条
#   stderr.write 是裸写的，漏了。
#
# 修法：在【模块加载时】统一设置一次，而不是指望每个调用点自觉。
# 漏一处的代价就是用户看到乱码，这种事不该靠人工纪律。
def _force_utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 流不支持 reconfigure（被包装 / 老版本 Python）：跳过。
            # 本函数不能成为新的崩溃源。
            pass


_force_utf8_console()


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


def deny(reason, hook_event="PreToolUse", gate_id=None, rule_id=None,
         tool=None, session_id=None):
    """阻断【工具调用】（PreToolUse 家族）。用 exit 2。

    gate_id / rule_id 是【可选的记录参数】—— 传了就把这次 deny
    记进 gate-events.jsonl。**记录失败不影响这里的判定**（见
    record_gate_event 的 fail policy）。
    """
    if gate_id:
        record_gate_event(gate_id, rule_id or "unspecified", "deny",
                          tool=tool, session_id=session_id)
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


def deny_stop(reason, gate_id=None, rule_id=None, tool=None,
              session_id=None):
    """阻断【停止】（Stop / SubagentStop）。

    与工具事件是不同的 schema —— 这一点极易写错：
      PreToolUse 家族：{"hookSpecificOutput":{"hookEventName":..,"permissionDecision":"deny",..}}
      Stop 家族：      {"decision":"block","reason":..}

    写错的后果不是"语义不精确"，而是【门失效】：
    官方文档明确说，解析出的对象 schema 校验失败属于【非阻断错误】——
    动作照常进行，Claude 照常停止。也就是这道门看起来装了、其实没装。

    gate_id / rule_id 是【可选的记录参数】，用法同 deny()。
    """
    if gate_id:
        record_gate_event(gate_id, rule_id or "unspecified", "stop_feedback",
                          tool=tool, session_id=session_id)
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


# ---------------------------------------------------------------- Gate 事件记录
#
# 真实问题（CONFIRMED）：
#   为了分析一次真实使用里到底拦了什么，需要反向扫描约 3.5 万条消息 ——
#   因为门【不记录自己的决定】。closeout_gate 记的是「工具调用」，
#   不是「哪道门的哪条规则做了 deny」。
#   于是 G1/G3/G5/G7 的真实收益与误伤长期无法测量（NOT_MEASURED）。
#
# 这一层只做一件事：**把门已经做出的决定，用结构化方式记下来**。
#
# ⚠️ 它绝【不】参与判定。观测层不改变产品语义：
#      · 记录失败 → 仍按原判定执行（deny 还是 deny，allow 还是 allow）
#      · 记录失败 → 绝不抛异常、绝不 sys.exit
#      · 默认不记 allow（只用记「门真的出手了」的时刻）
#
# ⚠️ 边界（T1 本地观测证据，必须在代码里说清）：
#     证明  ：本机 Gate 代码记录了自己做出的决定
#     不证明：日志不可修改 / 不可伪造 / 攻击者不能删除 / 事件不可抵赖
#     不表示：事件一定代表真实世界风险（门只做字符串匹配）
#     T2 malicious-writer-proof → OUT_OF_SCOPE
#     T3 non-repudiation        → OUT_OF_SCOPE
#
# 刻意不做：hash chain / 签名 / 数据库 / attestation / 远端 telemetry。
#           本地观测不需要这些；加了就是把「看见行为」伪装成「可信审计」。

GATE_EVENTS_FILE = "gate-events.jsonl"

# 只接受明确枚举。非法值回落 UNKNOWN —— 不让脏值污染统计分母。
SOURCE_CLASSES = ("NATURAL", "CONTROLLED", "UNKNOWN")


def source_class():
    """事件来源分类。

    默认 NATURAL（真实日常开发自然发生）。
    测试 harness 可显式设 BEHAVIOR_GATE_SOURCE_CLASS=CONTROLLED。

    ⚠️ 这是【测试 harness 能力】，不是用户产品功能 —— 不写进用户文档、
       不让普通用户学这个配置。非法值一律 UNKNOWN，不猜测。
    """
    v = (os.environ.get("BEHAVIOR_GATE_SOURCE_CLASS") or "").strip().upper()
    return v if v in SOURCE_CLASSES else ("UNKNOWN" if v else "NATURAL")


def _read_version_file():
    """读同目录 VERSION。失败返回 None —— 不让身份读取影响记录。"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:
        return None


def _read_source_snapshot():
    """读部署侧清单里的 sourceSnapshot。

    ⚠️ 这是「产生这条事件的代码属于哪份源码快照」—— 没有它，
       跨多个快照累积后又会回到「这次误报究竟是哪份代码产生的」。
       读失败返回 None，事件照写（不因身份读取失败而丢事件）。
    """
    try:
        d = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(d, "DEPLOY_MANIFEST.json")
        with open(p, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("sourceSnapshot")
    except Exception:
        return None


def record_gate_event(gate_id, rule_id, decision, tool=None,
                      session_id=None, extra=None):
    """记录一次真实 Gate 干预。**永不抛异常、永不改变判定。**

    只记「门出手了」的事件（deny / block / stop_feedback），不记 allow。

    ⚠️ 绝不写入：完整 prompt / assistant 输出 / 完整 command /
       Write·Edit 正文 / MCP payload / 环境变量值 / 任何密钥。

    并发：多个匹配的 hook 会并行执行，所以 append 必须持锁。
         复用 _Lock（已处理 Windows 的 mkdir 竞态与陈旧锁）。
    """
    try:
        ev = {
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session": (session_id or "nosession")[:120],
            "gate_id": gate_id,
            "rule_id": rule_id,
            "decision": decision,
            "tool": tool,
            "source_class": source_class(),
            "version": _read_version_file(),
            "source_snapshot": _read_source_snapshot(),
        }
        if extra:
            # 只允许标量补充字段，防止有人顺手把大块正文塞进来
            for k, v in extra.items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    ev[k] = v if not isinstance(v, str) else v[:120]
        line = json.dumps(ev, ensure_ascii=False) + "\n"

        path = os.path.join(state_dir(), GATE_EVENTS_FILE)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            return False

        # ⚠️ timeout 必须极短 —— 观测【不该让门等】。
        #
        #    实测缺陷（外部复核发现，本机坐实）：
        #    这里原用 _Lock 的默认 timeout=3.0s。而 record_gate_event 是在
        #    deny() 里于【决策 JSON 发出之前】调用的 —— 于是锁被占时，
        #    一次本应立即生效的 deny 会等满 3 秒。
        #    实测：人为持锁时 deny 耗时 3.09s（无竞争时 86ms）。
        #
        #    原注释写着「拿不到锁就不记 —— 但绝不阻塞判定」，
        #    而实现用的是默认超时 —— 注释与实现不符（本项目的老毛病）。
        #
        #    修法：0.05s —— 拿不到就丢这条事件。
        #    观测数据的价值远低于「门立即生效」；丢一条事件是可接受的，
        #    延迟一次 deny 不是。
        lock = _Lock(path + ".lock", timeout=0.05)
        with lock:
            if not lock.held:
                return False        # 拿不到锁就丢事件，绝不阻塞判定
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        return True
    except Exception:
        # 观测层永远不能成为门的故障点。静默失败是有意的：
        # 这里往上抛会把一次本该干净的 deny 变成脚本崩溃。
        return False


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

            except (FileNotFoundError, NotADirectoryError):
                # ⚠️ NotADirectoryError 必须和 FileNotFoundError 归为一类（跨平台差异）：
                #   状态目录路径的父级是【文件】时（如 <某文件>/sub）：
                #     Windows → 抛 FileNotFoundError
                #     Linux/macOS → 抛 NotADirectoryError
                #   后者不是 FileNotFoundError 的子类，早先会掉进下面的
                #   except Exception 分支 → 被当成"未知异常"→ 保守拒绝
                #   → 用户被【永久卡死】，正是本分支要防的失败模式。
                #   实测：CI 在 ubuntu/macos 上 6/6 job 失败，Windows 全过。
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
        #
        # --- 3.5.3 重新引入，但换了语义 ---
        # bash_warn_at / webfetch_warn_at 是【警告阈值】，不是硬上限。
        # 为什么是警告不是拦截：
        #   拦截需要"合理阈值"，而当前【没有实测分布】作为依据。
        #   在没有数据时硬拦，会造成误伤（正常调研任务被卡死）。
        #   所以先做成可见的警告，跑一段时间拿到真实分布再决定是否硬拦。
        # 这比"只在 policy 里写个数字然后不执行"强：警告会被真的打印出来。
        "bash_warn_at": 300,
        "webfetch_warn_at": 50,
    },
    "effect_gate": {
        "enabled": True,
        # ⚠️ 曾经这里有 min_risk_level: "medium"。3.5.7 删除（#39）。
        #    它没有任何 .get() 访问 —— 改它不生效。
        #    风险分级实际由 effect_gate.py 的 REQUIRED 硬编码实现
        #    （低→L1 / 中→L2 / 高→L3），意图已达成，这个键是多余的。
    },
    "destructive_gate": {
        "enabled": True,
        # ⚠️ 曾经这里有 deny_patterns: []。3.5.7 删除（#39）。
        #    同样没有 .get() 访问。用户想加拦截规则应该用
        #    cmd_deny / content_deny（这两个是真的被读的，见
        #    destructive_gate.py:117-118）—— deny_patterns 是旧设计的残留。
        #    warn_patterns 保留：它在 destructive_gate.py:119 被真的读取。
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

    # ⚠️ 陈旧副本检测（3.5.3 新增，修 #29）
    #
    # 实测踩到：开发机上 ~/.claude/behavior-policy.json 留着 9 月 13 日
    # 安装时的旧副本（_version 2.5.0，closeout.enabled=false）。
    # 它排在候选列表里且【优先命中】，于是改了包里那份 policy 后：
    #   · 门读到的仍是旧副本 → 修改完全不生效
    #   · 而且没有任何提示 —— 用户以为改了，实际没改
    #
    # 这和项目定义的「最严重缺陷」是同一类：看起来改了、实际没生效。
    #
    # 修法不是改查找顺序（顺序本身是对的：包内优先于全局），
    # 而是【让覆盖可见】：命中的文件若版本低于本 hook 的 VERSION，
    # 打一行警告说明"你改的不是正在生效的那份"。
    chosen = None
    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                chosen = (p, json.load(f))
            break
        except Exception:
            continue

    if chosen is None:
        return dict(DEFAULT_POLICY)

    p, raw = chosen
    _warn_if_stale(p, raw)
    return _deep_merge(DEFAULT_POLICY, raw)


def _warn_if_stale(policy_path, raw):
    """命中的 policy 版本低于本 hook 的 VERSION 时，打一行可见警告。

    只警告，不改变行为 —— 因为这个判定本身可能误报
    （用户可能有意固定使用某个旧版本策略）。

    ⚠️ 每个 (生效文件, 版本) 组合只提示【一次】（3.5.5 修）。
       修前：每次 hook 调用都打印 —— 一个会话几十次工具调用会刷屏，
       用户被淹没之后反而看不见任何提示，等于没提示。
       用状态目录下的标记文件去重，跨进程有效。
    """
    try:
        got = str(raw.get("_version") or "").strip()
        here = os.path.dirname(os.path.abspath(__file__))
        want = open(os.path.join(here, "VERSION"), encoding="utf-8").read().strip()
        if not (got and want and got != want):
            return

        # 只在【全局兜底副本】上警告 —— 包内副本版本不符是打包错误，
        # 那种情况由 verify_deploy 负责，不在这里重复报。
        home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        in_home = os.path.abspath(policy_path).startswith(
            os.path.abspath(os.path.join(home, ".claude")))
        if not in_home:
            return

        # 去重标记：同一份陈旧策略只提示一次
        import hashlib
        tag = hashlib.sha256(
            ("%s|%s" % (policy_path, got)).encode("utf-8")).hexdigest()[:16]
        marker = os.path.join(state_dir(), ".warned-stale-%s" % tag)
        if os.path.exists(marker):
            return

        try:
            os.makedirs(state_dir(), exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                f.write(want)
        except Exception:
            # 标记写不下去也要提示 —— 宁可重复一次，不可静默
            pass

        sys.stderr.write(
            "【配置陈旧】正在生效的策略来自全局副本，版本 %s，"
            "而当前 hooks 是 %s。\n"
            "  生效文件：%s\n"
            "  影响：你对包内 policy/behavior-policy.json 的修改【不会生效】，"
            "因为全局副本优先级更高。\n"
            "  要使用新策略：删除或更新上面那个文件。\n"
            "  （此提示每个会话只出现一次）\n"
            % (got, want, policy_path))
        sys.stderr.flush()
    except Exception:
        # 本函数是提示性的，绝不能成为新的崩溃源
        pass


# ---------------------------------------------------------------- 预算解析

# 用户在 prompt 里显式声明预算的语法。
# 刻意不锚定行首 —— 用户会说「这个任务 agent_spawns: 2 可以派」，
# 预算声明夹在句子里是常态。锚行首会导致声明被静默忽略（实测踩过）。
BUDGET_LINE = re.compile(
    r"\b(agent_spawns|agent_depth)\s*[:=]\s*([0-9]+)",
    re.I)


def strip_referenced_text(text):
    """剔除【被引用/被讨论】的文本，只留【对 AI 说的话】。

    ⚠️ 为什么需要（3.5.10 新增，见 #47）：
        实测缺陷：用户在 prompt 里**引用别人的文字**时，其中的示例
        会被当成真实预算声明。四个场景全部误判：

            真实声明「agent_spawns: 3」        → 3  ✓（唯一正确）
            引用朋友点评「…agent_spawns: 3」   → 3  ✗ 不该命中
            ```代码块里写 agent_spawns: 5```   → 5  ✗ 不该命中
            「这个 agent_spawns: 0 太死板了」  → 0  ✗ 不该命中

        本会话真实发生过：用户贴了一份长篇点评，里面多处出现
        `agent_spawns = 0` / `agent_spawns: 3` 作为**论述对象**，
        结果门认为"用户显式声明了预算 3"。

    这是 use-mention 问题的又一实例 —— 与 G3 的同类问题同源。
    G3 早就修了（effect_gate._strip_quoted_context），
    G1 一直没修。**同一条规则只在一侧生效**，正是本项目反复出现的形状。
    所以这次提到 _lib 共享，而不是再抄一份。

    剔除范围（与 G3 保持一致）：
        · ``` 围栏代码块
        · `行内代码`
        · "双引号" / “中文双引号”
        · 「」『』中文引号
        · > 块引用行

    刻意【不】剔除单引号 —— 它太常见（it's / 变量名），会误伤正文。
    """
    if not text:
        return ""
    t = text
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`[^`\n]*`", " ", t)
    t = re.sub(r"\"[^\"\n]{0,200}\"", " ", t)
    t = re.sub(r"[“”][^“”\n]{0,200}[“”]", " ", t)
    t = re.sub(r"[「『][^」』\n]{0,200}[」』]", " ", t)
    t = re.sub(r"^[ \t]*>.*$", " ", t, flags=re.M)
    return t


# 声明前的「讨论/否定」语境词（#47 第二道防线）。
#
# ⚠️ 这道防线【不完整】—— 必须说清它的边界：
#    上面 strip_referenced_text() 只对【带标记】的引用有效
#    （代码块 / 引号 / 块引用）。而用户经常贴【裸文本】的长篇点评，
#    里面 `agent_spawns: 3` 没有任何标记 —— 剥离函数无从下手。
#
#    实测最接近真实场景的一条：
#        「朋友说：我不会支持 agent_spawns = 0，还提到 agent_spawns: 3」
#    剥离后仍是原文 → 被当成真实声明。
#
#    第二道防线：看声明【前面 25 字内】有没有讨论/否定语境词。
#    实测能挡住上面那条，但挡不住：
#        「agent_spawns: 0 这个默认值是不是太死板了」← 语境词在后面
#
#    所以最终结论必须诚实：
#      · 带标记的引用     → 可靠剔除
#      · 裸文本引用       → 大幅减少误判，但【不能根除】
#      · 根本方案是要求用户把引用放进代码块（README 已说明）
#    这是机械匹配的固有上限，不假装解决。
DISCUSSION_CTX = re.compile(
    r"(不会支持|不支持|不要|别用|反对|讨论|提到|举例|例如|比如|所谓|"
    r"是不是|太死板|建议|点评|报告|文档|说明|参考|quote|quoted|"
    r"not\s+support|don'?t|does\s+not)", re.I)


def _drop_discussed_hits(text, rx):
    """剔除【前面紧邻讨论语境】的匹配。返回剩下的匹配列表。

    text 必须已经过 strip_referenced_text()。
    """
    out = []
    for m in rx.finditer(text):
        before = text[max(0, m.start() - 25):m.start()]
        if DISCUSSION_CTX.search(before):
            continue
        out.append(m.group(1, 2))
    return out

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
        # ⚠️ 只在【用户真正说的】文本上判定，剔除引用/代码块/讨论（#47）。
        #    修前是对整个 prompt 做 matching —— 用户贴一篇提到
        #    `agent_spawns: 3` 的文章，就会被当成"用户要 3 个"。
        said = strip_referenced_text(prompt)

        # 口语信号同样要过讨论语境（#47 同类）。
        # 否则「他在质疑『不要开后台』这条规则」会被当成用户禁止后台。
        _forbid = [m.group(0) for m in SPAWN_FORBID.finditer(said)
                   if not DISCUSSION_CTX.search(said[max(0, m.start()-25):m.start()])]
        _permit = [m.group(0) for m in SPAWN_PERMIT.finditer(said)
                   if not DISCUSSION_CTX.search(said[max(0, m.start()-25):m.start()])]

        if _forbid:
            budget["agent_spawns"] = 0
            source = "prompt:forbid"
        elif _permit:
            budget["agent_spawns"] = 3   # 显式许可；不是无限，仍封顶
            source = "prompt:permit"

        # 第二道防线：剔除紧邻讨论语境的匹配（#47）。
        # findall 换成 _drop_discussed_hits，语义等价但会跳过"被讨论的"。
        hits = _drop_discussed_hits(said, BUDGET_LINE)
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
