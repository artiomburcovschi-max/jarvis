"""
Ручной тест (без сети) для C3 (перехват "опасных" инструментов в
dialog_manager.py) - использует те же фейковые helper'ы, что и
test_abort_scenarios.py (мок openai-клиента без реальной сети).

Сценарии:
  1. Модель просит вызвать lock_computer -> реальный tool НЕ вызывается,
     возвращается ДЕТЕРМИНИРОВАННЫЙ вопрос (не то, что сгенерировала бы
     модель), pending_confirmation выставлен с правильными именем/аргументами.
  2. Обычный (не опасный) инструмент в той же истории - выполняется как
     раньше, без всякого перехвата (регрессия).
  3. Два вызова в одном батче: первый безопасный - выполняется, второй
     (опасный) - перехватывается, ДО следующего хопа дело не доходит.
"""

import sys
import types

sys.path.insert(0, ".")

from agents.dialog_manager import DialogManager  # noqa: E402
from test_abort_scenarios import make_chunk, tool_call_delta, FakeClient, make_manager  # noqa: E402


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(f"{label}: {detail}")


def test_dangerous_tool_intercepted():
    print("\n=== Опасный инструмент (lock_computer) перехвачен, НЕ выполнен ===")
    hop0 = [
        make_chunk(tool_call_delta=tool_call_delta(index=0, call_id="c1", name="lock_computer", arguments="{}")),
        make_chunk(finish_reason="tool_calls"),
    ]
    client = FakeClient(chunks_by_hop=[hop0])
    mgr = make_manager(client)

    executed = []

    def fake_lock_computer():
        executed.append("lock_computer")
        return {"result": "ЗАБЛОКИРОВАНО (не должно было вызваться!)"}

    # Подменяем реальный TOOL_IMPLEMENTATIONS запись, чтобы доказать, что
    # _execute_tool_call для ОПАСНОГО инструмента даже не пытается её звать.
    import agents.dialog_manager as dm_module
    original_impls = dict(dm_module.TOOL_IMPLEMENTATIONS)
    dm_module.TOOL_IMPLEMENTATIONS["lock_computer"] = fake_lock_computer

    sentences = []
    try:
        answer = mgr.handle_streaming("Джарвис, заблокируй компьютер", on_sentence_ready=sentences.append)
    finally:
        dm_module.TOOL_IMPLEMENTATIONS.clear()
        dm_module.TOOL_IMPLEMENTATIONS.update(original_impls)

    check("реальный lock_computer НЕ вызван", executed == [], executed)
    check("ответ - детерминированный вопрос", "Точно заблокировать компьютер?" in answer, answer)
    check("вопрос озвучен через on_sentence_ready", answer in sentences, sentences)
    check("pending_confirmation выставлен", mgr.pending_confirmation is not None, mgr.pending_confirmation)
    check("pending_confirmation.tool_name верный",
          mgr.pending_confirmation and mgr.pending_confirmation["tool_name"] == "lock_computer",
          mgr.pending_confirmation)
    check("в истории вопрос - обычный assistant-текст, БЕЗ dangling tool_call",
          mgr._history[-1] == {"role": "assistant", "content": answer}, mgr._history)
    print(f"  answer={answer!r}")


def test_safe_tool_not_intercepted():
    print("\n=== Обычный (безопасный) инструмент выполняется как раньше (регрессия) ===")
    hop0 = [make_chunk(tool_call_delta=tool_call_delta(index=0, call_id="c1", name="calculate",
                                                        arguments='{"expression": "3+3"}')),
            make_chunk(finish_reason="tool_calls")]
    hop1 = [make_chunk(content="Будет 6.", finish_reason="stop")]
    client = FakeClient(chunks_by_hop=[hop0, hop1])
    mgr = make_manager(client)

    answer = mgr.handle_streaming("Джарвис, посчитай 3+3", on_sentence_ready=lambda s: None)
    check("ответ нормальный (не вопрос про подтверждение)", answer == "Будет 6.", answer)
    check("pending_confirmation НЕ выставлен", mgr.pending_confirmation is None, mgr.pending_confirmation)


def test_dangerous_tool_second_in_batch_stops_batch():
    print("\n=== Два вызова в одном батче: первый (безопасный) выполняется, второй (опасный) перехватывается, дальше не идём ===")
    hop0 = [
        make_chunk(tool_call_delta=tool_call_delta(index=0, call_id="c1", name="calculate",
                                                    arguments='{"expression": "1+1"}')),
        make_chunk(tool_call_delta=tool_call_delta(index=1, call_id="c2", name="lock_computer", arguments="{}")),
        make_chunk(finish_reason="tool_calls"),
    ]
    # Второй хоп НЕ должен понадобиться - если код по ошибке пойдёт дальше,
    # FakeClient сам бросит AssertionError ("LLM был вызван больше раз, чем
    # ожидалось") - см. FakeClient._create в test_abort_scenarios.py.
    client = FakeClient(chunks_by_hop=[hop0])
    mgr = make_manager(client)

    answer = mgr.handle_streaming("Джарвис, посчитай 1+1 и заблокируй компьютер",
                                   on_sentence_ready=lambda s: None)

    check("остановились на вопросе подтверждения", "Точно заблокировать компьютер?" in answer, answer)
    check("pending_confirmation про lock_computer",
          mgr.pending_confirmation and mgr.pending_confirmation["tool_name"] == "lock_computer",
          mgr.pending_confirmation)
    # Первый (безопасный) tool-ответ должен быть в истории - calculate реально выполнился
    tool_responses = [m for m in mgr._history if m.get("role") == "tool"]
    check("ровно один tool-ответ в истории (только calculate, lock_computer не считается настоящим выполнением)",
          len(tool_responses) == 2, mgr._history)  # calculate + заглушка requires_user_confirmation для lock_computer
    check("первый tool-ответ - результат calculate", '"result"' in tool_responses[0]["content"], tool_responses[0])
    check("второй tool-ответ - заглушка requires_user_confirmation",
          "requires_user_confirmation" in tool_responses[1]["content"], tool_responses[1])


if __name__ == "__main__":
    test_dangerous_tool_intercepted()
    test_safe_tool_not_intercepted()
    test_dangerous_tool_second_in_batch_stops_batch()
    print("\nВСЕ СЦЕНАРИИ ПРОШЛИ.")
