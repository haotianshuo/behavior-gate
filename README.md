# 行为门 BehaviorGate

> 给 Claude Code 加五道**机械门**，治「AI 助手想多做、做了却说完成了」这类毛病。
>
> 当前版本：**V3.5.2** ｜ 2026-09-14
> 许可证：[MIT](LICENSE)

---

## 这是什么

一个 Claude Code 的 hooks 集合。它不改变模型的能力，只改变模型的**行动方式**：

| 门 | 拦截内容 | 强制力 |
|---|---|---|
| **G1 预算门** | 默认不许派子代理；要派必须在消息里写 `agent_spawns: N` | ✅ 机械强制 |
| **G3 生效门** | 说「已完成 / 已修复」时必须给证据，否则打回 | ✅ 后置打回 |
| **G5 危险门** | `pkill -f`、`find /tmp`、整行 `rm -rf /` 这类会翻车的写法 | ✅ 机械强制 |
| **G7 意图门** | 用户说过「不要再问」仍弹询问、「不用测」仍跑测试 | ✅ 机械强制 |
| G6 收口 | 旁路采集（默认关闭） | 仅记录 |

---

## 它解决什么问题

AI 编码助手有三类反复出现的毛病：

```text
1. 想多做  —— 没让你派子代理，自己派了一堆
2. 假完成  —— 说"已修复"，但没有任何证据
3. 危险操作 —— pkill -f、rm -rf 写错路径，一次就翻车
```

**这些不是模型「不懂」，是模型「做不到自律」。** 提示词层的约束会被绕过，机械门不会。

---

## 它**不**解决什么（重要）

> ⚠️ **它证明不了什么，和它能拦什么同样重要。**

| 能力 | 状态 |
|---|---|
| 控制 Agent 派生 | ✅ 机械强制 |
| 拦截已知危险命令 | ✅ 机械强制（但只是**防误操作层，不是安全边界**） |
| 要求完成时给证据 | ⚠️ 能查「**有没有**证据」，**不能查证据对不对** |
| Bash / WebFetch 硬限额 | ❌ 没有 |
| 真正的根因判断 | ❌ 仍靠模型自己 |
| 重复会话 | ❌ 未解决 |
| 跨 Codex / Cursor / opencode | ❌ 当前没有强制提升 |

**准确说法：主要限制「行动方式」，不能保证 AI 的判断一定正确。**

`判定: MATCH` / `自检通过` / `证据：L3` 都**不等于**内容已被核实。

---

## 系统要求

- **Python 3.10+**（安装时自动探测，不写死路径）
- Claude Code（含 hooks 支持的版本）
- 平台：Windows / macOS / Linux

**零第三方依赖** —— 全部使用 Python 标准库。

---

## 安装

### 预演（不写任何文件）

```bash
cd <你解压后的目录>
python install.py
```

### 应用

```bash
python install.py --apply         # 安装到当前项目
python install.py --apply --global # 安装到全局（谨慎）
```

项目级安装写入 `<项目>/.claude/settings.local.json`，**只影响这一个项目**。
重启 Claude Code 后生效。

> **定位：可以 Shadow 试跑；不建议直接全局安装。**

---

## 最小使用示例

安装后，正常使用即可。门会在后台工作。

**要派子代理时**，在消息里显式声明：

```text
帮我调研一下 X

agent_spawns: 3
```

不写这一行，预算门默认按 `0` 执行（禁止派生子代理）。

**要临时放宽时**：

```text
questions: on     # 允许弹询问（覆盖「不要再问」）
verify: on        # 允许跑验证（覆盖「不用测」）
```

---

## 验证安装

```bash
python tools/verify_deploy.py     # 校验部署完整性
```

跑测试（预期 **154 项**全过）：

```bash
python tests/four_gate_selftest.py    # 50/50
python tests/cmd_chain_test.py        # 21/21
python tests/adversarial_test.py      # 21/21
python tests/budget_safety_test.py    # 23/23
python tests/intent_gate_test.py      # 21/21
python tests/upgrade_path_test.py     # 18/18
```

---

## 配置

策略内核在 `policy/behavior-policy.json`，适配器读取它，**不硬编码规则**。

```json
{
  "budget": {
    "agent_spawns": 0,
    "agent_depth": 1
  },
  "effect_gate":    { "enabled": true },
  "destructive_gate": { "enabled": true },
  "closeout":       { "enabled": false }
}
```

---

## 已知限制

**Threat Model —— 它防谁、不防谁：**

| 威胁 | 状态 |
|---|---|
| T1 误操作 / 本地损坏 | ✅ 当前重点保护 |
| T2 有写权限的恶意操作者 | ❌ NOT_PROTECTED |
| T3 多方审计 / 第三方不可抵赖 | ❌ OUT_OF_SCOPE |

**其他已知边界：**

- **G5 只拦 Bash 命令字符串**，拦不住等价的 Python / Node 实现
  （实测：写 `os.remove` / `shutil.rmtree` 的 .py 脚本可以完全绕过）
- **G3 只检查证据格式，不检查证据真伪**
  （实测：写 `L3 端到端` 但不真的验证，可以过）
- **G3 实测误报率约 1/3**（「A 已验证 + B 未验证」分属不同段落时会被误判）
- **失败策略是 FAIL-OPEN** —— 宁可漏拦，不可把用户会话卡死

---

## 回滚

```bash
python install.py --rollback
```

安装时会在 `.claude/` 下留 `.bak-<时间戳>` 备份。

---

## 文件结构

```
├─ install.py                      安装（双作用域，幂等）
├─ install.md                      安装说明
├─ CHANGELOG.md                    变更记录（28 项真实问题）
├─ policy/behavior-policy.json     策略内核（工具无关）
├─ adapters/claude-code/
│   ├─ settings.fragment.json      hook 配置片段
│   └─ hooks/                      _lib + 5 个门 + VERSION
├─ lib/                            共用模块
├─ tools/                          部署校验 / 可移植性校验
└─ tests/                          六套测试（154 项）
```

---

## 安全与联系

**请勿在公开 Issue 中披露疑似漏洞。** 使用
[GitHub 私下漏洞报告](https://github.com/haotianshuo/behavior-gate/security/advisories/new)
或发邮件至 `xrlcom@126.com`，主题注明 `Security report — behavior-gate`。

请勿在报告中包含密码、API key、Cookie、存储状态、私有 URL 或个人数据。
普通问题请开 GitHub Issue。

> 该邮箱是**公开的项目联系地址，不是版权人声明**。
> 许可边界见 [LICENSE](LICENSE)。

---

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

漏洞报告见 [SECURITY.md](SECURITY.md)。

---

## 许可证

[MIT](LICENSE)
