"""vocal2midi —— SwiftF0 转写引擎参考实现（standalone，可直接运行验证）

用法：
    python docs/swiftf0_main_reference.py "song.mp3" -o out
    python docs/swiftf0_main_reference.py --vocals out/stems/song_vocals.wav -o out
    python docs/swiftf0_main_reference.py "song.mp3" --engine basic-pitch   # A/B 对比

说明：
    * 本文件是"自成一体的完整管线"参考，不 import 项目内其他模块，可直接跑通验证。
    * 接入项目时**只取转写相关的部分**（`transcribe_swiftf0()` / `write_midi()` /
      `smooth_octave_jumps()` / `hz_to_midi()` / `check_f0_band()` 及 `transcribe_to_midi()`
      里的分派逻辑）搬到 `src/transcription.py`，保持 `transcribe_to_midi()` 的签名与
      事件格式 `(start, end, pitch_midi, amplitude, [bend…])` 不变 —— 这样
      `src/main.py` 与 `src/app.py` 一行都不用改。
    * 依赖：pip install --proxy "" -i https://mirrors.aliyun.com/pypi/simple/ "swift-f0[audio,midi]"
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
from pathlib import Path

import numpy as np

log = logging.getLogger("vocal2midi")

__version__ = "0.2.0"

DEMUCS_MODEL = "htdemucs_ft"  # fine-tuned 版，人声 SDR 优于默认 htdemucs
F0_MIN = 70.0  # Hz，限定人声基频范围，减少泛音/低频误检
F0_MAX = 900.0

# SwiftF0 模型能力边界（超出会抛异常）：46.875 Hz ~ 2093.75 Hz
SWIFTF0_MIN_HZ = 46.875
SWIFTF0_MAX_HZ = 2093.75
# fmin/fmax 是"搜索带掩码"：带外帧 confidence 直接置 0，所以带不能太窄
BAND_RATIO = 1.04125

# 转写默认值
CONF_THRESHOLD = 0.5  # SwiftF0 内部判定有声帧的阈值
PITCH_HOLD_MS = 80.0  # 颤音/滑音容忍度：调大 → 音符更少更长
MIN_NOTE_MS = 100.0  # 过滤碎音
MEDIAN_KERNEL = 5  # 中值滤波核（奇数），<=2 表示关闭
TEMPO = 120
VEL_MIN, VEL_MAX = 40, 127  # 写 MIDI 时的力度范围

_detector = None  # SwiftF0 实例复用（构造要加载 ONNX 模型，别每次新建）


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def resolve_device(name: str) -> str:
    """把 'auto' 解析成实际可用的设备字符串（只影响 Demucs，SwiftF0 只跑 CPU）。"""
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


def hz_to_midi(hz: float) -> int:
    """频率 → MIDI 音高号（最近半音，四舍五入）。"""
    return int(round(69.0 + 12.0 * math.log2(hz / 440.0)))


def check_f0_band(f0_min: float | None, f0_max: float | None) -> tuple[float | None, float | None]:
    """校验 SwiftF0 搜索带；两者都为空时返回 (None, None) 表示用模型全量程。"""
    if not f0_min and not f0_max:
        return None, None
    lo = float(f0_min) if f0_min else SWIFTF0_MIN_HZ
    hi = float(f0_max) if f0_max else SWIFTF0_MAX_HZ
    if lo < SWIFTF0_MIN_HZ or hi > SWIFTF0_MAX_HZ:
        raise ValueError(f"SwiftF0 支持 {SWIFTF0_MIN_HZ}-{SWIFTF0_MAX_HZ} Hz，收到 {lo}-{hi}")
    if hi < BAND_RATIO * lo:
        raise ValueError(
            f"音域带太窄：要求 f0_max >= {BAND_RATIO} * f0_min（当前 {lo:.1f}-{hi:.1f} Hz）"
        )
    return lo, hi


# --------------------------------------------------------------------------- #
# 人声分离（不变）
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# SwiftF0 转写
# --------------------------------------------------------------------------- #
def _get_detector():
    """复用同一个 SwiftF0 实例。spin=False：空闲时线程休眠，省 CPU。"""
    global _detector
    if _detector is None:
        from swift_f0 import SwiftF0

        _detector = SwiftF0(spin=False)
    return _detector


def smooth_octave_jumps(result, kernel: int = MEDIAN_KERNEL) -> int:
    """对有声帧的 F0 做中值滤波，压掉和声/叠唱造成的八度跳。返回被改动的帧数。

    注意：只在"有声帧"上滤波，帧索引会被压缩，跨越无声段的相邻帧会被当作邻居——
    对人声轨的连续乐句足够用；追求严格可以按乐句分段后再滤波。
    """
    if kernel < 3:
        return 0
    from scipy.signal import medfilt

    voiced = result.confidence >= CONF_THRESHOLD
    if int(voiced.sum()) <= kernel:
        return 0
    raw = result.pitch_hz[voiced].astype("float64").copy()
    filt = medfilt(raw, kernel_size=kernel)
    changed = int((filt != raw).sum())
    result.pitch_hz[voiced] = filt  # 原地写回 ndarray，不重建 dataclass
    return changed


def _note_amp(note, result, lo_db: float = -60.0, hi_db: float = -10.0) -> float:
    """音符时段的响度中位数 → 0..1 归一化幅度（用于 CSV，也用来推 MIDI 力度）。"""
    i = int(np.searchsorted(result.timestamps, note.start))
    j = max(i + 1, int(np.searchsorted(result.timestamps, note.end)))
    seg = result.loudness_db[i:j]
    seg = seg[np.isfinite(seg)]
    db = float(np.median(seg)) if seg.size else lo_db
    return min(max((db - lo_db) / (hi_db - lo_db), 0.0), 1.0)


def write_midi(
    events: list,
    out_mid: str | Path,
    tempo: float = TEMPO,
    vmin: int = VEL_MIN,
    vmax: int = VEL_MAX,
    program: int = 52,
) -> Path:
    """用 pretty_midi 写 MIDI：力度按响度分层。

    SwiftF0 自带的 export_to_midi() 只支持全局固定 velocity，所以想要力度就自己写。
    program=52 是 GM 的 Choir Aahs；想要钢琴改成 0。
    """
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(initial_tempo=float(tempo))
    inst = pretty_midi.Instrument(program=program, name="Vocals")
    for start, end, pitch, amp, _bends in events:
        inst.notes.append(
            pretty_midi.Note(
                velocity=int(round(vmin + float(amp) * (vmax - vmin))),
                pitch=int(pitch),
                start=float(start),
                end=float(end),
            )
        )
    pm.instruments.append(inst)

    out_mid = Path(out_mid)
    out_mid.parent.mkdir(parents=True, exist_ok=True)
    pm.write(str(out_mid))
    return out_mid


def transcribe_swiftf0(
    vocals_path: str | Path,
    out_mid: str | Path,
    f0_min: float | None = F0_MIN,
    f0_max: float | None = F0_MAX,
    confidence: float = CONF_THRESHOLD,
    pitch_hold_ms: float = PITCH_HOLD_MS,
    min_note_ms: float = MIN_NOTE_MS,
    median_kernel: int = MEDIAN_KERNEL,
    tempo: float = TEMPO,
    writer: str = "pretty",
) -> tuple[Path, list]:
    """SwiftF0：人声轨 → 音符事件 → .mid。返回 (MIDI 路径, 事件列表)。

    事件格式与 basic-pitch 版保持一致：(start_s, end_s, pitch_midi, amplitude, [bend...])
    以便 write_notes_csv() 原样复用。
    """
    from swift_f0 import export_to_midi, segment_notes

    lo, hi = check_f0_band(f0_min, f0_max)
    detector = _get_detector()
    log.info("SwiftF0 检测中：音域 %s-%s Hz，中值核 %d", lo or "全量程", hi or "", median_kernel)

    # 1) 帧级 F0（16ms/帧；多声道自动混单声道、非 16k 自动重采样）
    result = detector.detect_file(str(vocals_path), fmin=lo, fmax=hi)
    log.info("帧数 %d，有声帧占比 %.1f%%", len(result.timestamps), 100.0 * np.mean(result.confidence >= confidence))

    # 2) 可选：压八度跳
    fixed = smooth_octave_jumps(result, median_kernel)
    if fixed:
        log.info("中值滤波修正 %d 帧八度跳", fixed)

    # 3) F0 轮廓 → 音符（pitch_hold_ms 越大，音符越少越长）
    notes = segment_notes(result, pitch_hold_ms=pitch_hold_ms)

    # 4) 过滤碎音 + 转成统一事件格式
    kept, events = [], []
    for note in notes:
        if (note.end - note.start) * 1000.0 < min_note_ms:
            continue
        kept.append(note)
        events.append((note.start, note.end, hz_to_midi(note.pitch_hz), _note_amp(note, result), []))
    log.info("音符 %d 个（丢弃 %d 个短于 %.0fms 的碎音）", len(events), len(notes) - len(events), min_note_ms)

    # 5) 写 MIDI
    out_mid = Path(out_mid)
    out_mid.parent.mkdir(parents=True, exist_ok=True)
    if writer == "swiftf0":
        # 一行版：力度固定为 VEL_MAX，无力度分层
        export_to_midi(kept, str(out_mid), tempo=int(tempo), velocity=VEL_MAX, track_name="Vocals")
    else:
        write_midi(events, out_mid, tempo=tempo)
    log.info("MIDI 已写出：%s", out_mid)
    return out_mid, events


# --------------------------------------------------------------------------- #
# basic-pitch 转写（保留，用于 A/B 对比）
# --------------------------------------------------------------------------- #
def transcribe_basicpitch(
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
        _model_output, midi_data, note_events = predict(
            str(vocals_path), minimum_frequency=f0_min, maximum_frequency=f0_max, **kwargs
        )
    except TypeError:
        # 旧版 basic-pitch 没有频率限定参数，去掉后重试
        _model_output, midi_data, note_events = predict(str(vocals_path), **kwargs)

    out_mid = Path(out_mid)
    out_mid.parent.mkdir(parents=True, exist_ok=True)
    midi_data.write(str(out_mid))
    log.info("MIDI 已写出：%s（%d 个音符）", out_mid, len(note_events))
    return out_mid, note_events


def transcribe_to_midi(
    vocals_path: str | Path,
    out_mid: str | Path,
    engine: str = "swiftf0",
    # ---- 两个引擎共用 ----
    min_note_length_ms: float = MIN_NOTE_MS,
    f0_min: float | None = F0_MIN,
    f0_max: float | None = F0_MAX,
    # ---- SwiftF0 专有 ----
    confidence: float = CONF_THRESHOLD,
    pitch_hold_ms: float = PITCH_HOLD_MS,
    median_kernel: int = MEDIAN_KERNEL,
    tempo: float = TEMPO,
    writer: str = "pretty",
    # ---- basic-pitch 专有（engine="basic-pitch" 时才生效）----
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
) -> tuple[Path, list]:
    """统一入口：按 engine 分派。签名对 app.py 的旧调用完全兼容。"""
    if engine == "basic-pitch":
        return transcribe_basicpitch(
            vocals_path,
            out_mid,
            onset_threshold=onset_threshold,
            frame_threshold=frame_threshold,
            min_note_length_ms=min_note_length_ms,
            f0_min=f0_min,
            f0_max=f0_max,
        )
    return transcribe_swiftf0(
        vocals_path,
        out_mid,
        f0_min=f0_min,
        f0_max=f0_max,
        confidence=confidence,
        pitch_hold_ms=pitch_hold_ms,
        min_note_ms=min_note_length_ms,
        median_kernel=median_kernel,
        tempo=tempo,
        writer=writer,
    )


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vocal2midi",
        description="歌曲 → 人声 MIDI：Demucs 分离人声 + SwiftF0/basic-pitch 转写",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input", nargs="?", help="输入歌曲文件（mp3/wav/flac…）")
    p.add_argument("-o", "--out", default="out", help="输出目录")
    p.add_argument("--vocals", help="已分离好的人声 wav，指定后跳过 Demucs")
    p.add_argument("--model", default=DEMUCS_MODEL, help="Demucs 模型名")
    p.add_argument("--device", default="auto", help="auto / cpu / cuda（只影响 Demucs）")
    p.add_argument("--overwrite", action="store_true", help="强制重新分离人声")
    p.add_argument("--engine", choices=["swiftf0", "basic-pitch"], default="swiftf0", help="转写引擎")

    g = p.add_argument_group("共用转写参数")
    g.add_argument("--min-note-len", type=float, default=MIN_NOTE_MS, help="最短音符时长（毫秒），过滤碎音")
    g.add_argument("--f0-min", type=float, default=F0_MIN, help="限定最低基频 Hz，0 表示不限制")
    g.add_argument("--f0-max", type=float, default=F0_MAX, help="限定最高基频 Hz，0 表示不限制")

    s = p.add_argument_group("SwiftF0 参数（--engine swiftf0）")
    s.add_argument("--pitch-hold-ms", type=float, default=PITCH_HOLD_MS, help="颤音容忍；调大音符更少更长")
    s.add_argument("--confidence", type=float, default=CONF_THRESHOLD, help="有声判定阈值（用于统计/滤波）")
    s.add_argument("--median-kernel", type=int, default=MEDIAN_KERNEL, help="中值滤波核（奇数，<3 关闭）")
    s.add_argument("--tempo", type=float, default=TEMPO, help="输出 MIDI 的 BPM")
    s.add_argument("--midi-writer", choices=["pretty", "swiftf0"], default="pretty", help="pretty=力度分层；swiftf0=一行版固定力度")

    b = p.add_argument_group("basic-pitch 参数（--engine basic-pitch）")
    b.add_argument("--onset", type=float, default=0.5, help="起始点阈值；调高（0.6~0.8）可减少碎音和鬼音")
    b.add_argument("--frame", type=float, default=0.3, help="持续帧阈值；调高音符更短更少")

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

    # 两个引擎各自输出，便于 A/B 对比（不再互相覆盖）
    tag = "swiftf0" if args.engine == "swiftf0" else "basicpitch"
    try:
        midi_path, events = transcribe_to_midi(
            vocals,
            out_dir / f"{stem}_vocals.{tag}.mid",
            engine=args.engine,
            min_note_length_ms=args.min_note_len,
            f0_min=args.f0_min or None,
            f0_max=args.f0_max or None,
            confidence=args.confidence,
            pitch_hold_ms=args.pitch_hold_ms,
            median_kernel=args.median_kernel,
            tempo=args.tempo,
            writer=args.midi_writer,
            onset_threshold=args.onset,
            frame_threshold=args.frame,
        )
    except ImportError as exc:
        log.error(
            "缺少依赖（%s）：SwiftF0 → pip install \"swift-f0[audio,midi]\"；basic-pitch → pip install basic-pitch",
            exc,
        )
        return 1
    except ValueError as exc:
        log.error("参数错误：%s", exc)
        return 2

    if not events:
        log.warning("未检出音符：可放宽 --f0-min/--f0-max，或调大 --pitch-hold-ms")
    write_notes_csv(events, out_dir / f"{stem}_vocals.{tag}_notes.csv")
    log.info("完成：%s", midi_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
