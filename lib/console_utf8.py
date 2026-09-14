# -*- coding: utf-8 -*-
"""把标准输出/错误强制为 UTF-8。

为什么需要这个：
    Windows 的控制台默认编码是 GBK（cp936）。脚本里 print 中文时，
    Python 会尝试用 GBK 编码 —— 遇到 GBK 表里没有的字符（如 `第`）
    就抛 UnicodeEncodeError，脚本直接崩。

    实测：CI 在 windows-latest 上 2/2 job 失败，ubuntu/macos 全过。
    本地 Git Bash 恰好是 UTF-8 环境，所以这个缺陷在开发者机器上
    【看不见】—— 换到干净的 Windows 就崩。

    这不是测试环境问题，是产品缺陷：用户在自己的 Windows 上
    跑 install.py 会看到同样的崩溃。

用法（在任何会 print 中文的入口脚本顶部调用）：
    from console_utf8 import force_utf8_console
    force_utf8_console()

放在入口脚本而不是被导入的模块里，是因为 reconfigure 是
对【进程级】流对象的操作，越早执行越好。
"""

import sys


def force_utf8_console():
    """把 stdout / stderr 重设为 UTF-8。

    幂等，可重复调用。旧版 Python 或已被包装过的流上静默跳过 ——
    这个函数本身不能成为新的崩溃源。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 流不支持 reconfigure（被包装、被重定向、老版本 Python）：
            # 跳过。宁可输出乱码，也不要在处理编码问题时崩掉。
            pass


if __name__ == "__main__":
    force_utf8_console()
    print("编码检查：中文正常输出 ✓")
    print("stdout encoding:", getattr(sys.stdout, "encoding", "?"))
