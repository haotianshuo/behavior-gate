# BehaviorGate · 行为门

**为 Claude Code 增加可配置的开发行为约束：控制子代理、检查已知危险操作、尊重询问与测试约定，让完成声明附带可检查的证据。**

[![CI](https://github.com/haotianshuo/behavior-gate/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/haotianshuo/behavior-gate/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](#运行要求)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

> 当前版本：**V3.5.17** ｜ 2026-10-01

[下载发行包](https://github.com/haotianshuo/behavior-gate/releases/latest) · [快速开始](#快速开始) · [安装与升级](install.md) · [本版更新](docs/releases/v3.5.17.md) · [参与贡献](CONTRIBUTING.md)

## 为什么使用 BehaviorGate

编码助手能完成越来越复杂的工作，但“理解了要求”和“执行时遵守要求”并不是同一件事。你可能只授权了一次修改，助手却开始派生子代理；你说不需要测试，它仍然启动测试；你需要经过验证的结果，却只收到一句“已经完成”。

BehaviorGate 把部分开发约定接入 Claude Code 的工具执行与收尾流程。它通过本地 hooks 检查可明确表达的规则，并在需要理解自然语言时使用可关闭的语义评审。

它的目标是让开发过程更可控、结果更容易核对，**不是代替模型编程，也不是替你判断代码一定正确**。

### 适合的使用场景

- 明确控制子代理的使用范围与数量。
- 避免助手在你已表达意图后反复询问或擅自启动测试。
- 让“完成、修复、验证通过”等声明附带位置、方法和未验证项。
- 在执行前发现部分已知危险命令或误操作模式。
- 通过本地事件记录复查规则产生的拦截与摩擦。

如果需要隔离不可信代码、保护生产数据或建立强制安全边界，应使用宿主权限、沙箱和操作系统级控制；BehaviorGate 只能作为补充。

## 核心能力

| 能力 | 作用 | 边界 |
|---|---|---|
| **子代理预算 · G1** | 检查代理派生授权与预算，限制嵌套派生 | 默认派生预算为 `0`；不限制主模型的判断能力 |
| **完成声明检查 · G3** | 对完成类声明检查证据结构与风险级别 | 检查格式，不验证证据真伪或业务结果 |
| **危险模式检查 · G5** | 检查已登记的危险命令模式和部分写入内容规则 | 防误操作，不是完整命令分析器或安全沙箱 |
| **询问与测试约束 · G7** | 识别并检查“不再询问”“不运行测试”等要求 | 不是通用需求理解，不保证任意自然语言约束都能执行 |
| **收口与用量观测 · G6** | 采集相关活动、记录用量、生成阈值与重复提醒 | 提醒不等于硬限额，也不等于已避免事故 |

G1、G5 等执行检查主要使用本地规则。G3、G7 的部分自然语言判断可交给语义评审；两者是不同层次，不能把模型判断当成确定性的安全保证。

## 运行要求

- **Python 3.10 或更新版本**。
- 支持 hooks 的 **Claude Code**。
- Windows、macOS 或 Linux。
- **零第三方 Python 依赖**：本地脚本只使用标准库，无需安装额外的 Python 包。

默认开启的语义评审另需可用的 Anthropic 兼容模型通道，并可能产生模型调用费用。只使用本地规则时可以关闭它，详见[语义评审与隐私](#语义评审与隐私)。

## 快速开始

建议先在一个项目中安装，确认行为符合工作习惯后，再考虑全局使用。

### 1. 下载并解压

从 [GitHub Releases](https://github.com/haotianshuo/behavior-gate/releases/latest) 下载 `behavior-gate-V3.5.17.zip`，解压后进入包含 `install.py` 的目录。

发行包同时提供 `SHA256SUMS.txt`；校验和用于发现下载损坏，不是独立的供应链签名。

### 2. 明确指定目标项目

以下命令在**发行包目录**中执行。将 `D:\your-project` 替换为你实际使用 Claude Code 的项目目录，**不是解压目录**。

```powershell
python --version

# 预演：显示安装计划，不写入配置
python install.py --scope project --project "D:\your-project"

# 安装：确认目标正确后执行
python install.py --scope project --project "D:\your-project" --apply
```

在 macOS / Linux 上，将路径换成自己的项目路径；如果 Python 命令是 `python3`，对应替换 `python` 即可。

项目级安装写入目标项目的 `.claude/hooks/`、`.claude/settings.local.json` 等文件。安装器维护本方案条目并保留第三方 hook；它仍会更新配置，因此应检查计划和备份。

**预演只展示安装计划，不替代部署完整性检查。** 若正式安装报告漂移或冲突，先确认原因，不要直接用 `--force` 覆盖。

明确需要影响所有项目时，可选择全局安装；执行前先阅读[备份与升级说明](install.md)：

```powershell
python install.py --scope global
python install.py --scope global --apply
```

### 3. 重新启动 Claude Code

在目标项目中重新启动 Claude Code，然后正常提出开发需求。全局安装、升级、备份及恢复说明见 [install.md](install.md)。

### 4. 使用你需要的约束

默认不允许派生子代理。需要时，在本次请求中明确给出预算：

```text
请分析这两个模块的依赖关系，可以使用最多两个子代理。
agent_spawns: 2
```

限制询问或测试时，可以直接表达：

```text
只修改这段文案，不要运行测试，也不要再向我提问。
```

G7 当前主要处理询问与验证活动。不要由此推断“保持布局”“不要改变业务合同”等所有需求也会受到强制保护。

若要重新允许相应活动，可以明确覆盖：

```text
questions: on
verify: on
```

## 完成声明如何检查

当助手宣称工作已完成、已修复或已验证时，G3 会检查是否提供相应的证据结构。格式整理应由助手完成，用户不需要手工编写一套报告。

```text
证据：L2 动态
风险级别：中

- 用户下次会看到什么不同：说明本次实际变化
- 在哪看：文件、页面或输出的位置
- 我怎么确认它生效了：执行的方法与观察到的结果
- 未验证项：仍未覆盖的条件；没有时明确说明
```

证据级别用于区分静态检查、动态验证与端到端验收。**填写了级别标签，不会自动提高证据可信度。** G3 不能检查截图是否真实、测试是否确实执行，也不能替你验收业务结果。

## 配置

安装后的策略文件位于对应作用域的 `.claude/behavior-policy.json`。要调整当前项目的行为，应修改**正在被该项目 hooks 使用的文件**，而不是只修改下载包里的默认副本。

查找顺序为：

1. `CLAUDE_BEHAVIOR_POLICY` 指定的文件。
2. hooks 所在作用域的 `.claude/behavior-policy.json`。
3. 用户目录的 `.claude/behavior-policy.json`。
4. 未找到可用文件时使用内置默认值。

以下是配置字段示例，**不是要求整份替换现有文件**：

```json
{
  "budget": {
    "agent_spawns": 0,
    "agent_depth": 1,
    "bash_warn_at": 300,
    "webfetch_warn_at": 50
  },
  "effect_gate": { "enabled": true },
  "destructive_gate": { "enabled": true },
  "closeout": { "enabled": true },
  "protocol_card": { "enabled": true },
  "semantic_review": { "enabled": true }
}
```

| 配置 | 含义 |
|---|---|
| `budget.agent_spawns` | 代理派生预算；请求中的明确授权也会参与判断 |
| `budget.agent_depth` | 子代理嵌套深度限制 |
| `budget.bash_warn_at` / `webfetch_warn_at` | 用量提醒阈值，不是工具调用硬限额 |
| `effect_gate.enabled` | 开关完成声明检查 |
| `destructive_gate.enabled` | 开关危险模式检查 |
| `closeout.enabled` | 开关收口与相关用量观测 |
| `protocol_card.enabled` | 开关任务理解与验证协议的上下文注入；它不是强制门 |
| `semantic_review.enabled` | 开关语义评审 |

G5 支持自定义命令和内容规则。**当前非空自定义规则数组会替换相应内置规则，而不是自动追加。** 修改前请阅读默认策略中的字段说明，并验证原有保护是否仍然存在。

## 语义评审与隐私

语义评审用于减少词表对自然语言的误读，例如区分“分别测试”和“别测试”，以及判断完成类文字是在声明结果还是引用别人的话。

### 开启时会发生什么

- G7 可提交本轮相关用户文本；G3 可提交待判断的助手声明、收尾文字及相关上下文片段。
- 请求使用环境配置中的模型通道，例如 `ANTHROPIC_BASE_URL`，并使用对应认证；插件不替你创建账户或修改凭据。
- 评审是独立、只读的文本调用，没有工具执行权限。
- 调用可能增加等待时间和费用；次数与耗时取决于事件、文本、配置和模型服务，不能视为每轮固定开销。

不自动附带完整项目文件或完整会话历史，**但待评审文本里如果含有敏感内容，该片段仍可能发送给配置的模型端点**。使用前应确认该端点适合处理你的数据。

### 只使用本地规则

在正在生效的策略文件中设置：

```json
{
  "semantic_review": {
    "enabled": false
  }
}
```

关闭后，相关理解路径回到本地词表，不再产生语义评审请求；本地 hooks 本身仍有执行成本。

如需单独指定评审模型，可设置 `semantic_review.model`，模型必须由当前端点支持。未设置时，程序会尝试从宿主环境解析模型。

### 调用失败时

超时、通道不可用或响应不合规时，不伪造评审结论。G7 按既有故障策略保留**能够读取的既有约束**，不据此新增禁令；状态读取或保存失败仍可能影响约束的延续。

插件生成的 stdout 上下文、stderr 告警与用户界面显示不是同一件事。**故障文本已生成，不等于用户已在界面上看见它。** 显示效果需要结合具体 Claude Code 版本验收。

## 检查安装与运行状态

在发行包目录中，针对你刚才安装的**同一个项目**执行：

```powershell
python tools/verify_deploy.py --source "adapters/claude-code/hooks" --installed "D:\your-project\.claude\hooks" --require-complete
```

如果是全局安装，Windows PowerShell 对应为：

```powershell
python tools/verify_deploy.py --source "adapters/claude-code/hooks" --installed "$env:USERPROFILE\.claude\hooks" --require-complete
```

`MATCH` 表示源码身份、受管理部署文件、清单及相关配置检查一致。它**不证明门一定在宿主中触发，不证明告警界面可见，也不证明拦截判断正确**。

本地代码测试、部署一致性、真实宿主触发和实际使用效果是不同的检查，不能互相替代。

## 事件记录与复查

事件文件位于状态目录中的 `gate-events.jsonl`。默认状态目录是系统临时目录下的 `claude-behavior-gates`；需要长期保留时，可设置 `CLAUDE_BUDGET_STATE_DIR` 指向持久化位置。

记录用于观察门的决定与评审状态，不是完整操作流水，也不是防篡改审计系统。未记录某个操作不代表该操作已经过检查。

发行包提供两个只读分析工具：

```powershell
# 查看事件统计及分组
python tools/gate_stats.py --cohort

# 导出样本，人工标注后再评分
python tools/guardrail_triage.py sample
python tools/guardrail_triage.py score
```

复查时应固定**源码快照、流量用途和时间窗**。用途需要外部依据，不能把默认 `NATURAL` 标签直接当成真实业务；拦截数量也不能换算成“避免事故数量”。

只检查被拦样本，不能据此估计全部漏拦，也不能证明净效率收益。

## 安全与能力边界

- **不是安全边界。** G5 只覆盖已登记的写法与输入范围；等价 Python、Node 或其他执行路径可能不受同样检查。
- **不是质量验收器。** G3 检查证据格式，不验证测试结果、数据真实性或最终正确性。
- **不是通用需求执行器。** G7 当前重点是询问与测试，不承诺理解和强制执行全部业务要求。
- **不是无故障的强制系统。** 存在 FAIL-OPEN 路径；评审、状态或宿主故障可能使部分保护失效。
- **不防有写权限的恶意操作者。** 本地文件、日志与清单可被共同修改，哈希不提供第三方不可抵赖性。
- **当前针对 Claude Code。** 不宣称已在 Codex、Cursor 或其他助手中提供同等宿主级控制。
- **效率收益尚未得到真实业务验证。** 测试通过、拦截发生和开发效率提升是不同结论。

权限管理、沙箱、代码审查、浏览器验收与业务验证仍应由相应工具和流程承担。

## 开发与测试

在仓库或发行包目录中执行：

```powershell
python tools/source_identity.py
python tools/preflight_release.py
```

源码身份工具默认只读。preflight 运行本机检查与测试，不替代跨平台 CI 或真实宿主验收。在线模型测试默认不启用，不能把未执行的在线测试算作通过。

<details>
<summary>维护者：测试套件与计数</summary>

当前十九套测试中，18 套功能测试合计 **494 项**，文档一致性检查自身 **20 项**另计。数字用于维护检查覆盖，不代表产品可靠性百分比。

| 测试 | 用例数 |
|---|---:|
| `tests/four_gate_selftest.py` | 57 |
| `tests/cmd_chain_test.py` | 21 |
| `tests/adversarial_test.py` | 25 |
| `tests/budget_safety_test.py` | 44 |
| `tests/intent_gate_test.py` | 43 |
| `tests/upgrade_path_test.py` | 19 |
| `tests/g5_real_regression_test.py` | 42 |
| `tests/source_identity_consistency_test.py` | 13 |
| `tests/gate_events_test.py` | 33 |
| `tests/g5_windows_path_test.py` | 18 |
| `tests/install_cli_test.py` | 19 |
| `tests/mcp_field_dispatch_test.py` | 27 |
| `tests/g3_attribution_regression_test.py` | 31 |
| `tests/semantic_intent_test.py` | 21 |
| `tests/semantic_contract_test.py` | 47 |
| `tests/semantic_g3_test.py` | 2 |
| `tests/g7_state_disclosure_test.py` | 16 |
| `tests/gate_stats_cohort_test.py` | 16 |
| `tests/doc_consistency_test.py` | 20，另计 |

构造用例、真实会话回归和在线层的证据性质不同。[CHANGELOG.md](CHANGELOG.md) 保留 136 项真实问题及修复记录；贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

</details>

## 文档导航

| 文档 | 用途 |
|---|---|
| [安装与升级](install.md) | 作用域、配置保留、校验、备份及人工恢复 |
| [新电脑安装说明](安装说明.md) | 在另一台电脑下载发行包后开始使用 |
| [v3.5.17 发行说明](docs/releases/v3.5.17.md) | 本版变化、升级提醒与已知限制 |
| [完整变更记录](CHANGELOG.md) | 历史问题、修复和未解决项 |
| [贡献指南](CONTRIBUTING.md) | 开发规则、回归测试与提交流程 |
| [安全说明](SECURITY.md) | 威胁模型与私下报告渠道 |

## 反馈与贡献

欢迎提交可复现的问题、规则误拦案例和文档改进。报告问题时请附上插件版本、系统、Python / Claude Code 版本及最小复现，并移除私有路径、密钥和业务数据。

普通问题请使用 [GitHub Issues](https://github.com/haotianshuo/behavior-gate/issues)。疑似安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要公开发布敏感细节。

## 许可证

[MIT](LICENSE)
