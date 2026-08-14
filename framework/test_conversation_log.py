"""Тесты для раунда 28 (E2) - conversation_log.py (полный транскрипт на
диск). Путь к файлу подменяется на временный - реальный
data/conversation_log.jsonl проекта не трогается."""
import json
import sys

import pytest

sys.path.insert(0, ".")

import conversation_log  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    log_file = tmp_path / "conversation_log.jsonl"
    monkeypatch.setattr(conversation_log, "_log_path", lambda: log_file)
    yield log_file


def _read_lines(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_log_message_writes_entry(_isolated_log):
    conversation_log.log_message("user", "привет, Джарвис")

    entries = _read_lines(_isolated_log)
    assert len(entries) == 1
    assert entries[0]["role"] == "user"
    assert entries[0]["content"] == "привет, Джарвис"
    assert "ts" in entries[0]


def test_multiple_messages_appended_in_order(_isolated_log):
    conversation_log.log_message("user", "первое")
    conversation_log.log_message("assistant", "второе")
    conversation_log.log_message("tool", "третье")

    entries = _read_lines(_isolated_log)
    assert [(e["role"], e["content"]) for e in entries] == [
        ("user", "первое"), ("assistant", "второе"), ("tool", "третье"),
    ]


def test_fifo_eviction_when_over_cap(_isolated_log, monkeypatch):
    monkeypatch.setattr(conversation_log, "MAX_CONVERSATION_LOG_ENTRIES", 3)

    for i in range(5):
        conversation_log.log_message("user", f"сообщение {i}")

    entries = _read_lines(_isolated_log)
    assert len(entries) == 3
    assert [e["content"] for e in entries] == ["сообщение 2", "сообщение 3", "сообщение 4"]


def test_log_message_does_not_raise_on_unwritable_directory(tmp_path, monkeypatch):
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_text("x", encoding="utf-8")
    bad_path = blocking_file / "conversation_log.jsonl"
    monkeypatch.setattr(conversation_log, "_log_path", lambda: bad_path)

    conversation_log.log_message("user", "не должно упасть")  # не должно бросить исключение


def test_empty_content_is_logged_as_empty_string(_isolated_log):
    conversation_log.log_message("assistant", "")
    entries = _read_lines(_isolated_log)
    assert entries[0]["content"] == ""
