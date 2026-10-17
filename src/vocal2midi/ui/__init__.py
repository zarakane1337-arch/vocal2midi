"""界面层：PySide6 实现。

只有这一层可以 import PySide6；core 层保持无界面依赖，便于单独测试与复用。
"""

from __future__ import annotations

__all__ = ["theme", "widgets", "main_window", "deps_dialog"]
