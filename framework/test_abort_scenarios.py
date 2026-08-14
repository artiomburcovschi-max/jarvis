"""
Ручной тест (без pytest, без сети) на три сценария отмены (barge-in) в
DialogManager.handle_streaming(), которые были добавлены/починены:

  1. Отмена ДО первого хопа (should_abort() уже True при первом заходе в
     цикл) -> запрос к LLM вообще не создаётся, история откатывается.
  2. Отмена ПОСЕРЕДИНЕ стрима, СРАЗУ ПОСЛЕ того как tool_call полностью
     накопился, но мы его ещё не выполнили -> assistant-сообщение с
     "подвешенным" tool_call НЕ попадает в историю (иначе следующий
     реальный запрос к LLM сломался бы протокольной ошибкой). Также
     проверяем, что следующий обычный запрос после отмены отрабатывает
     штатно - то есть история не осталась испорченной.
  3. Отмена ПЕРЕД выполнением ВТОРОГО инструмента из двух запрошенных ->
     первый инструмент уже выполнился (сайд-эффект случился), но история
     всё равно откатывается ЦЕЛИКОМ, и второй инструмент не вызывается
     вовсе (его сайд-эффект НЕ должен произойти).

should_abort() в реальном коде вызывается в строго определённых точках
(см. dialog_manager.py): один раз в начале каждого хопа, один раз после
каждого полученного чанка стрима, один раз перед каждым вызовом
инструмента. Тест использует эту точную последовательность (по счётчику
вызовов), чтобы имитировать "перебивание ровно в нужный момент" без
реальных времянок/сна.
"""

import sys
import types

sys.path.insert(0, ".")

from agents.dialog_manager import DialogManager  # noqa: E402


def make_chunk(content=None, tool_call_delta=None, finish_reason=None):
    delta = types.SimpleNamespace(content=content, tool_calls=None)
    if tool_call_delta is not None:
        delta.tool_calls = [tool_call_delta]
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice])


def tool_call_delta(index=0, call_id=None, name=None, arguments=None):
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(index=index, id=call_id, function=fn)


class FakeClient:
    """Мок openai-клиента: .chat.completions.create(...) -> поток чанков.

    chunks_by_hop[i] - чанки, которые отдаются на i-й по счёту вызов
    create() (i-й хоп цикла в handle_streaming)."""

    def __init__(self, chunks_by_hop):
        self._chunks_by_hop = list(chunks_by_hop)
        self._call_count = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        idx = self._call_count
        self._call_count += 1
        if idx >= len(self._chunks_by_hop):
            raise AssertionError("LLM был вызван больше раз, чем ожидалось тестом")
        return iter(self._chunks_by_hop[idx])

    @property
    def call_count(self):
        return self._call_count


def make_manager(fake_client, fallback_client=None, fallback_model="fake/fallback-model"):
    mgr = DialogManager.__new__(DialogManager)  # обходим __init__ (не нужен реальный API-ключ)
    mgr._client = fake_client
    mgr._model = "fake/model"
    # Раунд 23 (B6): по умолчанию fallback не настроен (None) - ровно как
    # было ДО этого раунда для всех уже существующих тестов, которые
    # зовут make_manager(client) без второго аргумента.
    mgr._fallback_client = fallback_client
    mgr._fallback_model = fallback_model if fallback_client is not None else None
    mgr._history = []
    mgr._history_summary = ""
    return mgr


def counting_abort(trigger_after: int):
    """Возвращает should_abort(), который начинает отвечать True начиная
    с (trigger_after + 1)-го вызова - то есть первые `trigger_after` вызовов
    честно отвечают False."""
    state = {"n": 0}

    def _should_abort():
        state["n"] += 1
        return state["n"] > trigger_after

    return _should_abort


def scenario_1_abort_before_first_hop():
    print("\n=== Сценарий 1: отмена ДО первого хопа ===")
    client = FakeClient(chunks_by_hop=[])  # ни разу не должен вызваться
    mgr = make_manager(client)

    sentences = []
    answer = mgr.handle_streaming(
        "Джарвис, привет",
        on_sentence_ready=sentences.append,
        should_abort=lambda: True,  # уже отменено с самого начала
    )

    assert client.call_count == 0, "LLM НЕ должен был вызываться, а был вызван"
    assert mgr._history == [], f"История должна быть пустой (полный откат), а там: {mgr._history}"
    assert sentences == [], f"Не должно быть озвученных предложений, а есть: {sentences}"
    print(f"  answer={answer!r}, history={mgr._history}, sentences={sentences}")
    print("  OK: запрос к LLM не отправлен, история чистая.")


def scenario_2_abort_mid_stream_with_pending_tool_call():
    print("\n=== Сценарий 2: отмена сразу после накопления tool_call, до его выполнения ===")
    hop0_chunks = [
        make_chunk(content="Секунду, "),
        make_chunk(tool_call_delta=tool_call_delta(index=0, call_id="call_1", name="calculate", arguments="{")),
        make_chunk(tool_call_delta=tool_call_delta(index=0, arguments='"expression": "2+2"}')),
    ]
    client = FakeClient(chunks_by_hop=[hop0_chunks])
    mgr = make_manager(client)

    # Последовательность вызовов should_abort() внутри handle_streaming для
    # этого случая: #1 - верх хопа (до запроса), #2/#3/#4 - после каждого из
    # 3 чанков. Хотим False на #1..#3 (ещё копим tool_call) и True на #4
    # (сразу как только последний чанк с аргументами обработан).
    should_abort = counting_abort(trigger_after=3)

    sentences = []
    answer = mgr.handle_streaming(
        "Джарвис, посчитай 2+2",
        on_sentence_ready=sentences.append,
        should_abort=should_abort,
    )

    assert mgr._history == [], (
        f"История должна быть полностью откачена (никакого dangling tool_call), "
        f"а там: {mgr._history}"
    )
    print(f"  answer={answer!r}, history={mgr._history}, sentences={sentences}")
    print("  OK: assistant-сообщение с недовыполненным tool_call НЕ попало в историю.")

    # Дополнительная проверка "протокол не сломан": следующий обычный вызов
    # (без отмены) должен отработать нормально, как будто прерванного хода
    # никогда не было в истории.
    hop0_chunks_2 = [make_chunk(content="Привет!", finish_reason="stop")]
    client2 = FakeClient(chunks_by_hop=[hop0_chunks_2])
    mgr._client = client2
    sentences2 = []
    answer2 = mgr.handle_streaming("Джарвис, привет", on_sentence_ready=sentences2.append)
    assert answer2 == "Привет!", f"Ожидался чистый ответ, получено: {answer2!r}"
    assert len(mgr._history) == 2, f"Ожидалось user+assistant, а в истории: {mgr._history}"
    print("  OK: следующий обычный запрос после отмены отработал штатно (протокол цел).")


def scenario_3_abort_before_second_tool_call():
    print("\n=== Сценарий 3: отмена перед вторым из двух инструментов ===")
    hop0_chunks = [
        make_chunk(tool_call_delta=tool_call_delta(index=0, call_id="call_a", name="tool_a", arguments="{}")),
        make_chunk(tool_call_delta=tool_call_delta(index=1, call_id="call_b", name="tool_b", arguments="{}")),
        make_chunk(finish_reason="tool_calls"),
    ]
    client = FakeClient(chunks_by_hop=[hop0_chunks])
    mgr = make_manager(client)

    executed = []

    def fake_execute_tool_call(name, arguments):
        executed.append(name)
        if name == "tool_a":
            return '{"ok": true}'
        raise AssertionError("tool_b НЕ должен был вызваться - отмена должна была случиться раньше")

    mgr._execute_tool_call = fake_execute_tool_call

    # Последовательность вызовов: #1 верх хопа, #2/#3/#4 после каждого из
    # 3 чанков стрима (весь стрим должен дочитаться БЕЗ отмены, чтобы оба
    # tool_calls успели накопиться), #5 - перед вызовом tool_a, #6 - перед
    # вызовом tool_b. Хотим False на #1..#5, True на #6.
    should_abort = counting_abort(trigger_after=5)

    sentences = []
    answer = mgr.handle_streaming(
        "Джарвис, сделай a и b",
        on_sentence_ready=sentences.append,
        should_abort=should_abort,
    )

    assert executed == ["tool_a"], f"Ожидался ровно один выполненный инструмент, а было: {executed}"
    assert mgr._history == [], f"История должна быть откачена целиком, а там: {mgr._history}"
    print(f"  executed={executed}, answer={answer!r}, history={mgr._history}")
    print("  OK: tool_b не вызван, история откачена целиком несмотря на уже случившийся сайд-эффект tool_a.")


if __name__ == "__main__":
    scenario_1_abort_before_first_hop()
    scenario_2_abort_mid_stream_with_pending_tool_call()
    scenario_3_abort_before_second_tool_call()
    print("\nВСЕ СЦЕНАРИИ ПРОШЛИ.")
