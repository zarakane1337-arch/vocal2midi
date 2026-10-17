"""vocal2midi 核心层：与界面无关的纯 Python 逻辑。

这一层不 import PySide6，可被界面层、安装器、单元测试直接复用。
"""

from __future__ import annotations

__all__ = [
    "params",
    "separation",
    "transcription",
    "pipeline",
    "net",
    "mirrors",
    "installer",
    "weights",
    "environment",
    "settings",
]
