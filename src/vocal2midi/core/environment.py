"""运行环境自检：依赖、ffmpeg、模型权重、设备。

给 UI 一个统一的「环境状态」对象，避免界面里到处写 try/import。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__, paths
from . import installer, separation, weights


@dataclass
class EnvironmentReport:
    python_version: str = ""
    app_version: str = ""
    deps_ready: bool = False
    missing: list[tuple[str, str]] = field(default_factory=list)
    ffmpeg: Path | None = None
    torch_version: str | None = None
    cuda_version: str | None = None
    gpu_available: bool = False
    gpu_name: str = ""
    weights_ready: bool = False
    weights_bytes: int = 0

    @property
    def ready(self) -> bool:
        """能否立刻开始转换。"""
        return self.deps_ready and self.ffmpeg is not None

    @property
    def can_separate(self) -> bool:
        return self.ready and self.weights_ready

    def summary(self) -> str:
        parts = [f"Python {self.python_version}"]
        if self.torch_version:
            device = "CUDA" if self.gpu_available else "CPU"
            parts.append(f"torch {self.torch_version}（{device}）")
        parts.append("ffmpeg 已就绪" if self.ffmpeg else "缺少 ffmpeg")
        return " · ".join(parts)

    def issues(self) -> list[str]:
        """需要用户处理的问题清单（人类可读）。"""
        problems: list[str] = []
        if self.missing:
            names = "、".join(label for _mod, label in self.missing)
            problems.append(f"缺少依赖：{names}")
        if self.ffmpeg is None:
            problems.append("缺少 ffmpeg，部分音频格式无法读取")
        if not self.weights_ready:
            problems.append("分离模型权重尚未下载（约 400MB，首次分离时自动下载）")
        return problems


def inspect() -> EnvironmentReport:
    """探测当前环境（不触发任何下载）。"""
    report = EnvironmentReport(
        python_version=sys.version.split()[0],
        app_version=__version__,
        deps_ready=False,
        missing=installer.missing_modules(),
        ffmpeg=paths.ffmpeg_exe(),
    )
    # 安装态以安装器写的 runtime/.deps-ok 标记为准；源码运行时没有这个标记，
    # 只要模块都能找到就应当视为可用，否则每次启动都会误报「缺少依赖」。
    marker_ok = paths.deps_marker().exists() or not paths.is_frozen()
    report.deps_ready = not report.missing and marker_ok

    state = installer.torch_state()
    report.torch_version = state.get("version") if isinstance(state.get("version"), str) else None
    cuda = state.get("cuda")
    report.cuda_version = cuda if isinstance(cuda, str) else None
    report.gpu_available = bool(state.get("gpu"))
    report.gpu_name = str(state.get("gpu_name") or "")

    summary = weights.model_cache_summary()
    report.weights_ready = bool(summary.get("ready"))
    report.weights_bytes = int(summary.get("total_bytes") or 0)
    return report


def quick_check() -> tuple[bool, list[str]]:
    """轻量检查：只判断能否启动 UI。"""
    missing = installer.missing_modules()
    return (not missing), [label for _mod, label in missing]


def resolve_device(preference: str = "auto") -> str:
    return separation.resolve_device(preference)
