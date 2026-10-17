"""人声分离层（Demucs）。

职责边界：
    输入：任意音频文件
    输出：<out_dir>/<stem>_vocals.wav
    对外：`separate_vocals()` / `resolve_device()` / `ensure_weights()`

模型权重不随程序分发，首次使用时由 `core.weights` 从官方源下载并缓存到
`torch.hub` 的 checkpoints 目录，demucs 检测到缓存后自动离线加载。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .. import paths

log = logging.getLogger("vocal2midi.separation")

DEMUCS_MODEL = "htdemucs_ft"
AVAILABLE_MODELS = ("htdemucs_ft", "htdemucs", "mdx_extra_q")
DEVICE_CHOICES = ("auto", "cpu", "cuda")

ProgressHook = Callable[[float, str], None]


def cuda_available() -> bool:
    """torch 是否可用 CUDA（torch 未安装时返回 False，不抛异常）。"""
    try:
        import torch
    except Exception:  # noqa: BLE001 - 依赖缺失时按 CPU 处理
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def torch_info() -> dict[str, object]:
    """返回 torch 版本与设备信息，供 UI 展示。"""
    info: dict[str, object] = {"torch": None, "cuda": None, "device_name": None, "gpu_count": 0}
    try:
        import torch
    except Exception:  # noqa: BLE001
        return info
    info["torch"] = getattr(torch, "__version__", "?")
    try:
        info["cuda"] = getattr(torch.version, "cuda", None)
        info["gpu_count"] = int(torch.cuda.device_count())
        if info["gpu_count"]:
            info["device_name"] = torch.cuda.get_device_name(0)
    except Exception:  # noqa: BLE001
        pass
    return info


def resolve_device(name: str) -> str:
    """把 auto/cpu/cuda 解析成 demucs 认识的具体设备。"""
    name = (name or "auto").strip().lower()
    if name == "cuda" and cuda_available():
        return "cuda"
    if name == "auto":
        return "cuda" if cuda_available() else "cpu"
    return "cpu"


def separate_vocals(
    audio_path: str | Path,
    out_dir: str | Path,
    model: str = DEMUCS_MODEL,
    device: str = "cpu",
    overwrite: bool = False,
    on_progress: ProgressHook | None = None,
) -> Path:
    """分离出人声轨并写成 wav，返回人声路径。

    已存在且非空的 `<stem>_vocals.wav` 会复用，`overwrite=True` 时强制重算。
    """
    audio_path = Path(audio_path)
    out_dir = Path(out_dir)
    if not audio_path.exists():
        raise FileNotFoundError(f"找不到音频文件：{audio_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{audio_path.stem}_vocals.wav"
    if target.exists() and target.stat().st_size > 1024 and not overwrite:
        log.info("复用已有人声轨：%s", target)
        if on_progress:
            on_progress(1.0, "已复用人声轨")
        return target

    paths.prepend_to_path(paths.bin_dir())
    # 必须先把缓存目录与离线开关准备好：
    #   * 不固定 TORCH_HOME，demucs 会去 ~/.cache/torch 找权重，找不到就回源；
    #   * 不开 HF_HUB_OFFLINE，demucs 会先去 HuggingFace 探测，国内会挂几十秒。
    paths.apply_runtime_env()
    from demucs.api import Separator

    resolved = resolve_device(device)
    log.info("Demucs 分离中：model=%s device=%s", model, resolved)
    if on_progress:
        on_progress(0.05, f"加载模型 {model}（{resolved}）")

    separator = Separator(
        model=model,
        device=resolved,
        shifts=1,
        progress=False,
    )
    if on_progress:
        on_progress(0.2, "正在分离音轨")
    _origin, separated = separator.separate_audio_file(str(audio_path))

    vocals = separated.get("vocals")
    if vocals is None:
        raise RuntimeError("Demucs 未返回 vocals 轨，模型可能不支持人声分离")

    if on_progress:
        on_progress(0.9, "写出人声轨")
    import soundfile as sf

    # demucs 返回 [channels, samples] 的 tensor，soundfile 需要 [samples, channels]
    data = vocals.detach().cpu().numpy()
    sf.write(str(target), data.T, int(separator.samplerate))
    if on_progress:
        on_progress(1.0, "人声分离完成")
    log.info("人声轨已写出：%s", target)
    return target
