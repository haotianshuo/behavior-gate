# 行为门安装说明

> 对应版本：**V3.5.14** ｜ 2026-09-18
>
> ⚠️ **它证明不了什么，和它能拦什么同样重要 —— 见下面的「已知边界」。**
> 一句话：门管的是「行动方式」和「格式」，不是「判断对不对」。
> `判定: MATCH` / `自检通过` / `证据：L3` 都**不等于**内容已被核实。
> 当前实测：四关 54/54、安装链路 21/21、对抗 25/25、预算安全 30/30、
> 　　　　　意图门 21/21、升级路径 19/19、G5 真实回归 25/25、
> 　　　　　源身份一致性 13/13、Gate 事件 33/33、Windows 路径 18/18、
> 　　　　　安装参数 12/12、MCP 字段分派 27/27（=306 项）
> 定位：**可以 Shadow 试跑；不建议直接全局安装**
>
> 版本声明以 `adapters/claude-code/hooks/VERSION` 为准，它随 hooks 一起部署与校验；
> 本文档的版本行与它不一致时，以 VERSION 为准。

---

## 一分钟安装（推荐：项目级）

```bash
cd <你解压后的目录>

python install.py                 # 预演，不写任何文件
python install.py --apply         # 安装到当前项目
```

项目级写入 `<项目>/.claude/settings.local.json`，**只影响这一个项目**，
不会碰你的全局配置，也不会碰其他项目。

重启 Claude Code 后生效。

---

## 作用域

| | 项目级（默认） | 全局 |
|---|---|---|
| 影响范围 | 当前项目 | 所有项目 |
| 写入位置 | `<项目>/.claude/settings.local.json` | `~/.claude/settings.json` |
| 回滚 | 删一个文件 | 从备份恢复 |
| 何时用 | **Shadow 试跑（现在）** | 六项真实测试跑完之后 |

```bash
python install.py --scope global           # 预演
python install.py --scope global --apply   # 安装
```

> `install.py` 是**幂等**的：只新增 hook 条目，不删除、不覆盖任何已有条目。
> 先备份，再写入，并打印回滚命令。

---

## 安装后验证

```bash
python tests/four_gate_selftest.py        # 期望 54/54
python tests/cmd_chain_test.py            # 期望 21/21
python tests/adversarial_test.py          # 期望 25/25
python tests/budget_safety_test.py        # 期望 38/38
python tests/intent_gate_test.py          # 期望 21/21
python tests/upgrade_path_test.py         # 期望 19/19
python tests/g5_real_regression_test.py   # 期望 25/25  ← 真实会话用例
python tests/source_identity_consistency_test.py  # 期望 13/13
python tests/gate_events_test.py          # 期望 33/33
python tests/g5_windows_path_test.py      # 期望 18/18
python tests/install_cli_test.py          # 期望 12/12
python tests/mcp_field_dispatch_test.py   # 期望 27/27
python tests/doc_consistency_test.py      # 期望 20/20
```

**合计 306 项**（12 套功能测试；`doc_consistency_test` 是检查器，不计入）。
也可以一次跑完：

```bash
python tools/verify_portable.py       # 可移植性校验
```

**这些只证明「包装器链路」通过，不等于 Claude Code 集成已验证。**
真正要在新会话里手测的六项见 §日常使用。

### 三句话快速自测

```
帮我调研一下这个项目                      → 应被 G1 拦下并解释
帮我调研一下这个项目，agent_spawns: 2     → 应放行 2 个，第 3 个才拦
已经修复完成了                            → 应被 G3 打回要求补证据
```

第 3 条最关键 —— 如果它没被打回，说明 Stop hook 的 schema 在本机不生效，G3 完全失效。

---

## 回滚

```bash
# 项目级
cp "<项目>/.claude/settings.local.json.bak-<时间戳>" "<项目>/.claude/settings.local.json"

# 或者干脆整个删掉
rm -rf "<项目>/.claude/settings.local.json"
```

---

## 日常使用

**大多数时候它应该是隐形的。** 你只会在三种情况下看到它：

1. AI 想派后台 Agent（默认禁止）
2. AI 想执行已知危险操作（`pkill -f` / `find /tmp` / 整行 `rm -rf /` 等）
3. AI 想宣布完成但没给证据

**要放行子代理**，在 prompt 里写一行就行，不用改文件：

```
帮我调研这个项目，agent_spawns: 3
```

口语也认：`可以派 Agent` → 放行 3 个；`不要开后台` → 强制 0 个。

---

## 已知边界（不要当成自动驾驶）

| 能力 | 状态 |
|---|---|
| 控制 Agent 派生 | ✅ 机械强制 |
| 拦截已知危险命令 | ✅ 机械强制（防误操作层，不是安全边界） |
| 要求完成时给证据 | ✅ 后置打回，能查"有没有证据"（**不判断真伪**） |
| Bash / WebFetch 用量 | ✅ 越过阈值时提示 |
| 同一命令重复 3 次 | ✅ 提示（不拦截） |

**准确说法：主要限制「行动方式」，不保证 AI 的判断一定正确。**

### 刻意不做的（边界，不是欠债）

以下能力**原理上不适合由机械门实现**，本项目不做，也不声称能做：

```text
根因判断        需要理解语义 —— 门只能匹配字符串与结构
证据真伪        门能逼你写"在哪看"，查不了那是不是真的
跨工具强制提升    hook 机制由宿主提供，本项目只在 Claude Code 上生效
输出风格        属于产品层，不属于行为约束层
```

它们不是「待办」。写成待办会让人以为将来会做 —— 做了就是过度承诺。

### Threat Model —— 它防谁、不防谁

```text
T1  误操作 / 本地损坏 / 漂移          → 本项目保护目标
T2  有写权限的故意破坏者               → 不防。它不是安全产品
T3  多方审计 / 第三方不可抵赖          → 不防。本地哈希不构成不可抵赖证据
```

本包的 sha256 与清单**只用于 T1 本地比对**，不构成
malicious-writer-proof、不可抵赖、密码学不可篡改 ——
有写权限的人可以同时改文件和清单，本包**发现不了**。

> ⚠️ **需要防 T2 / T3 的场合，本工具不适用** —— 请改用操作系统级沙箱
> 或外部审计系统。不要把本包的结果当作安全边界。

### MCP 通道的字段分派取舍（#53）

MCP 工具的输入要按字段语义分派规则：**命令类字段 → cmd 规则，
内容类字段 → content 规则**。但**字段名判不出字段语义**，所以必须选一边。

当前取舍（按本包 FAIL-OPEN 原则）：

```text
已知命令字段（command / cmd / script / shell / exec / argv / args / code / stdin）
    —— 只收【有明确命令语义】的名字，不含 data / input / payload
       这类通用容器名（它们语义不明，与"名单外字段"同类）
    → 只过 cmd 规则
其他所有字段（含名单外的）
    → 只过 content 规则
```

**已知代价**：若某个 MCP server 用**名单外的字段名**承载真正会被执行的命令，
本规则会漏拦。

**这个代价有多大，取决于你装了什么 server**。实测（本机）：
`mcp__scheduled-tasks__*` 的全部字段是
`taskId / prompt / description / cronExpression / fireAt / enabled` ——
语义是 ID / 文本 / 时间 / 布尔，**没有一个承载 shell 命令**。

> ⚠️ 这条证据只覆盖**本机当前可达**的工具。它**不证明**
> "文本字段永远不会被执行" —— 那取决于各 server 的实现，不在本门可观测范围。
>
> **如果你要装 filesystem / shell / 数据库类 MCP server**，
> 请确认其命令字段名在上面的名单里；不在的话，G5 对那条通道是失效的。

### 三条最容易误读的

1. `判定: MATCH` = 文件与源一致，**不等于**门工作正常（源本身写错，它一样报 MATCH）。
2. **G3 放行 ≠ 结论正确** —— 它只确认你写了证据块，**一个空洞的证据块照样能过**。
   它唯一的好用处是逼出"在哪看、怎么确认"这两栏；那两栏是不是真的，只有人能查。
3. `自检 4/4 通过` = 那四条命令的退出码符合预期，**不等于**整条链路被验证过。

---

## 文件结构

```
├─ install.py                      安装（双作用域，幂等）
├─ install.md                      安装说明（本文件）
├─ CHANGELOG.md                    变更记录
├─ policy/behavior-policy.json     策略内核（工具无关）
├─ adapters/claude-code/
│   ├─ settings.fragment.json      hook 配置片段
│   └─ hooks/                      _lib + 5 个门 + VERSION
├─ lib/                            共用模块（部署清单 / Python 探测）
├─ tools/                          部署校验 / 可移植性校验
└─ tests/                          十三套测试（=306 项）
```

### 五道门

| 门 | 触发 | 强制力 |
|---|---|---|
| **G1 预算** | `PreToolUse(Agent)` | ✅ exit 2 |
| G2 范围 | 提示词层 | ⚠️ 不强制 |
| **G3 生效** | `Stop` | ✅ `{"decision":"block"}` |
| G4 循环 | 提示词层 + 收口记录 | ⚠️ 不强制 |
| **G5 危险** | `PreToolUse(Bash/Edit/Write/MCP)` | ✅ exit 2 |
| **G7 意图** | `PreToolUse(AskUserQuestion/Bash)` | ✅ exit 2 |
| G6 收口 | 旁路采集 | 只记录 + 用量提示，不阻断 |

> ⚠️ **matcher 用 `Agent`，不是 `Task`。**
> 本 harness 里派生工具叫 `Agent`；`Task*` 系列是待办清单工具，
> 写 `Task` 匹配不到任何东西，写 `Task.*` 正则会误伤待办工具。
