"""路径与运行环境定位。

所有「文件放哪」的问题都集中在这里，其余模块只调用函数，不自己拼路径。

两种运行形态都要支持：
    源码/开发态   src/vocal2midi/paths.py  → 项目根 = src/ 的父目录
    安装态        <安装目录>/python/Lib/site-packages/vocal2midi/paths.py
                  → 程序根 = <安装目录>（由环境变量 V2M_ROOT 显式指定，最可靠）
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def app_root() -> Path:
    """程序根目录：安装目录（安装态）或源码仓库根（开发态）。

    安装态的布局是
        <root>/runtime/python/Lib/site-packages/vocal2midi/paths.py
    层级并不固定（可能放在 site-packages，也可能平铺在 app/ 下），
    所以不数层数，而是向上找第一个「带 runtime 子目录」的祖先。
    """
    explicit = _env_path("V2M_ROOT")
    if explicit is not None:
        return explicit
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "runtime").is_dir():
            return parent
    # 开发态布局：<root>/src/vocal2midi/paths.py
    return here.parents[2]


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def is_frozen() -> bool:
    """是否为安装后的发行版运行态（存在 runtime 目录）。"""
    return (app_root() / "runtime").is_dir()


def runtime_dir() -> Path:
    return app_root() / "runtime"


def python_dir() -> Path:
    """内置解释器目录；开发态下退回当前解释器所在目录。"""
    override = _env_path("V2M_PYTHON_DIR")
    if override is not None:
        return override
    candidate = runtime_dir() / "python"
    if candidate.is_dir():
        return candidate
    return Path(sys.executable).resolve().parent


def python_exe(windowed: bool = False) -> Path:
    name = "pythonw.exe" if windowed else "python.exe"
    candidate = python_dir() / name
    if candidate.exists():
        return candidate
    return Path(sys.executable).resolve()


def bin_dir() -> Path:
    """随程序分发的可执行文件（ffmpeg 等）。"""
    candidate = runtime_dir() / "bin"
    if candidate.is_dir():
        return candidate
    return app_root() / "bin"


def ffmpeg_exe() -> Path | None:
    for name in ("ffmpeg.exe", "ffmpeg"):
        candidate = bin_dir() / name
        if candidate.exists():
            return candidate
    found = _which("ffmpeg")
    return Path(found) if found else None


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def site_packages() -> Path:
    """依赖安装目标目录。

    安装器通常由**另一个** Python 启动、却要往内置解释器里装包，
    所以默认以 python_dir() 为准；只有当前解释器就是内置解释器时才问 sysconfig。
    """
    marker = "Lib" if os.name == "nt" else "lib"
    target = python_dir()
    try:
        current = Path(sys.executable).resolve().parent
        if current == target.resolve():
            import sysconfig

            resolved = sysconfig.get_paths().get("purelib")
            if resolved:
                return Path(resolved)
    except OSError:
        pass
    return target / marker / "site-packages"


def models_dir() -> Path:
    """随程序附带的模型目录（当前版本不含权重，仅保留扩展位）。"""
    return app_root() / "models"


def torch_home() -> Path:
    raw = os.environ.get("TORCH_HOME", "").strip()
    if raw:
        return Path(raw)
    return user_cache_dir() / "torch"


def demucs_checkpoints_dir() -> Path:
    """demucs 通过 torch.hub 缓存的 .th 权重实际落盘位置。"""
    return torch_home() / "hub" / "checkpoints"


def apply_torch_home() -> Path:
    """把 TORCH_HOME 固定到程序自己的缓存目录。

    demucs 通过 `torch.hub.load_state_dict_from_url` 取权重，缓存位置由
    TORCH_HOME 决定。默认值是 `~/.cache/torch`，与 `core.weights` 下载的
    目录不同 —— 如果不统一，权重会下载两次（第二次还走慢速官方源）。
    因此所有入口（界面 / 安装器 / 自检脚本）都要先调用这个函数。
    """
    target = torch_home()
    os.environ.setdefault("TORCH_HOME", str(target))
    return Path(os.environ["TORCH_HOME"])


def apply_runtime_env() -> Path:
    """固定缓存目录，并让模型库直接使用本地缓存。

    demucs 4.x 的 `pretrained.get_model()` 会**先尝试 HuggingFace hub**，
    失败后才回退到 torch.hub 缓存。国内访问 HF 会长时间挂起（实测 >40 秒
    仍在等待），导致预下载好的权重看起来「没用上」。我们的权重由
    `core.weights` 用多连接下载器直接落到 torch.hub 缓存目录，因此显式关闭
    HF 在线探测，让 demucs 立刻命中本地缓存（实测 1.1 秒加载完成）。
    """
    target = apply_torch_home()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    return target


def _writable_dir(candidates: list[Path]) -> Path:
    """返回第一个能创建/写入的目录；全部失败时退回临时目录。"""
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        return path
    fallback = Path(tempfile.gettempdir()) / "vocal2midi"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def user_cache_dir() -> Path:
    override = _env_path("V2M_CACHE_DIR")
    if override is not None:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or tempfile.gettempdir()
    return _writable_dir([Path(base) / "vocal2midi" / "cache", app_root() / "cache"])


def user_data_dir() -> Path:
    """配置与日志：%APPDATA%\\vocal2midi；不可写时退回程序目录/临时目录。"""
    override = _env_path("V2M_DATA_DIR")
    if override is not None:
        return _writable_dir([override])
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home())
    return _writable_dir([Path(base) / "vocal2midi", app_root() / "data"])


def settings_file() -> Path:
    return user_data_dir() / "settings.json"


def log_file() -> Path:
    return user_data_dir() / "vocal2midi.log"


def default_output_dir() -> Path:
    return app_root() / "out"


def deps_marker() -> Path:
    """依赖安装完成标记（setup.exe 与本程序共同读取）。"""
    return runtime_dir() / ".deps-ok"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepend_to_path(path: Path) -> None:
    """把目录插到 PATH 最前，让 demucs 子进程能直接调用 ffmpeg。"""
    if not path.is_dir():
        return
    current = os.environ.get("PATH", "")
    entries = current.split(os.pathsep) if current else []
    text = str(path)
    if entries and entries[0] == text:
        return
    os.environ["PATH"] = text + (os.pathsep + current if current else "")
