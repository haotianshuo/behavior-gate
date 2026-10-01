#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G7 / inject_budget 的【状态故障披露】回归（2026-09-30）。

## 它守什么

两处披露修复（保留既定 fail-open，只修"故障时用户能否知道"）：

  1. G7 意图门读状态失败（文件存在但损坏 / 读取被拒 / 空文件）→ 必须【明确告警】；
     而"状态缺失"（首次使用）与"正常读到"必须【保持安静】（安静原则）。
     修前：三种异常全部 rc=0 且 stderr 空 —— 既有禁令丢失且零感知。
  2. inject_budget 写状态失败 → 卡片必须说【未能保存】；
     修前无论成败都宣称"已记录"，与同轮 STATE_SAVE_FAILED 告警自相矛盾。

## 边界（如实声明）

  证明    : 进程层面的 discloure 行为（rc + stderr）符合上表
  不证明  : 宿主的 TUI 里用户实际看到了这些文字（本测试只读进程输出）
  不证明  : 权限级"读取被拒绝"（本测试用"路径为目录"模拟读取异常；
            Windows 上构造 ACL 拒绝需要改权限，代价与副作用不成比例）
  时效性  : 改动 intent_gate / inject_budget 后必须重跑
"""
import json
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(ROOT, "adapters", "claude-code", "hooks")
POLICY = os.path.join(ROOT, "policy", "behavior-policy.json")
PY = sys.executable

FAILED = []
PASSED = []


def check(label, ok, detail=""):
    print("  %s %s%s" % ("PASS" if ok else "FAIL", label,
                         ("  " + str(detail)) if detail else ""))
    (PASSED if ok else FAILED).append(label)


def offline_policy(tmpdir):
    """确定性夹具：关闭语义评审的策略副本（不依赖凭据 / 网络 / 模型）。

    ⚠️ 为什么必须关（修于 2026-09-30，朋友方复核指出）：
        本测试断言的是【披露文字】（"已记录" / "未能保存" / 告警句）。
        若沿用启用语义评审的正式策略，离线运行时走 hold-on-failure
        —— 不新增禁令 —— "不要测试"就不会形成约束，卡片自然不出现
        被断言的文字；结果随通道可用性而变，测试不确定。
        项目测试分层约定：程序逻辑测试一律用【关闭语义评审的隔离策略】。
    """
    pol = json.load(open(POLICY, encoding="utf-8"))
    pol.setdefault("semantic_review", {})["enabled"] = False
    dst = os.path.join(tmpdir, "policy_offline.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(pol, f, ensure_ascii=False)
    return dst


POL_OVERRIDE = None      # main() 里指向确定性夹具（关闭语义的策略副本）


def call(hook, payload, sd):
    env = dict(os.environ)
    env["CLAUDE_BEHAVIOR_POLICY"] = POL_OVERRIDE or POLICY
    env["CLAUDE_BUDGET_STATE_DIR"] = sd
    p = subprocess.run([PY, os.path.join(HOOKS, hook)],
                       input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=90)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


ASK = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
       "tool_input": {"questions": [{"question": "x"}]}}
G7, IB = "intent_gate.py", "inject_budget.py"
DISCLOSURE = "既有约束可能丢失"      # 告警的稳定子串（避免断言整句）


def fresh():
    return tempfile.mkdtemp(prefix="g7disc-")


def main():
    global POL_OVERRIDE
    POL_OVERRIDE = offline_policy(tempfile.mkdtemp(prefix="g7disc-pol-"))
    print("夹具：关闭语义评审的策略副本（确定性，不依赖模型/网络）")
    print()
    print("=" * 62)
    print("G7 读状态披露（rc / stderr / 安静性）")
    print("=" * 62)
    # 1 正常 + 禁令：拦，且不告警
    d = fresh()
    json.dump({"session_id": "m", "intent_no_ask": True, "intent_ask_excerpt": "不要再问"},
              open(os.path.join(d, "m.budget.json"), "w", encoding="utf-8"), ensure_ascii=False)
    rc, _, err = call(G7, {**ASK, "session_id": "m"}, d)
    check("正常+禁令 → rc=2", rc == 2, "rc=%s" % rc)
    check("正常+禁令 → 无披露告警", DISCLOSURE not in err)
    # 2 损坏 JSON：放行（既定策略）+ 必须告警
    d = fresh()
    open(os.path.join(d, "m.budget.json"), "w", encoding="utf-8").write("{坏")
    rc, _, err = call(G7, {**ASK, "session_id": "m"}, d)
    check("损坏 JSON → rc=0（fail-open 策略不变）", rc == 0, "rc=%s" % rc)
    check("损坏 JSON → 明确告警", DISCLOSURE in err, err.splitlines()[0][:60] if err else "(空)")
    # 3 缺失（首次使用）：放行且安静
    d = fresh()
    rc, _, err = call(G7, {**ASK, "session_id": "m"}, d)
    check("状态缺失 → rc=0", rc == 0, "rc=%s" % rc)
    check("状态缺失 → 保持安静（首次使用不告警）", DISCLOSURE not in err)
    # 4 目录不可写（= 无状态）：安静
    rc, _, err = call(G7, {**ASK, "session_id": "m"}, "Z:\\nonexistent\\dir")
    check("无状态目录 → rc=0 且安静", rc == 0 and DISCLOSURE not in err, "rc=%s" % rc)
    # 5 存在可读、仅不可写：禁令保留、不告警
    d = fresh()
    f = os.path.join(d, "m.budget.json")
    json.dump({"session_id": "m", "intent_no_ask": True, "intent_ask_excerpt": "不要再问"},
              open(f, "w", encoding="utf-8"), ensure_ascii=False)
    os.chmod(f, stat.S_IREAD)
    rc, _, err = call(G7, {**ASK, "session_id": "m"}, d)
    check("仅不可写 → 禁令保留 rc=2", rc == 2, "rc=%s" % rc)
    check("仅不可写 → 不告警（读成功）", DISCLOSURE not in err)
    try:
        os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass
    # 6 受控读取异常：路径为目录
    d = fresh()
    os.makedirs(os.path.join(d, "m.budget.json"), exist_ok=True)
    rc, _, err = call(G7, {**ASK, "session_id": "m"}, d)
    check("读取异常(路径为目录) → rc=0 且告警",
          rc == 0 and DISCLOSURE in err, "rc=%s" % rc)
    # 7 空文件
    d = fresh()
    open(os.path.join(d, "m.budget.json"), "w", encoding="utf-8").close()
    rc, _, err = call(G7, {**ASK, "session_id": "m"}, d)
    check("空文件 → rc=0 且告警", rc == 0 and DISCLOSURE in err, "rc=%s" % rc)

    print()
    print("=" * 62)
    print("inject_budget 写失败披露")
    print("=" * 62)
    d = fresh()
    f = os.path.join(d, "n.budget.json")
    json.dump({"session_id": "n"}, open(f, "w", encoding="utf-8"))
    os.chmod(f, stat.S_IREAD)
    rc, out, err = call(IB, {"session_id": "n", "hook_event_name": "UserPromptSubmit",
                             "prompt": "不要测试"}, d)
    check("写失败 → 不宣称『已记录本会话要求』", "已记录本会话要求" not in out)
    check("写失败 → 明说『未能保存』", "未能保存" in out)
    check("写失败 → 仍有 STATE_SAVE_FAILED（既有告警未回归）",
          "STATE_SAVE_FAILED" in err, err.splitlines()[0][:60] if err else "(空)")
    # 对照：可写时仍正常宣称已记录
    d2 = fresh()
    rc, out2, _ = call(IB, {"session_id": "n2", "hook_event_name": "UserPromptSubmit",
                            "prompt": "不要测试"}, d2)
    check("写成功 → 仍说『已记录本会话要求』（不误伤）",
          "已记录本会话要求" in out2 and "未能保存" not in out2)
    try:
        os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass

    print()
    total = len(PASSED) + len(FAILED)
    print("G7 状态披露回归：%d / %d 通过" % (len(PASSED), total))
    if FAILED:
        print("失败项：")
        for x in FAILED:
            print("  - %s" % x)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
