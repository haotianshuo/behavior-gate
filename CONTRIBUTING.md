# 贡献指南

感谢你有兴趣为本项目做贡献。

## 开发环境

**要求：** Python 3.10+

**零第三方依赖** —— 本项目只使用 Python 标准库。请勿引入外部依赖。

```bash
git clone https://github.com/haotianshuo/behavior-gate.git
cd behavior-gate
```

无需 `pip install`。

## 运行测试

提交前必须全部通过（共 **167 项**）：

```bash
python tests/four_gate_selftest.py    # 53/53
python tests/cmd_chain_test.py        # 21/21
python tests/adversarial_test.py      # 24/24
python tests/budget_safety_test.py    # 30/30
python tests/intent_gate_test.py      # 21/21
python tests/upgrade_path_test.py     # 18/18
```

## 提交变更

1. Fork 本仓库
2. 创建分支：`git checkout -b fix/your-fix`
3. 提交（见下方规范）
4. 推送并创建 Pull Request

### 提交信息规范

本项目使用 [Conventional Commits 1.0.0](https://www.conventionalcommits.org/)：

```text
<type>: <描述>

[可选正文]

[可选 footer]
```

类型：

| 类型 | 用途 |
|------|------|
| `fix` | 缺陷修复 |
| `feat` | 新功能 |
| `docs` | 文档 |
| `test` | 测试 |
| `refactor` | 重构 |

示例：

```text
fix: G5 误拦句子中间的 rm -rf

原规则未做行首锚定，导致文档中提到的
「禁止 rm -rf / 这类命令」也被拦截。

Closes #31
```

## 代码规范

**本项目代码注释使用中文，标识符使用英文。**

### 必须遵守

```text
1. 失败必须可见
   门静默失败 = 门不存在。任何 FAIL-OPEN 必须调用 warn_inactive()。

2. 只使用标准库
   引入任何第三方依赖的 PR 会被拒绝。

3. 规则必须行首锚定
   内容匹配规则不加行首锚定会误伤文档中提及规则的句子。

4. 修改 hook 内容必须同时更新 VERSION
   见 adapters/claude-code/hooks/VERSION。
   这是设计意图 —— 否则升级时会被自己的漂移检查拦下。
```

### 变更 CHANGELOG

本项目 CHANGELOG 只记**真实发生过的问题**，格式为：

```markdown
| # | 问题 | 级别 | 出现于 | 修于 | 谁发现 |
|---|------|------|--------|------|--------|
| 27 | 你修的问题 | 🟡 | 3.5.0 | 3.6.0 | 你 |
```

严重度：🔴 高 / 🟡 中 / ⚪ 低

**请为你的修复分配下一个可用编号。**

## Pull Request 检查清单

```text
[ ] 167 项测试全部通过
[ ] 无新增第三方依赖
[ ] 如改了 hook，已同步更新 VERSION
[ ] 已在 CHANGELOG.md 添加条目
[ ] 代码注释为中文
[ ] 提交信息符合 Conventional Commits
```

## 联系

普通贡献问题请开 GitHub Issue。

私有安全报告请发邮件至 `xrlcom@126.com`，主题注明 `Security report — behavior-gate`；
请勿包含仍在使用的密钥或私有客户数据。

> 该邮箱是**公开的项目联系地址，不是版权人声明**。

## 报告问题

- **缺陷 / 功能建议**：GitHub Issues
- **安全漏洞**：见 [SECURITY.md](SECURITY.md)（**请勿公开**）

## 许可证

提交贡献即表示你同意你的贡献以本项目的 [MIT 许可证](LICENSE) 发布。
