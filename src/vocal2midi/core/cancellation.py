"""取消语义的共享定义。

`pipeline` 与 `separation` 都要用它，而 `pipeline` 已经 import 了 `separation`，
所以放在两者之外，避免循环导入，也让「用户主动取消」这个概念只有一个定义。
"""

from __future__ import annotations


class Cancelled(RuntimeError):
    """用户主动取消。"""
