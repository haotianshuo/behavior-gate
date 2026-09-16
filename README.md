# 行为门 BehaviorGate

> 给 Claude Code 加五道**机械门**，治「AI 助手想多做、做了却说完成了」这类毛病。
>
> 当前版本：**V3.5.11** ｜ 2026-09-16
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
| G6 收口 | 旁路采集（记录派生/写操作，并做用量提示） | 仅记录 |

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
| 拦截已知危险命令 | ✅ 机械强制（防误操作层，不是安全边界） |
| 要求完成时给证据 | ✅ 能查「有没有证据」（**不判断证据真伪**，见下） |
| Bash / WebFetch 用量 | ✅ 越过阈值时提示 |
| 同一命令重复 3 次 | ✅ 提示（**不拦截**） |

**准确说法：主要限制「行动方式」，不保证 AI 的判断一定正确。**

### 刻意不做的（不是欠债，是设计边界）

以下能力**在原理上不适合由机械门实现**，因此本项目不做，也不声称能做：

```text
根因判断          需要理解语义，门只能匹配字符串与结构
证据真伪          同上 —— 门能逼你写"在哪看"，查不了那是不是真的
跨工具强制提升      hook 机制是宿主提供的，本项目只在 Claude Code 上生效
输出风格           属于产品层，不属于行为约束层
```

这些不是「待办」，是**明确划出去的边界**。
把它们写成 ❌ 会让人误以为「将来会做」—— 实际上做了就是过度承诺。

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
python install.py --apply              # 安装到当前项目
python install.py --apply --scope global  # 安装到全局（谨慎）
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

跑测试（预期 **291 项**全过）：

```bash
python tests/four_gate_selftest.py        # 53/53
python tests/cmd_chain_test.py            # 21/21
python tests/adversarial_test.py          # 24/24
python tests/budget_safety_test.py        # 30/30
python tests/intent_gate_test.py          # 21/21
python tests/upgrade_path_test.py         # 19/19
python tests/g5_real_regression_test.py   # 25/25  ← 真实会话挖出的用例
python tests/source_identity_consistency_test.py  # 13/13  ← 集合一致性
python tests/gate_events_test.py          # 30/30  ← Gate 事件记录
python tests/g5_windows_path_test.py      # 18/18  ← Windows 路径回归
python tests/install_cli_test.py          # 12/12  ← 安装参数回归
python tests/mcp_field_dispatch_test.py   # 26/26  ← MCP 字段分派
```

> **synthetic 与 REAL 分开** —— `tests/` 里前六套是按规则构造的用例（写法理想）；
> `g5_real_regression_test.py` 里的用例来自**真实会话**并已复现。
> #48 之所以漏了这么久，正是因为理想写法（`rm -rf /`）与真实写法
> （`rm -rf /tmp/x`、`rm -r -f /`、`rm -Rf /`）不同。

---

## 门做了什么（观测）

每次门**出手**（deny / stop-feedback），会往状态目录追加一行事件：

```
<state_dir>/gate-events.jsonl
```

一行一个事件，形如：

```json
{"ts":"2026-09-15T19:40:00+0800","session":"...","gate_id":"G5",
 "rule_id":"rm_rf_traversal","decision":"deny","tool":"Bash",
 "source_class":"NATURAL","version":"3.5.10","source_snapshot":"..."}
```

**只记「门出手了」，不记放行。** 有了它，就能回答
「这周哪道门拦了多少次、哪条规则、哪份源码快照」——
而不必再反向翻几万条聊天记录。

**它不记什么**（有意为之）：完整 prompt、assistant 输出、
完整命令、Write/Edit 正文、MCP payload、任何密钥。

> ⚠️ **边界**：这是 **T1 本地观测证据**。
> 它证明「本机门代码记录了自己做出的决定」；
> **不**证明日志不可修改、不可伪造、不可删除，**也不构成不可抵赖的证据**。
> 有写权限的人可以改它。它不是安全审计系统 —— 需要那种能力请用操作系统级沙箱。

**记录失败绝不改变门的判定**：写不进日志时，门照常执行原来的 deny / allow。
观测层永远不会成为门的故障点。

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
  "closeout":       { "enabled": true }
}
```

---

## 适用范围

**它防谁：**

```text
T1  误操作 / 本地损坏 / 配置漂移     → 本项目的保护目标
```

**它不防谁（重要）：**

```text
T2  有写权限的恶意操作者
    → 本工具挡不住。哈希与清单只用于【自己发现改动】，
      有写权限的人可以同时改文件和清单，本包发现不了。
      这不是缺陷，是它本来就不是安全产品。

T3  多方审计 / 第三方不可抵赖
    → 不在设计范围内。本地哈希不构成不可抵赖的证据。
```

> ⚠️ **如果你需要防 T2 / T3，这个工具不适用 —— 请改用操作系统级沙箱或外部审计系统。**
> 不要把本包的校验结果当作安全边界。

**其他已知边界：**

- **G5 只拦 Bash 命令字符串**，拦不住等价的 Python / Node 实现
  （实测：写 `os.remove` / `shutil.rmtree` 的 .py 脚本可以完全绕过）
- **G3 只检查证据格式，不检查证据真伪**
  （实测：写 `L3 端到端` 但不真的验证，可以过）
- **G3 实测误报率约 1/3**（「A 已验证 + B 未验证」分属不同段落时会被误判）
- **用量与重复提示只提示不拦截** —— 打印一行，不影响执行
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
├─ CHANGELOG.md                    变更记录（53 项真实问题）
├─ BASELINE.md                     已验证基线（冻结状态）
├─ policy/behavior-policy.json     策略内核（工具无关）
├─ adapters/claude-code/
│   ├─ SOURCE_IDENTITY.json        源快照身份声明（元数据层，不进 hooks/）
│   ├─ settings.fragment.json      hook 配置片段
│   └─ hooks/                      _lib + 5 个门 + VERSION
├─ lib/                            共用模块
├─ tools/                          部署校验 / 源身份 / 可移植性校验
└─ tests/                          十二套测试（291 项，含真实回归）
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
