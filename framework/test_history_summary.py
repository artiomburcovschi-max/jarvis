"""Тесты для раунда 28 (E2) - сжатие выпадающих реплик истории в резюме
(dialog_manager._update_summary/_pick_summary_client/_trim_history) и
запись полного транскрипта в conversation_log.py через
DialogManager._append_history.
"""
import sys
import types

import pytest

sys.path.insert(0, ".")

import conversation_log  # noqa: E402
import gap_log  # noqa: E402
from agents import llm_mode  # noqa: E402
from agents.dialog_manager import _build_system_prompt, HISTORY_MAX_TURNS, SYSTEM_PROMPT  # noqa: E402
from test_abort_scenarios import make_chunk, make_manager  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(conversation_log, "_log_path", lambda: tmp_path / "conversation_log.jsonl")
    monkeypatch.setattr(gap_log, "_log_path", lambda: tmp_path / "gaps.jsonl")
    llm_mode.reset_for_tests()
    yield
    llm_mode.reset_for_tests()


class FakeSummaryOnlyClient:
    """Мок openai-клиента для НЕстримингового вызова, который использует
    _update_summary() - другая форма ответа, чем у стримингового FakeClient
    (response.choices[0].message.content, а не итератор чанков)."""

    def __init__(self, summary_text=None, raise_error=None):
        self.summary_text = summary_text
        self.raise_error = raise_error
        self.call_count = 0
        self.last_kwargs = None
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        if self.raise_error:
            raise self.raise_error
        message = types.SimpleNamespace(content=self.summary_text)
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice])


class DualModeFakeClient:
    """Отвечает и на стриминговые вызовы (обычный ответ пользователю), и
    на обычные (summary-запрос _update_summary) - нужно для сквозного
    теста, где в одном прогоне участвуют оба вида вызова."""

    def __init__(self, reply_text="Ответ.", summary_text="Резюме прошлых реплик."):
        self.reply_text = reply_text
        self.summary_text = summary_text
        self.stream_call_count = 0
        self.summary_call_count = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        if kwargs.get("stream"):
            self.stream_call_count += 1
            return iter([make_chunk(content=self.reply_text, finish_reason="stop")])
        self.summary_call_count += 1
        message = types.SimpleNamespace(content=self.summary_text)
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice])


# --- _build_system_prompt() --------------------------------------------------


def test_build_system_prompt_unchanged_with_nothing_extra():
    assert _build_system_prompt("") == SYSTEM_PROMPT


def test_build_system_prompt_includes_history_summary_when_present():
    result = _build_system_prompt("Ранее обсуждали проект Джарвис.")
    assert SYSTEM_PROMPT in result
    assert "Ранее обсуждали проект Джарвис." in result


# --- _update_summary() / _pick_summary_client() -----------------------------


def test_update_summary_calls_llm_and_stores_result():
    client = FakeSummaryOnlyClient(summary_text="Обсуждали проект Джарвис.")
    mgr = make_manager(client)

    mgr._update_summary([{"role": "user", "content": "расскажи про Джарвис"}])

    assert client.call_count == 1
    assert mgr._history_summary == "Обсуждали проект Джарвис."


def test_update_summary_includes_prior_summary_in_prompt():
    client = FakeSummaryOnlyClient(summary_text="Новое объединённое резюме.")
    mgr = make_manager(client)
    mgr._history_summary = "Старое резюме про Python."

    mgr._update_summary([{"role": "user", "content": "новая реплика"}])

    sent_prompt = client.last_kwargs["messages"][0]["content"]
    assert "Старое резюме про Python." in sent_prompt
    assert mgr._history_summary == "Новое объединённое резюме."


def test_update_summary_failure_keeps_old_summary_and_logs_gap():
    client = FakeSummaryOnlyClient(raise_error=ConnectionError("сеть упала"))
    mgr = make_manager(client)
    mgr._history_summary = "Резюме, которое не должно потеряться."

    mgr._update_summary([{"role": "user", "content": "что-то"}])

    assert mgr._history_summary == "Резюме, которое не должно потеряться."

    gaps = gap_log.read_recent_gaps()
    assert any(g["kind"] == "summary_failed" for g in gaps)


def test_update_summary_handles_tool_call_only_messages_gracefully():
    # Реплика без текстового content (чистый tool_call) не должна ронять
    # сборку текста для запроса резюме.
    client = FakeSummaryOnlyClient(summary_text="Резюме.")
    mgr = make_manager(client)

    mgr._update_summary([{"role": "assistant", "content": None}])

    assert client.call_count == 1
    assert mgr._history_summary == "Резюме."


def test_pick_summary_client_uses_cloud_by_default():
    cloud = FakeSummaryOnlyClient()
    mgr = make_manager(cloud)

    client, model = mgr._pick_summary_client()

    assert client is cloud


def test_pick_summary_client_uses_fallback_when_forced_offline():
    cloud = FakeSummaryOnlyClient()
    fallback = FakeSummaryOnlyClient()
    mgr = make_manager(cloud, fallback_client=fallback)
    llm_mode.set_forced_offline(True)

    client, model = mgr._pick_summary_client()

    assert client is fallback


def test_pick_summary_client_returns_none_when_forced_offline_without_fallback():
    cloud = FakeSummaryOnlyClient()
    mgr = make_manager(cloud)
    llm_mode.set_forced_offline(True)

    client, model = mgr._pick_summary_client()

    assert client is None


# --- _trim_history() - реально сжимает выпадающие реплики -------------------


def test_trim_history_summarizes_dropped_turns():
    client = FakeSummaryOnlyClient(summary_text="Сжатое резюме старых реплик.")
    mgr = make_manager(client)

    # Заполняем историю "вручную" - больше, чем HISTORY_MAX_TURNS ходов.
    for i in range(HISTORY_MAX_TURNS + 3):
        mgr._history.append({"role": "user", "content": f"вопрос {i}"})
        mgr._history.append({"role": "assistant", "content": f"ответ {i}"})

    mgr._trim_history()

    assert len(mgr._history) <= HISTORY_MAX_TURNS * 2
    assert mgr._history_summary == "Сжатое резюме старых реплик."
    assert client.call_count >= 1


def test_trim_history_within_limit_does_not_call_summary():
    client = FakeSummaryOnlyClient(summary_text="Не должно вызваться.")
    mgr = make_manager(client)

    mgr._history.append({"role": "user", "content": "один вопрос"})
    mgr._history.append({"role": "assistant", "content": "один ответ"})

    mgr._trim_history()

    assert client.call_count == 0
    assert mgr._history_summary == ""


# --- Сквозной прогон через handle() -----------------------------------------


def test_end_to_end_summary_appears_after_many_turns():
    client = DualModeFakeClient(reply_text="Ответ.", summary_text="Резюме прошлого.")
    mgr = make_manager(client)

    for i in range(HISTORY_MAX_TURNS + 2):
        mgr.handle(f"вопрос номер {i}")

    assert len(mgr._history) <= HISTORY_MAX_TURNS * 2
    assert mgr._history_summary == "Резюме прошлого."
    assert client.summary_call_count >= 1

    # И резюме реально долетает до системного промпта следующего запроса.
    mgr.handle("ещё один вопрос")
    assert client.stream_call_count >= HISTORY_MAX_TURNS + 3


# --- conversation_log - полный транскрипт пишется на диск -------------------


def test_history_messages_are_logged_to_conversation_log(tmp_path, monkeypatch):
    log_path = tmp_path / "conv.jsonl"
    monkeypatch.setattr(conversation_log, "_log_path", lambda: log_path)

    client = DualModeFakeClient(reply_text="Привет!")
    mgr = make_manager(client)

    mgr.handle("Привет, Джарвис")

    assert log_path.exists()
    import json
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    roles_and_content = [(e["role"], e["content"]) for e in lines]
    assert ("user", "Привет, Джарвис") in roles_and_content
    assert ("assistant", "Привет!") in roles_and_content
