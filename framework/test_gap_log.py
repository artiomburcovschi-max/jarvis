"""Тесты для раунда 27 - gap_log.py ("лог трения").

Путь к файлу подменяется на временный в каждом тесте - реальный
data/gaps.jsonl проекта не трогается.
"""
import json
import sys

import pytest

sys.path.insert(0, ".")

import gap_log  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    log_file = tmp_path / "gaps.jsonl"
    monkeypatch.setattr(gap_log, "_log_path", lambda: log_file)
    yield log_file


def _read_lines(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_log_gap_writes_entry(_isolated_log):
    gap_log.log_gap("tool_exception", "что-то сломалось", "открой блокнот")

    entries = _read_lines(_isolated_log)
    assert len(entries) == 1
    assert entries[0]["kind"] == "tool_exception"
    assert entries[0]["detail"] == "что-то сломалось"
    assert entries[0]["user_text"] == "открой блокнот"
    assert "ts" in entries[0]


def test_multiple_entries_appended_in_order(_isolated_log):
    gap_log.log_gap("kind_a", "первое")
    gap_log.log_gap("kind_b", "второе")
    gap_log.log_gap("kind_c", "третье")

    entries = _read_lines(_isolated_log)
    assert [e["kind"] for e in entries] == ["kind_a", "kind_b", "kind_c"]


def test_user_text_defaults_to_empty_string(_isolated_log):
    gap_log.log_gap("hop_limit_reached", "исчерпано 4 шага")
    entries = _read_lines(_isolated_log)
    assert entries[0]["user_text"] == ""


def test_fifo_eviction_when_over_cap(_isolated_log, monkeypatch):
    monkeypatch.setattr(gap_log, "MAX_GAP_LOG_ENTRIES", 3)

    for i in range(5):
        gap_log.log_gap("kind", f"событие {i}")

    entries = _read_lines(_isolated_log)
    assert len(entries) == 3
    assert [e["detail"] for e in entries] == ["событие 2", "событие 3", "событие 4"]


def test_read_recent_gaps_returns_latest_n(_isolated_log):
    for i in range(10):
        gap_log.log_gap("kind", f"событие {i}")

    recent = gap_log.read_recent_gaps(limit=3)
    assert [e["detail"] for e in recent] == ["событие 7", "событие 8", "событие 9"]


def test_read_recent_gaps_with_no_file_returns_empty(_isolated_log):
    assert not _isolated_log.exists()
    assert gap_log.read_recent_gaps() == []


def test_read_recent_gaps_skips_corrupted_lines(_isolated_log):
    _isolated_log.parent.mkdir(parents=True, exist_ok=True)
    with open(_isolated_log, "w", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "ok1", "detail": "x", "user_text": "", "ts": 1.0}) + "\n")
        f.write("это не json совсем\n")
        f.write(json.dumps({"kind": "ok2", "detail": "y", "user_text": "", "ts": 2.0}) + "\n")

    entries = gap_log.read_recent_gaps()
    assert [e["kind"] for e in entries] == ["ok1", "ok2"]


def test_log_gap_does_not_raise_on_unwritable_directory(monkeypatch, tmp_path):
    # Путь указывает на файл, чей родитель - существующий ФАЙЛ (не
    # директория) - mkdir/open внутри упадут; log_gap должен проглотить
    # ошибку, а не бросить исключение наружу.
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_text("x", encoding="utf-8")
    bad_path = blocking_file / "gaps.jsonl"
    monkeypatch.setattr(gap_log, "_log_path", lambda: bad_path)

    gap_log.log_gap("kind", "не должно упасть")  # не должно бросить исключение
