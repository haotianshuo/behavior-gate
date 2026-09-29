# -*- coding: utf-8 -*-
"""G3 语义归属测试（本地候选 3.6.0）—— 治「引用被当成声明」

分层报告，与 semantic_intent_test.py 相同约定：

  A 离线层 —— 语义评审不可用时，G3 的行为必须与 3.5.17 完全一致
    （引用形态仍被误拦 = 已知未解决，但【不许变得更糟】）。
    不计入通过数的一律标 KNOWN-UNRESOLVED。

  B 在线层 —— 真实调用评审，验证引用识别是否成立。
    通道不可用时明确 SKIP，不计入通过数。

跑法：python tests/semantic_g3_test.py
跳过在线层：BEHAVIOR_GATE_SKIP_ONLINE=1
"""
import json
import os
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(os.path.dirname(HERE), "adapters", "claude-code", "hooks")
HOOK = os.path.join(HOOKS, "effect_gate.py")
PY = sys.executable

results = []
skipped = []
unresolved = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  %s %s" % ("PASS" if cond else "FAIL", name))
    if not cond and detail:
        print("       %s" % detail.replace("\n", " | ")[:300])


def skip(name, why):
    skipped.append((name, why))
    print("  SKIP %s（%s）" % (name, why))


def known_unresolved(name, got, want):
    unresolved.append(name)
    print("  [KNOWN-UNRESOLVED] %s  expect=%s got=%s（不计入通过）" % (name, want, got))


def run(msg, sid, env):
    payload = {"session_id": sid, "hook_event_name": "Stop",
               "last_assistant_message": msg}
    pr = subprocess.run([PY, HOOK], input=json.dumps(payload).encode("utf-8"),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env=env, timeout=180)
    return pr.returncode, (pr.stdout + pr.stderr).decode("utf-8", "replace")


# 四个样本。R1/R4 是「引用」形态，R2/R3 是必须继续拦住的真声明。
SAMPLES = [
    ("R1", "表格里的用例名（real_03 形态）", """本轮复验结果如下：

**证据**：L2 动态
**风险级别**：中

| 反例 | 旧版 | 第三轮 |
|---|---|---|
| 两行说同一件事（已完成 + 未验证生效） | 2 | 0 |

其余无变化。""", 0),
    ("R2", "真声明 + 无证据块", """本轮修复已完成，全部通过。""", 2),
    ("R3", "表格里的当前状态声明", """**证据**：L2 动态
**风险级别**：中

| 事项 | 当前结果 |
|---|---|
| 本次修复 | 已完成，但本次修复未验证生效 |""", 2),
    ("R4", "正文里引用用例名", """本轮把「已完成 + 未验证生效」这条反例加进了回归。

其余无变化。""", 0),
]


def offline_env(tmp):
    """离线层：显式掐断通道，验证"不可用时不改变原有行为"。"""
    env = dict(os.environ, CLAUDE_BUDGET_STATE_DIR=tmp)
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_MODEL"):
        env.pop(k, None)
    return env


def online_env(tmp):
    """在线层：优先用环境里已有的通道；缺失时从 ~/.claude/settings.json 补。"""
    env = dict(os.environ, CLAUDE_BUDGET_STATE_DIR=tmp)
    have = env.get("ANTHROPIC_BASE_URL") and env.get("ANTHROPIC_API_KEY")
    if not have:
        p = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
        try:
            with open(p, encoding="utf-8") as f:
                cfg = (json.load(f) or {}).get("env") or {}
        except Exception:
            return None
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_MODEL"):
            if cfg.get(k):
                env[k] = cfg[k]
    if not (env.get("ANTHROPIC_BASE_URL") and env.get("ANTHROPIC_API_KEY")):
        return None
    return env


def main():
    tmp = tempfile.mkdtemp(prefix="semantic-g3-")

    print("\n===== A 离线层：评审不可用时行为不变 =====")
    env_off = offline_env(tmp)
    # R2 必须仍然拦（这是硬性保护，任何时候都不许因评审故障而放宽）
    rc, out = run(SAMPLES[1][2], "off-r2", env_off)
    check("A1 评审不可用时，真声明无证据仍被拦", rc == 2, "rc=%s" % rc)
    # R3 同上
    rc, out = run(SAMPLES[2][2], "off-r3", env_off)
    check("A2 评审不可用时，表格里的状态声明仍被拦", rc == 2, "rc=%s" % rc)
    # R1 在离线时是已知未解决的误拦 —— 如实登记，不伪装
    rc, out = run(SAMPLES[0][2], "off-r1", env_off)
    if rc == 2:
        known_unresolved("A3 R1 引用形态在无评审时仍误拦", rc, 0)
    else:
        # ⚠️ 审计修复（2026-09-29，三遍交叉验证后实施）：原为 check(..., True)
        #    恒真断言 —— 与「没跑不能记成通过」的纪律相反（testing reviewer #14，
        #    validator confirmed）：rc != 2 时会无条件打印 PASS，掩盖真实行为。
        #    改为如实登记为已知未解决：两种 rc 都不伪装 PASS。
        known_unresolved("A3 R1 引用形态在无评审时已被放行（非预期，待核）", rc, 2)

    print("\n===== B 在线层：语义归属 =====")
    # 默认跳过 —— 理由同 semantic_intent_test：在线层依赖外部通道，
    # 波动会让套件时绿时红。要跑：BEHAVIOR_GATE_ONLINE=1
    if os.environ.get("BEHAVIOR_GATE_ONLINE") != "1":
        skip("B 全部", "默认跳过（设 BEHAVIOR_GATE_ONLINE=1 开启真实调用）")
    else:
        env_on = online_env(tmp)
        if env_on is None:
            skip("B 全部", "没有可用的模型通道")
        else:
            for tag, name, msg, expect in SAMPLES:
                rc, out = run(msg, "on-%s" % tag, env_on)
                check("B %s %s → rc=%s" % (tag, name, expect), rc == expect,
                      "rc=%s\n%s" % (rc, out[:200]))

    print("\n" + "=" * 60)
    ok = sum(1 for _, c, _ in results if c)
    print("G3 语义归属：%d / %d 通过；跳过 %d 项；已知未解决 %d 项（均不计入通过）"
          % (ok, len(results), len(skipped), len(unresolved)))
    print("=" * 60)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
