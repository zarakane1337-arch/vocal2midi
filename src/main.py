from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

log = logging.getLogger("vocal2midi")

__version__ = "0.1.0"

DEMUCS_MODEL = "htdemucs_ft"  # fine-tuned 版，人声 SDR 优于默认 htdemucs
F0_MIN = 70.0  # Hz，限定人声基频范围，减少泛音/低频误检
F0_MAX = 900.0


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


def transcribe_to_midi(
    vocals_path: str | Path,
    out_mid: str | Path,
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
    min_note_length_ms: float = 100.0,
    f0_min: float | None = F0_MIN,
    f0_max: float | None = F0_MAX,
) -> tuple[Path, list]:
    """basic-pitch 转写人声轨，写出 .mid，返回 (MIDI 路径, 音符事件列表)。"""
    from basic_pitch.inference import predict

    kwargs = {
        "onset_threshold": onset_threshold,
        "frame_threshold": frame_threshold,
        "minimum_note_length": min_note_length_ms,
    }
    log.info(
        "basic-pitch 转写中：onset=%.2f frame=%.2f 最短音符 %.0fms 音域 %.0f-%.0fHz",
        onset_threshold,
        frame_threshold,
        min_note_length_ms,
        f0_min or 0,
        f0_max or 0,
    )
    try:
        _model_output, midi_data, note_events = predict(str(vocals_path), minimum_frequency=f0_min, maximum_frequency=f0_max, **kwargs)
    except TypeError:
        # 旧版 basic-pitch 没有频率限定参数，去掉后重试
        _model_output, midi_data, note_events = predict(str(vocals_path), **kwargs)

    out_mid = Path(out_mid)
    out_mid.parent.mkdir(parents=True, exist_ok=True)
    midi_data.write(str(out_mid))
    log.info("MIDI 已写出：%s（%d 个音符）", out_mid, len(note_events))
    return out_mid, note_events


def write_notes_csv(note_events: list, out_csv: str | Path) -> Path:
    """音符事件表，便于核对错音（Excel 可直接打开）。"""
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["start_s", "end_s", "dur_s", "pitch_midi", "amplitude", "bend_start"])
        for ev in note_events:
            start, end, pitch, amp = ev[0], ev[1], ev[2], ev[3]
            bends = ev[4] if len(ev) > 4 else []
            writer.writerow(
                [f"{start:.3f}", f"{end:.3f}", f"{end - start:.3f}", pitch, f"{amp:.3f}", bends[0] if bends else 0.0]
            )
    log.info("音符列表已写出：%s", out_csv)
    return out_csv


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vocal2midi",
        description="歌曲 → 人声 MIDI：Demucs 分离人声 + basic-pitch 转写",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input", nargs="?", help="输入歌曲文件（mp3/wav/flac…）")
    p.add_argument("-o", "--out", default="out", help="输出目录")
    p.add_argument("--vocals", help="已分离好的人声 wav，指定后跳过 Demucs")
    p.add_argument("--model", default=DEMUCS_MODEL, help="Demucs 模型名")
    p.add_argument("--device", default="auto", help="auto / cpu / cuda")
    p.add_argument("--overwrite", action="store_true", help="强制重新分离人声")

    g = p.add_argument_group("basic-pitch 转写参数")
    g.add_argument("--onset", type=float, default=0.5, help="起始点阈值；调高（0.6~0.8）可减少碎音和鬼音")
    g.add_argument("--frame", type=float, default=0.3, help="持续帧阈值；调高音符更短更少")
    g.add_argument("--min-note-len", type=float, default=100.0, help="最短音符时长（毫秒）")
    g.add_argument("--f0-min", type=float, default=F0_MIN, help="限定最低基频 Hz，0 表示不限制")
    g.add_argument("--f0-max", type=float, default=F0_MAX, help="限定最高基频 Hz，0 表示不限制")

    p.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    device = resolve_device(args.device)

    if args.vocals:
        vocals = Path(args.vocals)
        if not vocals.exists():
            log.error("找不到人声文件：%s", vocals)
            return 2
        stem = vocals.stem.replace("_vocals", "")
    else:
        if not args.input:
            log.error("请提供输入歌曲文件，或用 --vocals 指定已分离的人声")
            return 2
        src = Path(args.input)
        if not src.exists():
            log.error("找不到输入文件：%s", src)
            return 2
        stem = src.stem
        try:
            vocals = separate_vocals(
                src, out_dir / "stems", model=args.model, device=device, overwrite=args.overwrite
            )
        except (ImportError, KeyError, FileNotFoundError, RuntimeError, ValueError) as exc:
            log.error("人声分离失败：%s", exc)
            return 1

    try:
        midi_path, events = transcribe_to_midi(
            vocals,
            out_dir / f"{stem}_vocals.mid",
            onset_threshold=args.onset,
            frame_threshold=args.frame,
            min_note_length_ms=args.min_note_len,
            f0_min=args.f0_min or None,
            f0_max=args.f0_max or None,
        )
    except ImportError:
        log.error("未安装 basic-pitch：pip install basic-pitch")
        return 1

    if not events:
        log.warning("未检出音符：可调低 --onset/--frame，或检查 --f0-min/--f0-max 是否把人声排除在外")
    write_notes_csv(events, out_dir / f"{stem}_vocals_notes.csv")
    log.info("完成：%s", midi_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
