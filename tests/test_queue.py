"""队列：入队校验、状态迁移、JSON 往返、批次选取。

队列不碰磁盘上的产物、不依赖 Qt，所以这里全是纯数据测试。
"""

from __future__ import annotations

import json

import pytest

from vocal2midi.core.queue import (
    Queue,
    QueueItem,
    TrackStatus,
    check_importable,
    normalise_status,
)
from vocal2midi.core.settings import Settings


# ---- 入队校验 ----------------------------------------------------------


@pytest.fixture()
def audio_file(tmp_path):
    def make(name: str = "song.mp3") -> str:
        path = tmp_path / name
        path.write_bytes(b"fake audio")
        return str(path)

    return make


def test_check_importable_accepts_supported_audio(audio_file):
    assert check_importable(audio_file("a.mp3")) == ""


def test_check_importable_rejects_unknown_suffix(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hi", encoding="utf-8")
    assert "不支持的格式" in check_importable(path)


def test_check_importable_rejects_missing_file(tmp_path):
    assert check_importable(tmp_path / "ghost.mp3") == "文件不存在"


def test_check_importable_rejects_directory(tmp_path):
    folder = tmp_path / "album.mp3"
    folder.mkdir()
    assert check_importable(folder) == "不是文件"


def test_check_importable_rejects_empty_path():
    assert check_importable("") == "路径为空"


# ---- 增删与顺序 --------------------------------------------------------


def test_add_paths_keeps_given_order(audio_file):
    queue = Queue()
    accepted, rejected = queue.add_paths([audio_file("b.mp3"), audio_file("a.mp3")])
    assert rejected == []
    assert [item.display_name for item in accepted] == ["b.mp3", "a.mp3"]
    assert [item.display_name for item in queue.items] == ["b.mp3", "a.mp3"]


def test_add_paths_rejects_bad_entries_without_touching_the_rest(audio_file, tmp_path):
    queue = Queue()
    accepted, rejected = queue.add_paths(
        [audio_file("good.mp3"), str(tmp_path / "nope.mp3"), str(tmp_path / "x.txt")]
    )
    assert [item.display_name for item in accepted] == ["good.mp3"]
    assert len(rejected) == 2
    assert queue.total == 1


def test_remove_move_and_clear(audio_file):
    queue = Queue()
    queue.add_paths([audio_file("1.mp3"), audio_file("2.mp3"), audio_file("3.mp3")])

    assert queue.move_down(0) is True
    assert [item.display_name for item in queue.items] == ["2.mp3", "1.mp3", "3.mp3"]
    assert queue.move_up(1) is True
    assert [item.display_name for item in queue.items] == ["1.mp3", "2.mp3", "3.mp3"]

    assert queue.move_up(0) is False
    assert queue.move_down(2) is False

    assert queue.remove_at(1) is True
    assert [item.display_name for item in queue.items] == ["1.mp3", "3.mp3"]
    assert queue.remove_at(9) is False

    queue.clear()
    assert queue.total == 0


def test_display_name_falls_back_to_path(tmp_path):
    item = QueueItem(path=str(tmp_path))
    assert item.display_name


# ---- 状态 --------------------------------------------------------------


def test_normalise_status_falls_back_to_pending():
    assert normalise_status("done") is TrackStatus.DONE
    assert normalise_status("DONE") is TrackStatus.DONE
    assert normalise_status("nonsense") is TrackStatus.PENDING
    assert normalise_status(None) is TrackStatus.PENDING


def test_status_labels_are_chinese():
    assert TrackStatus.PENDING.label == "待转换"
    assert TrackStatus.RUNNING.label == "转换中"
    assert TrackStatus.DONE.label == "完成"
    assert TrackStatus.FAILED.label == "失败"


def test_mark_done_records_products_and_clears_error():
    item = QueueItem(path="a.mp3")
    item.mark_failed("坏了")
    assert item.track_status is TrackStatus.FAILED
    assert item.error == "坏了"

    item.mark_done(vocals_path="v.wav", midi_path="m.mid", csv_path="c.csv")
    assert item.track_status is TrackStatus.DONE
    assert item.error == ""
    assert item.products == ["v.wav", "m.mid", "c.csv"]


def test_mark_pending_can_keep_batch_dir():
    item = QueueItem(path="a.mp3", batch_dir="/out/20260101-000000", stem="a")
    item.mark_done(midi_path="m.mid")
    item.mark_pending()
    assert item.track_status is TrackStatus.PENDING
    assert item.products == []
    # 批次目录与产物主名不能丢：重试要回到同一个地方
    assert item.batch_dir == "/out/20260101-000000"
    assert item.stem == "a"


def test_products_exist_requires_every_recorded_file(tmp_path):
    midi = tmp_path / "m.mid"
    midi.write_bytes(b"m")
    item = QueueItem(path="a.mp3")
    item.mark_done(midi_path=str(midi))
    assert item.products_exist() is True

    item.csv_path = str(tmp_path / "gone.csv")
    assert item.products_exist() is False


def test_products_exist_is_false_when_nothing_recorded():
    assert QueueItem(path="a.mp3").products_exist() is False


# ---- 批次选取 ----------------------------------------------------------


def test_pending_items_are_pending_and_failed(audio_file):
    queue = Queue()
    items, _ = queue.add_paths([audio_file("1.mp3"), audio_file("2.mp3"), audio_file("3.mp3")])
    items[0].mark_done(midi_path="m")
    items[1].mark_failed("x")
    assert [item.display_name for item in queue.pending_items()] == ["2.mp3", "3.mp3"]


def test_counts_tally_every_status(audio_file):
    queue = Queue()
    items, _ = queue.add_paths([audio_file("1.mp3"), audio_file("2.mp3")])
    items[0].mark_done(midi_path="m")
    counts = queue.counts()
    assert counts["done"] == 1
    assert counts["pending"] == 1
    assert set(counts) == {status.value for status in TrackStatus}


# ---- JSON 往返 ---------------------------------------------------------


def test_queue_round_trips_through_json(audio_file):
    queue = Queue()
    items, _ = queue.add_paths([audio_file("1.mp3"), audio_file("2.mp3")])
    items[0].mark_done(vocals_path="v.wav", midi_path="m.mid")
    items[0].batch_dir = "/out/batch"
    items[0].stem = "1"

    payload = json.loads(json.dumps(queue.to_dicts()))
    restored = Queue.from_dicts(payload)

    assert [item.path for item in restored.items] == [item.path for item in queue.items]
    assert restored.items[0].track_status is TrackStatus.DONE
    assert restored.items[0].batch_dir == "/out/batch"
    assert restored.items[0].stem == "1"
    assert restored.items[1].track_status is TrackStatus.PENDING


def test_queue_does_not_truncate_like_recent_files(audio_file):
    queue = Queue()
    paths = [audio_file(f"{index}.mp3") for index in range(12)]
    queue.add_paths(paths)
    assert queue.total == 12


def test_reordering_survives_a_save_and_reload(audio_file):
    """新顺序要跟着队列一起存下来，重启后不能又弹回旧顺序。"""
    queue = Queue()
    queue.add_paths([audio_file("1.mp3"), audio_file("2.mp3"), audio_file("3.mp3")])

    queue.move_down(0)
    restored = Queue.from_dicts(json.loads(json.dumps(queue.to_dicts())))

    assert [item.display_name for item in restored.items] == ["2.mp3", "1.mp3", "3.mp3"]


def test_moving_never_loses_or_duplicates_an_item(audio_file):
    """把能合法的每一步都走一遍，条目集合不能变。"""
    queue = Queue()
    queue.add_paths([audio_file("1.mp3"), audio_file("2.mp3"), audio_file("3.mp3")])
    before = sorted(item.path for item in queue.items)

    for index in range(queue.total):
        queue.move_up(index)
        queue.move_down(index)

    assert sorted(item.path for item in queue.items) == before
    assert queue.total == 3


def test_from_dicts_tolerates_garbage():
    assert Queue.from_dicts("nope").total == 0
    assert Queue.from_dicts([{"no_path": 1}, 42, {"path": "  "}]).total == 0
    assert Queue.from_dicts([{"path": "a.mp3", "status": "weird"}]).items[0].track_status is TrackStatus.PENDING


def test_a_running_item_restored_from_disk_goes_back_to_pending():
    """落盘的 running 只可能是「进程在批次中途被杀」：它没有人在推进。

    留着它会让这首曲目永远显示「转换中」且再也不会被处理，所以重启后
    必须退回待转换；批次目录与主名要保留，重试才能回到同一个地方。
    """
    restored = Queue.from_dicts(
        [
            {
                "path": "C:/music/a.mp3",
                "status": "running",
                "batch_dir": "C:/out/20260101-000000",
                "stem": "a",
            }
        ]
    )

    (item,) = restored.items
    assert item.track_status is TrackStatus.PENDING
    assert item.batch_dir == "C:/out/20260101-000000"
    assert item.stem == "a"
    assert restored.pending_items() == [item]


# ---- 设置文件 ----------------------------------------------------------


def test_a_corrupted_settings_file_degrades_to_an_empty_queue(tmp_path):
    """文件被截断或写坏时，不能把启动也一起弄挂。"""
    broken = tmp_path / "settings.json"
    broken.write_text('{"queue": [{"path": "a.mp3"', encoding="utf-8")

    settings = Settings.load(broken)

    assert settings.queue == []
    assert Queue.from_dicts(settings.queue).total == 0
