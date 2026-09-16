# -*- coding: utf-8 -*-
"""
安装脚本 —— 支持【项目级】和【全局】两种作用域。

为什么要有项目级（这不是可选项）：
    V2.2 的结论是「可以 Shadow 试跑，不建议直接全局安装」。
    但只支持全局安装的工具，等于逼用户在"全局装"和"手改 JSON"之间选 ——
    这与结论直接矛盾。所以项目级是必须的，而且是推荐默认。

作用域对比：
    项目级  <project>/.claude/hooks/ + <project>/.claude/settings.local.json
            只影响这个项目；settings.local.json 通常不进 git；最容易回滚。
            ← 推荐用于 Shadow 试跑
    全局    ~/.claude/hooks/ + ~/.claude/settings.json
            影响所有项目。等 Shadow 跑完 6 项真实测试再考虑。

行为：
    - 先备份目标 settings 文件
    - 只【新增】hook 条目，不删除、不覆盖任何已有条目
    - 幂等：重复运行不会重复添加
    - 打印回滚命令

用法：
    python install.py                     预演（项目级，不写文件）
    python install.py --apply             安装到当前项目
    python install.py --scope global      预演全局
    python install.py --scope global --apply
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("USERPROFILE") or os.path.expanduser("~")

sys.path.insert(0, os.path.join(HERE, "lib"))
try:
    from find_python import find_python
except Exception:
    def find_python(verbose=False):
        return sys.executable, None, "当前解释器", None

# 「哪些文件属于被同步的集合」只能有一处定义。verify_deploy.py 的 scan_dir
# 也从同一个模块取 —— 两边各写一份规则，已经不一致过两次。
from deploy_files import is_tracked  # noqa: E402

# --- 控制台编码（可移植性）---
# Windows 控制台默认 GBK(cp936)；print 中文时遇到非 GBK 字符会抛
# UnicodeEncodeError，脚本直接崩。实测：CI 在 windows-latest 上必崩，
# ubuntu/macos 正常 —— 本地 Git Bash 恰好是 UTF-8，所以看不见。
# 详见 lib/console_utf8.py。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
# --- end ---


SRC_HOOKS = os.path.join(HERE, "adapters", "claude-code", "hooks")
# 元数据层目录（含 SOURCE_IDENTITY.json）。
# ⚠️ 它【不】随 hooks 复制到部署位置 —— 它是源侧身份，
#    复制过去会让部署副本自称"我是某份源码"，语义就错了。
PKG_DIR = os.path.join(HERE, "adapters", "claude-code")
SRC_FRAG = os.path.join(HERE, "adapters", "claude-code", "settings.fragment.json")
SRC_POLICY = os.path.join(HERE, "policy", "behavior-policy.json")

APPLY = "--apply" in sys.argv
FORCE = "--force" in sys.argv

# 本安装器认识的参数（含无值的开关）。
# ⚠️ 这张表必须与下面 parse_args 的实际读取保持一致 ——
#    它是「未知参数报错」的判据来源。
KNOWN_FLAGS = {
    "--apply", "--force", "--rollback", "--write-manifest",
    "--scope", "--project", "--global", "--help", "-h",
}


def check_unknown_flags(argv):
    """拦下不认识的参数 —— 不要静默忽略。

    为什么必须有这个（实测坐实的缺陷）：
        README 里写的是 `python install.py --apply --global`，
        而实现只读 `--scope global`。`--global` 从来没被解析过 ——
        arg_value 找不到就返回默认值，于是【静默降级成项目级】，
        而且因为 --project 默认取当前目录，会装进【包自己所在的目录】。

        用户以为装到全局了，重启后门完全不生效，且没有任何提示。
        这是「照着文档做反而做错」那一类。

    静默忽略未知参数是根因 —— 不只是 --global 一个参数的问题。
    报错而不是忽略，以后再加参数时不会再重演同一个形状。

    返回：错误信息列表（空 = 没有未知参数）
    """
    bad = []
    for a in argv:
        if a.startswith("-") and a not in KNOWN_FLAGS:
            bad.append(a)
    return bad


def arg_value(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
            return sys.argv[i + 1]
    return default


def echo(*a):
    print(*a)


def resolve_scope():
    """返回 (scope, claude_dir, settings_path, hooks_dir, label)"""
    # `--global` 是 `--scope global` 的别名。
    # README 曾经只写 `--global`（另两份文档写 `--scope global`）——
    # 让两种写法都能工作，是为了不辜负已经照旧文档做过的人。
    if "--global" in sys.argv:
        scope = "global"
    else:
        scope = (arg_value("--scope", "project") or "project").lower()

    if scope == "global":
        d = os.path.join(HOME, ".claude")
        return ("global", d, os.path.join(d, "settings.json"),
                os.path.join(d, "hooks"), "全局（影响所有项目）")

    # 项目级
    proj = arg_value("--project") or os.getcwd()
    d = os.path.join(proj, ".claude")
    # settings.local.json 而非 settings.json ——
    # settings.json 通常进 git（团队共享），local 是本机个人的，更适合试用
    return ("project", d, os.path.join(d, "settings.local.json"),
            os.path.join(d, "hooks"), "项目级（只影响 %s）" % proj)


def build_fragment(hooks_dir, python_exe=None):
    """生成 hook 配置。

    两个占位符都要替换：
      {{HOOKS}}  → hooks 目录（绝对路径 + 正斜杠）
      {{PYTHON}} → Python 解释器（安装时探测出来的，不是写死的）

    ⚠️ 为什么 Python 路径必须探测而不是写死：
        原来硬编码 `C:/Program Files/Python312/python.exe`。
        换台电脑可能是 Python311 / C:\\Python312 / py 启动器 / 没装 ——
        写死的结果是【门全部静默失效】，而这正是本方案要消灭的模式。
        这就是"1:1 复刻到别的电脑"的头号阻塞。

    ⚠️ 为什么用正斜杠绝对路径：
        Claude Code 在 Windows 上用 Git Bash 执行 hook：
        - 反斜杠会被当转义符吃掉（实测 D:\\临时 → D:临时）
        - %USERPROFILE% 不展开（实测 sh -c 'echo [%USERPROFILE%]' 原样输出）
    """
    txt = open(SRC_FRAG, encoding="utf-8").read()

    abs_hooks = os.path.abspath(hooks_dir).replace("\\", "/")
    txt = txt.replace("{{HOOKS}}", abs_hooks)

    # ⚠️ 类型校验：`python_exe` 必须是"像个解释器路径"的字符串。
    # 实测踩过：改函数签名后漏改调用点，把 scope（"project"）传了进来，
    # 结果配置里被写成 `"project" "D:/.../budget_gate.py"` ——
    # 自检才抓出来（rc=127: sh: project: command not found）。
    # 这类"参数位置写错"在 Python 里不报错，只会静默生成错配置。
    if python_exe is not None and (
            not isinstance(python_exe, str)
            or not python_exe.lower().endswith(".exe")
            and not os.path.basename(python_exe).lower().startswith(("python", "py"))):
        raise ValueError(
            "build_fragment 的 python_exe 参数不是解释器路径：%r\n"
            "  这通常是调用点传错了参数。" % (python_exe,))

    # py 启动器需要版本前缀（py -3.13 script.py），
    # 否则某些机器上会把脚本路径当成版本号。
    # 所以探测结果里的 prefix 必须一并写进命令 —— 不能只写 "py"。
    py_prefix = ""
    if not python_exe:
        python_exe, _v, _n, _p = find_python()
        if _p:
            py_prefix = " ".join(_p) + " "
    if not python_exe:
        raise RuntimeError(
            "找不到可用的 Python 解释器。\n"
            "  这套门需要 Python 3.8+。\n"
            "  下载：https://www.python.org/downloads/windows/\n"
            "  安装时请勾选 “Add Python to PATH”。")

    txt = txt.replace("{{PYTHON}}", py_prefix + python_exe.replace("\\", "/"))

    return json.loads(txt)


def _is_our_file(name):
    """判断一个【文件名】是不是本方案安装的（用于清理废弃文件）。

    与 _is_ours(cmd) 的区别：那个判的是命令字符串，这个判的是文件名。
    必须认历史名字（bg-*.cmd 是 V2.7 之前的），
    否则升级时清不掉旧文件 —— 实测踩到过。
    绝不能认别人的文件（cbm-*.cmd）。
    """
    n = (name or "").lower()
    if n.startswith("bg-"):
        return True
    return n in ("_lib.py", "intent_gate.py", "budget_gate.py", "effect_gate.py",
                 "destructive_gate.py", "closeout_gate.py", "inject_budget.py")


OUR_FILES = (
    "_lib.py", "budget_gate.py", "inject_budget.py", "effect_gate.py",
    "destructive_gate.py", "closeout_gate.py", "intent_gate.py",
)


def _is_ours(cmd):
    """判断一条 hook 命令是不是本方案装的（用于升级时清理旧版本条目）。

    必须能识别【所有历史版本】的命令格式，否则升级会残留死条目：
      v1：cmd.exe ... /c '""<path>\\bg-xxx.cmd""'   （坏的，装不上）
      v2：cmd.exe ... /c ""<path>\\bg-xxx.cmd""     （可用）
      v3：python.exe "<path>/xxx.py"                （当前）
    三者 matcher 相同但命令串不同，不去重就会并存 —— 旧的会静默失败。

    ⚠️ 两个曾出错的地方（实测复现，不要改回去）：

    1. 【不能用 "bg-" 做整条命令的子串匹配】
       原写法 `"bg-" in cmd` 本意是匹配文件名前缀，实际匹配的是整条命令。
       后果：第三方 hook 只要【路径里含 bg-】（比如项目目录叫 bg-project），
       就会被当成我们自己的、然后在升级时被删掉。
       删除是不可逆的，所以必须精确判【文件基名】。

    2. 【markers 白名单曾漏掉 intent_gate】
       后果：G7 那条永远清不掉。换机/升级 Python 后，
       旧条目（指向失效解释器）会和新条目并存 —— 死 hook 静默失败。
       本函数必须列出【全部】自家脚本，一个都不能少。
    """
    if not cmd:
        return False
    # 抽出命令里所有像「文件路径」的片段，只看基名 —— 不做整条子串匹配
    for m in re.findall(r"[\w./\\:+-]+\.(?:py|cmd)", cmd, re.I):
        base = os.path.basename(m.replace("\\", "/")).lower()
        if base in OUR_FILES or base.startswith("bg-"):
            return True
    return False


def _matcher_of(entry):
    return entry.get("matcher") or ""


def prune_stale(settings_hooks, new_hooks):
    """删除【本方案】的旧条目（保留 cbm 等第三方 hook）。返回删除条数。"""
    removed = 0
    for event, entries in list(settings_hooks.items()):
        keep = []
        for ent in entries:
            cmds = [h.get("command", "") for h in ent.get("hooks", [])]
            ours = [c for c in cmds if _is_ours(c)]
            foreign = [c for c in cmds if not _is_ours(c)]

            if not ours:
                keep.append(ent)          # 不是我们的，原样保留
                continue
            if foreign:
                # 混合条目：只摘掉我们的，保留外部 hook
                ent["hooks"] = [h for h in ent["hooks"]
                                if not _is_ours(h.get("command", ""))]
                keep.append(ent)
                removed += 1
                continue
            # 纯我们的条目 —— 丢弃（下面会按新格式重新加回）
            removed += 1
        settings_hooks[event] = keep
    return removed


def same_entry(existing, new):
    if existing.get("matcher") != new.get("matcher"):
        return False
    e = {h.get("command") for h in existing.get("hooks", [])}
    n = {h.get("command") for h in new.get("hooks", [])}
    return bool(n) and n.issubset(e)


def _owned_paths(claude_dir):
    """上一次部署清单里记录过的文件路径 —— 用来按【身份】判归属，而不是只按名字。

    优先读 ownedPaths（= 上次真正从源码复制过去的那些），
    没有就退回 items（= 上次校验时目录里的全部文件），再没有就返回空集合。
    空集合 = 调用方退回名字判定（兼容老部署）。
    """
    try:
        p = os.path.join(claude_dir, "DEPLOY_MANIFEST.json")
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return set()
    v = d.get("ownedPaths")
    if isinstance(v, list):
        return set(v)
    # 老清单（≤3.0.0）没有 ownedPaths，只有 items —— 而 items 是
    # 「上次校验时目录里有什么」，【含第三方放进去的文件】，不能直接当归属。
    # 用它做回退必须收窄：#10 的残余风险正是"名字像我们、又恰好在目录里"
    # 的第三方文件被清掉。本方案从未发布过子目录，所以子目录里的一切都不是
    # 我们的 —— 回退只认顶层。
    return {i.get("path") for i in (d.get("items") or [])
            if i.get("path") and "/" not in i.get("path", "")}


def main():
    # 未知参数【报错】而不是静默忽略 —— 见 check_unknown_flags 的注释。
    # 放在最前面：宁可不安，也不要在"装错地方"之后才告诉用户。
    unknown = check_unknown_flags(sys.argv[1:])
    if unknown:
        echo("=" * 64)
        echo("[!] 有无法识别的参数：%s" % " ".join(unknown))
        echo("=" * 64)
        echo("")
        echo("  本安装器认识的参数：")
        echo("    --apply              真正写入（不加则只预演）")
        echo("    --scope global       安装到全局（~/.claude/）")
        echo("    --scope project      安装到当前项目（默认）")
        echo("    --project <路径>     指定项目目录")
        echo("    --global             --scope global 的别名")
        echo("    --force              以源码为准覆盖部署")
        echo("    --rollback           回滚")
        echo("")
        echo("  刻意不忽略未知参数：以前 `--global` 就是被静默忽略的，")
        echo("  结果照文档执行却装进了包自己所在的目录，且没有任何提示。")
        echo("")
        return 2

    scope, claude_dir, settings, hooks_dst, label = resolve_scope()

    echo("=" * 64)
    echo("行为门安装%s" % ("" if APPLY else "（预演，未写任何文件）"))
    echo("作用域：%s" % label)
    echo("目标：  %s" % settings)
    echo("=" * 64)

    # ---- 前置检查 ----
    if os.path.isfile(settings):
        try:
            cur = json.load(open(settings, encoding="utf-8"))
        except Exception as e:
            echo("\n[!] %s 解析失败：%r" % (settings, e))
            echo("    先修好它再装 —— 脚本不会动一个解析不了的文件。")
            return 1
    else:
        if scope == "global":
            echo("\n[!] 找不到 %s —— 请先确认 Claude Code 已初始化。" % settings)
            return 1
        echo("\n[i] %s 不存在，将新建（这正常：项目级配置可能还没建过）。" % settings)
        cur = {}

    new_hooks = build_fragment(hooks_dst)["hooks"]

    # ---- 计划 ----
    existing = cur.get("hooks", {})
    plan = []
    for event, entries in new_hooks.items():
        for ent in entries:
            dup = any(same_entry(c, ent) for c in existing.get(event, []))
            plan.append(("skip" if dup else "add", event,
                         ent.get("matcher") or "(no matcher)"))

    echo("\n将要改动：")
    for act, ev, mt in plan:
        echo("  [%s] %-18s %s" % ("新增" if act == "add" else "已存在", ev, mt[:70]))

    if existing:
        echo("\n将保留不动（已有 hook）：")
        for ev, entries in existing.items():
            for ent in entries:
                mt = ent.get("matcher") or "(no matcher)"
                tag = ""
                for h in ent.get("hooks", []):
                    if "cbm-" in (h.get("command") or ""):
                        tag = "  <- cbm"
                echo("  %-18s %s%s" % (ev, mt, tag))

    if not APPLY:
        echo("\n" + "-" * 64)
        echo("这是预演。确认无误后运行：")
        if scope == "global":
            echo("    python install.py --scope global --apply")
        else:
            echo("    python install.py --apply")
        echo("-" * 64)
        return 0

    # ---- 执行 ----
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(claude_dir, exist_ok=True)

    # 备份前先清理本方案产生的旧备份 —— 否则每次安装都攒一个，永不回收。
    #
    # ⚠️ 归属判据必须是【文件名以本方案备份的基名开头】，不能是"任意 .bak-<时间戳>"。
    #    实测踩到：原来只匹配 `\.bak-\d{8}-\d{6}$`，
    #    结果用户自己的 `my-own-notes.bak-20250202-020202` 被当成我们的删掉了。
    #    注释当时写着"不碰用户自己的备份" —— 注释在撒谎。
    #
    #    正确做法：只认我们真正会创建的那两个备份名。
    #    别人起的名字无论多像，都不是我们的，不能删。
    ours_prefix = (
        os.path.basename(settings) + ".bak-",
        "behavior-policy.json.bak-",
    )
    kept = []
    try:
        for f in os.listdir(claude_dir):
            if not any(f.startswith(pfx) for pfx in ours_prefix):
                continue
            if not re.search(r"\.bak-\d{8}-\d{6}$", f):
                continue
            kept.append(os.path.join(claude_dir, f))
        kept.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in kept[3:]:            # 只留最近 3 个
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass

    backup = None
    if os.path.isfile(settings):
        backup = "%s.bak-%s" % (settings, stamp)
        shutil.copy2(settings, backup)
        echo("\n[1/4] 已备份 -> %s" % backup)
        if len(kept) > 3:
            # 说清楚最终会有几个：本次还会再写 settings + policy 两个备份。
            # 实测踩到：原来只说"保留最近 3 个"，但清理跑在创建之前，
            # 用户实际看到的是 5 个（3 + 本次新增 2）—— 文案与观察不符。
            echo("      已清理 %d 个旧备份（本次将再产生 2 个，最终保留 5 个）"
                 % (len(kept) - 3))
    else:
        echo("\n[1/4] 无既有文件需备份")

    os.makedirs(hooks_dst, exist_ok=True)

    # ⚠️ 必须【递归】收集，与 verify_deploy 的 scan_dir（用 rglob）对齐。
    #    实测踩到：scan_dir 递归、这里只 listdir（顶层），
    #    往 hooks/ 放一个子目录后，里面的文件【永远装不上】，
    #    而校验一直报 MISSING —— 安装永久被卡住。
    #    这与之前 .json 的扩展名不对称是同一类问题，只是换了一根轴。
    # 「收哪些文件」不在这里决定 —— 一律走 lib/deploy_files.is_tracked()，
    # 与 verify_deploy.scan_dir 共用同一处定义（含 VERSION，Gate 1）。
    def _collect(root):
        out = set()
        for base, _dirs, files in os.walk(root):
            for f in files:
                if is_tracked(f):
                    rel = os.path.relpath(os.path.join(base, f), root)
                    out.add(rel.replace("\\", "/"))
        return out

    src_names = _collect(SRC_HOOKS)

    # ---- 先清理【源码里已删除】的自家文件，再校验 ----
    #
    # ⚠️ 顺序（实测踩到，两个 bug 都出在这里）：
    #
    #   1. 校验必须在【复制之前】—— 否则复制本身把漂移覆盖，校验必然通过，
    #      变成"看起来会拦、实际拦不住"的假门。
    #
    #   2. 但清理必须在【校验之前】—— 否则源码合法地删掉文件时，
    #      校验先看到 EXTRA 就中止了，清理永远轮不到执行。
    #      实测：V2.6→V2.7 升级时，5 个废弃的 bg-*.cmd 让安装直接中止，
    #      而新写的清理正是为了修这个场景 —— 却排在它后面，白写了。
    #
    #   所以正确顺序是：清理 → 校验 → 复制 → 再校验。
    #   只删【本方案自己的】文件，绝不碰别人的（如 cbm-*.cmd）。
    #
    # ⚠️ 归属判据要两个信号同时成立：名字像我们【而且】上次部署清单里真的记过它。
    #
    #    实测缺陷（#10）：只按文件名判，会把第三方文件删掉 ——
    #    一个恰好叫 _lib.py 的第三方文件被当成我们的清掉了（删除不可逆）。
    #    而 _lib.py 正是白名单里最普通的名字，第三方完全可能用。
    #    加一道"上次确实装过它"的关口，删错的面就收窄到几乎为零。
    #    清单缺失（老部署、测试造的残留）时退回原来的名字判定，保持兼容。
    owned = _owned_paths(claude_dir)
    stale = []
    if os.path.isdir(hooks_dst):
        dst_names = _collect(hooks_dst)
        for rel in sorted(dst_names - src_names):
            # 只清本方案自己的；别人的（cbm-*、第三方）一律不动
            if not _is_our_file(os.path.basename(rel)):
                continue
            if owned and rel not in owned:
                continue        # 名字像，但从没被我们装过 —— 不是我们的
            stale.append(rel)
    for rel in stale:
        try:
            os.remove(os.path.join(hooks_dst, rel.replace("/", os.sep)))
        except Exception:
            pass
    # 删完文件后把空目录一并收掉（#11）。os.rmdir 只在目录确实为空时成功，
    # 所以 __pycache__ 这类还有东西的目录不会被误删。
    if stale:
        for base, dirs, _files in os.walk(hooks_dst, topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(base, d))
                except OSError:
                    pass
    if stale:
        echo("      已清理 %d 个废弃文件：%s" % (len(stale), ", ".join(stale)))

    # ---- 校验现存部署（此时自家废弃文件已清，剩下的差异才是真漂移）----
    verify = os.path.join(HERE, "tools", "verify_deploy.py")
    # ⚠️ 全新安装【不是漂移】—— 目标目录还空着，当然"什么都没装"。
    #    实测踩到：装到一台新电脑时，第一次运行会被这个检查挡住，
    #    报一堆 MISSING，用户以为出错了。
    #    只有【已经装过东西】之后内容不一致，才叫漂移。
    _already = os.path.isdir(hooks_dst) and bool(_collect(hooks_dst))
    if os.path.isfile(verify) and _already:
        r0 = subprocess.run([sys.executable, verify,
                             "--source", SRC_HOOKS,
                             "--installed", hooks_dst,
                             "--pkg", PKG_DIR],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if r0.returncode != 0:
            echo("")
            echo("[!] 升级前检测到不一致 —— 已中止。")
            for line in r0.stdout.decode("utf-8", "replace").splitlines():
                if any(k in line for k in ("STALE", "FAKE", "MISSING", "EXTRA",
                                           "判定:", "L1 ", "L2 ", "L3 ")):
                    echo("      " + line.strip())
            echo("")
            echo("    三种可能，先分清是哪一种：")
            echo("      · DEPLOYMENT_DRIFT        —— 有人直接改了 .claude/hooks/")
            echo("      · SOURCE_IDENTITY_UNPROVEN —— 源码改了但源身份声明未刷新")
            echo("      · MANIFEST_INTEGRITY_FAILURE —— 部署清单自身被改动")
            echo("    先看完整原因：")
            echo("      python tools/verify_deploy.py --source \"%s\" --installed \"%s\""
                 % (SRC_HOOKS, hooks_dst))
            echo("    如果是【你刚改完 hooks】：显式刷新源身份（不会被自动刷新，这是设计意图）")
            echo("      python tools/source_identity.py --write")
            echo("    如果确认要以源码为准覆盖部署，加 --force。")
            if not FORCE:
                return 2

    n = 0
    for rel in sorted(src_names):
        dst = os.path.join(hooks_dst, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)   # 支持子目录
        shutil.copy2(os.path.join(SRC_HOOKS, rel.replace("/", os.sep)), dst)
        n += 1
    echo("[2/4] 已复制 %d 个 hook 文件 -> %s" % (n, hooks_dst))

    policy_dst = os.path.join(claude_dir, "behavior-policy.json")
    if os.path.isfile(policy_dst):
        shutil.copy2(policy_dst, policy_dst + ".bak-" + stamp)
        # ⚠️ --force 时必须覆盖（实测踩到）：
        #    原来无论什么情况都"文件已存在就不覆盖"，
        #    结果升级时全局那份永远是旧的 → 校验报 FAKE
        #    （策略里还留着已删除的 bash_rounds/webfetch）。
        #    "保护用户改动"是对的，但升级场景下它保护的是【过期内容】。
        if FORCE:
            shutil.copy2(SRC_POLICY, policy_dst)
            echo("[3/4] 策略已更新（--force，旧版备份在 .bak-%s）" % stamp)
        else:
            echo("[3/4] 策略文件已存在，已备份（未覆盖；要更新请加 --force）")
    else:
        shutil.copy2(SRC_POLICY, policy_dst)
        echo("[3/4] 已写入策略 -> %s" % policy_dst)

    # 先清掉本方案的旧条目（可能来自历史版本的坏命令格式）
    npruned = prune_stale(cur.setdefault("hooks", {}), new_hooks)
    if npruned:
        echo("[4a/4] 已清理 %d 条本方案的旧 hook 条目（升级）" % npruned)

    for event, entries in new_hooks.items():
        got = cur.setdefault("hooks", {}).setdefault(event, [])
        for ent in entries:
            if not any(same_entry(c, ent) for c in got):
                got.append(ent)

    tmp = settings + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, settings)
    echo("[4/4] 已合并 -> %s" % settings)

    # ---- [5/5] 部署文件一致性校验（强制，不可跳过）----
    #
    # 为什么必须内置：本窗口里"我改了但没生效"发生了三次，
    # 每次都是我先宣布完成、然后被发现没生效。
    # 把校验做成安装流程的最后一步，就不需要靠"我记得验证"。
    #
    # 详见 tools/verify_deploy.py 的文件头。
    echo("")
    verify = os.path.join(HERE, "tools", "verify_deploy.py")
    if os.path.isfile(verify):
        echo("[5/5] 部署文件一致性校验 ...")
        # --require-complete：这是复制【之后】的最终校验，
        # 文件必须齐全。复制前的预检不带这个参数（缺文件不算漂移）。
        r = subprocess.run([sys.executable, verify,
                            "--source", SRC_HOOKS,
                            "--installed", hooks_dst,
                            "--pkg", PKG_DIR,
                            "--write-manifest",
                            "--require-complete"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode("utf-8", "replace")
        # 只回显判定行，细节留给用户按需查看
        for line in out.splitlines():
            if any(k in line for k in ("判定:", "SOURCE_IDENTITY_DRIFT",
                                       "SOURCE_IDENTITY_UNPROVEN",
                                       "MANIFEST_INTEGRITY_FAILURE",
                                       "DEPLOYMENT_DRIFT",
                                       "STALE", "FAKE", "MISSING", "EXTRA",
                                       "生效策略 budget")):
                echo("      " + line.strip())
        if r.returncode != 0:
            echo("")
            echo("[!] 部署校验未通过（%s）—— 安装不算完成。" % r.returncode)
            echo("    运行下面这条看完整原因：")
            echo("      python tools/verify_deploy.py --source \"%s\" --installed \"%s\""
                 % (SRC_HOOKS, hooks_dst))
            return 2
        # ⚠️ 这里不能说死成 "MATCH" —— 部署校验的判定已经分层（#49），
        #    同一个 returncode=0 可能是 MATCH / NORMAL_UPGRADE / SOURCE_ADVANCED /
        #    LEGACY_BASELINE_MIGRATION。写死一句话会造出自相矛盾的输出形状
        #    （上面写 SOURCE_ADVANCED、下面写 MATCH），
        #    而本项目正好一直在治这个病。
        echo("      部署校验通过 —— 源码与已安装一致，且生效策略里无已删除的键")
        # Claim 边界：用户看的是【这里】的输出，
        # 不是 verify_deploy 的。边界不写在这里等于没写。
        echo("      ⚠️ 边界：这只证明【文件一致】，不证明门的行为正确。")
        echo("              详见包内 install.md 的「已知边界」。")
    else:
        echo("[5/5] 跳过（找不到 tools/verify_deploy.py）")

    # ---- [6/6] 装完自己跑一遍，证明"真的能拦" ----
    #
    # 为什么必须要这一步（真实问题验证）：
    #   这个项目在本窗口里出现过 5 次「看起来装了、实际没装」——
    #   .cmd 格式错、%USERPROFILE% 不展开、两个作用域互相覆盖……
    #   每一次安装脚本都报告成功。
    #   所以"安装成功"这句话本身不可信，必须用【真实执行】来证。
    echo("\n[6/6] 自检：用真实注册的命令跑一遍五道门 …")
    smoke_ok, smoke_lines = smoke_test(cur, hooks_dst)
    for line in smoke_lines:
        echo("      " + line)

    echo("\n" + "=" * 64)
    if smoke_ok:
        echo("安装完成（%s），自检通过。" % label)
        echo("  边界：自检只证明那四条命令的退出码符合预期 ——")
        echo("        不证明门的行为正确，也不证明你的结论是对的。见包内 install.md 的「已知边界」")
    else:
        echo("安装完成（%s），但【自检未全部通过】—— 见上方。" % label)
        echo("  请不要当成装好了。把上面的输出发给开发者。")
    echo("  1. 重启 Claude Code")
    echo("  2. 发一句「帮我调研这个项目」—— 应被 G1 拦下并给出解释")
    echo("  3. 再发「帮我调研这个项目，agent_spawns: 2」—— 应放行 2 个")
    echo("\n回滚：")
    if backup:
        echo("    copy \"%s\" \"%s\"" % (backup, settings))
    else:
        echo("    直接删除 %s" % settings)
    echo("=" * 64)
    return 0 if smoke_ok else 2


def smoke_test(settings_obj, hooks_dir):
    """用【真实注册的命令 + 真实 shell】验证五道门。

    刻意不复用测试套件：测试套件读的是源码目录，
    而这里必须验证【刚刚写进配置的那条命令】能不能跑。
    两者是两个不同的路径，这个区别在本窗口里骗过我 4 次。
    """
    lines = []
    ok = True

    def find(ev, needle):
        for e in settings_obj.get("hooks", {}).get(ev, []):
            for h in e.get("hooks", []):
                if needle in h.get("command", ""):
                    return h["command"]
        return None

    def shell_run(cmd, payload):
        """用 sh 执行（Claude Code 在 Windows 上就用 Git Bash）。"""
        try:
            p = subprocess.run(["sh", "-c", cmd],
                               input=json.dumps(payload).encode(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=20)
            return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
        except Exception as e:
            return -1, repr(e)

    state = os.path.join(HERE, ".smoke-state")
    env_backup = os.environ.get("CLAUDE_BUDGET_STATE_DIR")
    os.environ["CLAUDE_BUDGET_STATE_DIR"] = state

    checks = [
        ("G1 预算门", "budget_gate", "PreToolUse",
         {"session_id": "smoke", "tool_name": "Agent",
          "tool_input": {"subagent_type": "Explore", "description": "smoke"}}, 2),
        ("G5 危险门", "destructive_gate", "PreToolUse",
         {"session_id": "smoke", "tool_name": "Bash",
          "tool_input": {"command": "p" + "kill -f x"}}, 2),
        ("G3 生效门", "effect_gate", "Stop",
         {"session_id": "smoke", "stop_hook_active": False,
          "last_assistant_message": "已经修复完成了。"}, 2),
        ("放行路径", "destructive_gate", "PreToolUse",
         {"session_id": "smoke", "tool_name": "Bash",
          "tool_input": {"command": "ls -la"}}, 0),
    ]

    # 先注入预算（否则 G1 走 fallback，测不到真实路径）
    inj = find("UserPromptSubmit", "inject_budget")
    if inj:
        shell_run(inj, {"session_id": "smoke", "prompt": "x"})

    for name, needle, ev, payload, want in checks:
        cmd = find(ev, needle)
        if not cmd:
            lines.append("  [!] %s：配置里找不到命令" % name)
            ok = False
            continue
        rc, out = shell_run(cmd, payload)
        if rc == want:
            lines.append("  [OK] %s (rc=%d)" % (name, rc))
        else:
            lines.append("  [!] %s rc=%d 期望%d | %s"
                         % (name, rc, want, out[:90].replace("\n", " ")))
            ok = False
        # 交互横幅 = 命令格式错（本窗口踩过的坑）
        if "Microsoft Windows" in out:
            lines.append("       ^ 出现 cmd.exe 横幅 → 命令格式有问题")
            ok = False

    try:
        shutil.rmtree(state, ignore_errors=True)
    except Exception:
        pass
    if env_backup is None:
        os.environ.pop("CLAUDE_BUDGET_STATE_DIR", None)
    else:
        os.environ["CLAUDE_BUDGET_STATE_DIR"] = env_backup

    return ok, lines


if __name__ == "__main__":
    sys.exit(main())
