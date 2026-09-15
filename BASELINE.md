# 已验证基线

> 本文件记录**已经完整闭环验证**的状态。标记为 BASELINE 的内容，
> 在下一轮 change set 里**不得顺手修改** —— 要改必须开新的 change set 并重新走完整验证。

---

## BASELINE-3.5.10（冻结于 2026-09-15）

### 包含的变更

```text
#48  G5 rm-family fix
       · rm_rf_traversal / rm_rf_windows_drive / rm_rf_windows_env /
         rm_rf_in_content 四条规则共享 _RM_OPTS 选项解析片段
       · 补结尾锚定（修前缀贪婪误报）
       · 认短选项分离与大写 R（修漏报）
       · MCP 通道改为逐字段扫描 + 按字段语义分派规则

#49  source / deploy identity 分层
       · DEPLOYED_INTEGRITY（已装副本 vs 安装时清单）
       · SOURCE_IDENTITY（源码自证快照）
       · 四层固定判定顺序，前三层失败不得被「升 VERSION」绕过
       · LEGACY_BASELINE_MIGRATION 一次性严格迁移
```

### 验证状态（全部实测）

```text
H1 IMPLEMENTATION           PASS
H2 DEPLOYMENT               PASS
H3 RUNTIME MATCH            PASS
H4 RUNTIME SMOKE            PASS

tests                       206/206
state matrix                18/18
runtime smoke               20/20
CI（version/deps/dead/源身份） 4/4
```

### 身份（可机械核对）

```text
VERSION                 3.5.10
SOURCE_IDENTITY.snapshot b355b9b38e6fb394...
DEPLOY_MANIFEST.status   MATCH
部署 sourceSnapshot      == 源身份 snapshot   （一致）
```

### 本轮禁止修改（要改必须开新 change set）

```text
G1 策略
G3 策略
G5 判定规则
G7 策略
source identity 判定
deploy identity 判定
VERSION 语义
```

---

## 已知开放边界（有意保留，非欠债）

| 边界 | 状态 |
|---|---|
| `posix_tmp_on_windows` 拦 `/tmp/...` | 有意保留。Git Bash 下 `/tmp` → `C:\tmp` 是真实陷阱。实测归因：`rm -rf /tmp/v350` → `rule=posix_tmp_on_windows`，**不是** rm_rf 族回归 |
| 写入内容里的 Windows 盘符删根 | 有意保留。content 字段只过 `content_deny`，`rm_rf_in_content` 不含盘符。Edit/Write 一直如此，已用测试锁住 |
| `rm_rf_in_content` 多行覆盖 | 未修。`^[ \t]*` 无 `re.M` → 「脚本第 2 行是删根命令」拦不到。既有行为 |

---

## 独立问题登记（OPEN / SEPARATE）

> 本节只**记录**，不在其他 change set 里顺手修 —— 混在一起会破坏归因。

### ISSUE-1：`install.py --global` 与文档不一致

```text
CONFIRMED

文档 / 预期 CLI：
    install.py --apply --global

实际实现：
    resolve_scope() 读的是 arg_value("--scope", "project")
    → 需要 --scope global
    → --global 被当作无关参数忽略

影响：
    用户按 README 执行会【静默装到项目级】，而非全局。
    实测坐实：本轮 H2 用 `--apply --global` 时，
    安装目标变成了 <cwd>/.claude/，不是 ~/.claude/。

证据：
    install.py:88-91  resolve_scope()
    README.md         "python install.py --apply --global"

状态：OPEN / SEPARATE
    本轮（第 7 步 observability）【不修】。
    它有真实证据，可在后续 change set 里独立评估。
```

### ISSUE-2：`check_no_deps.py` 的 LOCAL 白名单需随新模块维护

```text
CONFIRMED（本轮已随 #49 修复，登记以免再犯）

现象：
    新增 lib 模块后 CI 零依赖检查误报为「第三方依赖」。

已修：
    LOCAL 白名单补入 src_identity / console_utf8 / source_identity。

状态：CLOSED（随 #49）
    教训：加新 lib 模块时记得同步白名单。
```
