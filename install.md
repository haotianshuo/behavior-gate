# 安装与升级

> 对应版本：**V3.5.17** ｜ 2026-10-01

本文说明如何选择安装范围、保留已有配置、校验部署，以及在需要时人工恢复。项目能力与配置入口见 [README](README.md)，本版变化见 [发行说明](docs/releases/v3.5.17.md)。

## 环境要求

- 已安装 Claude Code，并允许相应作用域加载 hooks。
- Python **3.10 或更高**；CI 覆盖 Python 3.10、3.12。
- Windows、macOS 或 Linux。
- 机械检查只依赖 Python 标准库，不需要安装第三方 Python 包。
- 默认开启的语义评审需要可用的模型调用配置；它不是完全离线的功能。关闭方式见 README。

先确认解释器可用：

```powershell
python --version
```

下载 [最新发行包](https://github.com/haotianshuo/behavior-gate/releases/latest)，核对校验和，解压到独立目录。在该目录运行后续安装命令。**发行包目录不是你要开发的目标项目**；请显式指定目标。

## 选择安装范围

| 范围 | 配置位置 | 适合的情况 |
| --- | --- | --- |
| 项目级，推荐首次使用 | `<目标项目>/.claude/settings.local.json` | 先在一个项目内了解行为与配置 |
| 全局 | `<用户目录>/.claude/settings.json` | 希望所有 Claude Code 项目使用相同约束 |

项目级安装不等于完全隔离：Claude Code 仍可能加载全局 hooks、企业策略或其他项目配置。请检查实际加载情况，不要仅凭安装目录判断执行链路。

### 项目级安装

在发行包目录运行，将示例路径替换为你已有的项目：

```powershell
python install.py --scope project --project "D:\your-project"
python install.py --scope project --project "D:\your-project" --apply
```

macOS / Linux 可使用相同参数，将路径换为 `/path/to/your-project`。省略 `--project` 时，默认目标是当前工作目录；因此，不建议直接在发行包目录运行不带目标的安装命令。

### 全局安装

```powershell
python install.py --scope global
python install.py --scope global --apply
```

不带 `--apply` 的命令是预演，不写安装文件。预演展示计划，不代表所有部署完整性检查或宿主验证都已经通过。

## 安装会修改什么

安装器将部署受管理 hooks、版本文件、源码身份对应的部署清单，并将本方案条目合并到目标 settings 中。

重要行为：

- 现有 settings 会备份；第三方 hook 条目应保留。
- 已有 `behavior-policy.json` 默认保留，不因仓库默认策略更新而被覆盖。
- 安装器会清理它识别出的旧自有 hook 条目、废弃自有文件及超出保留数量的自有备份。**它不是“只新增、从不删除”的复制器。**
- `--force` 可能覆盖配置或绕过正常漂移处理；不要为了消除警告直接添加该参数。
- 升级遇到未知漂移、身份不一致或第三方冲突时，应停下核对，而不是继续覆盖。

安装或升级前，建议另存一份独立备份，至少包含目标作用域的：

```text
settings.json 或 settings.local.json
hooks/
behavior-policy.json
DEPLOY_MANIFEST.json
```

备份放在安装目录之外，并保留文件清单或校验和。不要只依赖安装器保留的少量历史备份。

## 安装后验证

### 1. 检查安装输出

确认命令正常结束，并阅读安装器的文件校验与自检结果。安装器自检是脚本级检查，不能替代 Claude Code 的实际调用。

### 2. 比对部署文件

在发行包目录运行：

```powershell
python tools/verify_deploy.py --source "adapters/claude-code/hooks" --installed "D:\your-project\.claude\hooks" --require-complete
```

Windows PowerShell 下验证全局安装：

```powershell
python tools/verify_deploy.py --source "adapters/claude-code/hooks" --installed "$env:USERPROFILE\.claude\hooks" --require-complete
```

macOS / Linux 下验证全局安装：

```sh
python tools/verify_deploy.py --source "adapters/claude-code/hooks" --installed "$HOME/.claude/hooks" --require-complete
```

`MATCH` 表示这组文件符合比对要求，**不是**“所有约束正确、所有告警可见”的证明。若检查失败，保留输出，并按具体缺失或漂移核对。

### 3. 确认真实宿主调用

重新启动 Claude Code，在目标项目中新建一个会话。用你本来就需要执行的小任务观察约束，并核对会话、时间与事件是否对应。

- 验证子代理预算时，必须实际触发 `Agent` 工具调用；仅发送“调研项目”不能保证助手会派子代理。
- 验证 G3 时，对象是助手自己的完成声明，不是用户在消息里说“已经完成”。
- 不要为了验证危险门，在真实项目执行破坏性命令。
- 告警进入 stdout、stderr 或模型上下文，不等于用户在普通界面能看到；界面效果要单独观察。

默认事件文件位于系统临时目录下的 `claude-behavior-gates/gate-events.jsonl`。它记录门的决策信息，不是完整任务审计。需要长期评估时，请按配置选择稳定存储位置。

## 配置与日常使用

策略读取顺序及完整说明见 [README 的配置部分](README.md#配置)。安装后修改配置时，确认你编辑的是**实际被加载**的那份策略。

常见任务约束可以直接写在消息中：

```text
agent_spawns: 2
这次不要测试，也不要再提问。
```

需要重新允许时，明确表达新授权；项目也支持：

```text
questions: on
verify: on
```

这些参数约束助手的行动，不取代工具权限、沙箱、代码审查或业务验收。不要把允许某个动作当成授权助手修改任意文件或部署系统。

## 升级已有安装

1. 下载新包到独立目录，不覆盖正在使用的源码目录。
2. 保存上述独立备份，包含 hooks **和部署清单**。
3. 显式选择原安装作用域与目标，先运行预演。
4. 执行正常 `--apply`；若出现漂移或冲突，停下保留证据。
5. 运行文件一致性检查，再新建 Claude Code 会话验证实际调用。

已有策略默认不覆盖。新版本的默认值或新配置项需要你逐项比较，不应为了让策略中的版本文字一致而直接替换整份用户配置。

## 人工恢复与停用

**v3.5.17 没有自动回滚或卸载命令。** `install.py --rollback` 会明确拒绝执行；不要把它当成恢复入口。

恢复时：

1. 结束受影响的 Claude Code 会话。
2. 从同一次安装前备份恢复受管理 hooks、对应 `DEPLOY_MANIFEST.json`，以及需要恢复的策略。
3. 恢复 settings 前，比较安装后的其他变化；如果后来增加了第三方 hooks，应做定向合并，而不是覆盖掉它们。
4. 重新启动，检查恢复后的文件身份和实际 hook 调用。

只恢复旧 hooks 而保留新 manifest，会产生错误的源码身份标签。只恢复 settings 也不等于完整恢复。

需要停用时，可定向移除本方案注册的 hook 条目。**不要删除整份 settings、整个 `.claude` 目录或第三方 hooks。** 如果无法判断归属，先保留配置并反馈，不要批量清理。

## 排查顺序

| 现象 | 先检查什么 |
| --- | --- |
| 安装命令无法启动 | Python 版本、路径与当前目录 |
| 文件校验不一致 | 安装目标、缺失文件、是否手工改过 hooks、源身份 |
| 配置似乎没有变化 | 策略优先级、是否保留了原用户策略 |
| 语义评审超时或失败 | 模型配置与通道、请求超时、故障输出 |
| hooks 没有执行 | 宿主版本、配置加载、项目信任与作用域 |
| 找不到某条告警 | 输出通道、退出码、普通界面和展开日志分别观察 |

反馈问题时请附版本、系统、最小复现和脱敏输出。不要上传模型密钥、完整私人会话或未经检查的策略与日志。

## 已知边界

BehaviorGate 限制行动方式，不保证判断正确。G3 检查格式而非真伪，G5 是防误操作层而非安全边界。FAIL-OPEN 路径可能使部分约束失效；文件哈希只用于本地一致性检查，不防有写权限的人同时改动文件与清单。

完整能力边界见 [README](README.md#安全与能力边界)。请把包内测试、真实宿主行为、界面可见性和业务收益分开验收。

### MCP 字段分派

MCP 工具输入按字段名选择规则：已知命令字段，如 `command`、`cmd`、`script`、`argv`，进入命令规则；其余字段进入内容规则。字段名不能保证字段的实际执行语义。

如果某个 MCP server 用名单外字段承载可执行命令，该通道可能漏拦。使用文件系统、Shell 或数据库类 server 时，应单独确认其输入字段与执行行为，不能把 G5 的检查当成这些服务的权限控制。

## 开发者检查

发行包包含完整检查工具与测试。需要验证包本身时，串行运行：

```powershell
python tools/preflight_release.py
```

不要同时启动多个全量预检，以免资源竞争干扰耗时敏感测试。源码与包内回归通过，不等于所有真实宿主和业务场景都通过。

<details>
<summary>完整测试清单：19 套测试，功能回归 494 项，文档检查另计</summary>

| 脚本 | 用例 |
| --- | ---: |
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
| `tests/doc_consistency_test.py` | 20，单独计数 |

在线模型测试默认不启用；以实际输出区分执行、跳过与已知未解决项。

</details>
