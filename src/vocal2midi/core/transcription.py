"""人声 → MIDI 转写层（SwiftF0 引擎）。

职责边界：
    输入：已分离的人声 wav（来自 `core.separation`）
    输出：.mid + 音符事件列表
    对外：`transcribe_to_midi()` / `write_notes_csv()` / `TranscriptionParams`

事件格式固定为 `(start_s, end_s, pitch_midi, amplitude, [bend…])`，
换引擎时保持该结构不变，上层 pipeline / UI 无需改动。
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..core import params as param_registry

log = logging.getLogger("vocal2midi.transcription")

F0_MIN = 70.0  # Hz，限定人声基频范围，减少泛音/低频误检
F0_MAX = 900.0

# MIDI 默认的 pitch bend 量程为 ±2 半音，14 位精度即 8192 步 / 2 半音 = 4096 步/半音。
BEND_UNITS_PER_SEMITONE = 4096
BEND_MAX_UNITS = 8191
TICKS_PER_BEAT = 480
MIDI_TEMPO = 120

_QUANTIZE_DIVISIONS = {"off": 4.0, "1/4": 1.0, "1/8": 2.0, "1/16": 4.0, "1/32": 8.0}

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
    """把「切分灵敏度」映射成 SwiftF0 的音符分段惩罚。

    SwiftF0 用 `pitch_hold_ms` 控制「多短的一个音高停留才值得单独成一个音符」：
    值越小切得越碎，越大越倾向于合并。阈值调高本意是减少碎音，所以正相关映射：
    0.5（默认）→ 30ms（实测最优），0.3 → 18ms，0.9 → 54ms。
    """
    return max(0.0, min(120.0, 30.0 * float(onset_threshold) / 0.5))


@dataclass
class TranscriptionParams:
    """转写参数。字段与 `core.params.TRANSCRIPTION_PARAMS` 一一对应。"""

    onset_threshold: float = 0.5
    frame_threshold: float = 0.3
    min_note_length_ms: float = 50.0
    f0_min: float = F0_MIN
    f0_max: float = F0_MAX
    pitch_hold_ms: float = 0.0
    velocity_floor: float = 0.0
    bend_range: int = 2
    tempo: int = MIDI_TEMPO
    quantize: str = "off"
    transpose: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "TranscriptionParams":
        values = param_registry.normalize(mapping)
        known = {spec.key for spec in param_registry.iter_specs()}
        return cls(
            **{k: v for k, v in values.items() if k in known},
            extra={k: v for k, v in values.items() if k not in known},
        )

    def as_mapping(self) -> dict[str, Any]:
        return {spec.key: getattr(self, spec.key, spec.default) for spec in param_registry.iter_specs()}

    # ---- 派生量 ---------------------------------------------------------
    @property
    def hold_ms(self) -> float:
        """实际使用的音高保持时长：手动值优先，0 表示按灵敏度推导。"""
        manual = float(self.pitch_hold_ms or 0.0)
        return manual if manual > 0 else _hold_ms_from_onset(self.onset_threshold)

    @property
    def units_per_semitone(self) -> float:
        if self.bend_range <= 0:
            return 0.0
        return 8192.0 / float(self.bend_range)

    @property
    def quantize_divisions(self) -> float:
        return _QUANTIZE_DIVISIONS.get(self.quantize, 4.0)


def transcribe_to_midi(
    vocals_path: str | Path,
    out_mid: str | Path,
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
    min_note_length_ms: float = 50.0,
    f0_min: float | None = F0_MIN,
    f0_max: float | None = F0_MAX,
    **extra: Any,
) -> tuple[Path, list]:
    """转写人声轨并写出 .mid，返回 (MIDI 路径, 音符事件列表)。

    兼容旧签名（前 5 个关键字参数），额外参数（pitch_hold_ms / velocity_floor /
    bend_range / tempo / quantize / transpose）通过 `**extra` 传入。
    """
    payload = {
        "onset_threshold": onset_threshold,
        "frame_threshold": frame_threshold,
        "min_note_length_ms": min_note_length_ms,
        "f0_min": f0_min if f0_min else F0_MIN,
        "f0_max": f0_max if f0_max else F0_MAX,
    }
    payload.update(extra)
    params = TranscriptionParams.from_mapping(payload)
    return transcribe_file(vocals_path, out_mid, params)


def transcribe_file(
    vocals_path: str | Path,
    out_mid: str | Path,
    params: TranscriptionParams | None = None,
) -> tuple[Path, list]:
    """按参数对象转写，供 pipeline / UI 使用。"""
    from swift_f0 import segment_notes

    if not isinstance(params, TranscriptionParams):
        # 允许调用方直接传 dict，避免不得不先构造参数对象
        params = TranscriptionParams.from_mapping(params or {})
    vocals_path, out_mid = Path(vocals_path), Path(out_mid)
    if not vocals_path.exists():
        raise FileNotFoundError(f"找不到人声文件：{vocals_path}")

    hold_ms = params.hold_ms
    log.info(
        "SwiftF0 转写中：置信度≥%.2f 音高保持 %.0fms 最短音符 %.0fms 音域 %.0f-%.0fHz",
        params.frame_threshold,
        hold_ms,
        params.min_note_length_ms,
        params.f0_min,
        params.f0_max,
    )

    detector = _get_detector()
    result = detector.detect_file(
        str(vocals_path),
        fmin=params.f0_min or None,
        fmax=params.f0_max or None,
    )

    notes = segment_notes(result, pitch_hold_ms=hold_ms)

    # 丢置信度不足的音符：取音符覆盖帧的平均置信度
    frame_rate = 1000.0 / 16.0  # SwiftF0 固定 16ms 一帧
    confidence = result.confidence
    kept = []
    for note in notes:
        if (note.end - note.start) * 1000.0 < params.min_note_length_ms:
            continue
        i0 = max(0, int(round(note.start * frame_rate)))
        i1 = max(i0 + 1, int(round(note.end * frame_rate)))
        mean_conf = float(confidence[i0:i1].mean()) if i1 <= len(confidence) else 0.0
        if mean_conf < params.frame_threshold:
            continue
        kept.append((note, i0, i1))

    events = _build_events(result, kept, params)
    events = _apply_post_processing(events, params)
    _write_midi(events, out_mid, vocals_path.stem, params)

    log.info("MIDI 已写出：%s（%d 个音符）", out_mid, len(events))
    return out_mid, events


def _build_events(result, kept: list, params: TranscriptionParams) -> list:
    """把 SwiftF0 音符 + 逐帧连续 F0 组装成统一事件格式。"""
    if not kept:
        return []

    loudness = result.loudness_db
    pitch_hz = result.pitch_hz
    units = params.units_per_semitone

    # 力度：把每个音符覆盖帧的 dB 换成线性幅度，再按全曲最响音符归一化到 0~1
    raw_amp = []
    for _note, i0, i1 in kept:
        seg = loudness[i0:i1]
        raw_amp.append(float(10.0 ** (seg.mean() / 20.0)) if len(seg) else 0.0)
    peak = max(raw_amp) if raw_amp else 0.0
    floor = max(0.0, min(0.95, float(params.velocity_floor)))

    events = []
    for (note, i0, i1), amp in zip(kept, raw_amp):
        base = int(round(_hz_to_midi(note.pitch_hz)))
        bends: list[int] = []
        if units > 0:
            last = None
            limit = float(params.bend_range)
            for i in range(i0, min(i1, len(pitch_hz))):
                hz = float(pitch_hz[i])
                if not math.isfinite(hz) or hz <= 0:
                    continue
                semis = _hz_to_midi(hz) - base
                value = int(round(max(-limit, min(limit, semis)) * units))
                if value != last:
                    bends.append(max(-BEND_MAX_UNITS, min(BEND_MAX_UNITS, value)))
                    last = value
        normalized = (amp / peak) if peak > 0 else 0.0
        normalized = floor + normalized * (1.0 - floor)
        events.append(
            (
                round(float(note.start), 3),
                round(float(note.end), 3),
                round(_hz_to_midi(note.pitch_hz), 2),
                round(normalized, 3),
                bends,
            )
        )
    return events


def _apply_post_processing(events: list, params: TranscriptionParams) -> list:
    """移调 + 量化：在写文件前统一处理，保持事件结构不变。"""
    if not events:
        return events

    divisions = params.quantize_divisions
    grid_seconds = None
    if params.quantize != "off":
        beat_seconds = 60.0 / max(1.0, float(params.tempo))
        grid_seconds = beat_seconds / divisions

    shift = int(params.transpose or 0)
    result = []
    for start, end, pitch, amp, bends in events:
        if grid_seconds:
            start = round(round(start / grid_seconds) * grid_seconds, 3)
            end = round(round(end / grid_seconds) * grid_seconds, 3)
            if end <= start:
                end = round(start + grid_seconds, 3)
        pitch = float(pitch) + shift
        if pitch < 0 or pitch > 127:
            continue
        result.append((start, end, round(pitch, 2), amp, bends))
    result.sort(key=lambda ev: ev[0])
    return result


def _ascii_track_name(name: str) -> str:
    """MIDI 的 meta 文本按规范是 latin-1，中日文曲名会让 mido 编码失败，这里降级成 ASCII。"""
    try:
        name.encode("latin-1")
        return name[:64] or "vocals"
    except UnicodeEncodeError:
        safe = name.encode("ascii", "ignore").decode("ascii").strip()
        return (safe or "vocals")[:64]


def _write_midi(
    events: list,
    out_mid: Path,
    track_name: str,
    params: TranscriptionParams | None = None,
) -> Path:
    """按统一事件格式写 .mid：整数音符 + 每音符的 pitchwheel 弯音。"""
    import mido

    params = params or TranscriptionParams()
    tempo = max(20, int(round(float(params.tempo or MIDI_TEMPO))))

    out_mid.parent.mkdir(parents=True, exist_ok=True)
    mid = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name=_ascii_track_name(track_name), time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo), time=0))

    # 弯音量程写进 RPN 0（Pitch Bend Sensitivity），DAW 才会按同样量程回放
    bend_range = int(params.bend_range or 0)
    if bend_range > 0:
        track.append(mido.Message("control_change", channel=0, control=101, value=0, time=0))
        track.append(mido.Message("control_change", channel=0, control=100, value=0, time=0))
        track.append(mido.Message("control_change", channel=0, control=6, value=max(0, min(24, bend_range)), time=0))
        track.append(mido.Message("control_change", channel=0, control=38, value=0, time=0))
        track.append(mido.Message("program_change", channel=0, program=53, time=0))

    ticks_per_second = TICKS_PER_BEAT * tempo / 60.0

    def to_ticks(seconds: float) -> int:
        return int(round(seconds * ticks_per_second))

    # 收集绝对时刻事件；(tick, 排序键) —— 同一 tick 上 pitchwheel 先于 note_on，
    # note_off 先于 note_on，保证相邻同音高音符不会被误并
    merged: list[tuple[int, int, object]] = []
    for start, end, pitch, amp, bends in events:
        base = int(round(pitch))
        if base < 0 or base > 127:
            continue
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
