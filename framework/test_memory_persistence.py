"""Тесты для раунда 26 (C7) - долгосрочная память вне истории диалога.

Путь к файлу состояния (`_storage_path()`) подменяется на временный в
каждом тесте - реальный `data/memory.json` проекта никогда не трогается
(тот же паттерн, что и в test_timer_persistence.py, раунд 25).

Два блока: сами инструменты remember_fact/forget_fact/load_facts_summary
(без dialog_manager вообще), и интеграция - что dialog_manager реально
подмешивает факты в системный промпт КАЖДОГО запроса.
"""
import json
import sys

import pytest

sys.path.insert(0, ".")

import agents.tools.memory as memory_module  # noqa: E402
from agents.dialog_manager import SYSTEM_PROMPT  # noqa: E402
from test_abort_scenarios import make_chunk, make_manager  # noqa: E402
from test_cloud_local_tool_split import CapturingClient  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    storage_file = tmp_path / "memory.json"
    monkeypatch.setattr(memory_module, "_storage_path", lambda: storage_file)
    yield storage_file


# --- remember_fact() ---------------------------------------------------------


def test_remember_fact_persists_to_disk(_isolated_storage):
    result = memory_module.remember_fact("Артём работает над Джарвисом")
    assert "result" in result

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["text"] == "Артём работает над Джарвисом"
    assert "id" in data[0] and "created_at" in data[0]


def test_remember_fact_rejects_empty_text():
    assert "error" in memory_module.remember_fact("")
    assert "error" in memory_module.remember_fact("   ")


def test_remember_fact_rejects_too_long_text():
    result = memory_module.remember_fact("x" * (memory_module.MAX_FACT_LENGTH + 1))
    assert "error" in result


def test_remember_multiple_facts_all_stored(_isolated_storage):
    memory_module.remember_fact("Первый факт")
    memory_module.remember_fact("Второй факт")
    memory_module.remember_fact("Третий факт")

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert len(data) == 3
    assert {e["text"] for e in data} == {"Первый факт", "Второй факт", "Третий факт"}


def test_fifo_eviction_when_over_cap(_isolated_storage, monkeypatch):
    monkeypatch.setattr(memory_module, "MAX_REMEMBERED_FACTS", 3)

    for i in range(5):
        memory_module.remember_fact(f"факт {i}")

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert len(data) == 3
    # Самые СТАРЫЕ (0, 1) должны были вытесниться, остались последние три.
    texts = [e["text"] for e in data]
    assert texts == ["факт 2", "факт 3", "факт 4"]


# --- forget_fact() -----------------------------------------------------------


def test_forget_fact_removes_matching_fact(_isolated_storage):
    memory_module.remember_fact("Артём работает над Джарвисом")
    memory_module.remember_fact("Любимый язык - Python")

    result = memory_module.forget_fact("Джарвис")
    assert "result" in result

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["text"] == "Любимый язык - Python"


def test_forget_fact_low_confidence_does_not_remove(_isolated_storage):
    memory_module.remember_fact("Артём работает над Джарвисом")

    result = memory_module.forget_fact("совершенно не связанная фраза про погоду")
    assert "error" in result

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert len(data) == 1  # ничего не удалено


def test_forget_fact_empty_hint_is_error():
    assert "error" in memory_module.forget_fact("")


def test_forget_fact_on_empty_memory_is_error(_isolated_storage):
    assert not _isolated_storage.exists()
    result = memory_module.forget_fact("что угодно")
    assert "error" in result


# --- load_facts_summary() ----------------------------------------------------


def test_load_facts_summary_empty_when_no_facts(_isolated_storage):
    assert memory_module.load_facts_summary() == ""


def test_load_facts_summary_lists_all_facts(_isolated_storage):
    memory_module.remember_fact("Первый факт")
    memory_module.remember_fact("Второй факт")

    summary = memory_module.load_facts_summary()
    assert "Первый факт" in summary
    assert "Второй факт" in summary


def test_corrupted_storage_file_does_not_crash(_isolated_storage):
    _isolated_storage.write_text("не json {{{", encoding="utf-8")
    assert memory_module.load_facts_summary() == ""
    assert "error" not in memory_module.remember_fact("новый факт")  # можно писать поверх битого файла


def test_non_list_json_does_not_crash(_isolated_storage):
    _isolated_storage.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    assert memory_module.load_facts_summary() == ""


# --- Интеграция с dialog_manager (системный промпт) -------------------------


def test_system_prompt_unchanged_when_no_facts(_isolated_storage):
    cloud = CapturingClient([make_chunk(content="Привет!", finish_reason="stop")])
    mgr = make_manager(cloud)

    mgr.handle("Привет")

    system_message = cloud.captured_kwargs["messages"][0]
    assert system_message["role"] == "system"
    assert system_message["content"] == SYSTEM_PROMPT  # один в один, как до раунда 26


def test_system_prompt_includes_remembered_facts(_isolated_storage):
    memory_module.remember_fact("Артём работает над Джарвисом")

    cloud = CapturingClient([make_chunk(content="Привет!", finish_reason="stop")])
    mgr = make_manager(cloud)

    mgr.handle("Привет")

    system_message = cloud.captured_kwargs["messages"][0]
    assert "Артём работает над Джарвисом" in system_message["content"]
    # Базовый промпт по-прежнему на месте, факты - ДОПОЛНЕНИЕ, а не замена.
    assert SYSTEM_PROMPT in system_message["content"]
