"""应用入口：由安装目录下的 `vocal2midi.exe` 调起 `pythonw -m vocal2midi`。

启动顺序刻意保持「永不卡住」：
    1. 先起 Qt 与主窗口（依赖缺失也能进界面）
    2. 窗口出现后再异步做环境自检
    3. 缺依赖时由界面弹「依赖安装」对话框，而不是在启动阶段阻塞

本程序只提供图形界面。`--check` 是给安装器/自检用的隐藏开关，不属于面向用户的
命令行功能，因此不做参数解析、不接受音频路径。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import APP_NAME, APP_TITLE, __version__, paths

log = logging.getLogger("vocal2midi.app")


def _configure_logging() -> None:
    handlers: list[logging.Handler] = []
    try:
        log_path = paths.log_file()
        paths.ensure_dir(log_path.parent)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers or [logging.NullHandler()],
        force=True,
    )


def _prepare_environment() -> None:
    """把运行期依赖的路径准备好（ffmpeg、torch 缓存、Qt 插件）。"""
    paths.prepend_to_path(paths.bin_dir())
    # 固定 torch 缓存目录并关闭 HuggingFace 在线探测（详见 paths.apply_runtime_env）
    paths.apply_runtime_env()
    # 高分屏缩放：交给 Qt 6 的默认行为，避免界面被二次放大
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")


def _load_app_icon():
    """窗口与任务栏图标（assets/vocal2midi.ico，随包分发）。"""
    from PySide6.QtGui import QIcon

    icon_path = Path(__file__).resolve().parent / "assets" / "vocal2midi.ico"
    return QIcon(str(icon_path)) if icon_path.is_file() else QIcon()


def run_gui(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _configure_logging()
    _prepare_environment()

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - 安装器会先装依赖
        _report_missing_pyside6(exc)
        return 2

    from .core import settings as settings_mod
    from .ui.main_window import MainWindow
    from .ui.theme import pick_ui_font

    app = QApplication(argv)
    # Fusion 是唯一在深浅两套 QSS 下表现一致的样式（原生样式会忽略部分调色板）
    app.setStyle("Fusion")
    app.setFont(pick_ui_font())
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_TITLE)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("vocal2midi")
    app.setDesktopFileName(APP_NAME)
    app.setWindowIcon(_load_app_icon())

    window = MainWindow(settings_mod.Settings.load())
    window.show()
    return app.exec()


def _report_missing_pyside6(exc: BaseException) -> None:
    message = (
        "缺少 PySide6，无法启动图形界面。\n\n"
        "请重新运行安装目录下的 vocal2midi.exe --repair 补装依赖。\n\n"
        f"详细信息：{exc}"
    )
    log.error("%s", message)
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, f"{APP_TITLE} - 缺少依赖", 0x10)
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    """应用入口：默认启动图形界面，`--check` 输出环境报告（供安装器调用）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--check":
        return _print_check()
    return run_gui([sys.argv[0], *args])


def _print_check() -> int:
    """输出环境自检 JSON；未就绪时返回码为 1。"""
    import json

    from .core import environment

    report = environment.inspect()
    payload = {
        "app_version": report.app_version,
        "python": report.python_version,
        "deps_ready": report.deps_ready,
        "missing": [label for _mod, label in report.missing],
        "ffmpeg": str(report.ffmpeg) if report.ffmpeg else None,
        "torch": report.torch_version,
        "cuda": report.cuda_version,
        "gpu": report.gpu_name,
        "weights_ready": report.weights_ready,
        "issues": report.issues(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
