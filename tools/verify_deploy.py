#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署文件一致性校验。

⚠️ 它不是一个完整的 Source Identity Guard：
   完整形态需要绑定 repository + harness + scoring + prompt authority 四类事实；
   本工具只比对【已安装文件与源逐字节一致】。声称同等强度就是 Claim > Evidence。

## 为什么需要这个

本窗口里"我改了但没生效"发生了三次，全都是同一个形状：

  1. 改了 settings.fragment.json 的命令格式，但注册的配置是旧的
  2. 删了 policy 里的键，但 DEFAULT_POLICY 把它 merge 回来了
  3. 改了源码，但 .claude/hooks/ 里跑的是旧副本

这类问题归为 SOURCE_IDENTITY_DRIFT：
  **声明的修订 ≠ 实际运行的产物** → 先停下，不要在此基础上继续

本工具用的方法：
  - sha256_file / 规范化路径计算
  - MATCH / DRIFT 二元判定
  - (path, sha256, bytes) 三元组作为文件身份形状
  - 「Checkpoint 是派生结果，必须从真实产物重算」

## 两层校验（对应三次失败）

  L1 文件级：已安装文件的 sha256 == 源文件     → 抓 #1 #3
  L2 生效级：从【已安装的 _lib】实际加载策略，   → 抓 #2
             断言生效值里不含已删除的键
             （不信任"我删了声明"，只信任"加载出来是什么"）

## 用法

    python tools/verify_deploy.py --source <src_hooks> --installed <dst_hooks>
    python tools/verify_deploy.py ... --write-manifest   # 安装后写下基线

退出码：0 = MATCH，2 = DRIFT（与包的 STOP 语义一致）
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# 「哪些文件属于被同步/被校验的集合」只能有一处定义 —— 见该模块的文件头。
# install.py 的 _collect 与本文件的 scan_dir 都从它取，避免再次出现两边不一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from deploy_files import is_tracked  # noqa: E402
import src_identity as sid  # noqa: E402

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



# ---------------------------------------------------------------- 基础（复用包的做法）

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_bytes(obj, exclude=()):
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in set(exclude)}
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_obj(obj, exclude=()):
    return hashlib.sha256(canonical_bytes(obj, exclude)).hexdigest()


# ---------------------------------------------------------------- L1 文件级

SKIP_SUFFIX = (".pyc",)


def scan_dir(root):
    """扫描 hooks 目录。

    ⚠️ 收哪些文件【不在这里决定】—— 一律走 lib/deploy_files.is_tracked()。
        历史上这里的规则和 install.py 的复制规则各写一份，已经不一致过两次：
          · 一边 .py/.cmd、另一边还多算 .json → 源码里的 .json 永远装不上；
          · 一边 listdir（只顶层）、另一边 rglob（递归）→ 子目录里的永远装不上。
        两处规则必须一致，而"保持一致"的唯一可靠做法是只有一处规则。

    ⚠️ VERSION 也在集合里（源身份校验）。
        包要以机器可读的方式声明自己是哪一版，该声明随 hooks 同步、参与校验；
        否则「声明的身份」与「现场的身份」可以不一致而无人发现。
        实测：包内 install.md 写着「对应版本 V2.4」，而实际已是 V3.0。
    """
    out = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if not is_tracked(p.name):
            continue
        if any(str(p).endswith(s) for s in SKIP_SUFFIX):
            continue
        if "__pycache__" in p.parts:
            continue
        out[p.relative_to(root).as_posix()] = {
            "sha256": sha256_file(p),
            "bytes": p.stat().st_size,
        }
    return out


def _read_version(root):
    """读 hooks 目录里的 VERSION 声明（源身份校验）。

    没有它就是「没有声明身份」—— 返回 None，不猜版本号。
    身份必须能被现场读到，而不是只存在于发布包的名字里。
    """
    try:
        v = (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
        return v or None
    except Exception:
        return None


def _load_manifest(path):
    """读部署清单。返回 (dict|None, 错误说明)

    没有清单不算错误 —— 它的意思是「还没装过」，交给 MISSING 逻辑处理。
    """
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, "清单无法解析：%r" % (e,)


def _check_manifest_integrity(man):
    """L1：清单自身是否完好（manifestDigest 自洽）。

    清单是 DEPLOYED_INTEGRITY 的判据来源 —— 它自己坏了，
    下游结论就不可信。所以这一层必须排在最前，且独立报错。
    """
    if not isinstance(man, dict):
        return False, "清单不是对象"
    claimed = man.get("manifestDigest")
    if claimed is None:
        # 旧清单没有该字段 —— 不判为「坏」（那是 legacy 形态，由 L4 处理）
        return True, "（旧清单无 manifestDigest）"
    d = dict(man)
    d.pop("manifestDigest", None)
    if sha256_obj(d) != claimed:
        return False, "manifestDigest 不符 —— 清单自身被改动过"
    return True, "manifestDigest 校验通过"


def _check_deployed_integrity(installed, man):
    """L2：已装副本是否仍匹配清单记录的 hash。

    与 compare_files 的区别：后者比的是「源码 vs 已装」，
    这里比的是「清单 vs 已装」—— 即「有没有人动过部署」。
    这两件事必须分开：源码合法前进不该被判成部署被改。
    """
    bad = []
    for it in (man.get("items") or []):
        rel = it.get("path")
        if not rel:
            continue
        p = os.path.join(installed, rel)
        if not os.path.isfile(p):
            bad.append("%s（缺失）" % rel)
        elif sha256_file(p) != it.get("sha256"):
            bad.append("%s（被改动）" % rel)
    return bad


def _is_higher(a, b):
    """a 的版本号是否高于 b。任一不可解析 → False。"""
    if not a or not b:
        return False
    try:
        return tuple(int(x) for x in a.split(".")) > \
               tuple(int(x) for x in b.split("."))
    except Exception:
        return False


def compare_files(src, dst):
    """返回 (problems, info)"""
    problems = []
    src_names = set(src)
    dst_names = set(dst)

    for name in sorted(src_names - dst_names):
        problems.append("MISSING  未安装: %s" % name)
    for name in sorted(dst_names - src_names):
        # ⚠️ 只有【本方案自己的】文件才算陈旧残留。
        #    实测踩到：全局 hooks 目录里还有 cbm 的 3 个 .cmd，
        #    旧逻辑一律报 EXTRA，导致每次全局安装都判"漂移"，
        #    用户看到一堆红字以为装坏了 —— 实际那是别人的文件，不该动。
        #    判据：文件名带本方案的标识（bg- / _gate / inject_budget）。
        if _is_ours(name):
            problems.append("EXTRA    已安装但源里没有（陈旧残留）: %s" % name)
    for name in sorted(src_names & dst_names):
        if src[name]["sha256"] != dst[name]["sha256"]:
            problems.append(
                "STALE    内容不一致: %s\n"
                "           源   %s (%d bytes)\n"
                "           已装 %s (%d bytes)"
                % (name, src[name]["sha256"][:16], src[name]["bytes"],
                   dst[name]["sha256"][:16], dst[name]["bytes"]))
    return problems


# ---------------------------------------------------------------- L2 生效级

def load_effective_policy(installed_hooks_dir):
    """从【已安装的】_lib 实际加载策略。

    这是关键：不读 policy 文件（那只是声明），
    而是跑 load_policy() 看【最终生效的值】。
    三次失败里的 #2（删键被默认值 merge 回来）只有这样才能抓到。
    """
    lib = os.path.join(installed_hooks_dir, "_lib.py")
    if not os.path.isfile(lib):
        return None, "找不到已安装的 _lib.py"

    # 在干净模块名空间里加载，避免缓存
    spec = importlib.util.spec_from_file_location(
        "_installed_lib_%d" % os.getpid(), lib)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, "加载已安装 _lib 失败: %r" % (e,)

    try:
        pol = mod.load_policy()
    except Exception as e:
        return None, "调用 load_policy() 失败: %r" % (e,)
    return pol, None


def _is_ours(name):
    """判断一个 hook 文件是不是本方案安装的。

    只有本方案的文件才该被当成"陈旧残留"去报。
    别的工具（如 cbm）的文件出现在同一目录是正常的，不能报错。
    """
    n = name.lower()
    if n.startswith("bg-"):
        return True
    if n in ("_lib.py", "intent_gate.py", "budget_gate.py", "effect_gate.py",
             "destructive_gate.py", "closeout_gate.py", "inject_budget.py"):
        return True
    return False


# 这些键被判定为「假能力」并已删除 —— 生效值里绝不能再出现
MUST_NOT_APPEAR = {
    "budget": ["bash_rounds", "webfetch"],
}

# 这些键必须存在（真会被执行）
MUST_APPEAR = {
    "budget": ["agent_spawns", "agent_depth"],
}


def check_dead_sections(policy, hooks_dir):
    """检查 policy 里有没有【没有任何代码读】的段（#36 新增）。

    为什么加这个：
      本项目历史上出现过两次同形状的缺陷 ——
        · bash_rounds / webfetch 声明上限，但没有拦截器（假能力）
        · closeout.enabled 写 false，但门不读它、无条件运行（#31）
      两次都是靠人事后扫描发现的。而 policy 的注释里写着
      「装一个不生效的开关比没有开关更糟」—— 说明概念早就有，只是没有机械检查。

      人扫一次能发现问题，但【挡不住下一次】。所以做成自动的。

    判定方法：
      段名必须作为【访问表达式】出现，即 policy.get("段名") 或 policy["段名"]。

      ⚠️ 初版规则是「任意带引号的字面量即算被读」，3.5.7 修正（见 #38）。
         它结构性地抓不到 closeout 那类死段，原因：
           _lib.py 的 DEFAULT_POLICY 为每一个段都写了一遍段名，
           → 段名永远躺在 .py 里 → 永远判活，无论死得多彻底。
         实测：在 v3.5.0（closeout 与 gateway_budget 都真死）上，
         初版规则只抓到 1 个，漏掉 closeout；收紧后两个都抓到，
         且对真活的段零误报。
         另一处误救：closeout_gate.py 的 state_path(session_id, "closeout")
         是状态文件名，不是 policy 读取，但字面量相同。

    为什么不直接删掉死段：
      删掉是修复手段之一，但【先要让人看见】。自动报出来之后，
      由人决定是删掉、还是接上实现。见 #36 的处理。
    """
    problems = []
    if not policy or not hooks_dir or not os.path.isdir(hooks_dir):
        return problems

    # 只统计真正的 hook 脚本，不含测试与工具
    sources = []
    for name in os.listdir(hooks_dir):
        if name.endswith(".py"):
            p = os.path.join(hooks_dir, name)
            try:
                sources.append(open(p, encoding="utf-8").read())
            except Exception:
                pass
    blob = "\n".join(sources)

    for section, value in policy.items():
        if section.startswith("_"):
            continue
        # 必须是访问表达式，不能是任意字面量（见上方 ⚠️）
        access = r'(\.get\(\s*["\']%s["\']|\[\s*["\']%s["\']\s*\])' % (section, section)
        if not re.search(access, blob):
            problems.append(
                "DEAD     策略段没有任何 hook 读取: %s\n"
                "           在 %s 下的 .py 里找不到 policy.get(%r) 形式的访问。\n"
                "           这是「假能力」的一种：声明存在，但永远不会生效。\n"
                "           处理：要么删掉该段，要么接上实现 —— 不要留着。"
                % (section, hooks_dir, section))
            continue

        # 段内键（3.5.7 新增）：#2 那个形状（bash_rounds / webfetch）
        # 是段里的键，只看顶层段会结构性地漏掉它。
        if isinstance(value, dict):
            for key in value:
                if key.startswith("_"):
                    continue
                kaccess = r'(\.get\(\s*["\']%s["\']|\[\s*["\']%s["\']\s*\])' % (key, key)
                if not re.search(kaccess, blob):
                    problems.append(
                        "DEAD     策略键没有任何 hook 读取: %s.%s\n"
                        "           段本身有读取者，但这个键没有。\n"
                        "           处理：要么删掉该键，要么接上实现。"
                        % (section, key))
    return problems


def check_effective(policy):
    problems = []
    if policy is None:
        return ["无法加载生效策略"]

    for section, keys in MUST_NOT_APPEAR.items():
        sec = policy.get(section) or {}
        for k in keys:
            if k in sec:
                problems.append(
                    "FAKE     生效策略里仍存在已删除的键: %s.%s = %r\n"
                    "           （删声明不够 —— 默认值会把它 merge 回来）"
                    % (section, k, sec[k]))

    for section, keys in MUST_APPEAR.items():
        sec = policy.get(section) or {}
        for k in keys:
            if k not in sec:
                problems.append(
                    "ABSENT   生效策略缺少必需的键: %s.%s" % (section, k))
    return problems


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(
        description="校验已安装文件与源是否逐字节一致（不证明门的行为正确）")
    ap.add_argument("--source", required=True, help="源 hooks 目录")
    ap.add_argument("--installed", required=True, help="已安装 hooks 目录")
    ap.add_argument("--write-manifest", action="store_true",
                    help="把当前已安装状态写成基线清单")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--require-complete", action="store_true",
                    help="要求文件齐全（MISSING 致命）。"
                         "复制【之后】的最终校验用它；复制【之前】的预检不要用。")
    ap.add_argument("--pkg", default=None,
                    help="元数据层目录（含 SOURCE_IDENTITY.json）。"
                         "默认取 source 的上一层。")
    a = ap.parse_args()

    src = scan_dir(a.source)
    dst = scan_dir(a.installed)

    file_problems = compare_files(src, dst)
    policy, perr = load_effective_policy(a.installed)
    eff_problems = check_effective(policy) if perr is None else [perr]
    # 死段检测（#36）：policy 里声明了但没有任何 hook 读的段。
    # 在【源】上检查而不是已安装副本 —— 这是打包期问题，不是部署问题。
    if policy is not None:
        eff_problems += check_dead_sections(policy, a.source)

    # ⚠️ "找不到已安装的 _lib.py" 不算漂移（实测踩到）。
    #    它的意思就是【还没装】—— 而"把它装上"正是安装要做的事。
    #    把"缺文件"当漂移，等于安装脚本自己把自己挡住。
    #    真正常见的 eff_problem 是 FAKE（生效策略含已删除的键），
    #    那才说明"装过、但装的是旧的"。
    eff_problems = [p for p in eff_problems
                    if not p.startswith("找不到已安装")]

    # ⚠️ MISSING【不】算漂移（实测踩到）。
    #
    #   漂移 = 「已经装过，但内容和源码对不上」—— 那是"被改坏了"的信号。
    #   而 MISSING = 「源码有、已安装没有」—— 那正是安装要解决的问题。
    #
    #   把两者混在一起，后果是：升级/重装时只要有文件没装全，
    #   检查就中止安装，而"把文件装全"恰恰是它该做的动作。
    #   实测：V2.6→V2.7 升级时 5 个废弃 .cmd 触发 EXTRA，
    #   加上旧安装本身不完整触发 MISSING，安装被卡死在门口。
    #
    #   STALE（内容不一致）和 FAKE（假能力键）才是真漂移，必须拦。
    #   STALE（内容不一致）才是真漂移，必须拦。
    #
    # ⚠️ 但"缺文件不致命"只适用于【复制之前】的预检。
    #    复制【之后】的最终校验必须要求齐全 —— 那时复制已经跑过，
    #    还缺就是真没装上。实测踩到：把判据统一收窄后，
    #    残缺部署（7 个只装 2 个）会打出 `判定: MATCH`
    #    而上面还列着 5 行 `MISSING` —— 自我矛盾。
    #    改用 --require-complete 区分两次调用的语义。
    #
    # ⚠️⚠️ 判定分层（3.5.10 / #49）—— 这里有【两层完全不同的东西】，
    #      原先被「版本号是否相同」一个判据混在一起，导致未发布版本的
    #      合法修订被误判成篡改（本轮实测坐实）。
    #
    #      现在按固定顺序分四层，前三层任何一层失败都【不得】被
    #      「把 VERSION 调高」绕过：
    #
    #        L1 MANIFEST_INTEGRITY     —— 清单自身是否被改动
    #        L2 DEPLOYED_INTEGRITY     —— 已装副本是否仍匹配清单
    #        L3 CURRENT_SOURCE_IDENTITY —— 源码是否自证为一个一致快照
    #        L4 VERSION / SNAPSHOT RELATION —— 版本与快照的关系
    #
    #      前两层合起来就是原来的「部署侧」；L3 是新增的「源侧」身份。
    same_version = (_read_version(a.source) == _read_version(a.installed))

    # ---- L1 MANIFEST_INTEGRITY ----
    # 清单是 L2 的判据来源 —— 它自己坏了，L2 的结论就不可信。
    manifest_path = os.path.join(os.path.dirname(os.path.abspath(a.installed)),
                                 "DEPLOY_MANIFEST.json")
    old_manifest, man_err = _load_manifest(manifest_path)

    manifest_ok = True
    manifest_why = ""
    if old_manifest is None:
        # ⚠️ 三种情况都到这里，必须分清（实测踩到）：
        #   · 文件不存在         → 还没装过，不是"清单坏了"
        #   · 文件存在但解析失败 → 这才是清单坏了
        #   原先只判 man_err，导致"文件不存在"也走 _check_manifest_integrity(None)
        #   → 返回 False → 误报 MANIFEST_INTEGRITY_FAILURE。
        #   实测后果：upgrade_path_test 18→13，升级被自己的假报警拦下。
        if man_err:
            manifest_ok = False
            manifest_why = man_err
        else:
            manifest_ok = True
            manifest_why = "（无部署清单 —— 尚未安装过）"
    else:
        manifest_ok, manifest_why = _check_manifest_integrity(old_manifest)

    # ---- L3 CURRENT_SOURCE_IDENTITY ----
    pkg_dir = sid.default_pkg_dir(a.source) if not a.pkg else a.pkg
    sidr = sid.check(pkg_dir, a.source)

    # ---- L4 VERSION / SNAPSHOT RELATION ----
    legacy = (old_manifest or {}).get("sourceSnapshot") is None

    real_drift = []
    if a.require_complete:
        real_drift += [p for p in file_problems if p.startswith("MISSING")]

    status = "MATCH"
    notes = []

    # ⚠️ 全新安装不是漂移（项目 #18 的既有语义，实测踩到过两次）：
    #    目标目录里还没有我们的 VERSION → 这就是「没装过」。
    #    把它判成 UNPROVEN 会让每次装到新项目都被挡住，
    #    而"把文件装上去"恰恰是这次要做的动作。
    fresh_install = (_read_version(a.installed) is None)

    if not manifest_ok:
        # 清单自身坏了 → 先停下。这不是 source identity 的问题。
        status = "MANIFEST_INTEGRITY_FAILURE"
    elif fresh_install:
        status = "FRESH_INSTALL"
    else:
        # L2：已装副本 vs 清单
        #
        # ⚠️ --write-manifest 模式（安装【之后】的最终校验）必须跳过这一层。
        #    实测踩到：安装把文件复制完后，installed 已经是新内容，
        #    而 old_manifest 还是【安装之前】写的清单 →
        #    L2 必然对不上 → 判 DEPLOYMENT_DRIFT → 把错误的 status 写进新清单。
        #    这正是本次要刷新的对象：菜单在换菜的时候，不能拿旧菜单对账。
        #
        #    篡改检测没有因此丢失：真被改动过的部署会在【复制之前】的
        #    预检（不带 --write-manifest）里被 L2 拦下。
        stale = [p for p in file_problems if p.startswith("STALE")]
        if old_manifest is not None and not a.write_manifest:
            deployed_bad = _check_deployed_integrity(a.installed, old_manifest)
        else:
            deployed_bad = []
        if deployed_bad:
            status = "DEPLOYMENT_DRIFT"
        else:
            # L3：源身份 —— 前三层任何一层失败都不得被「升 VERSION」绕过，
            #     所以即使是 legacy 迁移，也必须先过这一层。
            if not sidr["ok"]:
                status = "SOURCE_IDENTITY_UNPROVEN"
            elif legacy:
                # legacy 迁移五项条件：无 sourceSnapshot + L1 过 + L2 过
                #                       + L3 过 + 版本一致
                if not same_version:
                    status = "SOURCE_IDENTITY_UNPROVEN"
                    notes.append("legacy 迁移要求版本一致，实际 %s ≠ %s"
                                 % (_read_version(a.installed),
                                    _read_version(a.source)))
                else:
                    status = "LEGACY_BASELINE_MIGRATION"
            else:
                # L4 VERSION / SNAPSHOT RELATION（仅在前三层全过后才看）
                dep_snap = (old_manifest or {}).get("sourceSnapshot")
                cur = sidr["snapshot"]
                if cur == dep_snap:
                    status = "MATCH"
                elif _is_higher(_read_version(a.source),
                                _read_version(a.installed)):
                    status = "NORMAL_UPGRADE"
                elif _read_version(a.source) == _read_version(a.installed):
                    status = "SOURCE_ADVANCED"
                else:
                    status = "SOURCE_IDENTITY_DRIFT"

    # 剩余差异并入最终判定（不允许"下面列着问题、上面判 MATCH"）。
    if a.require_complete and any(p.startswith("MISSING")
                                  for p in file_problems):
        if status in ("MATCH", "FRESH_INSTALL"):
            status = "SOURCE_IDENTITY_DRIFT"

    # ⚠️ eff_problems（生效策略问题）必须并入 status，不能只让 ok 变红。
    #    实测踩到：隔离环境里 load_policy 失败时，
    #      ok=False 但 status 仍打印 MATCH —— 上面列着问题、下面判 MATCH，
    #      正是本项目自己批评过的自相矛盾形状。
    #    判据："status 说没事" 与 "ok 说有事" 不允许同时成立。
    if eff_problems and status == "MATCH":
        status = "EFFECTIVE_POLICY_PROBLEM"

    BLOCKING = {"MANIFEST_INTEGRITY_FAILURE", "DEPLOYMENT_DRIFT",
                "SOURCE_IDENTITY_UNPROVEN", "SOURCE_IDENTITY_DRIFT",
                "EFFECTIVE_POLICY_PROBLEM"}
    ok = status not in BLOCKING

    if a.write_manifest:
        manifest = {
            "schemaVersion": "wuq-deploy-manifest-v1",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            # Gate 1：部署自身必须能回答「这是哪一版」。
            # 修前这里没有任何版本身份，只能靠 sourceDir 这个路径猜。
            "packageVersion": _read_version(a.installed),
            # #16：清单原先没有状态字段 —— 校验失败时也照写一份，
            # 而失败与成功写出来的清单长得一模一样，读的人无从分辨。
            "status": "MATCH" if ok else "SOURCE_IDENTITY_DRIFT",
            "sourceDir": os.path.abspath(a.source),
            "installedDir": os.path.abspath(a.installed),
            # #10：记录【我们真正装过去的】文件，供下一次清理判归属。
            # items 是"目录里有什么"（含第三方放进去的文件），不能拿来判归属；
            # ownedPaths 才是"我们装了什么"，这才是按身份判。
            "ownedPaths": sorted(src.keys()),
            "items": [{"path": k, "sha256": v["sha256"], "bytes": v["bytes"]}
                      for k, v in sorted(dst.items())],
            "effectivePolicyDigest": sha256_obj(policy) if policy else None,
            # ── Gate 1 · Source Identity（3.5.10 / #49）──
            # 「这次安装的源快照是谁」。写入后，下一次校验就能区分
            #   · 源码合法前进（快照变了但身份自证）→ SOURCE_ADVANCED
            #   · 部署被人动过（清单 items 对不上）  → DEPLOYMENT_DRIFT
            # 这两个概念原先被「版本号是否相同」混在一起判。
            #
            # ⚠️ 只有源身份【自证通过】时才写这个键。
            #    源身份缺失/不 OK 时写 null 是错的 —— 那会把
            #    「这次没检查源身份」伪装成「legacy 老部署」，
            #    而 legacy 的语义是「引入该机制之前就已装好的部署」。
            #    不写键 = 保持「无声明」这个诚实状态，下次仍判 UNPROVEN。
            **({"sourceSnapshot": sidr["snapshot"]} if sidr["ok"] else {}),
        }
        manifest["manifestDigest"] = sha256_obj(manifest, ("manifestDigest",))
        mp = os.path.join(a.installed, "..", "DEPLOY_MANIFEST.json")
        mp = os.path.abspath(mp)
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, mp)

    BLOCKING = {"MANIFEST_INTEGRITY_FAILURE", "DEPLOYMENT_DRIFT",
                "SOURCE_IDENTITY_UNPROVEN", "SOURCE_IDENTITY_DRIFT"}

    result = {
        "status": status,
        "blocking": status in BLOCKING,
        "packageVersion": _read_version(a.installed),
        "sourceDir": os.path.abspath(a.source),
        "installedDir": os.path.abspath(a.installed),
        "sourceFiles": len(src),
        "installedFiles": len(dst),
        "fileProblems": file_problems,
        "effectiveProblems": eff_problems,
        "effectiveBudgetKeys": sorted(
            k for k in (policy or {}).get("budget", {}) if not k.startswith("_")),
        # ── 分层判定明细（3.5.10 / #49）──
        # 四层各自的结果必须分开报，否则读的人分不清
        # 「部署被改」和「源码合法前进」—— 这正是本轮要修的误判。
        "layers": {
            "manifestIntegrity": {"ok": manifest_ok, "detail": manifest_why},
            "deployedIntegrity": {
                "ok": not (old_manifest is not None
                           and _check_deployed_integrity(a.installed, old_manifest)),
                "detail": "（无清单，跳过）" if old_manifest is None else "清单比对",
            },
            "currentSourceIdentity": {
                "ok": sidr["ok"], "detail": sidr["reason"],
                "declared": sidr["declared"], "actual": sidr["snapshot"],
            },
            "legacyManifest": legacy,
        },
        # Claim 边界：机器能力、Threat Model、允许的 Claim 必须对齐。
        # 放进 JSON 是为了让消费这个输出的人/程序也能看到边界，而不是只看到 MATCH。
        "claimBoundary": ("T1 本地一致性 only：source identity 只证明源码目录内部自洽"
                          "且与声明一致；不证明源码正确、可部署、经过测试；"
                          "不防有写权限者同时改源与声明（T2 NOT_PROTECTED）。"
                          "见包内 install.md 的「已知边界」。"),
    }

    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 70)
        # ⚠️ 标题不能说成「生效校验」—— 它证明不了"生效"，只证明"文件一致"。
        #    这是本包最容易造成的误读：绿灯被读成"门工作正常"。见包内 install.md 的「已知边界」。
        print("部署文件一致性校验（不是「门能不能拦」的检验）")
        print("=" * 70)
        print("源:     %s  (%d 个文件)" % (result["sourceDir"], len(src)))
        print("已安装: %s  (%d 个文件)" % (result["installedDir"], len(dst)))
        print()
        print("L2 生效策略 budget 实际键: %s" % result["effectiveBudgetKeys"])
        print()
        if file_problems:
            print("L1 文件级问题 (%d):" % len(file_problems))
            for p in file_problems:
                print("  " + p)
            print()
        if eff_problems:
            print("L2 生效级问题 (%d):" % len(eff_problems))
            for p in eff_problems:
                print("  " + p)
            print()
        print("=" * 70)
        print("分层判定（3.5.10 / #49 —— 两层概念必须分开报）：")
        L = result["layers"]
        print("  L1 MANIFEST_INTEGRITY      : %s  %s"
              % ("PASS" if L["manifestIntegrity"]["ok"] else "FAIL",
                 L["manifestIntegrity"]["detail"]))
        print("  L2 DEPLOYED_INTEGRITY      : %s  %s"
              % ("PASS" if L["deployedIntegrity"]["ok"] else "FAIL",
                 L["deployedIntegrity"]["detail"]))
        print("  L3 CURRENT_SOURCE_IDENTITY : %s  %s"
              % ("PASS" if L["currentSourceIdentity"]["ok"] else "FAIL",
                 L["currentSourceIdentity"]["detail"]))
        print("  L4 版本/快照关系            : %s"
              % ("legacy 清单（无 sourceSnapshot）"
                 if L["legacyManifest"] else "新清单（含 sourceSnapshot）"))
        print()
        print("判定: %s" % result["status"])
        if result["status"] == "SOURCE_ADVANCED":
            print("      说明：同一个未发布版本号下，源码合法前进了一步。")
            print("            部署侧完好、源身份自证通过 —— 允许升级。")
        if result["status"] == "LEGACY_BASELINE_MIGRATION":
            print("      说明：这是首次引入 source identity 之前的存量部署。")
            print("            L1/L2/L3 全过 + 版本一致 → 允许【一次】迁移。")
            print("            迁移后清单会写入 sourceSnapshot，此后再不走 legacy。")
        # Claim 边界：结论必须限定在证据能支撑的范围内。
        # 没有这三行的后果是实测过的：MATCH 会被读成"门装好了、能拦了"。
        print("      边界：本判定只覆盖【T1 本地一致性】——")
        print("            已装文件与清单一致 + 源码目录内部自洽。")
        print("            它不证明门的行为正确，也不证明源本身是对的。")
        print("            详见包内 install.md 的「已知边界」。")
        if not ok:
            print()
            print("%s —— 声明的修订 ≠ 实际运行的产物。" % result["status"])
            if result["status"] in ("SOURCE_IDENTITY_UNPROVEN",):
                print("源身份无法证明。若这是你刚改完 hooks 的预期结果，显式刷新：")
                print("    python tools/source_identity.py --write")
                print("（install / verify / CI 都不会自动刷新 —— 那是设计意图）")
            print("Checkpoint 是派生结果：不要在此基础上继续，先修正部署。")
        print("=" * 70)

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
