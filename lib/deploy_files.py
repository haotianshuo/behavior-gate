# -*- coding: utf-8 -*-
"""部署文件清单 —— 「哪些文件属于被同步 / 被校验的集合」的【唯一定义】。

为什么必须只定义一次：
    install.py 的 _collect（决定复制哪些）和 verify_deploy.py 的 scan_dir
    （决定比对哪些）必须【完全一致】。不一致的两种后果都已经真实发生过：

      · 一边 .py/.cmd、另一边还多算了 .json
        → 源码里一旦出现 .json，就"源码有、永远装不上"，报 MISSING 卡死；
      · 一边 listdir（只顶层）、另一边 rglob（递归）
        → 往 hooks/ 放子目录，里面的文件永远装不上。

    两处各写一份规则，就必然会有第三、第四次。所以抽到这里，
    两边都 import 它 —— 加新文件类型时只改这一个地方。
"""

TRACKED_SUFFIXES = (".py", ".cmd")

# 非按后缀识别的身份文件（Gate 1 · Source Identity）：
# 包必须以机器可读的方式声明自己是哪一版，且该声明随 hooks 同步、参与校验。
TRACKED_NAMES = ("VERSION",)


def is_tracked(name):
    """name 是【文件名】（不含目录），大小写不敏感。"""
    n = (name or "")
    return n.endswith(TRACKED_SUFFIXES) or n in TRACKED_NAMES
