"""人声转 MIDI 层 —— SwiftF0 转写（引擎可替换）。

职责边界（单人一层，改动互不越界）：
    输入：已分离的人声 wav（来自 `separation.py`）
    输出：.mid + 音符 CSV
    对外接口：`transcribe_to_midi()` / `write_notes_csv()` / `F0_MIN` / `F0_MAX`
    调用方：`main.py`（CLI）、`app.py`（UI）

**约定：换转写引擎时保持 `transcribe_to_midi()` 的签名与返回结构不变**
（返回 `(midi_path, events)`，事件为 `(start_s, end_s, pitch_midi, amplitude, [bend…])`），
这样上层 `main.py` / `app.py` 一行都不用改。
单独调试（不经过 UI/CLI）：
    python src/transcription.py out/stems/song_vocals.wav -o out
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
from pathlib import Path

log = logging.getLogger("vocal2midi.transcription")

F0_MIN = 70.0  # Hz，限定人声基频范围，减少泛音/低频误检
F0_MAX = 900.0

# MIDI 默认的 pitch bend 量程为 ±2 半音，14 位精度即 8192 步 / 2 半音 = 4096 步/半音。
BEND_UNITS_PER_SEMITONE = 4096
TICKS_PER_BEAT = 480
MIDI_TEMPO = 120

_detector = None  # SwiftF0 实例（构造一次约 20ms，复用即可）


def _get_detector():
    """惰性构造并缓存 SwiftF0 检测器。"""
    global _detector
    if _detector is None:
        from swift_f0 import SwiftF0

        _detector = SwiftF0()
        log.debug("SwiftF0 检测器已就绪")
    return _detector


def _hz_to_midi(hz: float) -> float:
    return 69.0 + 12.0 * math.log2(hz / 440.0)


def _hold_ms_from_onset(onset_threshold: float) -> float:
    """把 basic-pitch 风格的「起始阈值」映射成 SwiftF0 的音符分段惩罚。

    SwiftF0 用 `pitch_hold_ms` 控制「多短的一个音高停留才值得单独成一个音符」：
    值越小切得越碎，越大越倾向于合并。阈值调高本意是减少碎音，所以正相关映射：
    0.5（默认）→ 30ms（实测最优），0.3 → 18ms，0.9 → 54ms。
    """
    return max(0.0, min(120.0, 30.0 * float(onset_threshold) / 0.5))


def transcribe_to_midi(
    vocals_path: str | Path,
    out_mid: str | Path,
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
    min_note_length_ms: float = 50.0,
    f0_min: float | None = F0_MIN,
    f0_max: float | None = F0_MAX,
) -> tuple[Path, list]:
    """SwiftF0 转写人声轨，写出 .mid，返回 (MIDI 路径, 音符事件列表)。

    参数含义（保持与 basic-pitch 版本一致，便于替换）：
        onset_threshold: 音符切分灵敏度，调高 → 音符更长更少（内部映射到 pitch_hold_ms）
        frame_threshold: 发声置信度阈值，低于此值的帧视为静音/清音
        min_note_length_ms: 最短音符时长，短于该值的音符被丢弃
        f0_min / f0_max: 基频搜索范围（Hz），None 表示用库默认值
    """
    from swift_f0 import segment_notes

    vocals_path, out_mid = Path(vocals_path), Path(out_mid)
    if not vocals_path.exists():
        raise FileNotFoundError(f"找不到人声文件：{vocals_path}")

    hold_ms = _hold_ms_from_onset(onset_threshold)
    log.info(
        "SwiftF0 转写中：置信度≥%.2f 音符惩罚 %.0fms 最短音符 %.0fms 音域 %.0f-%.0fHz",
        frame_threshold,
        hold_ms,
        min_note_length_ms,
        f0_min or 0,
        f0_max or 0,
    )

    detector = _get_detector()
    result = detector.detect_file(str(vocals_path), fmin=f0_min or None, fmax=f0_max or None)

    notes = segment_notes(result, pitch_hold_ms=hold_ms)

    # 丢置信度不足的音符：取音符覆盖帧的平均置信度
    frame_rate = 1000.0 / 16.0  # SwiftF0 固定 16ms 一帧
    confidence = result.confidence
    kept = []
    for note in notes:
        if (note.end - note.start) * 1000.0 < min_note_length_ms:
            continue
        i0 = max(0, int(round(note.start * frame_rate)))
        i1 = max(i0 + 1, int(round(note.end * frame_rate)))
        mean_conf = float(confidence[i0:i1].mean()) if i1 <= len(confidence) else 0.0
        if mean_conf < frame_threshold:
            continue
        kept.append((note, i0, i1))

    events = _build_events(result, kept)
    _write_midi(events, out_mid, vocals_path.stem)

    log.info("MIDI 已写出：%s（%d 个音符）", out_mid, len(events))
    return out_mid, events


def _build_events(result, kept: list) -> list:
    """把 SwiftF0 音符 + 逐帧连续 F0 组装成统一事件格式。

    返回 `(start_s, end_s, pitch_midi, amplitude, [bend…])`：
        pitch_midi 为浮点（= 音符拟合音高的最近 MIDI 号，可直接量化使用）
        amplitude  为 0~1 的力度，由 loudness_db 线性化后按全曲最大值归一
        bends      为该音符内的 pitchwheel 值序列（4096 = 1 半音，与 basic-pitch 同量纲）
    """
    if not kept:
        return []

    loudness = result.loudness_db
    pitch_hz = result.pitch_hz

    # 力度：把每个音符覆盖帧的 dB 换成线性幅度，再按全曲最响音符归一化到 0~1
    raw_amp = []
    for _note, i0, i1 in kept:
        seg = loudness[i0:i1]
        raw_amp.append(float(10.0 ** (seg.mean() / 20.0)) if len(seg) else 0.0)
    peak = max(raw_amp) if raw_amp else 0.0

    events = []
    for (note, i0, i1), amp in zip(kept, raw_amp):
        base = int(round(_hz_to_midi(note.pitch_hz)))
        bends: list[int] = []
        last = None
        for i in range(i0, min(i1, len(pitch_hz))):
            hz = float(pitch_hz[i])
            if not math.isfinite(hz) or hz <= 0:
                continue
            semis = _hz_to_midi(hz) - base
            value = int(round(max(-2.0, min(2.0, semis)) * BEND_UNITS_PER_SEMITONE))
            if value != last:
                bends.append(max(-8192, min(8191, value)))
                last = value
        events.append(
            (
                round(float(note.start), 3),
                round(float(note.end), 3),
                round(_hz_to_midi(note.pitch_hz), 2),
                round(amp / peak, 3) if peak > 0 else 0.0,
                bends,
            )
        )
    return events


def _ascii_track_name(name: str) -> str:
    """MIDI 的 meta 文本按规范是 latin-1，中日文曲名会让 mido 编码失败，这里降级成 ASCII。"""
    try:
        name.encode("latin-1")
        return name[:64] or "vocals"
    except UnicodeEncodeError:
        safe = name.encode("ascii", "ignore").decode("ascii").strip()
        return (safe or "vocals")[:64]


def _write_midi(events: list, out_mid: Path, track_name: str) -> Path:
    """按统一事件格式写 .mid：整数音符 + 每音符的 pitchwheel 弯音。"""
    import mido

    out_mid.parent.mkdir(parents=True, exist_ok=True)
    mid = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name=_ascii_track_name(track_name), time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(MIDI_TEMPO), time=0))

    ticks_per_second = TICKS_PER_BEAT * MIDI_TEMPO / 60.0

    def to_ticks(seconds: float) -> int:
        return int(round(seconds * ticks_per_second))

    # 收集绝对时刻事件；(tick, 排序键) —— 同一 tick 上 pitchwheel 先于 note_on，
    # note_off 先于 note_on，保证相邻同音高音符不会被误并
    merged: list[tuple[int, int, object]] = []
    for start, end, pitch, amp, bends in events:
        base = int(round(pitch))
        velocity = max(1, min(127, int(round(float(amp) * 127))))
        a = to_ticks(start)
        b = max(to_ticks(end), a + 1)
        merged.append((a, 2, mido.Message("note_on", channel=0, note=base, velocity=velocity, time=0)))
        merged.append((b, 0, mido.Message("note_off", channel=0, note=base, velocity=0, time=0)))
        span = max(b - a, 1)
        for k, value in enumerate(bends):
            # bends 从音符起始帧到结束帧等间隔摊开
            tick = a if len(bends) == 1 else a + int(round(span * k / max(len(bends) - 1, 1)))
            merged.append((tick, 1, mido.Message("pitchwheel", channel=0, pitch=int(value), time=0)))

    merged.sort(key=lambda item: (item[0], item[1]))
    previous = 0
    for tick, _order, message in merged:
        message.time = tick - previous
        track.append(message)
        previous = tick

    mid.save(str(out_mid))
    return out_mid


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


def main(argv: list[str] | None = None) -> int:
    """分层自测入口：只做转写，不碰分离。"""
    p = argparse.ArgumentParser(prog="transcription", description="人声 → MIDI 转写（分层自测用）")
    p.add_argument("vocals", help="已分离的人声 wav")
    p.add_argument("-o", "--out", default="out", help="MIDI / CSV 输出目录")
    p.add_argument("--onset", type=float, default=0.5, help="起始点阈值（调高音符更长更少）")
    p.add_argument("--frame", type=float, default=0.3, help="发声置信度阈值")
    p.add_argument("--min-note-len", type=float, default=50.0, help="最短音符时长（毫秒）")
    p.add_argument("--f0-min", type=float, default=F0_MIN, help="最低基频 Hz，0 = 不限制")
    p.add_argument("--f0-max", type=float, default=F0_MAX, help="最高基频 Hz，0 = 不限制")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    vocals = Path(args.vocals)
    if not vocals.exists():
        log.error("找不到人声文件：%s", vocals)
        return 2
    stem = vocals.stem.replace("_vocals", "")
    out_dir = Path(args.out)
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
    except ImportError as exc:
        log.error("未安装 swift-f0（%s）：pip install \"swift-f0[audio,midi]\"", exc)
        return 1
    if not events:
        log.warning("未检出音符：可调低 --onset/--frame，或检查 --f0-min/--f0-max 是否把人声排除在外")
    write_notes_csv(events, out_dir / f"{stem}_vocals_notes.csv")
    print(midi_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
