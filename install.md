# 行为门安装说明

> 对应版本：**V3.5.0** ｜ 2026-09-14
>
> ⚠️ **它证明不了什么，和它能拦什么同样重要 —— 见下面的「已知边界」。**
> 一句话：门管的是「行动方式」和「格式」，不是「判断对不对」。
> `判定: MATCH` / `自检通过` / `证据：L3` 都**不等于**内容已被核实。
> 当前实测：四关 50/50、安装链路 21/21、对抗 21/21、预算安全 23/23、
> 　　　　　意图门 21/21、升级路径 18/18（=154 项）+ 1:1 复刻 13/13
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
python tests/four_gate_selftest.py    # 期望 50/50
python tests/cmd_chain_test.py        # 期望 21/21
python tests/adversarial_test.py      # 期望 21/21
python tests/budget_safety_test.py    # 期望 23/23
python tests/intent_gate_test.py      # 期望 21/21
python tests/upgrade_path_test.py     # 期望 18/18
```

**合计 154 项。** 也可以一次跑完：

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
| 拦截已知危险命令 | ✅ 机械强制（但只是防误操作层，**不是安全边界**） |
| 要求完成时给证据 | ⚠️ 后置打回，能查"有没有证据"、**不能查证据对不对** |
| Bash / WebFetch 硬限额 | ❌ **没有**（只记录，不拦截） |
| 真正的根因判断 | ❌ 仍靠模型自己 |
| 重复会话 | ❌ 未解决 |
| 输出风格 | ❌ 未解决 |
| 跨 Codex / Cursor / opencode | ❌ **当前没有强制提升** |

**准确说法：主要限制「行动方式」，不能保证 AI 的判断一定正确。**

### Threat Model —— 它防谁、不防谁

```text
T1  误操作 / 本地损坏 / 漂移          → 当前保护目标
T2  有写权限的故意破坏者               → NOT_PROTECTED / NOT_VERIFIED
T3  多方审计 / 第三方不可抵赖          → OUT_OF_SCOPE
```

本包的 sha256 与清单**只用于 T1 本地比对**，不得被描述成
malicious-writer-proof、不可抵赖、密码学不可篡改 ——
有写权限的人可以同时改文件和清单，本包**发现不了**。
这不是缺陷，是边界没说清。

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
└─ tests/                          六套测试（=154 项）
```

### 五道门

| 门 | 触发 | 强制力 |
|---|---|---|
| **G1 预算** | `PreToolUse(Agent)` | ✅ exit 2 |
| G2 范围 | 提示词层 | ⚠️ 不强制 |
| **G3 生效** | `Stop` | ✅ `{"decision":"block"}` |
| G4 循环 | 提示词层 + 收口记录 | ⚠️ 不强制 |
| **G5 危险** | `PreToolUse(Bash/Edit/Write/MCP)` | ✅ exit 2 |
| G6 收口 | 旁路采集 | 只记录，不阻断 |

> ⚠️ **matcher 用 `Agent`，不是 `Task`。**
> 本 harness 里派生工具叫 `Agent`；`Task*` 系列是待办清单工具，
> 写 `Task` 匹配不到任何东西，写 `Task.*` 正则会误伤待办工具。
