"""llm_mode.py - раунд 23 (B6): ручной оффлайн-режим.

Голосовая команда "перейди в офлайн" (intent_router) переключает этот
флаг, dialog_manager читает его перед КАЖДЫМ запросом - если включён,
облачный провайдер вообще не дёргается (незачем ждать таймаут заведомо
не нужного запроса), сразу идёт локальный fallback.

Вынесено в отдельный модуль БЕЗ зависимостей от intent_router или
dialog_manager СПЕЦИАЛЬНО: intent_router по дизайну (см. его докстринг)
полностью независим от dialog_manager/LLM - и должен остаться таким же
после этого раунда. Общее состояние живёт здесь, оба модуля импортируют
только его.

Простой module-level флаг с блокировкой, а не что-то сложнее - процесс
один (server.py), потоков немного (transcribe_worker), поэтому polling
через lock более чем достаточен, никакой pub/sub-инфраструктуры не нужно.
"""
import threading

_lock = threading.Lock()
_forced_offline = False


def set_forced_offline(value: bool) -> None:
    global _forced_offline
    with _lock:
        _forced_offline = value


def is_forced_offline() -> bool:
    with _lock:
        return _forced_offline


def reset_for_tests() -> None:
    """Только для тестов - сбрасывает флаг между тест-кейсами, чтобы один
    тест не влиял на следующий (module-level состояние иначе пережило бы
    границу теста)."""
    set_forced_offline(False)
