# AGENTS.md

本项目供 Claude Code 阅读的项目说明。

---

## 项目概述

**行为门 BehaviorGate** —— 一个 Claude Code hooks 集合，通过机械拦截约束 AI 助手的行动方式。

核心目标：**消灭「静默失败」**。看起来装了门、其实门是假的，是本项目定义的最严重缺陷。

---

## 仓库结构

```text
├─ install.py                      安装脚本（双作用域，幂等）
├─ policy/behavior-policy.json     策略内核（工具无关，适配器读取）
├─ adapters/claude-code/
│   ├─ settings.fragment.json      hook 配置片段（含 {{HOOKS}}/{{PYTHON}} 占位符）
│   └─ hooks/
│       ├─ VERSION                 版本声明（独立事实源）
│       ├─ _lib.py                 共享库（输入解析 / 输出 / 策略加载）
│       ├─ inject_budget.py        G1 注入端
│       ├─ budget_gate.py          G1 强制端
│       ├─ effect_gate.py          G3 生效门
│       ├─ destructive_gate.py     G5 危险门
│       ├─ intent_gate.py          G7 意图门
│       └─ closeout_gate.py        G6 收口 + 用量提示
├─ lib/
│   ├─ deploy_files.py             文件部署清单（唯一事实源）
│   └─ find_python.py              Python 解释器探测
├─ tools/
│   ├─ verify_deploy.py            部署校验
│   └─ verify_portable.py          可移植性校验
└─ tests/                          十三套测试（293 项，用 ls tests/*.py 枚举）
```

---

## 开发命令

```bash
# 运行安装预演（不写文件）
python install.py

# 应用安装
python install.py --apply

# 全部测试（293 项，提交前必须通过）
# ⚠️ 不要照抄这段清单 —— 用 glob 枚举，否则新增套件会被漏掉：
#     ls tests/*.py
python tests/four_gate_selftest.py        # 53
python tests/cmd_chain_test.py            # 21
python tests/adversarial_test.py          # 24
python tests/budget_safety_test.py        # 30
python tests/intent_gate_test.py          # 21
python tests/upgrade_path_test.py         # 19
python tests/g5_real_regression_test.py   # 25  ← 真实会话用例
python tests/source_identity_consistency_test.py  # 13
python tests/gate_events_test.py          # 30
python tests/g5_windows_path_test.py      # 18
python tests/install_cli_test.py          # 12
python tests/mcp_field_dispatch_test.py   # 27
python tests/doc_consistency_test.py      # 9   ← 会递归跑上面全部，并核对文档数字

# 部署校验
python tools/verify_deploy.py
```

**没有依赖安装步骤** —— 本项目零第三方依赖，只用标准库。

---

## 代码约定

### 语言

```text
注释 / 文档字符串 / 提交信息：中文
标识符 / 函数名 / 变量名：英文
```

### 必须遵守的约束

```text
1. 只用标准库
   禁止 import 任何第三方包。无 requirements.txt / pyproject.toml 依赖段。

2. 失败必须可见
   任何 FAIL-OPEN 路径必须调用 warn_inactive() 输出警告。
   静默失败 = 门不存在，这是本项目要消灭的模式。

3. 内容规则必须行首锚定
   匹配 Bash 命令内容的规则若不加 ^ 锚定，会误伤文档中提及规则的句子。
   实测代价：文档里整行含 `rm -rf /` 仍会被拦（可接受的保守）。

4. 唯一阻断码是 exit 2
   exit 1 是【非阻断错误】，工具会照常执行 —— 这是官方文档实测结论。

5. 修改 hook 内容必须同步更新 VERSION
   路径：adapters/claude-code/hooks/VERSION
   这是设计意图：不同步会导致升级时被自己的漂移检查拦下。
```

### 重要陷阱（实测踩过，不要重犯）

```text
- matcher 用 "Agent" 而非 "Task"
  Claude Code 里派生工具叫 Agent；
  Task* 是待办清单工具，写 Task 匹配不到，写 Task.* 会误伤 4 个待办工具。

- Python 路径必须安装时探测，不能写死
  写死 C:/Program Files/Python312 换机后静默失败。

- 不要经 cmd.exe 调用 hook
  Git Bash 会把它带进交互模式，hook 静默失效。
  必须用 python 绝对路径直接调用。
```

---

## 测试要求

**任何改动都必须保证全部套件通过（当前 293 项）。**
⚠️ 不要照抄某个数字或清单 —— 用 `ls tests/*.py` 枚举，
   否则新增的套件会被漏跑（真实发生过：照旧清单跑，漏了 4 个套件）。

新增功能需附带测试。测试文件放在 `tests/`，遵循现有格式。

测试失败时必须查看具体失败项，不要只改断言令其通过。

---

## 不要做的事

```text
❌ 引入第三方依赖
❌ 修改 tests/ 下的断言来「让测试通过」
❌ 在 hook 里写死绝对路径
❌ 让门静默失败（不报错的放行）
❌ 把「能拦」宣传成「能判断对错」—— 边界必须如实声明
❌ 修改 VERSION 但不修改 hook 内容（或反之）
```

---

## 声明边界（重要）

修改文档或提示文案时，必须保持以下边界声明：

```text
本项目限制「行动方式」，不保证「判断正确」。
G3 只检查证据【格式】，不检查证据【真伪】。
G5 是防误操作层，不是安全边界。
失败策略是 FAIL-OPEN（宁可漏拦，不可卡死）。
```

**不要把这些边界写成更强的承诺。** 这是本项目的核心设计原则。
