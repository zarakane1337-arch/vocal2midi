"""Demucs 模型权重下载。

权重不随安装包分发（htdemucs_ft 全量约 410MB）。首次分离时按需下载到
`torch.hub` 的 checkpoints 目录，demucs 会自动命中缓存、离线加载。

官方源 dl.fbaipublicfiles.com 在国内单连接约 0.85 MiB/s，但多连接可以叠加到
4 MiB/s 以上，因此这里同样走多连接下载器。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import paths
from . import net

log = logging.getLogger("vocal2midi.weights")

ROOT_URL = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/"

# 备用源：huggingface 镜像上的同名校验文件（官方源不可达时兜底）。
# 这些仓库的文件名与官方一致，demucs 的 check_hash 校验可通过。
FALLBACK_BASES: tuple[str, ...] = (
    "https://hf-mirror.com/JosefKuchar/Demucs/resolve/main/",
    "https://hf-mirror.com/dokodesuka/htdemucs_ft/resolve/main/",
)


@dataclass(frozen=True)
class ModelFile:
    filename: str
    size_bytes: int  # 官方公布的字节数，用于快速判断是否完整


# htdemucs_ft：4 个微调模型 + 1 个主模型（demucs/remote/files.txt）
HTDEMUCS_FT_FILES: tuple[ModelFile, ...] = (
    ModelFile("955717e8-8726e21a.th", 84_141_911),
    ModelFile("f7e0c4bc-ba3fe64a.th", 84_141_271),
    ModelFile("d12395a8-e57c48e6.th", 84_141_271),
    ModelFile("92cfc3b6-ef3bcb9c.th", 84_141_271),
    ModelFile("04573f0d-f3cf25b2.th", 84_141_271),
)

MODEL_FILES: dict[str, tuple[ModelFile, ...]] = {
    "htdemucs_ft": HTDEMUCS_FT_FILES,
    "htdemucs": (ModelFile("955717e8-8726e21a.th", 84_141_911),),
}

ProgressFn = Callable[[float, str], None]


def checkpoints_dir() -> Path:
    """权重缓存目录。

    先固定 TORCH_HOME，保证 demucs 运行时查的就是这里下载的目录。
    """
    paths.apply_runtime_env()
    return paths.demucs_checkpoints_dir()


def missing_files(model: str = "htdemucs_ft") -> list[ModelFile]:
    files = MODEL_FILES.get(model, HTDEMUCS_FT_FILES)
    target = checkpoints_dir()
    missing = []
    for item in files:
        path = target / item.filename
        if not path.exists() or path.stat().st_size < 1024:
            missing.append(item)
    return missing


def weights_ready(model: str = "htdemucs_ft") -> bool:
    return not missing_files(model)


def ensure_weights(
    model: str = "htdemucs_ft",
    on_progress: ProgressFn | None = None,
    on_log: Callable[[str], None] | None = None,
    cancel=None,
) -> bool:
    """确保权重齐全；返回是否全部就绪（不抛网络异常，只返回 False）。"""
    on_log = on_log or (lambda _t: None)
    missing = missing_files(model)
    if not missing:
        on_log(f"{model} 权重已就绪")
        return True

    target_dir = paths.ensure_dir(checkpoints_dir())
    total_bytes = sum(item.size_bytes for item in missing)
    done_bytes = 0
    on_log(f"需下载 {len(missing)} 个权重文件（约 {total_bytes / 1048576:.0f} MB）")

    for item in missing:
        dest = target_dir / item.filename
        sources = [ROOT_URL + item.filename] + [base + item.filename for base in FALLBACK_BASES]
        on_log(f"下载 {item.filename}")
        last_error = ""
        for url in sources:
            def report(done: int, size: int, _item=item, _base=done_bytes) -> None:
                if on_progress:
                    overall = (_base + done) / max(1, total_bytes)
                    on_progress(min(0.999, overall), f"下载模型 {_item.filename}（{done / 1048576:.0f}/{size / 1048576:.0f} MB）")

            try:
                net.download(url, dest, connections=8, on_progress=report, cancel=cancel)
                last_error = ""
                break
            except Exception as exc:  # noqa: BLE001 - 换下一个源
                last_error = f"{exc}"
                log.warning("权重源失败 %s：%s", url, exc)
                on_log(f"该源下载失败，切换下一个源…（{exc}）")
        if last_error:
            log.error("权重下载失败 %s：%s", item.filename, last_error)
            on_log(f"下载失败：{item.filename}（{last_error}）")
            return False
        done_bytes += item.size_bytes

    if on_progress:
        on_progress(1.0, "模型权重已就绪")
    on_log("模型权重下载完成")
    return weights_ready(model)


def model_cache_summary() -> dict[str, object]:
    target = checkpoints_dir()
    files = sorted(target.glob("*.th")) if target.exists() else []
    return {
        "dir": str(target),
        "files": [{"name": f.name, "size": f.stat().st_size} for f in files],
        "total_bytes": sum(f.stat().st_size for f in files),
        "ready": weights_ready(),
    }
