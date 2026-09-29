# -*- coding: utf-8 -*-
"""语义评审的 输入合同 / 时间预算 / 失败规则 回归测试（本地候选 3.6.1）

全部【离线】：用本地假模型服务代替真实通道 ——
不需要网络、不需要凭据，且能精确控制延迟与响应内容。
用户要求：「使用可控慢响应测试验证超时、降级及状态落盘」。

覆盖三组用户点名的缺陷：
  1. 输入合同：不得静默截断；长输入保留关键要求；截断须显式标注
  2. 时间预算：单次请求 < 整次事件总预算 < 宿主 hook timeout
  3. 失败规则：保留既有约束；不用有已知误判的词表新增禁令；
               「用户关闭」与「调用故障」分开

跑法：python tests/semantic_contract_test.py
"""
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
HOOKS = os.path.join(PKG, "adapters", "claude-code", "hooks")
sys.path.insert(0, HOOKS)

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail.replace("\n", " | ")[:300])


# ---------------------------------------------------------------- 假模型服务

class _FakeModel(object):
    """可控假模型：可设延迟、可设回复文本、记录收到的请求。"""

    def __init__(self):
        self.delay = 0.0
        self.reply = '{"ok": true}'
        self.requests = []
        self.model_name = "fake-model-1"
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n)
                try:
                    outer.requests.append(json.loads(raw.decode("utf-8")))
                except Exception:
                    outer.requests.append({"__unparsed__": True})
                if outer.delay:
                    time.sleep(outer.delay)
                text = outer.reply
                # 支持把「收到的用户消息」编码进 reason，供输入合同测试取证
                if text == "__ECHO__" and outer.requests:
                    msg = ""
                    try:
                        msg = outer.requests[-1]["messages"][0]["content"]
                    except Exception:
                        pass
                    # ⚠️ 检测串必须用【节选标记独有】的片段。
                    #    上一版用「此处省略」，而提示词模板里本来就写着
                    #    「如果原话带…（此处省略 N 字…）…标注」——
                    #    于是短输入也被判成"有省略"，是测试自己的缺陷。
                    text = json.dumps({
                        "verify": "none", "ask": "none",
                        "claims": [], "real_claim": False, "uncertain": False,
                        "reason": "len=%d has_tail=%s has_mark=%s"
                                  % (len(msg), "尾部指令XYZ" in msg, "原文共" in msg),
                    }, ensure_ascii=False)
                body = json.dumps({
                    "id": "fake", "type": "message", "role": "assistant",
                    "model": outer.model_name,
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    # 客户端按预算超时后会主动断开 —— 那是【预期行为】，
                    # 不是测试失败，不该打印堆栈吓人。
                    pass

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def base_url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass


def use_fake(fake, **policy_over):
    """把评审通道指向假模型（覆盖环境变量）。"""
    os.environ["ANTHROPIC_BASE_URL"] = fake.base_url()
    os.environ["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = fake.model_name
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("ANTHROPIC_MODEL", None)


def policy_with(**kw):
    p = {"semantic_review": {"enabled": True}}
    p["semantic_review"].update(kw)
    return p


# ---------------------------------------------------------------- 主流程

def main():
    fake = _FakeModel()
    use_fake(fake)
    import review_unit
    from intent_gate import decide_intent

    try:
        print("\n===== A 输入合同（长输入不得静默截断）=====")
        fake.delay = 0.0
        fake.reply = "__ECHO__"

        short = "这次不用测试了。"
        r = review_unit.review_intent(short, policy=policy_with(), session_id="c-a1")
        check("A1 短输入：原样送入，无省略标注",
              r.get("ok") and "has_mark=False" in r.get("reason", ""),
              r.get("reason", ""))

        # 4000 字无关材料 + 尾部关键指令
        long_prompt = ("这是一段与判断无关的长材料。" * 400) + "\n最后补一句：尾部指令XYZ 不要测试。"
        r = review_unit.review_intent(long_prompt, policy=policy_with(), session_id="c-a2")
        # ⚠️ 本地候选 3.6.2：超长输入的行为按用户要求改过。
        #   旧行为：保头尾 + 标注省略后【仍送模型】，靠模型自报 uncertain 兜底。
        #   用户指出：即使模型返回 uncertain=false，也不能证明省略部分不影响结论 ——
        #   它只看到片段，本来就没依据知道缺了什么。
        #   新行为：由【程序】判定 —— 过长就【不调用模型】，直接标记信息不足。
        det = r.get("detail") or {}
        check("A2 超长输入：程序标记信息不足，不据片段做判断",
              r.get("verdict") == "insufficient" and det.get("elided") is True,
              json.dumps(det, ensure_ascii=False))
        check("A3 超长输入：给出明确的跳过原因（供状态与卡片呈现），不是静默",
              (r.get("meta") or {}).get("skip_reason") == "too_long",
              str((r.get("meta") or {}).get("skip_reason")))

        # 边界：正好不超限
        just = "x" * 3990
        r = review_unit.review_intent(just, policy=policy_with(), session_id="c-a3")
        check("A4 长度未超限：不做节选",
              r.get("ok") and "has_mark=False" in r.get("reason", ""),
              r.get("reason", ""))

        print("\n===== B 时间预算（可控慢响应）=====")
        # 单次请求超时 → 必须返回 insufficient 且不抛异常
        fake.delay = 3.0
        t0 = time.time()
        r = review_unit.review_intent("这次不用测试了。", policy=policy_with(timeout_s=1),
                                      session_id="c-b1")
        dt = time.time() - t0
        check("B1 慢响应超过单次预算 → 未裁决（不伪造结论）",
              r.get("ok") is False and r.get("verdict") == "insufficient",
              json.dumps(r, ensure_ascii=False)[:200])
        check("B2 超时确实在预算附近触发（没有无限等待）", dt < 3.0, "耗时 %.2fs" % dt)
        check("B3 超时是【真实故障】而非空结论", bool(r.get("meta", {}).get("error")),
              str(r.get("meta", {}).get("error"))[:120])

        # 总预算 > 单次预算：多行命中时不得累计超过总预算
        fake.delay = 0.0
        from effect_gate import _review_deadline
        d = _review_deadline(policy_with(total_budget_s=2))
        check("B4 总预算可配置且被读取", d - time.time() <= 2.0, "剩余 %.2fs" % (d - time.time()))

        # 宿主 timeout 必须大于总预算（静态核对注册文件）
        frag = json.load(open(os.path.join(PKG, "adapters", "claude-code",
                                           "settings.fragment.json"), encoding="utf-8"))
        stop_hooks = frag["hooks"]["Stop"][0]["hooks"]
        stop_timeout = stop_hooks[0].get("timeout", 0)
        pol = json.load(open(os.path.join(PKG, "policy", "behavior-policy.json"), encoding="utf-8"))
        total_budget = (pol.get("semantic_review") or {}).get("total_budget_s")
        check("B5 宿主 Stop timeout > 策略总预算（三层预算不打架）",
              stop_timeout and total_budget and stop_timeout > total_budget,
              "stop_timeout=%s total_budget_s=%s" % (stop_timeout, total_budget))

        # ⚠️ 本地候选 3.6.0 在这里真实翻过车：往 ci.yml 插新 step 时用了 4 空格缩进，
        #    而其余 step 都是 6 空格 —— YAML 结构实际已被破坏，CI 会失效，
        #    但没有任何测试发现（7 个文件里只有这一个有该结构）。
        #    加这条回归：所有 `- name:` step 必须在同一缩进层级。
        ci_text = open(os.path.join(PKG, ".github", "workflows", "ci.yml"),
                       encoding="utf-8").read()
        levels = set()
        for ln in ci_text.split("\n"):
            m = re.match(r"^(\s*)- name:", ln)
            if m:
                levels.add(len(m.group(1)))
        check("B6 ci.yml 所有 step 缩进一致（本地候选 3.6.0 曾在此引入结构错误）",
              len(levels) == 1, "发现缩进层级: %s" % sorted(levels))

        print("\n===== C 失败规则（关闭 vs 故障分开）=====")
        prev = {"intent_no_verify": True, "intent_verify_excerpt": "不用测试了"}

        # 故障：不得用词表新增禁令
        fake.delay = 3.0
        bad = review_unit.review_intent("不要测试。", policy=policy_with(timeout_s=1),
                                        session_id="c-c1")
        s = decide_intent("不要测试。", {}, bad)
        check("C1 调用故障：不用词表新增禁令（旧实现会设成 True）",
              s["intent_no_verify"] is False, json.dumps(s, ensure_ascii=False))
        check("C2 故障路径可识别（source=hold-on-failure）",
              s["intent_source"] == "hold-on-failure", str(s["intent_source"]))

        s = decide_intent("不要测试。", prev, bad)
        check("C3 故障时保留此前已建立的约束",
              s["intent_no_verify"] is True, json.dumps(s, ensure_ascii=False))

        # 用户主动关闭：回到词表行为（这是用户选的路径）
        fake.delay = 0.0
        off = {"ok": False, "verdict": "insufficient", "detail": {},
               "reason": "policy.semantic_review.enabled = false（用户已关闭）", "meta": {}}
        s = decide_intent("不要测试。", {}, off)
        check("C4 用户关闭：回到词表行为（与故障路径不同）",
              s["intent_no_verify"] is True
              and s["intent_source"] == "regex-fallback(disabled)",
              json.dumps(s, ensure_ascii=False))

        print("\n===== D 结论必须基于完整判断（置信合同）=====")
        fake.delay = 0.0
        # 模型自报 uncertain → 不得升级为全局禁令
        fake.reply = json.dumps({"ask": "none", "verify": "forbid",
                                 "verify_evidence": "不要测试", "uncertain": True,
                                 "reason": "依赖被省略内容"}, ensure_ascii=False)
        r = review_unit.review_intent("不要测试。", policy=policy_with(), session_id="c-d1")
        check("D1 模型报 uncertain → 禁令降级为 none（不升级为全局结论）",
              r.get("ok") and (r.get("detail") or {}).get("verify") == "none",
              json.dumps(r.get("detail"), ensure_ascii=False))
        s = decide_intent("不要测试。", {}, r)
        check("D2 uncertain 结论不改变状态",
              s["intent_no_verify"] is False, json.dumps(s, ensure_ascii=False))

        print("\n===== E 审计新增（2026-09-29）：G3 入口 1（引用剔除）离线覆盖 =====")
        # 背景（ce-code-review 的 testing reviewer #11，独立验证批 confirmed）：
        # 入口 1（_semantic_filter_references）是语义功能的主体（把含完成词的
        # 行交评审判「引用 vs 真声明」），但剔除逻辑与预算耗尽留痕此前【零覆盖】
        # —— 唯一走它的 A 组只验证「评审不可用时行为不变」。本组用同一假模型
        # 直调该函数（响应格式：{"real_claim": true/false}）。
        import effect_gate as _EG
        fake.delay = 0.0
        _msg = '他说"任务已完成"只是引用。\n本轮没有别的动作。'

        # E1 判 allow（引用）→ 该行被剔除 + 留痕
        fake.reply = json.dumps({"real_claim": False, "reason": "引用，不是本回合声明"},
                                ensure_ascii=False)
        out_text, note = _EG._semantic_filter_references(
            _msg, policy_with(), "c-e1", deadline=time.time() + 10)
        check("E1 评审 allow → 引用行被剔除且留痕",
              "已完成" not in out_text and bool(note) and "剔除" in note,
              "note=%s out=%s" % (note, out_text[:60]))

        # E2 判 block（真声明）→ 不剔除，行保留给定级判定
        fake.reply = json.dumps({"real_claim": True, "reason": "真完成声明"},
                                ensure_ascii=False)
        out_text, note = _EG._semantic_filter_references(
            _msg, policy_with(), "c-e2", deadline=time.time() + 10)
        check("E2 评审 block → 行保留（门继续判证据/级别）",
              "已完成" in out_text, "out=%s" % out_text[:60])

        # E3 总预算已耗尽 → 不做评审、不剔除、且留痕（不得静默）
        fake.reply = json.dumps({"real_claim": False}, ensure_ascii=False)
        out_text, note = _EG._semantic_filter_references(
            _msg, policy_with(), "c-e3", deadline=time.time() - 5)
        check("E3 总预算耗尽 → 不剔除且明说『未做归属判断』（留痕）",
              "已完成" in out_text and bool(note) and "未做归属判断" in note,
              "note=%s" % note)

        # E4 类型坏值的 policy → 单次预算回落 15.0，不抛异常（同 C6 防御口径）
        try:
            out_text, note = _EG._semantic_filter_references(
                _msg, {"semantic_review": {"timeout_s": "15s"}}, "c-e4",
                deadline=time.time() + 10)
            ok_e4 = True
        except Exception as e:
            ok_e4 = False
            note = repr(e)
        check("E4 policy.timeout_s 坏值 → 不抛（回落默认）", ok_e4, str(note)[:80])

        # 对照：uncertain=false 时必须正常生效
        fake.reply = json.dumps({"ask": "none", "verify": "forbid",
                                 "verify_evidence": "不要测试", "uncertain": False,
                                 "reason": "明确禁令"}, ensure_ascii=False)
        r = review_unit.review_intent("不要测试。", policy=policy_with(), session_id="c-d2")
        s = decide_intent("不要测试。", {}, r)
        check("D3 对照：uncertain=false 时禁令正常生效",
              s["intent_no_verify"] is True, json.dumps(s, ensure_ascii=False))

        print("\n===== E 整段式入口（未见表达必须能进模型）=====")
        # 词表对「这件事办妥了，你直接用就行」一个字都不命中 ——
        # 修复前该句永远进不了模型。
        unseen = "这件事办妥了，你直接用就行"
        # ⚠️ 不要在函数内 import re —— 那会让整个 main() 的 re 变成局部变量，
        #    函数里更早用到 re 的地方（B6）会抛 UnboundLocalError。模块级已导入。
        from effect_gate import CLAIM_RX
        check("E1 该句确实不被词表覆盖（前提成立）",
              not CLAIM_RX.search(unseen), "词表竟然命中了，用例失去意义")

        fake.reply = json.dumps({"claims": [unseen], "uncertain": False,
                                 "reason": "宣称办妥"}, ensure_ascii=False)
        r = review_unit.review_claims(unseen, "AI 助手收尾陈述",
                                      policy=policy_with(), session_id="c-e1")
        check("E2 整段式：未见表达被识别为完成声明",
              r.get("ok") and r.get("verdict") == "block"
              and (r.get("detail") or {}).get("claims"),
              json.dumps(r.get("detail"), ensure_ascii=False))

        # E3 模型自报 uncertain=true（输入本身没超长）→ 不据此新增拦截
        fake.reply = json.dumps({"claims": [unseen], "uncertain": True,
                                 "reason": "依赖省略内容"}, ensure_ascii=False)
        r = review_unit.review_claims("这件事办妥了。", "AI 助手收尾陈述",
                                      policy=policy_with(), session_id="c-e2")
        check("E3 模型自报不确定 → 不据此新增拦截（结论降级）",
              r.get("ok") and r.get("verdict") == "allow",
              json.dumps(r.get("detail"), ensure_ascii=False))

        # E4 输入本身超长 → 程序直接不送模型（本地候选 3.6.2 新行为）
        r = review_unit.review_claims("x" * 5000, "AI 助手收尾陈述",
                                      policy=policy_with(), session_id="c-e3")
        check("E4 输入超长 → 程序标记信息不足（不送模型，不靠片段判定）",
              r.get("verdict") == "insufficient"
              and (r.get("detail") or {}).get("elided") is True,
              json.dumps(r.get("detail"), ensure_ascii=False))

        print("\n===== F 状态落盘（超时不得导致失联）=====")
        # 直接跑 hook 的真实入口：评审超时后，状态文件必须仍然写出
        import subprocess
        tmp = tempfile.mkdtemp(prefix="sem-ctr-")
        env = dict(os.environ, CLAUDE_BUDGET_STATE_DIR=tmp,
                   ANTHROPIC_BASE_URL=fake.base_url(),
                   ANTHROPIC_DEFAULT_HAIKU_MODEL=fake.model_name)
        env["CLAUDE_BEHAVIOR_POLICY"] = os.path.join(PKG, "policy", "behavior-policy.json")
        fake.delay = 3.0
        p = subprocess.run([sys.executable, os.path.join(HOOKS, "inject_budget.py")],
                           input=json.dumps({"session_id": "c-f1",
                                             "prompt": "这次不用测试了。"}).encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        state_path = os.path.join(tmp, "c-f1.budget.json")
        exists = os.path.exists(state_path)
        check("F1 评审超时后状态文件仍然写出（不失联）", exists,
              "rc=%s stderr=%s" % (p.returncode, p.stderr.decode("utf-8", "replace")[:150]))
        if exists:
            d = json.load(open(state_path, encoding="utf-8"))
            check("F2 降级被如实记录（intent_degraded 非空）",
                  bool(d.get("intent_degraded")), json.dumps(d, ensure_ascii=False)[:200])
            check("F3 故障路径不写词表禁令",
                  d.get("intent_no_verify") is False
                  and str(d.get("intent_source")) == "hold-on-failure",
                  json.dumps({k: d.get(k) for k in
                              ("intent_no_verify", "intent_source")}, ensure_ascii=False))
            check("F4 注入卡片把降级说给用户听",
                  "语义评审未完成" in p.stdout.decode("utf-8", "replace"),
                  p.stdout.decode("utf-8", "replace")[:200])
        print("\n===== G 事后对照组：旧机制确实会出错（修复完成后补做的对照）=====")
        # ⚠️ 本组是【事后对照】，不是"先红后绿"。
        #    它的作用：旧实现已被替换、跑不出红灯了，所以改为直接验证
        #    「旧机制本身会产生错误结果」，为修复必要性留证。
        #    —— 真正的"先红后绿"在缺陷一那处：先跑完整主流程得到 rc=0（失败），
        #       修复后用同一输入复验得到 rc=2（通过）。
        from intent_gate import scan_intent as _scan

        _, old_verify, _, _ = _scan("不要测试。", {})
        check("G1 词表把『不要测试』读成禁令 → 旧回退路径会据此【新增】禁令（C1 的红）",
              old_verify is True, "")

        # ⚠️ 用【记录在案的未修同类实例】，不是「分别测试」——
        #    后者在 3.5.17 已由复合词保护修掉，拿它当证据会失真。
        #    这两条在修复记录 §6.8 里明确标为「已实测复现、未获授权修」。
        _, wrong_verify, _, _ = _scan("需不需要测试？", {})
        check("G2a 词表把疑问句『需不需要测试？』读成禁令（已知未修误判）",
              wrong_verify is True, "词表 no_verify=%s" % wrong_verify)
        wrong_ask, _, _, _ = _scan("两个模块分别再问一次。", {})   # 第 1 位才是 no_ask
        check("G2b 词表把『分别再问一次』读成禁止提问（同病根）",
              wrong_ask is True, "词表 no_ask=%s" % wrong_ask)

        long_text = "x" * 3000 + "尾部指令XYZ"
        _clipped = long_text[:2000]          # 旧实现：_clip(text, 2000)
        check("G3 旧输入合同（取前2000字）会静默丢掉尾部指令（A2/A3 的红）",
              "尾部指令XYZ" not in _clipped, "截断后 %d 字" % len(_clipped))

        from effect_gate import CLAIM_RX as _CRX, _turn_had_tool_activity as _tta
        check("G4 旧入口用词表守门 → 未见表达『这件事办妥了』一个词都不命中（E1/E2 的红）",
              not _CRX.search("这件事办妥了，你直接用就行"), "")
        check("G5 任务状态判据可读（修复后新增的第二入口依据）",
              _tta(None) is None, "无 transcript_path 时应返回 None")
        print("\n===== H G3 语义结果必须进入证据检查（完整主流程退出码）=====")
        # 用户独立核验发现的缺陷一：语义找到的 claim 被词表的否定剔除分支覆盖 ——
        # 事件写了 review_found_claim，最终却 allow()。
        # ⚠️ 这里测的是【完整 effect_gate.py 主流程的退出码】，
        #    不是 review_claims 的返回值，也不是"事件是否落盘"。
        h_tmp = tempfile.mkdtemp(prefix="sem-ctr-h-")
        env_h = dict(os.environ, CLAUDE_BUDGET_STATE_DIR=h_tmp,
                     CLAUDE_BEHAVIOR_POLICY=os.path.join(PKG, "policy", "behavior-policy.json"),
                     ANTHROPIC_BASE_URL=fake.base_url(),
                     ANTHROPIC_DEFAULT_HAIKU_MODEL=fake.model_name)
        env_h.pop("ANTHROPIC_API_KEY", None)
        fake.delay = 0.0

        def _g3(msg, sid):
            return subprocess.run(
                [sys.executable, os.path.join(HOOKS, "effect_gate.py")],
                input=json.dumps({"session_id": sid, "last_assistant_message": msg,
                                  "stop_hook_active": False}).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env_h, timeout=90).returncode

        unseen2 = "这件事办妥了，你直接用就行。"
        fake.reply = json.dumps({"claims": [unseen2], "uncertain": False,
                                 "reason": "宣称办妥"}, ensure_ascii=False)
        rc = _g3(unseen2, "h1")
        check("H1 词表未覆盖的完成声明 + 无证据块 → 完整主流程拦下（rc=2）",
              rc == 2, "rc=%s（修复前为 0）" % rc)

        fake.reply = json.dumps({"claims": [], "uncertain": False,
                                 "reason": "引用，非本回合声明"}, ensure_ascii=False)
        rc = _g3("他说「这件事办妥了」，但那不是我的结论。", "h2")
        check("H2 反向保护：引用/他人说法 → 放行", rc == 0, "rc=%s" % rc)

        fake.reply = json.dumps({"claims": [], "uncertain": False,
                                 "reason": "否定"}, ensure_ascii=False)
        rc = _g3("这件事还没办妥，明天继续。", "h3")
        check("H3 反向保护：否定表述 → 放行", rc == 0, "rc=%s" % rc)

        fake.reply = json.dumps({"claims": [unseen2], "uncertain": False,
                                 "reason": "宣称办妥"}, ensure_ascii=False)
        rc = _g3("**证据**：L2 动态\n**风险级别**：中\n这件事办妥了，你直接用就行。", "h4")
        check("H4 反向保护：有证据块（中风险 + L2）→ 放行", rc == 0, "rc=%s" % rc)

        print("\n===== I 长输入：中间的关键要求（不得据片段做全局判断）=====")
        i_tmp = tempfile.mkdtemp(prefix="sem-ctr-i-")
        env_i = dict(os.environ, CLAUDE_BUDGET_STATE_DIR=i_tmp,
                     CLAUDE_BEHAVIOR_POLICY=os.path.join(PKG, "policy", "behavior-policy.json"),
                     ANTHROPIC_BASE_URL=fake.base_url(),
                     ANTHROPIC_DEFAULT_HAIKU_MODEL=fake.model_name)
        env_i.pop("ANTHROPIC_API_KEY", None)

        mid = "另外，之前说的不算了，现在可以测试了。"
        long_p = ("无关材料。" * 700) + "\n" + mid + "\n" + ("无关材料。" * 300)
        n_before = len(fake.requests)
        subprocess.run([sys.executable, os.path.join(HOOKS, "inject_budget.py")],
                       input=json.dumps({"session_id": "i1", "prompt": long_p}).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env_i, timeout=90)
        d_i = json.load(open(os.path.join(i_tmp, "i1.budget.json"), encoding="utf-8"))
        check("I1 关键要求在中间 → 程序标记『信息缺失未判断』（hold-too-long）",
              str(d_i.get("intent_source")) == "hold-too-long",
              json.dumps({k: d_i.get(k) for k in ("intent_source", "intent_no_verify")},
                         ensure_ascii=False))
        check("I2 该情况下不新增也不解除约束，且区别于『用户未提及』",
              d_i.get("intent_no_verify") is False and bool(d_i.get("intent_degraded")),
              json.dumps(d_i.get("intent_degraded"), ensure_ascii=False)[:120])
        check("I3 过长时不调用模型（程序判定，不靠模型猜省略部分）",
              len(fake.requests) == n_before,
              "调用次数 %d → %d" % (n_before, len(fake.requests)))

        fake.reply = json.dumps({"ask": "none", "verify": "require", "uncertain": False,
                                 "reason": "明确要求测试"}, ensure_ascii=False)
        subprocess.run([sys.executable, os.path.join(HOOKS, "inject_budget.py")],
                       input=json.dumps({"session_id": "i2",
                                         "prompt": "现在可以测试了。"}).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env_i, timeout=90)
        d_i2 = json.load(open(os.path.join(i_tmp, "i2.budget.json"), encoding="utf-8"))
        check("I4 对照：未超限的同类内容正常判断（source=semantic）",
              str(d_i2.get("intent_source")) == "semantic",
              json.dumps({k: d_i2.get(k) for k in ("intent_source", "intent_no_verify")},
                         ensure_ascii=False))

        print("\n===== J 通道边界：无凭据调用只对回环 / 显式授权地址开放 =====")
        from review_unit import channel_config as _cc
        _saved_url = os.environ.get("ANTHROPIC_BASE_URL")
        _saved_key = os.environ.get("ANTHROPIC_API_KEY")
        try:
            b, _k, _m, why = _cc(policy_with())
            check("J1 本地回环 + 无凭据 → 允许（就是当前通道）", bool(b), why)

            os.environ["ANTHROPIC_BASE_URL"] = "https://api.example-remote.invalid"
            os.environ.pop("ANTHROPIC_API_KEY", None)
            b3, _k, _m, why3 = _cc(policy_with(model="some-model"))
            check("J2 非回环 + 无凭据 → 拒绝调用（不把内容发到未授权地址）",
                  (not b3) and ("拒绝" in why3), why3)

            b4, _k, _m, why4 = _cc(policy_with(
                model="some-model", trusted_noauth_hosts=["api.example-remote.invalid"]))
            check("J3 显式列入 trusted_noauth_hosts → 允许（用户主动授权）", bool(b4), why4)

            os.environ["ANTHROPIC_API_KEY"] = "placeholder-not-a-real-key"
            b5, _k, _m, why5 = _cc(policy_with(model="some-model"))
            check("J4 有凭据 + 任意地址 → 允许（限制只针对无凭据场景）", bool(b5), why5)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            if _saved_key:
                os.environ["ANTHROPIC_API_KEY"] = _saved_key
            if _saved_url:
                os.environ["ANTHROPIC_BASE_URL"] = _saved_url

    finally:
        fake.stop()

    print("\n" + "=" * 60)
    ok = sum(1 for _, c, _ in results if c)
    print("输入合同/预算/失败规则：%d / %d 通过" % (ok, len(results)))
    print("=" * 60)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
