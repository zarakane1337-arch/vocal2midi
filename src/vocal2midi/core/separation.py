"""人声分离层（Demucs）。

职责边界：
    输入：任意音频文件
    输出：<out_dir>/<stem>_vocals.wav
    对外：`separate_vocals()` / `resolve_device()` / `SeparatorCache`

模型权重不随程序分发，首次使用时由 `core.weights` 从官方源下载并缓存到
`torch.hub` 的 checkpoints 目录，demucs 检测到缓存后自动离线加载。

一个 `Separator` 的构造代价是重新加载整套权重（`htdemucs_ft` 是四个子模型的
集合），因此批量处理时**必须**复用同一个实例：`SeparatorCache` 负责这件事。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Protocol

from .. import paths
from .cancellation import Cancelled

log = logging.getLogger("vocal2midi.separation")

DEMUCS_MODEL = "htdemucs_ft"
AVAILABLE_MODELS = ("htdemucs_ft", "htdemucs", "mdx_extra_q")
DEVICE_CHOICES = ("auto", "cpu", "cuda")

# 小于这个字节数的人声 wav 视为写坏了的残文件，不复用。
MIN_VOCALS_BYTES = 1024

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


# ---- 产物命名与复用判定 -------------------------------------------------


def vocals_file(out_dir: str | Path, stem: str) -> Path:
    """人声轨的落盘位置：由**目录 + 主名**决定，与输入文件路径无关。

    这一点是重试能复用已有轨的前提：重试时必须命中同一个文件。
    """
    return Path(out_dir) / f"{stem}_vocals.wav"


def is_reusable_vocals(target: str | Path) -> bool:
    """已有的人声轨是否可以直接用。只看文件本身，不问它从哪来。"""
    target = Path(target)
    try:
        return target.is_file() and target.stat().st_size > MIN_VOCALS_BYTES
    except OSError:
        return False


# ---- 中断 --------------------------------------------------------------


def make_abort_callback(
    cancel_event: threading.Event | None,
    on_progress: ProgressHook | None = None,
) -> Callable[[dict], None]:
    """把 demucs 的分段回调变成「取消 + 进度」两个动作。

    demucs 唯一的中断方式是让 callback 抛 `KeyboardInterrupt`（`demucs/api.py`），
    所以取消在这里表现为抛出，由 `SeparatorSession` 转换成 `Cancelled`。
    中断粒度是一个分段（`segment`）而不是一整首歌。
    """

    def callback(info: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("用户取消")
        if on_progress is None or info.get("state") != "start":
            return
        total = info.get("audio_length") or 0
        offset = info.get("segment_offset") or 0
        fraction = 0.0 if not total else max(0.0, min(1.0, float(offset) / float(total)))
        on_progress(0.2 + 0.7 * fraction, "正在分离音轨")

    return callback


class _SeparatorLike(Protocol):
    samplerate: int

    def separate_audio_file(self, path: str): ...


# ---- 分离器会话与缓存 ---------------------------------------------------


class SeparatorSession:
    """一个已加载模型 + 它当前的中断/进度绑定。

    复用 `Separator` 实例，但每次使用前把取消事件与进度回调换成本次的值——
    这与 demucs 自己的 `update_parameter()` 设计一致。
    """

    def __init__(
        self,
        separator: _SeparatorLike,
        on_progress: ProgressHook | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.separator = separator
        self._on_progress = on_progress
        self._cancel = cancel_event or threading.Event()
        self._bind()

    def _bind(self) -> None:
        update = getattr(self.separator, "update_parameter", None)
        if update is None:
            return
        callback = make_abort_callback(self._cancel, self._progress)
        try:
            update(callback=callback)
        except Exception as exc:  # noqa: BLE001 - 绑不上也不该让分离直接失败
            log.warning("无法为分离器绑定中断回调：%s", exc)

    def _progress(self, fraction: float, text: str) -> None:
        if self._on_progress:
            self._on_progress(fraction, text)

    def rebind(
        self,
        on_progress: ProgressHook | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """换成本次的进度回调与取消事件。"""
        self._on_progress = on_progress
        self._cancel = cancel_event or threading.Event()
        self._bind()

    def separate_to(self, audio_path: str | Path, target: str | Path) -> Path:
        """分离出一首曲目的人声轨并写到 `target`，返回该路径。"""
        audio_path, target = Path(audio_path), Path(target)
        if not audio_path.exists():
            raise FileNotFoundError(f"找不到音频文件：{audio_path}")
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            _origin, separated = self.separator.separate_audio_file(str(audio_path))
        except KeyboardInterrupt as exc:
            raise Cancelled("已取消") from exc

        vocals = separated.get("vocals")
        if vocals is None:
            raise RuntimeError("Demucs 未返回 vocals 轨，模型可能不支持人声分离")

        if self._on_progress:
            self._on_progress(0.9, "写出人声轨")
        import soundfile as sf

        # demucs 返回 [channels, samples] 的 tensor，soundfile 需要 [samples, channels]
        data = vocals.detach().cpu().numpy()
        sf.write(str(target), data.T, int(self.separator.samplerate))
        if self._on_progress:
            self._on_progress(1.0, "人声分离完成")
        log.info("人声轨已写出：%s", target)
        return target


def _load_separator(
    model: str,
    device: str,
    on_progress: ProgressHook | None = None,
) -> SeparatorSession:
    """真正构造一个 demucs `Separator`（会加载权重）。"""
    paths.prepend_to_path(paths.bin_dir())
    # 必须先把缓存目录与离线开关准备好：
    #   * 不固定 TORCH_HOME，demucs 会去 ~/.cache/torch 找权重，找不到就回源；
    #   * 不开 HF_HUB_OFFLINE，demucs 会先去 HuggingFace 探测，国内会挂几十秒。
    paths.apply_runtime_env()
    from demucs.api import Separator

    resolved = resolve_device(device)
    log.info("加载 Demucs 分离器：model=%s device=%s", model, resolved)
    if on_progress:
        on_progress(0.05, f"加载模型 {model}（{resolved}）")
    separator = Separator(
        model=model,
        device=resolved,
        shifts=1,
        progress=False,
    )
    return SeparatorSession(separator)


class SeparatorCache:
    """按 (模型, 设备) 缓存已加载的分离器。

    一次批次里所有曲目共用同一个实例：重新构造意味着重新加载整套权重，
    在 CPU 上是每首歌都要等的固定开销，在 CUDA 上还会制造显存抖动。
    """

    def __init__(self, loader: Callable[..., SeparatorSession] | None = None) -> None:
        self._loader = loader or _load_separator
        self._sessions: dict[tuple[str, str], SeparatorSession] = {}
        self._lock = threading.Lock()

    def get(
        self,
        model: str,
        device: str,
        on_progress: ProgressHook | None = None,
    ) -> SeparatorSession:
        key = (model, device)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = self._loader(model, device, on_progress)
                self._sessions[key] = session
            return session

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


# ---- 对外入口 ----------------------------------------------------------


def separate_vocals(
    audio_path: str | Path,
    out_dir: str | Path,
    model: str = DEMUCS_MODEL,
    device: str = "cpu",
    overwrite: bool = False,
    on_progress: ProgressHook | None = None,
    *,
    stem: str | None = None,
    session: SeparatorSession | None = None,
    cache: SeparatorCache | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """分离出人声轨并写成 wav，返回人声路径。

    已存在且非空的 `<stem>_vocals.wav` 会复用，`overwrite=True` 时强制重算。
    `stem` 不给时取输入文件名；给了就完全按它命名（批量里由曲目决定）。

    传入 `session` 或 `cache` 时复用已加载的模型，否则按需构造一次。
    """
    audio_path = Path(audio_path)
    out_dir = Path(out_dir)
    if not audio_path.exists():
        raise FileNotFoundError(f"找不到音频文件：{audio_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    target = vocals_file(out_dir, stem or audio_path.stem)
    if not overwrite and is_reusable_vocals(target):
        log.info("复用已有人声轨：%s", target)
        if on_progress:
            on_progress(1.0, "已复用人声轨")
        return target

    if session is None:
        session = (cache or SeparatorCache()).get(model, device, on_progress)
    session.rebind(on_progress=on_progress, cancel_event=cancel_event)
    if on_progress:
        on_progress(0.2, "正在分离音轨")
    return session.separate_to(audio_path, target)
