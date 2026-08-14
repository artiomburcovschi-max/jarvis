"""Тесты для раунда 24 (C5) - разделение облако/локально для "опасных"
AT-SPI-инструментов на уровне dialog_manager.py.

Два независимых слоя защиты, оба проверяются здесь:
  1. _create_stream() - облачный запрос получает УРЕЗАННЫЙ список
     инструментов (без LOCAL_ONLY_TOOLS), локальный/fallback - полный.
  2. _execute_tool_call(is_local=...) - защита "на всякий случай" ВТОРЫМ
     слоем: даже если бы облачная модель как-то вызвала local-only
     инструмент, выполнение отказывается происходить.
"""
import json
import sys
import types

sys.path.insert(0, ".")

from agents.tools import LOCAL_ONLY_TOOLS  # noqa: E402
from test_abort_scenarios import make_chunk, make_manager  # noqa: E402


class CapturingClient:
    """Как FakeClient, но ещё и запоминает kwargs, с которыми был вызван
    create() - нужно проверить, какие именно инструменты были в запросе."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.captured_kwargs = None
        self.call_count = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.call_count += 1
        self.captured_kwargs = kwargs
        return iter(self._chunks)


def test_cloud_request_excludes_local_only_tool_schemas():
    cloud = CapturingClient([make_chunk(content="Привет!", finish_reason="stop")])
    mgr = make_manager(cloud)

    mgr.handle("Привет")

    sent_tool_names = {t["function"]["name"] for t in cloud.captured_kwargs["tools"]}
    assert sent_tool_names.isdisjoint(LOCAL_ONLY_TOOLS)


def test_fallback_request_includes_local_only_tool_schemas():
    cloud = CapturingClient([])  # не должен вообще вызваться

    class FailingClient:
        def __init__(self):
            self.call_count = 0
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self._create)
            )

        def _create(self, **kwargs):
            self.call_count += 1
            raise ConnectionError("облако недоступно")

    cloud = FailingClient()
    fallback = CapturingClient([make_chunk(content="Локальный ответ.", finish_reason="stop")])
    mgr = make_manager(cloud, fallback_client=fallback)

    mgr.handle("Привет")

    assert fallback.captured_kwargs is not None
    sent_tool_names = {t["function"]["name"] for t in fallback.captured_kwargs["tools"]}
    assert LOCAL_ONLY_TOOLS.issubset(sent_tool_names)


def test_execute_tool_call_refuses_local_only_tool_when_not_local():
    mgr = make_manager(CapturingClient([]))
    mgr.pending_confirmation = None

    result_json = mgr._execute_tool_call(
        "atspi_click_element", json.dumps({"element_hint": "кнопка"}), is_local=False,
    )
    result = json.loads(result_json)

    assert "error" in result
    assert "локальн" in result["error"].lower() or "офлайн" in result["error"].lower()
    # Раз отказали ДО проверки DANGEROUS_TOOLS - подтверждение не должно
    # было запроситься вовсе.
    assert mgr.pending_confirmation is None


def test_execute_tool_call_allows_local_only_tool_when_local():
    mgr = make_manager(CapturingClient([]))
    mgr.pending_confirmation = None

    result_json = mgr._execute_tool_call(
        "atspi_click_element", json.dumps({"element_hint": "кнопка"}), is_local=True,
    )
    result = json.loads(result_json)

    # Инструмент ТАКЖЕ в DANGEROUS_TOOLS (C3) - значит правильный исход
    # здесь НЕ прямое выполнение, а запрос подтверждения (это ожидаемо и
    # проверяет, что local-only проверка не блокирует легитимный локальный
    # вызов, а просто пропускает его дальше по обычному пути C3).
    assert result.get("status") == "requires_user_confirmation"
    assert mgr.pending_confirmation is not None
    assert mgr.pending_confirmation["tool_name"] == "atspi_click_element"


def test_non_local_only_tool_unaffected_by_is_local_flag():
    # Обычный (не local-only, не dangerous) инструмент должен продолжать
    # работать одинаково независимо от is_local - регрессия.
    mgr = make_manager(CapturingClient([]))

    result_json_cloud = mgr._execute_tool_call("get_current_datetime", "{}", is_local=False)
    result_json_local = mgr._execute_tool_call("get_current_datetime", "{}", is_local=True)

    # Оба должны либо успешно выполниться, либо одинаково пожаловаться на
    # неизвестный инструмент - но НЕ на "доступен только локально".
    assert "доступен только" not in result_json_cloud
    assert "доступен только" not in result_json_local
