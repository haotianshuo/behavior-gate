# -*- coding: utf-8 -*-
"""源身份（SOURCE_IDENTITY）—— 包以机器可读方式声明「我这一版源码长什么样」。

## 为什么需要它

本项目的部署校验原先只有一层：**部署侧**的 hash 与清单比对。
它能回答「已装副本有没有被改」，但回答不了另一个问题：

    源码合法地往前走了（同一个未发布版本号下继续修订），
    与「部署被人动过」长得一模一样。

实测坐实（2026-09-15）：本机部署 3.5.10、源码也是 3.5.10、
但 `destructive_gate.py` 内容不同 → 旧判据判 `SOURCE_IDENTITY_DRIFT` 并拦下。
而 3.5.10 当时【没有 tag、没有 Release】，是未发布开发快照 ——
合法修订被误判成了篡改。

根因：旧判据用「版本号是否相同」当作两个概念的代理，
而版本号相同既可能是「被篡改」，也可能是「未发布版本原地修订」。

## 两个概念的拆分

    DEPLOYED_INTEGRITY : 已装副本 vs 它安装时的 DEPLOY_MANIFEST
    SOURCE_IDENTITY    : 源码是否声明了一个可自证的、内部一致的可部署快照

本模块只管后者。前者的实现留在 verify_deploy.py。

## 自证方式

    snapshot = sha256 over sorted[(relative_path, sha256), ...]

校验时**重算** snapshot 与声明比对：
    一致  → 声明诚实地描述了当前字节 → 内部一致的可部署快照
    不一致 → 声明与实际不符（DIRTY）→ 拦

`SOURCE_IDENTITY.json` 自身**不在** hooks/ 目录里，
因此天然不参与自己的 snapshot 计算。

## 文件集合

复用 lib/deploy_files.is_tracked —— 与 install 的复制规则、
verify_deploy 的比对规则**同一处定义**。
不在这里维护第二份文件列表：历史上「两处各写一份」已经不一致过两次，
（.py/.cmd vs 多算 .json；listdir vs rglob），这个教训写进了 deploy_files.py 的头注释。

## ⚠️ 边界（必须如实声明）

这是 **T1 本地一致性**证据：

    证明    : 源码目录内部自洽，且与声明一致
    不证明  : 源码是正确的、可部署的、经过测试的
    不防    : 有写权限者同时改源与声明（T2 NOT_PROTECTED）
    不构成  : 不可篡改 / 不可抵赖（T3 OUT_OF_SCOPE）

sha256 在本地只能检测「内容变化 / 重排 / 删除」，
它不是 malicious-writer-proof，也不是 non-repudiation 证据。

## 写权限

本模块提供 `write_identity()`，但**只有显式操作才允许调用**。

    tools/source_identity.py            # 默认：read + check
    tools/source_identity.py --write    # 显式刷新

install.py / verify_deploy.py / CI **都不得自动调用 write**。
理由：若安装过程会自动刷新，那么任何人让一份 dirty source 跑一次安装，
它就把自己「合法化」了 —— source identity 会立刻失去意义。
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_files import is_tracked  # noqa: E402

IDENTITY_NAME = "SOURCE_IDENTITY.json"
SCHEMA_ID = "wuq-source-identity-v1"

# ⚠️ 算法名必须诚实：它扫的是【managed files】（由 is_tracked 决定），
#    不是 git tracked files。原拟名 sha256-sorted-tracked-v1 不准确。
ALGORITHM = "sha256-sorted-managed-v1"

SKIP_SUFFIX = (".pyc",)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_bytes(obj, exclude=()):
    """稳定序列化 —— 与 verify_deploy 的同名函数语义一致。"""
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in exclude}
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def sha256_obj(obj, exclude=()):
    return hashlib.sha256(canonical_bytes(obj, exclude)).hexdigest()


def scan_managed(root):
    """扫描 hooks 目录里的 managed files。

    集合由 is_tracked 决定（唯一定义）—— 与 install 复制、
    verify_deploy 比对保持同一处规则。
    """
    out = {}
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return out
    for base, _dirs, files in os.walk(root):
        if "__pycache__" in base.split(os.sep):
            continue
        for name in files:
            if not is_tracked(name):
                continue
            if any(name.endswith(s) for s in SKIP_SUFFIX):
                continue
            p = os.path.join(base, name)
            rel = os.path.relpath(p, root).replace("\\", "/")
            out[rel] = {"sha256": sha256_file(p),
                        "bytes": os.path.getsize(p)}
    return out


def compute_snapshot(root):
    """对 managed files 取快照摘要。返回 None 表示目录不可读。"""
    files = scan_managed(root)
    if not files:
        return None
    return sha256_obj(sorted((k, v["sha256"]) for k, v in files.items()))


def identity_path(pkg_dir):
    """SOURCE_IDENTITY.json 的路径。

    pkg_dir = adapters/claude-code/ 这一层（元数据层，不是 hooks/）。
    """
    return os.path.join(pkg_dir, IDENTITY_NAME)


def default_pkg_dir(hooks_dir):
    """从 hooks 目录推出元数据层目录（hooks 的上一层）。"""
    return os.path.dirname(os.path.abspath(hooks_dir))


def load_identity(pkg_dir):
    """读声明。返回 (dict|None, 错误说明)"""
    p = identity_path(pkg_dir)
    if not os.path.isfile(p):
        return None, "源身份声明缺失：%s" % p
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, "源身份声明无法解析：%r" % (e,)


def read_version(hooks_dir):
    try:
        with open(os.path.join(hooks_dir, "VERSION"),
                  "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:
        return None


def check(pkg_dir, hooks_dir):
    """校验源身份是否自证。

    返回 dict：
        ok         bool
        snapshot   当前实际快照（目录不可读时为 None）
        declared   声明里的 snapshot
        reason     人类可读说明
    """
    d, err = load_identity(pkg_dir)
    if d is None:
        # 声明缺失时也把【实际快照】算出来 —— 否则输出只能显示"不可读"，
        # 而"源码其实读得到、只是没声明"与"源码读不到"是两回事。
        return {"ok": False, "snapshot": compute_snapshot(hooks_dir),
                "declared": None, "reason": err}

    actual = compute_snapshot(hooks_dir)
    if actual is None:
        return {"ok": False, "snapshot": None,
                "declared": d.get("snapshot"),
                "reason": "hooks 目录里没有任何 managed file（不可读或为空）"}

    if d.get("schemaVersion") != SCHEMA_ID:
        return {"ok": False, "snapshot": actual,
                "declared": d.get("snapshot"),
                "reason": "schema 不符：%r（期望 %r）"
                          % (d.get("schemaVersion"), SCHEMA_ID)}

    if d.get("algorithm") != ALGORITHM:
        return {"ok": False, "snapshot": actual,
                "declared": d.get("snapshot"),
                "reason": "算法不符：%r（期望 %r）"
                          % (d.get("algorithm"), ALGORITHM)}

    declared = d.get("snapshot")
    if not declared:
        return {"ok": False, "snapshot": actual, "declared": None,
                "reason": "声明不完整：缺 snapshot"}

    if declared != actual:
        return {"ok": False, "snapshot": actual, "declared": declared,
                "reason": "源身份 DIRTY：声明 %s ≠ 实际 %s"
                          % (declared[:12], actual[:12])}

    v_src = read_version(hooks_dir)
    if d.get("version") != v_src:
        return {"ok": False, "snapshot": actual, "declared": declared,
                "reason": "声明版本与 VERSION 不一致：%r ≠ %r"
                          % (d.get("version"), v_src)}

    return {"ok": True, "snapshot": actual, "declared": declared,
            "reason": "源身份自证通过（%s）" % actual[:12]}


def write_identity(pkg_dir, hooks_dir):
    """显式刷新 SOURCE_IDENTITY.json。

    ⚠️ 只有 tools/source_identity.py --write 应该调用它。
       install / verify_deploy / CI 都不得自动调用 ——
       否则 dirty source 跑一次安装就把自己合法化了。

    返回 (ok, message)
    """
    snap = compute_snapshot(hooks_dir)
    if snap is None:
        return False, "hooks 目录里没有 managed file，拒绝写入"
    v = read_version(hooks_dir)
    if not v:
        return False, "hooks 目录里没有 VERSION，拒绝写入（身份必须有版本）"

    doc = {
        "schemaVersion": SCHEMA_ID,
        "version": v,
        "algorithm": ALGORITHM,
        "snapshot": snap,
    }
    p = identity_path(pkg_dir)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, p)
    except Exception as e:
        return False, "写入失败：%r" % (e,)
    return True, "已写入 %s（version=%s snapshot=%s）" % (p, v, snap[:12])
