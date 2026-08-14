"""
Ручной тест (без сети) для C4 (audit-лог tool-call'ов) - проверяет, что
on_tool_call(name, args, result) в dialog_manager.handle_streaming()
вызывается РОВНО когда должен, с правильными данными.
"""

import sys
sys.path.insert(0, ".")

from test_abort_scenarios import make_chunk, tool_call_delta, FakeClient, make_manager  # noqa: E402


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(f"{label}: {detail}")


def test_on_tool_call_fires_for_normal_tool():
    print("\n=== on_tool_call вызывается для обычного инструмента с правильными данными ===")
    hop0 = [make_chunk(tool_call_delta=tool_call_delta(index=0, call_id="c1", name="calculate",
                                                        arguments='{"expression": "2+2"}')),
            make_chunk(finish_reason="tool_calls")]
    hop1 = [make_chunk(content="Будет 4.", finish_reason="stop")]
    client = FakeClient(chunks_by_hop=[hop0, hop1])
    mgr = make_manager(client)

    calls = []
    mgr.handle_streaming(
        "Джарвис, посчитай 2+2",
        on_sentence_ready=lambda s: None,
        on_tool_call=lambda name, args, result: calls.append((name, args, result)),
    )

    check("колбэк вызван ровно 1 раз", len(calls) == 1, calls)
    name, args, result = calls[0]
    check("имя инструмента верное", name == "calculate", name)
    check("аргументы разобраны верно", args == {"expression": "2+2"}, args)
    check("результат содержит 'result'", "result" in result, result)


def test_on_tool_call_fires_for_dangerous_tool_with_confirmation_status():
    print("\n=== on_tool_call вызывается и для перехваченного опасного вызова ===")
    hop0 = [make_chunk(tool_call_delta=tool_call_delta(index=0, call_id="c1", name="lock_computer", arguments="{}")),
            make_chunk(finish_reason="tool_calls")]
    client = FakeClient(chunks_by_hop=[hop0])
    mgr = make_manager(client)

    calls = []
    mgr.handle_streaming(
        "Джарвис, заблокируй компьютер",
        on_sentence_ready=lambda s: None,
        on_tool_call=lambda name, args, result: calls.append((name, args, result)),
    )

    check("колбэк вызван ровно 1 раз (даже для перехваченного)", len(calls) == 1, calls)
    name, args, result = calls[0]
    check("имя инструмента верное", name == "lock_computer", name)
    check("результат сигнализирует 'требует подтверждения', а не тихо выполнен",
          result.get("status") == "requires_user_confirmation", result)


def test_no_callback_is_safe_default():
    print("\n=== Без on_tool_call (по умолчанию) - ничего не падает ===")
    hop0 = [make_chunk(content="Привет!", finish_reason="stop")]
    client = FakeClient(chunks_by_hop=[hop0])
    mgr = make_manager(client)
    # Не передаём on_tool_call вообще - должен использоваться no-op по умолчанию.
    answer = mgr.handle_streaming("Джарвис, привет", on_sentence_ready=lambda s: None)
    check("отработало без исключений", answer == "Привет!", answer)


if __name__ == "__main__":
    test_on_tool_call_fires_for_normal_tool()
    test_on_tool_call_fires_for_dangerous_tool_with_confirmation_status()
    test_no_callback_is_safe_default()
    print("\nВСЕ СЦЕНАРИИ ПРОШЛИ.")
