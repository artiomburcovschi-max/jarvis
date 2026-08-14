"""Тесты для раунда 23 (B6) - локальный fallback-LLM без интернета.

Три независимых куска логики:
  - agents.llm_client.create_fallback_client() - опциональная конфигурация
    (None, если не настроено - никакого fallback, поведение как раньше);
  - agents.llm_mode - флаг форсированного офлайн-режима, переключаемый
    голосом через intent_router._match_llm_mode();
  - agents.dialog_manager.DialogManager._create_stream() - собственно
    решение "облако -> при неудаче локальный fallback -> при неудаче
    обоих сдаться с понятной ошибкой", плюс форсированный офлайн, который
    пропускает попытку облака вовсе.

Реального сетевого вызова ни к OpenRouter, ни к LM Studio здесь нет - всё
через FakeClient (см. test_abort_scenarios.py) и FailingClient (только в
этом файле, для симуляции "облако недоступно").
"""
import sys
import types

import jarvis_config
import pytest

sys.path.insert(0, ".")

from agents import llm_mode  # noqa: E402
from agents import intent_router  # noqa: E402
from agents.llm_client import create_fallback_client  # noqa: E402
from test_abort_scenarios import FakeClient, make_chunk, make_manager  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_llm_mode():
    # llm_mode - module-level состояние, переживает границу теста, если не
    # сбросить - один тест мог бы незаметно повлиять на следующий.
    llm_mode.reset_for_tests()
    yield
    llm_mode.reset_for_tests()


class FailingClient:
    """Мок openai-клиента, который ВСЕГДА бросает исключение при create() -
    имитирует недоступное облако (нет сети/таймаут/ошибка API)."""

    def __init__(self, message="сеть недоступна"):
        self.call_count = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )
        self._message = message

    def _create(self, **kwargs):
        self.call_count += 1
        raise ConnectionError(self._message)


# --- agents.llm_client.create_fallback_client() -----------------------------


def _point_project_root_at(monkeypatch, tmp_path):
    monkeypatch.setattr(jarvis_config, "_project_root", lambda: tmp_path)
    jarvis_config.reset_cache_for_tests()


def test_fallback_client_none_when_not_configured(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    assert create_fallback_client() is None


def test_fallback_client_none_when_only_url_set(tmp_path, monkeypatch):
    # И base_url, и model обязательны ВМЕСТЕ - половинчатая настройка
    # (например, опечатка) не должна тихо создавать клиента без модели.
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    assert create_fallback_client() is None


def test_fallback_client_created_when_both_set(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "gemma-12b")
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)

    result = create_fallback_client()
    assert result is not None
    client, model = result
    assert model == "gemma-12b"
    assert str(client.base_url).rstrip("/") == "http://127.0.0.1:1234/v1"


# --- agents.llm_mode + intent_router._match_llm_mode ------------------------


def test_llm_mode_defaults_to_online():
    assert llm_mode.is_forced_offline() is False


def test_voice_command_switches_to_offline_and_back():
    assert llm_mode.is_forced_offline() is False

    answer = intent_router.try_match("перейди в офлайн")
    assert answer is not None
    assert llm_mode.is_forced_offline() is True

    answer = intent_router.try_match("перейди в онлайн")
    assert answer is not None
    assert llm_mode.is_forced_offline() is False


def test_offline_word_variant_also_matches():
    # Второй распространённый вариант написания - "оффлайн" (двойное ф).
    intent_router.try_match("работай в оффлайн режиме")
    assert llm_mode.is_forced_offline() is True


def test_unrelated_phrase_does_not_touch_llm_mode():
    intent_router.try_match("сделай потише")
    assert llm_mode.is_forced_offline() is False


# --- DialogManager._create_stream() (через handle_streaming) ---------------
# Раунд 29 (E3): у трёх тестов ниже сообщение специально длиннее 8 слов -
# иначе оно попало бы под эвристику "рутина" и ушло бы в fallback ПЕРВЫМ
# ещё до того, как облако вообще тронули, что сломало бы саму суть этих
# тестов (проверить именно облако-первым поведение B6). Тесты самой
# эвристики - в test_multi_agent_routing.py.


def test_cloud_success_never_touches_fallback():
    cloud = FakeClient([[make_chunk(content="Привет!", finish_reason="stop")]])
    fallback = FakeClient([[make_chunk(content="ЛОКАЛЬНЫЙ ответ", finish_reason="stop")]])
    mgr = make_manager(cloud, fallback_client=fallback)

    result = mgr.handle("Привет, расскажи подробно что у тебя сегодня происходит в целом")

    assert result == "Привет!"
    assert cloud.call_count == 1
    assert fallback.call_count == 0


def test_cloud_failure_falls_back_to_local():
    cloud = FailingClient()
    fallback = FakeClient([[make_chunk(content="Отвечаю локально.", finish_reason="stop")]])
    mgr = make_manager(cloud, fallback_client=fallback)

    result = mgr.handle("Привет, расскажи подробно что у тебя сегодня происходит в целом")

    assert result == "Отвечаю локально."
    assert cloud.call_count == 1
    assert fallback.call_count == 1


def test_cloud_failure_without_fallback_gives_old_single_error():
    # Регрессия: если fallback не настроен - поведение НЕ должно измениться
    # относительно того, что было до раунда 23.
    cloud = FailingClient()
    mgr = make_manager(cloud)  # fallback_client не передан - как раньше

    result = mgr.handle("Привет")

    assert "интернет" in result or "ключ" in result
    assert "локальн" not in result.lower()


def test_both_cloud_and_fallback_fail_gives_combined_error():
    cloud = FailingClient("облако недоступно")
    fallback = FailingClient("локальный сервер тоже недоступен")
    mgr = make_manager(cloud, fallback_client=fallback)

    result = mgr.handle("Привет, расскажи подробно что у тебя сегодня происходит в целом")

    assert cloud.call_count == 1
    assert fallback.call_count == 1
    assert "облачн" in result.lower()
    assert "локальн" in result.lower()


def test_forced_offline_skips_cloud_entirely():
    llm_mode.set_forced_offline(True)
    cloud = FakeClient([[make_chunk(content="Не должно было вызваться", finish_reason="stop")]])
    fallback = FakeClient([[make_chunk(content="Офлайн-ответ.", finish_reason="stop")]])
    mgr = make_manager(cloud, fallback_client=fallback)

    result = mgr.handle("Привет")

    assert result == "Офлайн-ответ."
    assert cloud.call_count == 0  # ключевая проверка - облако вообще не тронули
    assert fallback.call_count == 1


def test_forced_offline_without_fallback_does_not_crash():
    llm_mode.set_forced_offline(True)
    cloud = FakeClient([[make_chunk(content="Не должно было вызваться", finish_reason="stop")]])
    mgr = make_manager(cloud)  # fallback не настроен

    result = mgr.handle("Привет")

    assert cloud.call_count == 0
    assert isinstance(result, str) and result  # понятная ошибка, не падение
