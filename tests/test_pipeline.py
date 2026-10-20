"""流水线的产物布局与取消语义。

这些测试不碰 demucs、不碰 torch：分离与转写都被替换成内存里的假实现，
只验证「调用方决定了什么，产物就落在哪」，以及不指定时行为与改造前一致。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from vocal2midi.core import pipeline as pipeline_mod
from vocal2midi.core.cancellation import Cancelled


class FakeSession:
    """只标记自己代表「一个已加载的分离器」，不加载任何模型。"""

    samplerate = 8000


def _install_fakes(monkeypatch, tmp_path):
    """把分离与转写换成假实现，返回记录调用顺序的列表。"""
    calls: list[tuple[str, Path]] = []

    def fake_separate(audio_path, out_dir, **kwargs):
        calls.append(("separate", Path(audio_path)))
        stem = kwargs.get("stem") or Path(audio_path).stem
        target = Path(out_dir) / f"{stem}_vocals.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 2048)
        if kwargs.get("on_progress"):
            kwargs["on_progress"](1.0, "人声分离完成")
        return target

    def fake_transcribe(vocals_path, out_mid, params=None):
        calls.append(("transcribe", Path(vocals_path)))
        out_mid = Path(out_mid)
        out_mid.parent.mkdir(parents=True, exist_ok=True)
        out_mid.write_bytes(b"mid")
        return out_mid, [(0.0, 1.0, 60.0, 0.5)]

    monkeypatch.setattr(pipeline_mod.separation, "separate_vocals", fake_separate)
    monkeypatch.setattr(pipeline_mod.transcription, "transcribe_file", fake_transcribe)
    return calls


# ---- 产物布局 ----------------------------------------------------------


def test_default_naming_uses_the_input_stem(monkeypatch, tmp_path):
    calls = _install_fakes(monkeypatch, tmp_path)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    out_dir = tmp_path / "out"

    result = pipeline_mod.run_pipeline(pipeline_mod.PipelineOptions(audio_path=audio, out_dir=out_dir))

    assert result.vocals_path == out_dir / "song_vocals.wav"
    assert result.midi_path == out_dir / "song_vocals.mid"
    assert result.csv_path == out_dir / "song_vocals_notes.csv"
    assert calls == [("separate", audio), ("transcribe", out_dir / "song_vocals.wav")]


def test_explicit_stem_moves_all_three_products(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, tmp_path)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    out_dir = tmp_path / "out"

    result = pipeline_mod.run_pipeline(
        pipeline_mod.PipelineOptions(audio_path=audio, out_dir=out_dir, stem="song-2")
    )

    assert result.vocals_path == out_dir / "song-2_vocals.wav"
    assert result.midi_path == out_dir / "song-2_vocals.mid"
    assert result.csv_path == out_dir / "song-2_vocals_notes.csv"


def test_the_given_session_is_passed_down_to_separation(monkeypatch, tmp_path):
    """批量里一个已加载的分离器必须被真正用上，而不是每首重开。"""
    seen_sessions = []

    def fake_separate(audio_path, out_dir, **kwargs):
        seen_sessions.append(kwargs.get("session"))
        target = Path(out_dir) / "song_vocals.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 2048)
        return target

    monkeypatch.setattr(pipeline_mod.separation, "separate_vocals", fake_separate)
    monkeypatch.setattr(
        pipeline_mod.transcription, "transcribe_file", lambda v, m, p=None: (Path(m), [])
    )
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    session = FakeSession()

    pipeline_mod.run_pipeline(
        pipeline_mod.PipelineOptions(audio_path=audio, out_dir=tmp_path / "out", session=session)
    )

    assert seen_sessions == [session]


def test_vocals_only_puts_products_in_the_out_dir(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, tmp_path)
    audio = tmp_path / "song_vocals.wav"
    audio.write_bytes(b"fake")
    out_dir = tmp_path / "out"

    result = pipeline_mod.run_pipeline(
        pipeline_mod.PipelineOptions(audio_path=audio, out_dir=out_dir, vocals_only=True)
    )

    assert result.vocals_path == audio
    assert result.midi_path == out_dir / "song_vocals.mid"


# ---- 取消 --------------------------------------------------------------


def test_cancel_before_start_raises_cancelled(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, tmp_path)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    event = threading.Event()
    event.set()

    with pytest.raises(Cancelled):
        pipeline_mod.run_pipeline(
            pipeline_mod.PipelineOptions(audio_path=audio, out_dir=tmp_path / "out"),
            cancel_event=event,
        )


def test_cancel_during_separation_stops_before_transcribing(monkeypatch, tmp_path):
    calls = _install_fakes(monkeypatch, tmp_path)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    event = threading.Event()

    original = pipeline_mod.separation.separate_vocals

    def cancel_then_separate(*args, **kwargs):
        event.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline_mod.separation, "separate_vocals", cancel_then_separate)

    with pytest.raises(Cancelled):
        pipeline_mod.run_pipeline(
            pipeline_mod.PipelineOptions(audio_path=audio, out_dir=tmp_path / "out"),
            cancel_event=event,
        )
    assert [name for name, _ in calls] == ["separate"]


# ---- 进度 --------------------------------------------------------------


def test_progress_is_monotonic_and_ends_at_one(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, tmp_path)
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    seen: list[float] = []

    pipeline_mod.run_pipeline(
        pipeline_mod.PipelineOptions(audio_path=audio, out_dir=tmp_path / "out"),
        on_progress=lambda frac, _text: seen.append(frac),
    )

    assert seen == sorted(seen)
    assert seen[-1] == 1.0


def test_missing_input_is_reported_as_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        pipeline_mod.run_pipeline(
            pipeline_mod.PipelineOptions(audio_path=tmp_path / "nope.mp3", out_dir=tmp_path / "out")
        )
