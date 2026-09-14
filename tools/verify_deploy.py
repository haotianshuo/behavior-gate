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
import sys
from datetime import datetime, timezone
from pathlib import Path

# 「哪些文件属于被同步/被校验的集合」只能有一处定义 —— 见该模块的文件头。
# install.py 的 _collect 与本文件的 scan_dir 都从它取，避免再次出现两边不一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from deploy_files import is_tracked  # noqa: E402


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
    a = ap.parse_args()

    src = scan_dir(a.source)
    dst = scan_dir(a.installed)

    file_problems = compare_files(src, dst)
    policy, perr = load_effective_policy(a.installed)
    eff_problems = check_effective(policy) if perr is None else [perr]

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
    # ⚠️ STALE 只在【同版本】时才等于漂移。
    #
    #    实测缺陷（本轮暴露）：装一个改了 hook 文件的新版本时，部署侧是旧版本，
    #    内容必然不一致 → STALE → 预检中止 → 每次升级都要人工加 --force。
    #    这与"升级不该需要 --force"直接矛盾。
    #
    #    根因还是 Gate 1：没有版本身份，就分不清两种情况 ——
    #      · 部署被人改过（真漂移，必须拦）
    #      · 我们发布了新版本（正常升级，不该拦）
    #    VERSION 正好补上这个身份：
    #      部署版本 == 源版本 → 内容还不一致 = 真漂移 → 拦
    #      部署版本 != 源版本 → 这是一次升级 → 不拦（覆盖正是本次操作的意图）
    #    没有 VERSION 的老部署（读到 None）同样按"版本不同"处理，即允许升级。
    same_version = (_read_version(a.source) == _read_version(a.installed))

    real_drift = ([p for p in file_problems if p.startswith("STALE")]
                  if same_version else [])
    if a.require_complete:
        real_drift += [p for p in file_problems if p.startswith("MISSING")]
    ok = not real_drift and not eff_problems

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
        }
        manifest["manifestDigest"] = sha256_obj(manifest, ("manifestDigest",))
        mp = os.path.join(a.installed, "..", "DEPLOY_MANIFEST.json")
        mp = os.path.abspath(mp)
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, mp)

    result = {
        "status": "MATCH" if ok else "SOURCE_IDENTITY_DRIFT",
        "packageVersion": _read_version(a.installed),
        "sourceDir": os.path.abspath(a.source),
        "installedDir": os.path.abspath(a.installed),
        "sourceFiles": len(src),
        "installedFiles": len(dst),
        "fileProblems": file_problems,
        "effectiveProblems": eff_problems,
        "effectiveBudgetKeys": sorted(
            k for k in (policy or {}).get("budget", {}) if not k.startswith("_")),
        # Claim 边界：机器能力、Threat Model、允许的 Claim 必须对齐。
        # 放进 JSON 是为了让消费这个输出的人/程序也能看到边界，而不是只看到 MATCH。
        "claimBoundary": ("T1 本地一致性 only：只证明已安装文件与源逐字节一致；"
                          "不证明门的行为正确，也不证明源本身正确。见包内 install.md 的「已知边界」。"),
        # 版本不同时 STALE 被豁免，MATCH 的含义变成"没有【无法解释的】漂移"。
        # 不把这件事显式说出来，输出就会变成"边列 STALE 边判 MATCH"的自相矛盾形状。
        "versionChanged": (not same_version),
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
        print("判定: %s" % result["status"])
        if result["versionChanged"]:
            # ⚠️ 不说这一句，输出就退化成"边列 STALE 边判 MATCH"——
            #    正是本包自己批评过的自相矛盾形状。判定必须说清它的含义。
            print("      说明：源与已安装的 VERSION 不同（源 %s / 已安装 %s）——"
                  % (_read_version(a.source), _read_version(a.installed)))
            print("            内容差异按【版本变更】处理，不计为漂移（这是升级的正常形态）；")
            print("            此处的 MATCH 意为「没有无法解释的漂移」，不是「内容完全相同」。")
        # Claim 边界：结论必须限定在证据能支撑的范围内。
        # 没有这三行的后果是实测过的：MATCH 会被读成"门装好了、能拦了"。
        print("      边界：本判定只覆盖【已安装文件与源逐字节一致】（T1 本地一致性）。")
        print("            它不证明门的行为正确，也不证明源本身是对的。")
        print("            详见包内 install.md 的「已知边界」。")
        if not ok:
            print()
            print("SOURCE_IDENTITY_DRIFT —— 声明的修订 ≠ 实际运行的产物。")
            print("Checkpoint 是派生结果：不要在此基础上继续，先修正部署。")
        print("=" * 70)

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
