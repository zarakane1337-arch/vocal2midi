"""测试夹具。

这里只做一件事：让临时目录在本机可用。

受限环境（本项目的开发机就是）只允许访问 0o755 的目录，而 CPython 的
`tempfile.mkdtemp` 与被它驱动的 pytest `tmp_path` 都固定用 0o700 —— 于是
建目录会反复失败重试，测试直接卡死。这里提供一个自建的临时目录实现，
权限用 0o755，并把 `tmp_path` 换成它。

这与产品行为无关，正常机器上这些权限本来也够用。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_ROOT = os.path.join(_REPO_ROOT, ".cache", "tmp")
_DIR_MODE = 0o755


def _mkdtemp(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
    base = dir or tempfile.gettempdir()
    os.makedirs(base, mode=_DIR_MODE, exist_ok=True)
    for _ in range(100):
        path = os.path.join(base, (prefix or "tmp") + uuid.uuid4().hex[:12] + (suffix or ""))
        try:
            os.mkdir(path, _DIR_MODE)
        except FileExistsError:
            continue
        return os.path.abspath(path)
    raise FileExistsError("无法创建临时目录")


def _rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


os.makedirs(_TMP_ROOT, mode=_DIR_MODE, exist_ok=True)
# pytest 自己的 tmp_path 依赖 mkdtemp；整体替换掉，避免它用 0o700 卡住。
tempfile.mkdtemp = _mkdtemp  # type: ignore[assignment]
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", _TMP_ROOT)


@pytest.fixture
def tmp_path():
    """一个可写的临时目录。"""
    path = _mkdtemp(dir=_TMP_ROOT, prefix="t-")
    try:
        yield Path(path)
    finally:
        _rmtree(path)
