"""Тесты для раунда 27 (E1) - словарь исправлений типичных ошибок STT.

Путь к конфигу подменяется на временный в каждом тесте - реальный
config/stt_corrections.yaml проекта не трогается.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, ".")

import stt_corrections  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    config_file = tmp_path / "stt_corrections.yaml"
    monkeypatch.setattr(stt_corrections, "_config_path", lambda: config_file)
    stt_corrections.reset_cache_for_tests()
    yield config_file
    stt_corrections.reset_cache_for_tests()


def _write_config(path, content):
    path.write_text(content, encoding="utf-8")


def test_replaces_known_word(_isolated_config):
    _write_config(_isolated_config, "темножить: умножить\n")
    result = stt_corrections.apply_corrections("реши сколько будет темножить пять на пять")
    assert result == "реши сколько будет умножить пять на пять"


def test_case_insensitive_match(_isolated_config):
    _write_config(_isolated_config, "темножить: умножить\n")
    result = stt_corrections.apply_corrections("Темножить два на два")
    assert "умножить" in result.lower()
    assert "темножить" not in result.lower()


def test_word_boundary_does_not_touch_longer_words(_isolated_config):
    # "темножить" НЕ должно тронуть слово, где оно встречается как часть
    # более длинного (искусственный пример, но принцип важен - замена по
    # границе слова, не голая подстрока).
    _write_config(_isolated_config, "темно: TESTMARK\n")
    result = stt_corrections.apply_corrections("темножить пять на пять")
    assert "TESTMARK" not in result
    assert result == "темножить пять на пять"


def test_empty_text_returns_empty(_isolated_config):
    _write_config(_isolated_config, "темножить: умножить\n")
    assert stt_corrections.apply_corrections("") == ""


def test_no_config_file_returns_text_unchanged(_isolated_config):
    assert not _isolated_config.exists()
    result = stt_corrections.apply_corrections("темножить пять на пять")
    assert result == "темножить пять на пять"


def test_corrupted_yaml_does_not_crash(_isolated_config):
    _write_config(_isolated_config, "не yaml вообще: [[[не закрыто")
    result = stt_corrections.apply_corrections("любой текст")
    assert result == "любой текст"


def test_non_dict_yaml_does_not_crash(_isolated_config):
    _write_config(_isolated_config, "- просто\n- список\n")
    result = stt_corrections.apply_corrections("любой текст")
    assert result == "любой текст"


def test_multiple_rules_applied_independently(_isolated_config):
    _write_config(_isolated_config, "темножить: умножить\nсложыть: сложить\n")
    result = stt_corrections.apply_corrections("темножить и сложыть числа")
    assert result == "умножить и сложить числа"


def test_hot_reload_picks_up_file_changes(_isolated_config):
    _write_config(_isolated_config, "темножить: умножить\n")
    first = stt_corrections.apply_corrections("темножить два")
    assert first == "умножить два"

    # Меняем файл и явно двигаем mtime вперёд (на файловых системах с
    # грубой гранулярностью mtime быстрые последовательные записи в тесте
    # иначе могут не отличаться по времени модификации).
    _write_config(_isolated_config, "темножить: РАЗДЕЛИТЬ\n")
    new_mtime = time.time() + 5
    os.utime(_isolated_config, (new_mtime, new_mtime))

    second = stt_corrections.apply_corrections("темножить два")
    assert second == "РАЗДЕЛИТЬ два"


def test_non_string_scalar_values_are_coerced(_isolated_config):
    # На случай, если кто-то в YAML напишет значение без кавычек, которое
    # парсится не как строка (например, число) - не должно падать.
    _write_config(_isolated_config, "пять: 5\n")
    result = stt_corrections.apply_corrections("это пять")
    assert result == "это 5"
