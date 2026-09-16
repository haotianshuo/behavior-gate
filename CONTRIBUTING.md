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

提交前必须全部通过（共 **293 项**）：

```bash
python tests/four_gate_selftest.py        # 53/53
python tests/cmd_chain_test.py            # 21/21
python tests/adversarial_test.py          # 24/24
python tests/budget_safety_test.py        # 30/30
python tests/intent_gate_test.py          # 21/21
python tests/upgrade_path_test.py         # 19/19
python tests/g5_real_regression_test.py   # 25/25  ← 真实会话用例
python tests/source_identity_consistency_test.py  # 13/13
python tests/gate_events_test.py          # 30/30
python tests/g5_windows_path_test.py      # 18/18
python tests/install_cli_test.py          # 12/12
python tests/mcp_field_dispatch_test.py   # 27/27
python tests/doc_consistency_test.py      # 9/9
```

### 改了 hooks 之后

本项目有**源身份**纪律（详见 `tools/source_identity.py`）：

```text
改了 hooks/ 下任何文件（含 VERSION）
  → 必须显式刷新源身份，否则 CI 的只读校验会红：
      python tools/source_identity.py --write
```

⚠️ `install.py` / `verify_deploy.py` / CI **都不会自动刷新** —— 这是设计意图：
若安装过程能自动刷新，一份 dirty source 跑一次安装就把自己「合法化」了。

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
[ ] 293 项测试全部通过
[ ] 无新增第三方依赖
[ ] 如改了 hook，已同步更新 VERSION
[ ] 如改了 hook，已刷新源身份（tools/source_identity.py --write）
[ ] 已在 CHANGELOG.md 添加条目
[ ] 代码注释为中文
[ ] 提交信息符合 Conventional Commits
```

---

## 发布检查清单

> ⚠️ **这一节是踩出来的。** 每一条都对应一次真实的漏发布 ——
> 2026-09-16 那一轮里，「推了代码」被当成了「发布了新版」，
> 结果 Release 上的 zip 还是旧代码，用户下载到的包不含新修复。

### 核心区分

```text
推送代码 ≠ 发布版本
```

**`git push` 只更新 main 分支。用户拿到的包来自 Release 页的 zip ——
那个要单独做，不会自动跟着 main 走。**

### 顺序

```text
[ ] 1. 确认工作区干净、测试全过
        python tests/*.py            # 293 项
        git status --short           # 应为空

[ ] 2. 升版本号（改 hook 就必须升）
        adapters/claude-code/hooks/VERSION
        policy/behavior-policy.json 的 _version
        README.md / install.md / 安装说明.md 的版本行
        → python .github/scripts/check_version_consistency.py

[ ] 3. 刷新源身份
        python tools/source_identity.py --write
        python tools/source_identity.py          # 确认自证通过

[ ] 4. 提交并推送
        git commit && git push origin main

[ ] 5. 打 tag（指向刚才那个提交）
        git tag -a vX.Y.Z -m "..."
        git push origin vX.Y.Z

[ ] 6. 写 Release 说明
        · 只详细写【本版新增】，旧版内容用一行链接指向旧 Release
          ⚠️ 写「完整版」会让两个 Release 看起来一模一样
        · 明写「本版包含上一版全部内容」

[ ] 7. 打包 + 上传
        用 git archive 打包（自动排除 .git、尊重 .gitattributes）
        把 Release 说明同时放进包内：本次更新说明.md
        资产名用 ASCII：behavior-gate-VX.Y.Z.zip
          ⚠️ 中文文件名会被 gh CLI 吃掉（实测：行为门-V3.5.10.zip → -V3.5.10.zip）

[ ] 8. 更新仓库主页
        gh repo edit <repo> --description "..."

[ ] 9. 部署到本机
        python install.py --scope global --apply
```

### 最后一步不能省：下载回验

```text
gh release download vX.Y.Z --pattern "*.zip"
  → 解开，比对包内 hooks 文件的 sha256 与源码
```

**为什么要这一步**：「上传成功」不等于「发布的是新东西」。
实测踩过：`gh release upload` 返回成功，但 zip 里是上一个版本的内容 ——
因为打包时用的是旧的 tag 而不是 HEAD。

```bash
# 一条命令确认
gh release download vX.Y.Z --pattern "*.zip" -D "$TEMP/chk"
python -c "
import zipfile,hashlib,glob,os
z=zipfile.ZipFile(glob.glob(os.path.join(os.environ['TEMP'],'chk','*.zip'))[0])
g=[x for x in z.namelist() if x.endswith('destructive_gate.py')][0]
print(hashlib.sha256(z.read(g)).hexdigest()[:16])
"
# 与源码对比
sha256sum adapters/claude-code/hooks/destructive_gate.py | cut -c1-16
```

### 如果发现已发布的包是错的

**不要强推 tag** —— 已发布的 tag 可能已被人下载。
按「已发布的 tag 不可原地改」处理：

```text
1. 发一个新版本（vX.Y.(Z+1)），包含正确内容
2. 在旧 Release 说明【开头】加一段提醒，指向上一个版本
   （保留原文，只在前面加提醒 —— 不要重写历史）
```

---

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
