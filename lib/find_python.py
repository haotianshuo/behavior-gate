# -*- coding: utf-8 -*-
"""Python 解释器探测 —— 让安装包在任何 Windows 机器上都能装。

为什么需要它（这是 1:1 复刻的头号阻塞）：
    原来所有配置里都写死 `C:/Program Files/Python312/python.exe`。
    换台电脑，可能是 Python311 / C:\\Python312 / py 启动器，
    或者根本没装 Python —— 那就整套门全部静默失效。

探测顺序（从最可靠到最兜底）：
    1. 当前正在运行的解释器（如果安装脚本本身就跑在 Python 里）
    2. py 启动器（Windows 官方，能处理多版本）
    3. PATH 里的 python / python3
    4. 常见安装路径扫描
    5. 注册表（Windows）

找不到时【不猜】，返回 None 让调用方给出人类可读的指引。
"""

import os
import shutil
import subprocess
import sys

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



def _works(exe, args_prefix=()):
    """验证一个解释器能否真正执行。返回版本字符串或 None。"""
    if not exe:
        return None
    try:
        p = subprocess.run(
            list(args_prefix) + [exe, "-c", "import sys;print(sys.version.split()[0])"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if p.returncode == 0:
            return p.stdout.decode("utf-8", "replace").strip()
    except Exception:
        pass
    return None


def _candidates():
    """产出 (显示名, 可执行, 前缀参数) 的候选序列。"""
    seen = set()

    def emit(name, exe, prefix=()):
        key = (exe, tuple(prefix))
        if exe and key not in seen:
            seen.add(key)
            yield_candidate(name, exe, prefix)

    out = []

    def yield_candidate(name, exe, prefix=()):
        out.append((name, exe, tuple(prefix)))

    # 1. 当前解释器
    emit("当前解释器", sys.executable)

    # 2. py 启动器（Windows 官方推荐）
    for v in ("-3.13", "-3.12", "-3.11", "-3.10", "-3"):
        emit("py %s" % v, "py", (v,))
    emit("py", "py")

    # 3. PATH
    for n in ("python", "python3"):
        emit(n, shutil.which(n) or n)

    # 4. 常见安装路径
    roots = [
        r"C:\Python313", r"C:\Python312", r"C:\Python311", r"C:\Python310",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python313"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python312"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python311"),
    ]
    for base in (r"C:\Program Files", r"C:\Program Files (x86)"):
        for v in ("Python313", "Python312", "Python311", "Python310"):
            roots.append(os.path.join(base, v))
    for r in roots:
        emit(r, os.path.join(r, "python.exe"))

    # 5. 注册表（仅 Windows）
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (r"SOFTWARE\Python\PythonCore",
                        r"SOFTWARE\WOW6432Node\Python\PythonCore"):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        i = 0
                        while True:
                            try:
                                ver = winreg.EnumKey(k, i)
                            except OSError:
                                break
                            i += 1
                            try:
                                with winreg.OpenKey(
                                        k, ver + r"\InstallPath") as ik:
                                    path, _ = winreg.QueryValueEx(ik, "")
                                    emit("注册表 %s" % ver,
                                         os.path.join(path, "python.exe"))
                            except Exception:
                                pass
                except Exception:
                    pass
    except Exception:
        pass

    return out


def find_python(verbose=False):
    """返回 (可执行路径, 版本字符串, 显示名, 是否需要前缀参数)。

    找不到返回 (None, None, None, None)。
    """
    for name, exe, prefix in _candidates():
        ver = _works(exe, prefix)
        if ver:
            if verbose:
                print("  [探测] 命中：%s -> %s (%s)" % (name, exe, ver))
            return exe, ver, name, (list(prefix) or None)
    return None, None, None, None


if __name__ == "__main__":
    print("正在探测本机 Python …")
    exe, ver, name, prefix = find_python(verbose=True)
    if not exe:
        print()
        print("  [!] 没找到可用的 Python。")
        print("      这套门需要 Python 3.8+ 才能运行。")
        print("      下载：https://www.python.org/downloads/windows/")
        print("      安装时请勾选 “Add Python to PATH”。")
        sys.exit(1)
    print()
    print("  命中：%s" % name)
    print("  路径：%s" % exe)
    print("  版本：%s" % ver)
    print("  前缀参数：%s" % (prefix or "（无）"))
