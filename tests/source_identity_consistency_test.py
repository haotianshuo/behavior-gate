# -*- coding: utf-8 -*-
"""源身份一致性回归（REAL_REGRESSION）

这个测试存在的理由（用户第 5 点）：
    将来有人往部署集合里加文件（比如新增一个 hook），
    如果只改了 install 的复制规则却没进 source snapshot，
    source identity 就会漏掉那个文件 —— 而【没有任何测试会变红】。

    所以这里做一个集合一致性回归：断言
        is_tracked 的判定结果
        == source_identity.scan_managed 扫到的集合
        == install 的复制集合
    三者必须同源同集。

运行：python tests/source_identity_consistency_test.py
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PKG, "lib"))
sys.path.insert(0, os.path.join(PKG, "tools"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import deploy_files  # noqa: E402
import src_identity as sid  # noqa: E402

HOOKS = os.path.join(PKG, "adapters", "claude-code", "hooks")

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("  %-6s %s" % ("PASS" if ok else "**FAIL**", name))
    if not ok and detail:
        print("         %s" % detail)


def main():
    print("=" * 88)
    print("源身份一致性回归")
    print("=" * 88)

    # ---------- 1. 集合同源 ----------
    print("\n----- 1. managed-file 集合必须只有一处定义 -----")
    scanned = set(sid.scan_managed(HOOKS).keys())
    print("        实际扫到 %d 个文件：%s"
          % (len(scanned), ", ".join(sorted(scanned))))

    # 逐个文件用 is_tracked 复核 —— 两者必须一致
    mismatched = []
    for name in scanned:
        if not deploy_files.is_tracked(name):
            mismatched.append(name)
    check("扫到的文件都通过 is_tracked", not mismatched,
          "不一致：%s" % mismatched)

    # 反向：目录里存在、is_tracked 为真、但没被扫到的文件
    missed = []
    for base, _d, files in os.walk(HOOKS):
        if "__pycache__" in base:
            continue
        for n in files:
            if deploy_files.is_tracked(n) and "__pycache__" not in base:
                rel = os.path.relpath(os.path.join(base, n), HOOKS)
                rel = rel.replace("\\", "/")
                if rel not in scanned:
                    missed.append(rel)
    check("is_tracked 为真的文件都被扫到", not missed,
          "漏扫：%s" % missed)

    # ---------- 2. VERSION 必须在集合里 ----------
    print("\n----- 2. VERSION 必须参与源身份 -----")
    check("VERSION 在扫描集合里", "VERSION" in scanned,
          "集合=%s" % sorted(scanned))

    # ---------- 3. SOURCE_IDENTITY.json 不得进集合 ----------
    print("\n----- 3. 源身份声明自身不得参与 snapshot -----")
    # 它在元数据层（hooks 的上一层），不该被扫到
    check("SOURCE_IDENTITY.json 不在 hooks 扫描集合里",
          "SOURCE_IDENTITY.json" not in scanned)
    # 即便有人把它放进 hooks/，is_tracked 也应该拒绝（.json 不在后缀里）
    check("is_tracked 不接受 SOURCE_IDENTITY.json",
          not deploy_files.is_tracked("SOURCE_IDENTITY.json"))

    # ---------- 4. snapshot 对内容变化敏感 ----------
    print("\n----- 4. snapshot 必须对内容变化敏感 -----")
    tmp = tempfile.mkdtemp(prefix="sid-cons-")
    try:
        a = os.path.join(tmp, "a")
        os.makedirs(a)
        for n in ("VERSION", "_lib.py", "gate.py"):
            open(os.path.join(a, n), "w", encoding="utf-8").write("x\n")
        s1 = sid.compute_snapshot(a)

        # 改内容
        open(os.path.join(a, "gate.py"), "a", encoding="utf-8").write("y\n")
        s2 = sid.compute_snapshot(a)
        check("改内容 → snapshot 变化", s1 != s2)

        # 加新文件
        open(os.path.join(a, "new_gate.py"), "w", encoding="utf-8").write("z\n")
        s3 = sid.compute_snapshot(a)
        check("加 managed 文件 → snapshot 变化", s2 != s3)

        # 加一个 non-managed 文件（.md）→ 不该变
        open(os.path.join(a, "notes.md"), "w", encoding="utf-8").write("doc\n")
        s4 = sid.compute_snapshot(a)
        check("加 non-managed 文件（.md）→ snapshot 不变", s3 == s4)

        # 删文件
        os.remove(os.path.join(a, "new_gate.py"))
        s5 = sid.compute_snapshot(a)
        check("删 managed 文件 → snapshot 变化", s4 != s5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 5. 声明与实际必须可比 ----------
    print("\n----- 5. 声明结构必须完整 -----")
    ident_p = sid.identity_path(sid.default_pkg_dir(HOOKS))
    if os.path.isfile(ident_p):
        import json
        d = json.load(open(ident_p, encoding="utf-8"))
        check("schemaVersion 正确", d.get("schemaVersion") == sid.SCHEMA_ID,
              "实际 %r" % d.get("schemaVersion"))
        check("algorithm 正确", d.get("algorithm") == sid.ALGORITHM,
              "实际 %r（期望 %r）" % (d.get("algorithm"), sid.ALGORITHM))
        check("snapshot 存在且为 64 位 hex",
              isinstance(d.get("snapshot"), str) and len(d["snapshot"]) == 64)
        check("version 与 VERSION 文件一致",
              d.get("version") == sid.read_version(HOOKS),
              "声明 %r / 文件 %r" % (d.get("version"),
                                     sid.read_version(HOOKS)))
    else:
        check("源身份声明存在", False, "缺少 %s" % ident_p)

    # ---------- 汇总 ----------
    npass = sum(1 for _, ok, _ in _results if ok)
    print("\n" + "=" * 88)
    print("源身份一致性回归：%d / %d 通过" % (npass, len(_results)))
    print("=" * 88)
    if npass != len(_results):
        print("\n失败项：")
        for n, ok, d in _results:
            if not ok:
                print("  - %s：%s" % (n, d))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
