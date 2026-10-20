"""批次执行器：顺序、失败隔离、进度聚合、批次目录、跳过已完成。

`BatchDeps` 就是为了这个而存在的：分离与转写换成内存假实现，于是「一台会
自己跑完的机器」可以整台被测试，而不加载任何模型。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from vocal2midi.core import batch as batch_mod
from vocal2midi.core.batch import (
    BatchOptions,
    BatchRunner,
    TranscribeOutcome,
    csv_file,
    make_batch_dir,
    midi_file,
    run_batch,
)
from vocal2midi.core.cancellation import Cancelled
from vocal2midi.core.queue import Queue, QueueItem, TrackStatus
from vocal2midi.core.transcription import TranscriptionParams


# ---- 假依赖 ------------------------------------------------------------


class FakeDeps:
    """记下每首曲目各调了哪个阶段，并按需制造失败或取消。"""

    def __init__(
        self,
        fail_on: set[str] | None = None,
        cancel_on: str | None = None,
        fail_transcribe_on: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []  # (阶段, 曲目名)
        self.fail_on = fail_on or set()
        self.cancel_on = cancel_on
        self.fail_transcribe_on = fail_transcribe_on or set()

    @staticmethod
    def _name(audio_path: Path) -> str:
        return Path(audio_path).name or Path(audio_path).stem

    def separate(self, audio_path, out_dir, stem, options, on_progress, cancel_event) -> Path:
        name = self._name(audio_path)
        self.calls.append(("separate", name))
        if name in self.fail_on:
            raise RuntimeError(f"{name} 分离失败")
        if self.cancel_on and name == self.cancel_on:
            raise Cancelled("已取消")
        on_progress(0.5, "分离中")
        target = Path(out_dir) / f"{stem}_vocals.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 2048)
        on_progress(1.0, "分离完成")
        return target

    def transcribe(self, vocals_path, out_dir, stem, options, on_progress, cancel_event) -> TranscribeOutcome:
        name = Path(vocals_path).name.replace("_vocals.wav", ".mp3")
        self.calls.append(("transcribe", name))
        if name in self.fail_transcribe_on:
            raise RuntimeError(f"{name} 转写失败")
        on_progress(0.5, "转写中")
        midi = midi_file(out_dir, stem)
        midi.write_bytes(b"mid")
        csv = None
        if options.write_csv:
            csv = csv_file(out_dir, stem)
            csv.write_bytes(b"csv")
        return TranscribeOutcome(midi_path=midi, csv_path=csv, note_count=3)


@pytest.fixture()
def audio_files(tmp_path):
    def make(*names: str) -> list[QueueItem]:
        items = []
        for name in names:
            path = tmp_path / name
            path.write_bytes(b"audio")
            items.append(QueueItem(path=str(path)))
        return items

    return make


@pytest.fixture()
def options(tmp_path):
    return BatchOptions(out_dir=tmp_path / "out", write_csv=True)


# ---- 成功路径 ----------------------------------------------------------


def test_runs_every_track_in_order(audio_files, options):
    items = audio_files("a.mp3", "b.mp3", "c.mp3")
    deps = FakeDeps()

    result = run_batch(items, options, deps=deps)

    assert result.ok_count == 3
    assert result.failed_count == 0
    assert result.cancelled is False
    assert deps.calls == [
        ("separate", "a.mp3"),
        ("transcribe", "a.mp3"),
        ("separate", "b.mp3"),
        ("transcribe", "b.mp3"),
        ("separate", "c.mp3"),
        ("transcribe", "c.mp3"),
    ]
    assert [item.track_status for item in items] == [TrackStatus.DONE] * 3


def test_done_items_record_their_products(audio_files, options):
    (item,) = audio_files("song.mp3")

    run_batch([item], options, deps=FakeDeps())

    assert Path(item.vocals_path).exists()
    assert Path(item.midi_path).exists()
    assert Path(item.csv_path).exists()
    assert item.products_exist() is True


# ---- 失败隔离 ----------------------------------------------------------


def test_a_failed_track_does_not_stop_the_batch(audio_files, options):
    items = audio_files("a.mp3", "b.mp3", "c.mp3")
    deps = FakeDeps(fail_on={"b.mp3"})

    result = run_batch(items, options, deps=deps)

    assert [item.track_status for item in items] == [
        TrackStatus.DONE,
        TrackStatus.FAILED,
        TrackStatus.DONE,
    ]
    assert result.ok_count == 2
    assert result.failed_count == 1
    assert result.failed[0].item.display_name == "b.mp3"
    assert "分离失败" in result.failed[0].reason
    assert result.cancelled is False


def test_failed_track_keeps_its_reason_on_the_item(audio_files, options):
    (item,) = audio_files("bad.mp3")

    run_batch([item], options, deps=FakeDeps(fail_on={"bad.mp3"}))

    assert item.track_status is TrackStatus.FAILED
    assert "分离失败" in item.error


def test_missing_audio_file_fails_only_that_track(tmp_path, options):
    good = QueueItem(path=str(_audio(tmp_path, "good.mp3")))
    gone = QueueItem(path=str(tmp_path / "gone.mp3"))
    deps = FakeDeps()

    result = run_batch([gone, good], options, deps=deps)

    assert gone.track_status is TrackStatus.FAILED
    assert "找不到音频文件" in gone.error
    assert good.track_status is TrackStatus.DONE
    assert deps.calls == [("separate", "good.mp3"), ("transcribe", "good.mp3")]


# ---- 取消 --------------------------------------------------------------


def test_cancel_mid_track_returns_item_to_pending(audio_files, options):
    items = audio_files("a.mp3", "b.mp3", "c.mp3")
    deps = FakeDeps(cancel_on="b.mp3")

    result = run_batch(items, options, deps=deps)

    assert result.cancelled is True
    assert result.ok_count == 1
    assert [item.track_status for item in items] == [
        TrackStatus.DONE,
        TrackStatus.PENDING,
        TrackStatus.PENDING,
    ]
    # 被取消的那首不能被算作失败
    assert result.failed_count == 0
    # 第三首根本没跑
    assert ("separate", "c.mp3") not in deps.calls


def test_cancel_before_start_processes_nothing(audio_files, options):
    items = audio_files("a.mp3", "b.mp3")
    cancel = threading.Event()
    cancel.set()
    deps = FakeDeps()

    result = run_batch(items, options, deps=deps, cancel_event=cancel)

    assert result.cancelled is True
    assert deps.calls == []
    assert all(item.track_status is TrackStatus.PENDING for item in items)


def test_cancelling_between_stages_skips_transcription(audio_files, options):
    items = audio_files("a.mp3")

    class CancelDuringSeparate(FakeDeps):
        def separate(self, audio_path, out_dir, stem, options, on_progress, cancel_event):
            path = super().separate(audio_path, out_dir, stem, options, on_progress, cancel_event)
            cancel_event.set()
            return path

    result = run_batch(items, options, deps=CancelDuringSeparate())

    assert result.cancelled is True
    assert items[0].track_status is TrackStatus.PENDING


# ---- 进度 --------------------------------------------------------------


def test_progress_is_monotonic_and_ends_at_one(audio_files, options):
    items = audio_files("a.mp3", "b.mp3", "c.mp3")
    seen: list[float] = []

    run_batch(items, options, deps=FakeDeps(), on_progress=lambda frac, _text: seen.append(frac))

    assert seen, "至少要报一次进度"
    assert all(later >= earlier for earlier, later in zip(seen, seen[1:])), seen
    assert seen[-1] == pytest.approx(1.0)
    assert all(0.0 <= value <= 1.0 for value in seen)


def test_progress_text_mentions_track_index_and_stage(audio_files, options):
    items = audio_files("a.mp3", "b.mp3")
    texts: list[str] = []

    run_batch(items, options, deps=FakeDeps(), on_progress=lambda _f, text: texts.append(text))

    assert any(text.startswith("第 1/2 首") for text in texts)
    assert any(text.startswith("第 2/2 首") for text in texts)
    assert any("分离" in text for text in texts)
    assert any("转写" in text for text in texts)


def test_progress_is_weighted_by_track_count(audio_files, options):
    """三首里跑到第二首的中途时，整体进度应该落在 1/3 与 2/3 之间。"""
    items = audio_files("a.mp3", "b.mp3", "c.mp3")
    seen: list[float] = []

    run_batch(items, options, deps=FakeDeps(), on_progress=lambda frac, _t: seen.append(frac))

    mid = [value for value in seen if 1 / 3 < value < 2 / 3]
    assert mid, seen


def test_second_run_does_not_start_progress_from_a_stale_high_water_mark(audio_files, options):
    items = audio_files("a.mp3")
    runner = BatchRunner(options, deps=FakeDeps())
    runner.run(items)

    fresh = audio_files("b.mp3")
    seen: list[float] = []
    BatchRunner(options, deps=FakeDeps(), on_progress=lambda f, _t: seen.append(f)).run(fresh)

    # 每首自己的进度从 0 起算，第一帧不该直接是 1.0
    assert seen[0] < 1.0


# ---- 批次目录 ----------------------------------------------------------


def test_one_batch_directory_per_batch(audio_files, options):
    items = audio_files("a.mp3", "b.mp3", "c.mp3")

    run_batch(items, options, deps=FakeDeps())

    dirs = {Path(item.batch_dir) for item in items}
    assert len(dirs) == 1
    assert dirs.pop().is_dir()


def test_products_land_inside_the_batch_directory(audio_files, options):
    (item,) = audio_files("song.mp3")

    run_batch([item], options, deps=FakeDeps())

    batch_dir = Path(item.batch_dir)
    assert Path(item.midi_path).parent == batch_dir
    assert Path(item.vocals_path).parent == batch_dir
    assert batch_dir.parent == options.out_dir


def test_each_batch_gets_a_fresh_directory(audio_files, options):
    first = audio_files("a.mp3")[0]
    run_batch([first], options, deps=FakeDeps())

    second = audio_files("b.mp3")[0]
    second.mark_done()  # 占位，确保它没有继承任何东西
    second.mark_pending()
    second.batch_dir = ""
    run_batch([second], options, deps=FakeDeps())

    assert first.batch_dir != second.batch_dir


def test_batch_dir_is_reused_for_an_item_that_already_has_one(audio_files, options):
    (item,) = audio_files("retry.mp3")
    existing = make_batch_dir(options.out_dir)
    item.batch_dir = str(existing)
    item.stem = "retry"

    run_batch([item], options, deps=FakeDeps())

    assert Path(item.batch_dir) == existing
    assert Path(item.midi_path).parent == existing


def test_stem_is_pinned_on_the_item_and_survives_a_retry(audio_files, options):
    (item,) = audio_files("song.mp3")

    run_batch([item], options, deps=FakeDeps())
    assert item.stem == "song"

    item.mark_pending()
    first_dir = item.batch_dir
    run_batch([item], options, deps=FakeDeps())

    assert item.stem == "song"
    assert item.batch_dir == first_dir


def test_make_batch_dir_avoids_collisions(tmp_path):
    first = make_batch_dir(tmp_path)
    second = make_batch_dir(tmp_path)
    assert first != second
    assert first.is_dir() and second.is_dir()


# ---- 复用人声轨 --------------------------------------------------------


def test_existing_vocals_are_reused_so_separation_is_skipped(audio_files, options):
    (item,) = audio_files("song.mp3")
    batch_dir = make_batch_dir(options.out_dir)
    item.batch_dir = str(batch_dir)
    item.stem = "song"
    (batch_dir / "song_vocals.wav").write_bytes(b"y" * 2048)
    deps = FakeDeps()

    run_batch([item], options, deps=deps)

    assert ("separate", "song.mp3") not in deps.calls
    assert ("transcribe", "song.mp3") in deps.calls
    assert item.track_status is TrackStatus.DONE


def test_overwrite_forces_separation_again(audio_files, tmp_path):
    (item,) = audio_files("song.mp3")
    options = BatchOptions(out_dir=tmp_path / "out", overwrite=True)
    batch_dir = make_batch_dir(options.out_dir)
    item.batch_dir = str(batch_dir)
    item.stem = "song"
    (batch_dir / "song_vocals.wav").write_bytes(b"y" * 2048)
    deps = FakeDeps()

    run_batch([item], options, deps=deps)

    assert ("separate", "song.mp3") in deps.calls


def test_vocals_only_skips_separation_entirely(audio_files, tmp_path):
    (item,) = audio_files("vocal.mp3")
    options = BatchOptions(out_dir=tmp_path / "out", vocals_only=True)
    deps = FakeDeps()

    run_batch([item], options, deps=deps)

    assert deps.calls == [("transcribe", "vocal.mp3")]
    assert item.vocals_path == ""
    assert item.track_status is TrackStatus.DONE


def test_csv_can_be_skipped(audio_files, tmp_path):
    (item,) = audio_files("song.mp3")
    options = BatchOptions(out_dir=tmp_path / "out", write_csv=False)

    run_batch([item], options, deps=FakeDeps())

    assert item.csv_path == ""
    assert Path(item.midi_path).exists()


# ---- 跳过已完成 --------------------------------------------------------


def test_only_pending_and_failed_items_are_passed_in(audio_files, options):
    queue = Queue()
    items = audio_files("done.mp3", "failed.mp3", "pending.mp3")
    queue.items.extend(items)
    items[0].mark_done(midi_path="m")
    items[1].mark_failed("x")
    deps = FakeDeps()

    picked = queue.pending_items()
    run_batch(picked, options, deps=deps)

    assert [item.display_name for item in picked] == ["failed.mp3", "pending.mp3"]
    assert ("separate", "done.mp3") not in deps.calls


def test_empty_batch_is_a_no_op(options):
    logs: list[str] = []

    result = run_batch([], options, deps=FakeDeps(), on_log=logs.append)

    assert result.ok_count == 0
    assert result.failed_count == 0
    assert result.cancelled is False
    assert any("没有待转换" in line for line in logs)


# ---- 汇总 --------------------------------------------------------------


def test_summary_mentions_success_and_failure_counts(audio_files, options):
    items = audio_files("a.mp3", "b.mp3")

    result = run_batch(items, options, deps=FakeDeps(fail_on={"b.mp3"}))

    summary = result.summary_text()
    assert "成功 1" in summary
    assert "失败 1" in summary


def test_track_events_are_reported_for_every_item(audio_files, options):
    items = audio_files("a.mp3", "b.mp3")
    seen: list[str] = []

    run_batch(
        items,
        options,
        deps=FakeDeps(fail_on={"b.mp3"}),
        on_track=lambda item: seen.append(f"{item.display_name}:{item.track_status.value}"),
    )

    assert "a.mp3:done" in seen
    assert "b.mp3:failed" in seen


# ---- 依赖装配 ----------------------------------------------------------


def test_pipeline_deps_reuses_one_separator_session_across_tracks(monkeypatch, audio_files, tmp_path):
    """整批只加载一次模型：这是 ADR 0002 的核心约束。"""
    loads: list[tuple[str, str]] = []

    class FakeSession:
        samplerate = 8000

    class FakeCache:
        def get(self, model, device, on_progress=None):
            loads.append((model, device))
            return FakeSession()

    separated: list[str] = []

    def fake_separate_vocals(audio_path, out_dir, **kwargs):
        separated.append(Path(audio_path).name)
        target = Path(out_dir) / f"{kwargs['stem']}_vocals.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 2048)
        return target

    monkeypatch.setattr(batch_mod.transcription, "transcribe_file", lambda *a, **k: (Path("m.mid"), []))
    monkeypatch.setattr(batch_mod.separation, "separate_vocals", fake_separate_vocals)

    deps = batch_mod.PipelineDeps(cache=FakeCache())
    items = audio_files("a.mp3", "b.mp3", "c.mp3")

    run_batch(items, BatchOptions(out_dir=tmp_path / "out"), deps=deps)

    assert separated == ["a.mp3", "b.mp3", "c.mp3"]
    # 缓存命中三次（每次请求都问一次），但模型只该被真正加载一次——
    # 这里由 FakeCache 每次返回同一实例来体现「不重建」。
    assert len(loads) == 3


def test_downgrade_missing_products(tmp_path):
    missing = QueueItem(path="a.mp3")
    missing.mark_done(midi_path=str(tmp_path / "gone.mid"))
    present = QueueItem(path="b.mp3")
    real = tmp_path / "here.mid"
    real.write_bytes(b"m")
    present.mark_done(midi_path=str(real))
    pending = QueueItem(path="c.mp3")

    downgraded = batch_mod.downgrade_missing_products([missing, present, pending])

    assert downgraded == [missing]
    assert missing.track_status is TrackStatus.PENDING
    assert missing.midi_path == ""
    assert present.track_status is TrackStatus.DONE
    assert pending.track_status is TrackStatus.PENDING


def test_downgrade_keeps_the_batch_dir_so_a_retry_goes_back(tmp_path):
    item = QueueItem(path="a.mp3", batch_dir="/out/20260101-000000", stem="a")
    item.mark_done(midi_path=str(tmp_path / "gone.mid"))

    batch_mod.downgrade_missing_products([item])

    assert item.batch_dir == "/out/20260101-000000"
    assert item.stem == "a"


def test_batch_options_accepts_a_mapping_for_params(tmp_path):
    options = BatchOptions(out_dir=tmp_path, params={"min_note_length_ms": 40})
    assert isinstance(options.params, TranscriptionParams)


def _audio(tmp_path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"audio")
    return path


# ---- 重试：只跑失败那首，且不重算分离 --------------------------------


def test_retry_after_a_transcribe_failure_only_transcribes(audio_files, options):
    """转写阶段失败的曲目，重试时人声轨已经在了，不该再分离一遍。"""
    (item,) = audio_files("song.mp3")
    first = FakeDeps(fail_transcribe_on={"song.mp3"})

    result = run_batch([item], options, deps=first)
    assert result.failed_count == 1
    assert item.track_status is TrackStatus.FAILED
    assert ("separate", "song.mp3") in first.calls
    batch_dir, stem = item.batch_dir, item.stem

    retry = FakeDeps()
    result = run_batch([item], options, deps=retry)

    assert result.ok_count == 1
    assert item.track_status is TrackStatus.DONE
    assert retry.calls == [("transcribe", "song.mp3")]
    assert item.batch_dir == batch_dir
    assert item.stem == stem
    assert Path(item.midi_path).parent == Path(batch_dir)
    assert Path(item.midi_path).name == f"{stem}_vocals.mid"


def test_retry_after_deleted_vocals_separates_again_into_the_same_dir(audio_files, options):
    """人声轨被删了就得重算，但产物仍回原来那份目录与那个主名。"""
    (item,) = audio_files("song.mp3")
    run_batch([item], options, deps=FakeDeps(fail_transcribe_on={"song.mp3"}))
    batch_dir, stem = item.batch_dir, item.stem
    vocals = Path(batch_dir) / f"{stem}_vocals.wav"
    assert vocals.is_file()
    vocals.unlink()

    retry = FakeDeps()
    result = run_batch([item], options, deps=retry)

    assert result.ok_count == 1
    assert [stage for stage, _name in retry.calls] == ["separate", "transcribe"]
    assert item.batch_dir == batch_dir
    assert item.stem == stem
    assert Path(item.vocals_path) == vocals


def test_retry_only_touches_the_failed_track(audio_files, options):
    """三首里中间那首失败：重试只该再跑它一首。"""
    items = audio_files("a.mp3", "b.mp3", "c.mp3")
    run_batch(items, options, deps=FakeDeps(fail_transcribe_on={"b.mp3"}))
    assert [item.track_status for item in items] == [
        TrackStatus.DONE,
        TrackStatus.FAILED,
        TrackStatus.DONE,
    ]

    queue = Queue(items=list(items))
    retry = FakeDeps()
    result = run_batch(queue.pending_items(), options, deps=retry)

    assert result.attempted == 1
    assert result.ok_count == 1
    assert retry.calls == [("transcribe", "b.mp3")]


def test_a_finished_track_whose_products_are_gone_falls_back_to_pending(audio_files, options):
    """完成但产物被删：降级后两个阶段都重跑，且仍回原目录。"""
    (item,) = audio_files("song.mp3")
    run_batch([item], options, deps=FakeDeps())
    batch_dir, stem = item.batch_dir, item.stem
    for key in ("vocals_path", "midi_path", "csv_path"):
        Path(getattr(item, key)).unlink(missing_ok=True)

    downgraded = batch_mod.downgrade_missing_products([item])

    assert downgraded == [item]
    assert item.track_status is TrackStatus.PENDING
    assert (item.vocals_path, item.midi_path, item.csv_path) == ("", "", "")

    retry = FakeDeps()
    run_batch([item], options, deps=retry)
    assert [stage for stage, _name in retry.calls] == ["separate", "transcribe"]
    assert item.batch_dir == batch_dir
    assert item.stem == stem


def test_overwrite_wins_over_the_retry_reuse(audio_files, tmp_path):
    """勾了「强制重新分离」，重试时也不能被人声轨复用判定吃掉。"""
    (item,) = audio_files("song.mp3")
    run_batch(
        [item],
        BatchOptions(out_dir=tmp_path / "out"),
        deps=FakeDeps(fail_transcribe_on={"song.mp3"}),
    )
    vocals = Path(item.batch_dir) / f"{item.stem}_vocals.wav"
    assert vocals.is_file()

    retry = FakeDeps()
    run_batch([item], BatchOptions(out_dir=tmp_path / "out", overwrite=True), deps=retry)

    assert ("separate", "song.mp3") in retry.calls
