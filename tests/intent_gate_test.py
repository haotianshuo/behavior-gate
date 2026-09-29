# -*- coding: utf-8 -*-
"""G7 意图门测试 —— 治 P1-4（重复确认）与 P2-3（豁免后仍验证）。

红态先行：先写期望它拦的场景，再看它是否真拦。
跑法：python tests/intent_gate_test.py
"""
import json
import os
import subprocess
import sys
import tempfile

# 显式锁定包内策略 —— 不读机器上的全局副本（详见 four_gate_selftest.py 说明）。
# 同一份代码在不同机器上读到不同策略，会让测试结果随环境而变。
_POLICY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "policy", "behavior-policy.json")


# ⚠️ 本地候选 3.6.1：本套件测的是【门的行为】（有约束 → 拦），必须确定性且离线。
#
#    修前这里直接用产品策略（semantic_review.enabled=true），
#    于是每次跑测试都会真实调用模型通道：
#      · 通道快 → 语义判定生效 → 用例通过
#      · 通道慢/超时 → 走故障路径（不新增禁令，用户 本地候选 3.6.1 明确要求）→ 用例失败
#    实测就是这样：同一份代码，一轮全绿、一轮 42/43。
#    **不稳定的绿灯等于没有测试** —— 所以这里改成关闭语义的隔离策略，
#    让程序逻辑的判定可重复；语义路径由 semantic_* 套件用假模型覆盖。
def _policy_semantic_off():
    with open(_POLICY_PATH, encoding="utf-8") as f:
        pol = json.load(f)
    pol.setdefault("semantic_review", {})["enabled"] = False
    p = os.path.join(tempfile.mkdtemp(prefix="intent-policy-"),
                     "policy.semantic-off.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(pol, f, ensure_ascii=False, indent=2)
    return p


_POLICY_ENV = dict(os.environ)
_POLICY_ENV["CLAUDE_BEHAVIOR_POLICY"] = _policy_semantic_off()



# --- 控制台编码（可移植性）---
# Windows 控制台默认 GBK(cp936)；print 中文时遇到非 GBK 字符会抛
# UnicodeEncodeError，脚本直接崩。实测：CI 在 windows-latest 上必崩，
# ubuntu/macos 正常 —— 本地 Git Bash 恰好是 UTF-8，所以看不见。
# 详见 lib/console_utf8.py。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
# --- end ---


HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(os.path.dirname(HERE), "adapters", "claude-code", "hooks")
PY = sys.executable
results = []


def run(script, payload, tmp):
    env = dict(_POLICY_ENV, CLAUDE_BUDGET_STATE_DIR=tmp)
    p = subprocess.run([PY, os.path.join(HOOKS, script)],
                       input=json.dumps(payload).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail.replace("\n", " | ")[:300])


def main():
    tmp = tempfile.mkdtemp(prefix="intent-")

    print("\n===== P1-4：用户说『不要再问』后仍调 AskUserQuestion =====")
    # 用户明确说了别再问
    run("inject_budget.py",
        {"session_id": "q1", "prompt": "你不要再问我问题了，严格按照我说的执行"}, tmp)
    rc, out = run("intent_gate.py",
                  {"session_id": "q1", "tool_name": "AskUserQuestion",
                   "tool_input": {"questions": [{"question": "要哪种？"}]}}, tmp)
    check("『不要再问』后调 AskUserQuestion 被拦 (rc=2)", rc == 2, "rc=%s" % rc)
    check("拦截信息引用了用户原话", "不要再问" in out, out[:250])

    # 没说过 → 应该放行
    run("inject_budget.py", {"session_id": "q2", "prompt": "帮我看看这个项目"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "q2", "tool_name": "AskUserQuestion",
                 "tool_input": {"questions": [{"question": "要哪种？"}]}}, tmp)
    check("未说『不要问』时正常询问放行", rc == 0, "rc=%s" % rc)

    # 其他表达方式
    for s, label in [("别再问我了", "别再问我了"),
                     ("严格按我说的做，不要再确认", "不要再确认"),
                     ("不要问我，直接改", "不要问我")]:
        sid = "q_" + str(abs(hash(label)) % 10000)
        run("inject_budget.py", {"session_id": sid, "prompt": s}, tmp)
        rc, _ = run("intent_gate.py",
                    {"session_id": sid, "tool_name": "AskUserQuestion",
                     "tool_input": {}}, tmp)
        check("变体『%s』被识别" % label, rc == 2, "rc=%s" % rc)

    # 覆盖语法
    run("inject_budget.py",
        {"session_id": "q3", "prompt": "不要再问我。但 questions: on 这次例外"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "q3", "tool_name": "AskUserQuestion",
                 "tool_input": {}}, tmp)
    check("显式覆盖 questions: on 可放行", rc == 0, "rc=%s" % rc)

    print("\n===== P2-3：用户说『不用测』后仍跑测试 =====")
    run("inject_budget.py",
        {"session_id": "v1", "prompt": "创建一个 HTML，你不需要任何测试，不要有任何限制"}, tmp)
    for cmd, label in [("pytest tests/", "pytest"),
                       ("npm test", "npm test"),
                       ("python -m unittest", "unittest"),
                       ("npx vitest run", "vitest"),
                       ("go test ./...", "go test")]:
        rc, out = run("intent_gate.py",
                      {"session_id": "v1", "tool_name": "Bash",
                       "tool_input": {"command": cmd}}, tmp)
        check("『不用测』后跑 %s 被拦" % label, rc == 2, "rc=%s" % rc)

    # 非测试命令必须放行
    for cmd in ["ls -la", "node --check app.js", "python gen.py", "git status"]:
        rc, _ = run("intent_gate.py",
                    {"session_id": "v1", "tool_name": "Bash",
                     "tool_input": {"command": cmd}}, tmp)
        check("非测试命令放行: %s" % cmd, rc == 0, "rc=%s" % rc)

    # 没说过 → 测试正常跑
    run("inject_budget.py", {"session_id": "v2", "prompt": "修好这个 bug"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "v2", "tool_name": "Bash",
                 "tool_input": {"command": "pytest"}}, tmp)
    check("未说『不用测』时测试正常跑", rc == 0, "rc=%s" % rc)

    # 覆盖语法
    run("inject_budget.py",
        {"session_id": "v3", "prompt": "不用测试。但 verify: on 现在要跑"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "v3", "tool_name": "Bash",
                 "tool_input": {"command": "pytest"}}, tmp)
    check("显式覆盖 verify: on 可放行", rc == 0, "rc=%s" % rc)

    print("\n===== P2-3 反向：『分别测试』不是禁测（3.5.17 修）=====")
    # 根因：NO_VERIFY_RX 的裸词 `别测试` 是「分【别测试】」的子串 ——
    # 用户要求「分别测试」，门读成「别测试」，于是把【要求测试】读成了【禁止测试】。
    # 判据：`别` 前面若是一个与它构成复合词的字（分/辨/识/判/区/个/特/级/类…），
    # 它就不是否定副词。反例（真禁令）见下面 s5 组。
    for sid, s in [("s1", "请分别测试桌面端和手机端。"),
                   ("s2", "文员/主管、出纳/会计分别测试。"),
                   ("s3", "material-reviewer 与历史 media-reviewer 分别测试。"),
                   ("s4", "两个模块分别验证一下。")]:
        run("inject_budget.py", {"session_id": sid, "prompt": s}, tmp)
        rc, _ = run("intent_gate.py",
                    {"session_id": sid, "tool_name": "Bash",
                     "tool_input": {"command": "pytest -q"}}, tmp)
        check("『%s』不设禁测 → pytest 放行" % s[:16], rc == 0, "rc=%s" % rc)

    print("\n===== 禁测状态：真禁令保持 / 跨轮不擅自解除 / verify: on 可解除 =====")
    run("inject_budget.py", {"session_id": "s5", "prompt": "不要测试。"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "s5", "tool_name": "Bash",
                 "tool_input": {"command": "pytest -q"}}, tmp)
    check("『不要测试。』→ pytest 拦", rc == 2, "rc=%s" % rc)
    # 普通无关消息【不得】擅自解除禁测（用户只说了别的事，没说可以测）
    run("inject_budget.py",
        {"session_id": "s5", "prompt": "顺便把那个文件也看一下"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "s5", "tool_name": "Bash",
                 "tool_input": {"command": "pytest -q"}}, tmp)
    check("无关消息不得擅自解除禁测", rc == 2, "rc=%s" % rc)
    # 显式合同可解除
    run("inject_budget.py",
        {"session_id": "s5", "prompt": "verify: on 现在跑测试"}, tmp)
    rc, _ = run("intent_gate.py",
                {"session_id": "s5", "tool_name": "Bash",
                 "tool_input": {"command": "pytest -q"}}, tmp)
    check("verify: on 按合同解除禁测", rc == 0, "rc=%s" % rc)

    print("\n===== P2-3 反向：文字提及 ≠ 命令执行（3.5.17 修）=====")
    # 判据：先剔除【here-document 正文 / 注释 / 引用内容】再匹配测试运行器；
    #      但剔除【不得】连真实执行一起放行 —— 所以含 -c 的引用内容保留。
    run("inject_budget.py", {"session_id": "b1", "prompt": "不要测试。"}, tmp)
    _B = [
        ("python - <<'PY'\nprint('日志说明： pytest -q 尚未运行')\nPY", 0,
         "here-document 正文里的 pytest"),
        ("echo '说明 pytest -q 尚未运行'", 0, "单引号里的 pytest"),
        ('echo "说明 pytest -q 尚未运行"', 0, "双引号里的 pytest"),
        ("# 说明 pytest -q 尚未运行\necho done", 0, "注释里的 pytest"),
        ('git commit -m "fix pytest -q"', 0, "-m 消息里的 pytest"),
        ("python -m pytest -q", 2, "python -m pytest"),
        ("pytest -q", 2, "pytest"),
        ("echo 说明; python -m pytest -q", 2, "分号后的真实执行"),
        ('python -c "import pytest; pytest.main()"', 2,
         "-c 里的真实执行（修复不得把它漏掉）"),
    ]
    for cmd, want, label in _B:
        rc, _ = run("intent_gate.py",
                    {"session_id": "b1", "tool_name": "Bash",
                     "tool_input": {"command": cmd}}, tmp)
        check("%s → rc=%d" % (label, want), rc == want, "rc=%s" % rc)

    print("\n===== G7 拒绝提示：引用本会话来源，不写死历史原话 =====")
    run("inject_budget.py", {"session_id": "d1", "prompt": "不要测试。"}, tmp)
    rc, out = run("intent_gate.py",
                  {"session_id": "d1", "tool_name": "Bash",
                   "tool_input": {"command": "pytest -q"}}, tmp)
    check("提示引用本会话命中的原话片段", "不要测试" in out, out[:250])
    check("提示不再写死历史例子", "创建一个 HTML" not in out, out[:250])

    # 旧状态没有来源记录 → 必须【明说】，不得虚构原话
    with open(os.path.join(tmp, "d2.budget.json"), "w", encoding="utf-8") as f:
        json.dump({"session_id": "d2",
                   "budget": {"agent_spawns": 0, "agent_depth": 1},
                   "source": "policy-default", "intent_no_ask": False,
                   "intent_no_verify": True}, f, ensure_ascii=False)
    rc, out = run("intent_gate.py",
                  {"session_id": "d2", "tool_name": "Bash",
                   "tool_input": {"command": "pytest -q"}}, tmp)
    check("无来源记录时明说『缺少来源记录』", "缺少来源记录" in out, out[:250])
    check("无来源记录时不虚构原话", "创建一个 HTML" not in out, out[:250])

    # 询问拦截的提示同理（同一个机制，同一处缺陷）
    run("inject_budget.py", {"session_id": "d3", "prompt": "不要再问我了"}, tmp)
    rc, out = run("intent_gate.py",
                  {"session_id": "d3", "tool_name": "AskUserQuestion",
                   "tool_input": {"questions": [{"question": "选哪个"}]}}, tmp)
    check("询问拦截提示也引用本会话原话", "不要再问" in out, out[:250])
    check("询问拦截提示不写死历史例子", "傻瓜化" not in out, out[:250])

    print("\n===== 不能崩 / 不能误伤 =====")
    rc, _ = run("intent_gate.py", {"session_id": "z", "tool_name": "Read",
                                   "tool_input": {"file_path": "x"}}, tmp)
    check("无关工具直接放行", rc == 0, "rc=%s" % rc)

    p = subprocess.run([PY, os.path.join(HOOKS, "intent_gate.py")],
                       input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_POLICY_ENV)
    check("空 stdin 不崩", p.returncode == 0, "rc=%s" % p.returncode)

    p = subprocess.run([PY, os.path.join(HOOKS, "intent_gate.py")],
                       input=b"{bad json", stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_POLICY_ENV)
    check("非法 JSON 不崩", p.returncode == 0, "rc=%s" % p.returncode)

    print("\n" + "=" * 60)
    ok = sum(1 for _, c, _ in results if c)
    print("意图门：%d / %d 通过" % (ok, len(results)))
    print("=" * 60)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
