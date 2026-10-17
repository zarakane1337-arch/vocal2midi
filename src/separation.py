"""人声分离层 —— Demucs（htdemucs_ft）。

单独调试（不经过 UI/CLI）：
    python src/separation.py "song.mp3" -o out/stems
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

log = logging.getLogger("vocal2midi.separation")

DEMUCS_MODEL = "htdemucs_ft"  # fine-tuned 版，人声 SDR 优于默认 htdemucs


def resolve_device(name: str) -> str:
    """把 'auto' 解析成实际可用的设备字符串。"""
    if name and name != "auto":
        return name
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001 - 没装 torch 时不该崩
        pass
    return "cpu"


def separate_vocals(
    audio_path: str | Path,
    out_dir: str | Path,
    model: str = DEMUCS_MODEL,
    device: str = "cpu",
    overwrite: bool = False,
) -> Path:
    """Demucs 分离出人声轨，返回人声 wav 路径；已有结果则直接复用。"""
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"找不到输入音频：{audio_path}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{audio_path.stem}_vocals.wav"
    if target.exists() and target.stat().st_size > 0 and not overwrite:
        log.info("复用人声轨：%s", target)
        return target

    from demucs.api import Separator  # 延迟导入，报错信息更清晰
    import soundfile as sf

    log.info("Demucs 分离中：model=%s device=%s（CPU 上一首 4 分钟的歌约需数分钟）", model, device)
    separator = Separator(model=model, device=device, shifts=1, progress=True)
    _origin, separated = separator.separate_audio_file(str(audio_path))
    if "vocals" not in separated:
        raise KeyError(f"模型未输出 vocals 轨，可用轨：{sorted(separated)}")

    wav = separated["vocals"].detach().cpu().numpy()  # [channels, samples]
    sf.write(str(target), wav.T, separator.samplerate)  # soundfile 需要 [samples, channels]
    log.info("人声轨已保存：%s", target)
    return target


def main(argv: list[str] | None = None) -> int:
    """分层自测入口：只做分离，不碰转写。"""
    p = argparse.ArgumentParser(prog="separation", description="Demucs 人声分离（分层自测用）")
    p.add_argument("input", help="输入歌曲文件")
    p.add_argument("-o", "--out", default="out/stems", help="人声 wav 输出目录")
    p.add_argument("--model", default=DEMUCS_MODEL, help="Demucs 模型名")
    p.add_argument("--device", default="auto", help="auto / cpu / cuda")
    p.add_argument("--overwrite", action="store_true", help="强制重新分离")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    try:
        path = separate_vocals(
            args.input, args.out, model=args.model, device=resolve_device(args.device), overwrite=args.overwrite
        )
    except (ImportError, KeyError, FileNotFoundError, RuntimeError, ValueError) as exc:
        log.error("人声分离失败：%s", exc)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
