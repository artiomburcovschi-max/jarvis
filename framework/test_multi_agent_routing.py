"""Тесты для раунда 29 (E3) - "мультиагент": рутина сначала локально,
тяжёлое - сразу в облако. Путь к config/config.yaml подменяется на
временный (см. test_jarvis_config.py) - реальный конфиг проекта не трогается.
"""
import sys

import jarvis_config
import pytest

sys.path.insert(0, ".")

from agents import llm_mode  # noqa: E402
from agents.dialog_manager import _is_routine_query  # noqa: E402
from test_abort_scenarios import make_chunk, FakeClient, make_manager  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(jarvis_config, "_project_root", lambda: tmp_path)
    jarvis_config.reset_cache_for_tests()
    llm_mode.reset_for_tests()
    yield
    llm_mode.reset_for_tests()


# --- _is_routine_query() -----------------------------------------------------


def test_short_query_is_routine():
    assert _is_routine_query("включи спотифай") is True


def test_long_query_is_not_routine():
    text = "расскажи подробно про историю развития искусственного интеллекта начиная с пятидесятых годов"
    assert _is_routine_query(text) is False


def test_exactly_at_default_threshold_is_routine():
    # По умолчанию 8 слов - ровно 8 всё ещё "рутина" (<=, не <).
    text = "раз два три четыре пять шесть семь восемь"
    assert len(text.split()) == 8
    assert _is_routine_query(text) is True


def test_one_word_over_default_threshold_is_not_routine():
    text = "раз два три четыре пять шесть семь восемь девять"
    assert len(text.split()) == 9
    assert _is_routine_query(text) is False


def test_threshold_is_configurable(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "multi_agent:\n  routine_max_words: 2\n", encoding="utf-8"
    )
    monkeypatch.delenv("ROUTINE_MAX_WORDS", raising=False)

    assert _is_routine_query("привет как дела") is False  # 3 слова > 2
    assert _is_routine_query("привет джарвис") is True     # 2 слова <= 2


# --- Реальный роутинг через DialogManager._create_stream() ------------------


def test_routine_query_goes_to_local_first_when_fallback_configured():
    cloud = FakeClient([[make_chunk(content="ОБЛАКО", finish_reason="stop")]])
    fallback = FakeClient([[make_chunk(content="ЛОКАЛЬНО", finish_reason="stop")]])
    mgr = make_manager(cloud, fallback_client=fallback)

    result = mgr.handle("включи спотифай")  # короткая фраза - рутина

    assert result == "ЛОКАЛЬНО"
    assert fallback.call_count == 1
    assert cloud.call_count == 0  # облако вообще не тронули


def test_heavy_query_still_goes_to_cloud_first():
    cloud = FakeClient([[make_chunk(content="ОБЛАКО", finish_reason="stop")]])
    fallback = FakeClient([[make_chunk(content="ЛОКАЛЬНО", finish_reason="stop")]])
    mgr = make_manager(cloud, fallback_client=fallback)

    heavy_text = "объясни подробно почему небо голубое а закат оранжевый и красный"
    result = mgr.handle(heavy_text)

    assert result == "ОБЛАКО"
    assert cloud.call_count == 1
    assert fallback.call_count == 0


def test_routine_query_without_fallback_goes_to_cloud_as_before():
    # Регрессия: если fallback не настроен вовсе - рутина или нет, не
    # имеет значения, поведение как до раунда 29 (единственный провайдер).
    cloud = FakeClient([[make_chunk(content="ОБЛАКО", finish_reason="stop")]])
    mgr = make_manager(cloud)  # fallback не передан

    result = mgr.handle("привет")

    assert result == "ОБЛАКО"
    assert cloud.call_count == 1


def test_routine_query_escalates_to_cloud_when_local_fails():
    import types

    class FailingFallback:
        def __init__(self):
            self.call_count = 0
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self._create)
            )

        def _create(self, **kwargs):
            self.call_count += 1
            raise ConnectionError("локальный сервер недоступен")

    cloud = FakeClient([[make_chunk(content="ОБЛАКО СПАСЛО", finish_reason="stop")]])
    fallback = FailingFallback()
    mgr = make_manager(cloud, fallback_client=fallback)

    result = mgr.handle("включи спотифай")  # рутина, но локалка недоступна

    assert result == "ОБЛАКО СПАСЛО"
    assert fallback.call_count == 1
    assert cloud.call_count == 1


def test_forced_offline_ignores_routine_heuristic_entirely():
    # Форсированный офлайн (B6) уже безусловно идёт в fallback - эвристика
    # E3 здесь просто не участвует ни для рутины, ни для тяжёлого запроса.
    llm_mode.set_forced_offline(True)
    cloud = FakeClient([[make_chunk(content="НЕ ДОЛЖНО ВЫЗВАТЬСЯ", finish_reason="stop")]])
    fallback = FakeClient([[make_chunk(content="ОФЛАЙН", finish_reason="stop")]])
    mgr = make_manager(cloud, fallback_client=fallback)

    heavy_text = "объясни подробно почему небо голубое а закат оранжевый и красный"
    result = mgr.handle(heavy_text)

    assert result == "ОФЛАЙН"
    assert cloud.call_count == 0


def test_local_only_tools_still_gated_correctly_when_routed_as_routine():
    # Раунд 24 (C5): используемый бэкенд (used_fallback) определяет, можно
    # ли исполнять LOCAL_ONLY_TOOLS - убеждаемся, что рутинный маршрут в
    # локалку тоже правильно помечается как used_fallback=True, а не
    # ломает эту защиту.
    cloud = FakeClient([[make_chunk(content="не должно понадобиться", finish_reason="stop")]])
    fallback = FakeClient([[make_chunk(content="ЛОКАЛЬНО", finish_reason="stop")]])
    mgr = make_manager(cloud, fallback_client=fallback)

    stream, used_fallback = mgr._create_stream(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "привет"}],
        "привет",
    )
    assert used_fallback is True
