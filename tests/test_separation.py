"""分离层的纯逻辑测试。

不加载 demucs、不加载 torch、不下载权重：这些测试只覆盖
「产物命名」「复用判定」「取消信号到异常的转换」「分离器缓存」这四件纯逻辑。
真跑一次分离属于人工验收。
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from vocal2midi.core import separation
from vocal2midi.core.cancellation import Cancelled


class FakeTensor:
    """模拟 demucs 返回的 torch.Tensor，只实现被用到的三个方法。"""

    def __init__(self, array: np.ndarray) -> None:
        self._array = array

    def detach(self) -> "FakeTensor":
        return self

    def cpu(self) -> "FakeTensor":
        return self

    def numpy(self) -> np.ndarray:
        return self._array


class FakeSeparator:
    """模拟 demucs 的 Separator，记录被调用了几次。"""

    def __init__(self, samplerate: int = 8000, samples: int = 100) -> None:
        self.samplerate = samplerate
        self.calls: list[str] = []
        self._samples = samples
        self.raise_on_call: BaseException | None = None

    def separate_audio_file(self, path: str):
        self.calls.append(path)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        vocals = FakeTensor(np.zeros((2, self._samples), dtype=np.float32))
        return path, {"vocals": vocals, "drums": None}


# ---- 产物命名 ----------------------------------------------------------


def test_vocals_file_uses_stem_and_lands_in_out_dir(tmp_path):
    target = separation.vocals_file(tmp_path, "song")
    assert target == tmp_path / "song_vocals.wav"


def test_vocals_file_does_not_depend_on_the_input_path(tmp_path):
    """同一个主名在同一个目录里必须落到同一个文件，重试才能命中。"""
    first = separation.vocals_file(tmp_path, "song")
    second = separation.vocals_file(tmp_path, "song")
    assert first == second


# ---- 复用判定 ----------------------------------------------------------


def test_is_reusable_vocals_is_false_when_absent(tmp_path):
    assert separation.is_reusable_vocals(tmp_path / "missing.wav") is False


def test_is_reusable_vocals_rejects_a_truncated_file(tmp_path):
    target = tmp_path / "tiny.wav"
    target.write_bytes(b"x" * 16)
    assert separation.is_reusable_vocals(target) is False


def test_is_reusable_vocals_accepts_a_plausible_file(tmp_path):
    target = tmp_path / "ok.wav"
    target.write_bytes(b"x" * (separation.MIN_VOCALS_BYTES + 1))
    assert separation.is_reusable_vocals(target) is True


def test_reuse_only_looks_in_the_given_directory(tmp_path):
    """别的目录里有同名文件，不能影响本目录的判定。"""
    other = tmp_path / "other"
    other.mkdir()
    (other / "song_vocals.wav").write_bytes(b"x" * 4096)
    assert separation.is_reusable_vocals(separation.vocals_file(tmp_path, "song")) is False


# ---- 分离器缓存 --------------------------------------------------------


def test_separator_cache_loads_each_model_device_pair_once():
    loaded: list[tuple[str, str]] = []

    def loader(model, device, on_progress=None):
        loaded.append((model, device))
        return separation.SeparatorSession(FakeSeparator())

    cache = separation.SeparatorCache(loader=loader)
    first = cache.get("htdemucs_ft", "cuda")
    second = cache.get("htdemucs_ft", "cuda")

    assert first is second
    assert loaded == [("htdemucs_ft", "cuda")]


def test_separator_cache_keeps_models_apart():
    loaded: list[tuple[str, str]] = []

    def loader(model, device, on_progress=None):
        loaded.append((model, device))
        return separation.SeparatorSession(FakeSeparator())

    cache = separation.SeparatorCache(loader=loader)
    cache.get("htdemucs_ft", "cuda")
    cache.get("htdemucs", "cuda")

    assert len(loaded) == 2


# ---- 取消 --------------------------------------------------------------


def test_abort_callback_raises_when_cancel_is_set():
    event = threading.Event()
    event.set()
    callback = separation.make_abort_callback(event)
    with pytest.raises(KeyboardInterrupt):
        callback({"state": "start", "segment_offset": 0, "audio_length": 100})


def test_abort_callback_reports_progress_while_running():
    seen: list[float] = []
    callback = separation.make_abort_callback(
        threading.Event(), on_progress=lambda frac, _text: seen.append(frac)
    )
    callback({"state": "start", "segment_offset": 0, "audio_length": 100})
    callback({"state": "start", "segment_offset": 50, "audio_length": 100})

    assert len(seen) == 2
    assert seen[0] < seen[1]
    assert all(0.0 <= value <= 1.0 for value in seen)


def test_abort_callback_ignores_end_events():
    seen: list[float] = []
    callback = separation.make_abort_callback(
        threading.Event(), on_progress=lambda frac, _text: seen.append(frac)
    )
    callback({"state": "end", "segment_offset": 0, "audio_length": 100})
    assert seen == []


def test_session_turns_interrupt_into_cancelled(tmp_path):
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"fake")
    separator = FakeSeparator()
    separator.raise_on_call = KeyboardInterrupt()
    session = separation.SeparatorSession(separator)

    with pytest.raises(Cancelled):
        session.separate_to(audio, tmp_path / "out.wav")


def test_session_writes_vocals_to_the_given_target(tmp_path):
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"fake")
    separator = FakeSeparator()
    session = separation.SeparatorSession(separator)
    target = tmp_path / "song_vocals.wav"

    returned = session.separate_to(audio, target)

    assert returned == target
    assert target.exists()
    assert target.stat().st_size > 0
    assert separator.calls == [str(audio)]


# ---- separate_vocals 的编排 -------------------------------------------


def test_separate_vocals_reuses_an_existing_track(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "song_vocals.wav").write_bytes(b"x" * 4096)

    session = separation.SeparatorSession(FakeSeparator())
    result = separation.separate_vocals(audio, out_dir, session=session)

    assert result == out_dir / "song_vocals.wav"
    assert session.separator.calls == []


def test_separate_vocals_ignores_the_cache_when_overwrite_is_set(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "song_vocals.wav").write_bytes(b"x" * 4096)

    session = separation.SeparatorSession(FakeSeparator())
    separation.separate_vocals(audio, out_dir, overwrite=True, session=session)

    assert len(session.separator.calls) == 1


def test_separate_vocals_accepts_an_explicit_stem(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    out_dir = tmp_path / "out"

    session = separation.SeparatorSession(FakeSeparator())
    result = separation.separate_vocals(audio, out_dir, stem="song-2", session=session)

    assert result == out_dir / "song-2_vocals.wav"


def test_separate_vocals_never_hits_a_same_named_file_in_another_dir(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    batch = tmp_path / "batch"
    other = tmp_path / "other"
    other.mkdir(parents=True)
    (other / "song_vocals.wav").write_bytes(b"x" * 4096)

    session = separation.SeparatorSession(FakeSeparator())
    separation.separate_vocals(audio, batch, session=session)

    assert len(session.separator.calls) == 1
    assert (batch / "song_vocals.wav").exists()
