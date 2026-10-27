# -*- coding: utf-8 -*-
"""只读结果采集（3.5.19，修正方案第 3 项）。

用途：让 G3 的「绿灯」能落在【已经发生的工具结果】上，而不是只看
模型自己写的「**证据**：L3」标签。

    「现有 G3 不能校验证据真伪，L3 是模型填的标签。
      匹配工具产生的结果、版本和验证范围；没有结果时标『未验证』。
      只读取已发生的验证，不主动要求额外测试。」
                                        —— 修正方案 第 3 项

能做 / 不能做（必须分清，否则就是新的假能力）：

  能做：读 transcript，确认【本会话确实产生过工具结果】，并抽取其中
        可核对的观测（HTTP 状态码、退出码、监听端口），给出数量与摘要；
        同时标出每条结果【之后是否还有写操作】（after_write）——
        结果被后续修改覆盖时，不能再用它支撑「当前状态已验证」。
  不能做：判断结果是否真的来自那次执行、是否对应这次声明、是否覆盖
        用户路径。门只能证明「有结果可核对」，不能证明「结论为真」。
        证据块本身仍然是自报等级 —— 这里做的是【降低自报的权重】，
        不是把它升级成事实。

⚠️ 3.5.19 修补（只读核对后）：
  「results 非空」只说明会话里有工具返回，不说明它支持【这条】声明。
  本模块新增 obs_tokens()，把观测归一成可比对的 token
  （http:200 / exit:0 / listen::3000），由调用方判断
  「发现观测」与「支持具体声明」是否同一件事 —— 这一步不属于本模块。

隐私/体积：
  · 只读 transcript 尾部定长字节（默认 512KB），O(行数)。
  · 不保存输出正文，只保存结果摘要哈希 + 至多 4 个短观测串。
  · 永不写入 prompt / 命令正文 / 文件内容。
  · 扫描有界：尾部定长 + 最多 800 行 + 最多 40 条结果。
    读完仍有更早内容时 truncated=True —— 调用方必须当成「未确认」，
    不能当成「没有证据」。
"""

import hashlib
import json
import os
import re

# 工具结果里可核对的观测。刻意保守：宁可少认，不要凭空给出"已验证"。
_SIGNAL_RX = re.compile(
    r"(HTTP/\d(?:\.\d)?\s+[1-5]\d\d"
    r"|\b(?:status|状态码)\s*[:=]?\s*[1-5]\d\d"
    r"|\b(?:exit|Exit)\s*(?:code|status)?\s*[:=]?\s*\d+\b"
    r"|(?:退出码|返回码)\s*[:：=是为]?\s*\d+"
    r"|\blistening on\s+\S+"
    r"|\bEADDRINUSE\b)")

# 写操作类工具。结果之后出现它们 = 那条结果的"当前状态"可能已过期。
# 只用于标记，不用于拦截。
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# Bash 里【明确的】写入形态。只认重定向、tee、sed -i、truncate，
# 不把任何 Bash 命令都当写入 —— 大部分命令是读或执行。
_BASH_REDIR_RX = re.compile(r"(?:^|[^<>&|])(?:>>?)\s*([^\s;&|<>]+)")
_BASH_TEE_RX = re.compile(r"\btee\s+(?:-a\s+)?([^\s;&|]+)")
_BASH_SED_I_RX = re.compile(r"\bsed\s+-i")
_BASH_TRUNC_RX = re.compile(r"\btruncate\s+-s\s+\S+\s+([^\s;&|]+)")

# 这些目标不构成"修改了工作区对象"
_IGNORE_TARGETS = frozenset({
    "/dev/null", "/dev/stdout", "/dev/stderr", "nul", "nul:",
})


def _short_target(p):
    """把写目标压成可比的短串（最多两段，80 字）。

    只用于「结果之后有没有改到同一对象」的粗比对，不落完整路径。
    """
    p = (p or "").strip("'\"")
    if not p or p.lower() in _IGNORE_TARGETS:
        return None
    parts = [x for x in re.split(r"[\\/]+", p) if x]
    if not parts:
        return None
    return "/".join(parts[-2:])[:80]


def _bash_write_targets(cmd):
    """Bash 命令里可可靠识别的写入目标。识别不出 → 空列表（不是"没有写入"）。"""
    out = []
    for m in _BASH_REDIR_RX.finditer(cmd or ""):
        out.append(m.group(1))
    for m in _BASH_TEE_RX.finditer(cmd or ""):
        out.append(m.group(1))
    for m in _BASH_TRUNC_RX.finditer(cmd or ""):
        out.append(m.group(1))
    if _BASH_SED_I_RX.search(cmd or ""):
        toks = [t for t in re.split(r"[\s;&|]+", cmd or "") if t]
        if toks:
            out.append(toks[-1])
    return out


def _write_targets(name, tool_input):
    """这次工具调用【请求】写哪些对象。空列表 = 没识别到写形态。"""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if name in _WRITE_TOOLS:
        p = ti.get("file_path") or ti.get("notebook_path") or ""
        t = _short_target(p)
        return [t or "<file>"]
    if name == "Bash":
        out = []
        for p in _bash_write_targets(str(ti.get("command") or "")):
            t = _short_target(p)
            if t and t not in out:
                out.append(t)
        return out[:4]
    return []

# 观测归一化：把「HTTP 200」「status: 200」「都是 200」变成同一个 token。
# 刻意只认这三类可机器比对的观测；其余文本一律不猜。
_OBS_HTTP_RX = re.compile(r"\b([1-5]\d\d)\b")
_OBS_EXIT_RX = re.compile(
    r"(?:\bexit\s*(?:code|status)?|退出码|返回码)\s*"
    r"[:：=是为]?\s*(\d+)\b", re.I)
_OBS_LISTEN_RX = re.compile(r"listening on\s+(\S+)", re.I)


def obs_tokens(text):
    """把一段文本里的可核对观测归一成 token 集合。

    用于回答「声明里的 200 与结果里的 200 是不是同一件事」。
    ⚠️ 这不是语义理解：只比对字面观测，不判断对象/操作是否一致。
       调用方必须在结论里保留这一层边界。
    """
    s = text or ""
    out = set()
    for m in _OBS_HTTP_RX.finditer(s):
        # 只有出现在 HTTP/status/状态码/返回/响应/都是 等上下文里的三位数
        # 才算状态码 —— 否则时间、行号、端口都会被误认。
        head = s[max(0, m.start() - 12):m.start()].lower()
        if any(k in head for k in ("http", "status", "状态码", "返回", "响应",
                                   "都是", "均为", "全是", "全为", "都为")):
            out.add("http:" + m.group(1))
    for m in _OBS_EXIT_RX.finditer(s):
        out.add("exit:" + m.group(1))
    for m in _OBS_LISTEN_RX.finditer(s):
        out.add("listen:" + m.group(1).strip(".,;:，。；：\"'"))
    return out


def _blocks(entry):
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    content = msg.get("content")
    return content if isinstance(content, list) else []


def _result_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(str(b.get("text") or ""))
        return "\n".join(out)
    if isinstance(c, dict):
        return str(c.get("text") or "")
    return ""


def scan_session(transcript_path, max_bytes=524288, max_lines=800,
                 max_results=40):
    """扫描 transcript，返回本会话已发生的工具结果摘要。

    返回字典：
      ok        True = 读到了且可判断；False = 读不到（文件缺失/无路径/解析异常）
      truncated True = 只读了尾部，更早的结果未纳入（因此"0 条结果"不能当成
                       "从未发生过"，调用方必须区别对待）
      results   [{"tool_use_id","tool","is_error","signals":[...],
                  "chars":N,"digest":"…",
                  "after_write":bool,      # 该结果之后是否有写（任一状态）
                  "after_write_confirmed":bool,
                  "after_write_possible":bool,
                  "writes_after":[{"targets":[短串],
                                   "state":"confirmed|possible|pending"}]}]
      errors    失败结果数

    ⚠️ 写操作只在【tool_result 存在且成功】时才算发生；
       「请求过写入」不等于「写入已经成功」。
       Bash 只认重定向 / tee / sed -i / truncate 等明确形态，
       不把任意 Bash 命令当写入。
    """
    if not transcript_path:
        return {"ok": False, "why": "no transcript_path", "results": [],
                "truncated": False, "errors": 0}
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except Exception as e:
        return {"ok": False, "why": "read failed: %r" % (e,), "results": [],
                "truncated": False, "errors": 0}

    truncated = size > max_bytes
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]            # 首个残行丢掉
    if len(lines) > max_lines:
        # ⚠️ 3.5.19 修补：行数截断也必须记进 truncated。
        #    修前这里静默丢弃更早的行，调用方却以为「整个会话都看过了」——
        #    于是「没找到结果」可能被误当成「没有结果」。
        truncated = True
        lines = lines[-max_lines:]

    tool_names = {}
    # 按出现顺序同时收集「工具结果」与「写操作」。
    # ⚠️ 3.5.19 修补：写操作有三种状态，不能只有"成功"和"没发生"：
    #   confirmed —— tool_result 存在且成功
    #   possible  —— tool_result 存在但失败（命令可能已改了文件才失败）
    #   pending   —— 有写请求但没有 tool_result（无法确认是否执行）
    # 「请求过写入」仍不等于「写入已经成功」，但也不能直接排除。
    seq = []
    write_idx = {}               # tool_use_id -> seq 下标
    for ln in lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        for b in _blocks(d):
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "tool_use":
                tid = b.get("id")
                name = b.get("name") or "?"
                if tid:
                    tool_names[tid] = name
                    _tg = _write_targets(name, b.get("input") or {})
                    if _tg:
                        write_idx[tid] = len(seq)
                        seq.append(("write", {"targets": _tg,
                                              "state": "pending"}))
            elif t == "tool_result":
                tid = b.get("tool_use_id") or ""
                out = _result_text(b)
                is_err = bool(b.get("is_error"))
                sigs = []
                for m in _SIGNAL_RX.finditer(out):
                    s = re.sub(r"\s+", " ", m.group(0)).strip()[:60]
                    if s not in sigs:
                        sigs.append(s)
                    if len(sigs) >= 4:
                        break
                # 更新这条写操作的状态。位置在 tool_use 处，不在这条
                # result 之后 —— 反向标记时不会把它算成自己的后续修改。
                # ⚠️ 失败不等于没写入：命令可能改完文件才返回非零，
                #    所以 command 失败记 possible，而不是丢弃。
                if tid in write_idx:
                    seq[write_idx[tid]][1]["state"] = (
                        "possible" if is_err else "confirmed")
                seq.append(("result", {
                    "tool_use_id": tid,
                    "tool": tool_names.get(tid, "?"),
                    "is_error": is_err,
                    "signals": sigs,
                    "chars": len(out),
                    "digest": hashlib.sha256(
                        out.encode("utf-8", "replace")).hexdigest()[:12],
                }))
        if len(seq) > max_lines * 4:
            truncated = True     # 提前停止 = 扫描不完整，必须如实记录
            break

    # 反向标记：该结果【之后】有哪些写操作，以及它们的确定程度。
    # ⚠️ 有写操作 ≠ 结果无效 —— 是否能判过期取决于写目标是否与声明相关，
    #    这一步交给 G3；这里只如实记录目标与状态，不替它下结论。
    later = []
    for kind, payload in reversed(seq):
        if kind == "write":
            later = ([dict(payload)] + later)[:6]
        else:
            payload["writes_after"] = [dict(w) for w in later]
            payload["after_write"] = bool(later)
            payload["after_write_confirmed"] = any(
                w.get("state") == "confirmed" for w in later)
            payload["after_write_possible"] = any(
                w.get("state") in ("possible", "pending") for w in later)

    results = [p for k, p in seq if k == "result"]
    if len(results) > max_results:
        truncated = True
        results = results[-max_results:]
    errors = sum(1 for r in results if r.get("is_error"))

    return {"ok": True, "results": results, "truncated": truncated,
            "errors": errors, "size": size}


def summarize(scan):
    """把 scan 结果压成给 G3 用的一行事实描述（不含正文）。"""
    if not scan.get("ok"):
        return "未读到 transcript（%s）" % (scan.get("why") or "unknown")
    rs = scan.get("results") or []
    if not rs:
        return "本会话%s没有工具结果" % ("尾部可见范围内" if scan.get("truncated")
                                        else "")
    concrete = [r for r in rs if r.get("signals")]
    ok_runs = [r for r in rs if not r.get("is_error")]
    stale = [r for r in concrete if r.get("after_write")]
    extra = ("，其中 %d 条之后还有写操作（已确认或可能）" % len(stale)
             if stale else "")
    return ("工具结果 %d 条（成功 %d / 失败 %d），其中 %d 条含可核对观测%s"
            % (len(rs), len(ok_runs), int(scan.get("errors") or 0),
               len(concrete), extra))
