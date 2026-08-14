"""
Ручной тест (без сети, без реальных pactl/playerctl/subprocess) для
intent_router.try_match(). Реальные функции инструментов подменяются
фейками с логом вызовов - так же, как в test_abort_scenarios.py для
dialog_manager - чтобы проверить именно ЛОГИКУ распознавания, а не
работоспособность системных утилит в песочнице.

Проверяем два класса сценариев:
  1. Позитивные - команда распознана, вызван РОВНО ожидаемый инструмент с
     ожидаемыми аргументами.
  2. Негативные - router должен вернуть None (то есть "не уверен, отдаю
     LLM") и НЕ вызвать вообще никакой инструмент. Это не менее важно, чем
     позитивные случаи: router, который решает лишнее, опаснее router'а,
     который иногда лишний раз отдаёт фразу LLM.
"""

import sys

sys.path.insert(0, ".")

import agents.intent_router as intent_router  # noqa: E402


class Recorder:
    """Общий helper: подменяет функцию в модуле intent_router на фейк,
    который просто запоминает аргументы вызова и возвращает заданный
    результат - как это делают настоящие инструменты (dict с "result"/"error")."""

    def __init__(self, monkeypatch_target: str, fake_result: dict):
        self.calls = []
        self.target = monkeypatch_target
        self.fake_result = fake_result
        self._original = getattr(intent_router, monkeypatch_target)

    def __enter__(self):
        def fake(*args, **kwargs):
            self.calls.append((args, kwargs))
            return self.fake_result

        setattr(intent_router, self.target, fake)
        return self

    def __exit__(self, *exc):
        setattr(intent_router, self.target, self._original)


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(f"{label}: {detail}")


def test_volume_set_percent():
    print("\n=== Громкость: 'поставь громкость на 40 процентов' ===")
    with Recorder("set_volume", {"result": "Громкость установлена на 40%."}) as rec:
        answer = intent_router.try_match("поставь громкость на 40 процентов")
    check("распознано и выполнено", answer == "Громкость установлена на 40%.", answer)
    check("вызван set_volume ровно 1 раз", len(rec.calls) == 1, rec.calls)
    check("level=40", rec.calls[0][1].get("level") == 40, rec.calls[0])


def test_volume_adjust_up():
    print("\n=== Громкость: 'сделай погромче' ===")
    with Recorder("adjust_volume", {"result": "Готово, громче."}) as rec:
        answer = intent_router.try_match("сделай погромче")
    check("распознано", answer == "Готово, громче.", answer)
    check("direction=up", rec.calls[0][1].get("direction") == "up", rec.calls[0])


def test_volume_adjust_down():
    print("\n=== Громкость: 'тише' (одно слово) ===")
    with Recorder("adjust_volume", {"result": "Готово, тише."}) as rec:
        answer = intent_router.try_match("тише")
    check("распознано", answer == "Готово, тише.", answer)
    check("direction=down", rec.calls[0][1].get("direction") == "down", rec.calls[0])


def test_media_pause():
    print("\n=== Медиа: 'пауза' ===")
    with Recorder("media_control", {"result": "Готово: play_pause."}) as rec:
        answer = intent_router.try_match("пауза")
    check("распознано", answer == "Готово: play_pause.", answer)
    check("action=play_pause", rec.calls[0][1].get("action") == "play_pause", rec.calls[0])


def test_media_next_requires_context():
    print("\n=== Медиа: 'дальше' БЕЗ контекста трек/музыка -> НЕ команда ===")
    with Recorder("media_control", {"result": "не должно вызваться"}) as rec:
        answer = intent_router.try_match("дальше")
    check("router не уверен, вернул None", answer is None, answer)
    check("media_control НЕ вызван", len(rec.calls) == 0, rec.calls)


def test_media_next_with_context():
    print("\n=== Медиа: 'следующий трек' (с контекстом) ===")
    with Recorder("media_control", {"result": "Готово: next."}) as rec:
        answer = intent_router.try_match("следующий трек")
    check("распознано", answer == "Готово: next.", answer)
    check("action=next", rec.calls[0][1].get("action") == "next", rec.calls[0])


def test_timer_digits():
    print("\n=== Таймер: 'поставь таймер на 5 минут' ===")
    with Recorder("set_timer", {"result": "Таймер поставлен на 300 секунд."}) as rec:
        answer = intent_router.try_match("поставь таймер на 5 минут")
    check("распознано", answer == "Таймер поставлен на 300 секунд.", answer)
    check("seconds=300", rec.calls[0][1].get("seconds") == 300, rec.calls[0])


def test_timer_half_hour():
    print("\n=== Таймер: 'таймер на полчаса' ===")
    with Recorder("set_timer", {"result": "ok"}) as rec:
        intent_router.try_match("таймер на полчаса")
    check("seconds=1800", rec.calls[0][1].get("seconds") == 1800, rec.calls[0])


def test_open_app_confident():
    print("\n=== Открыть приложение: 'открой браузер' (уверенно) ===")
    with Recorder("open_application", {"result": "Открываю браузер."}) as rec:
        answer = intent_router.try_match("открой браузер")
    check("распознано", answer == "Открываю браузер.", answer)
    check("app_name=браузер", rec.calls[0][1].get("app_name") == "браузер", rec.calls[0])


def test_open_app_unconfident_falls_through():
    print("\n=== Открыть приложение: мусорное имя -> НЕ уверены, отдаём LLM ===")
    with Recorder("open_application", {"result": "не должно вызваться"}) as rec:
        answer = intent_router.try_match("открой ту штуку с котиками")
    check("router не уверен, вернул None", answer is None, answer)
    check("open_application НЕ вызван", len(rec.calls) == 0, rec.calls)


def test_long_sentence_never_matches():
    print("\n=== Длинная составная фраза с триггерным словом 'тише' -> НЕ instant ===")
    long_text = "тише пожалуйста я сейчас разговариваю по важному делу и мне нужна тишина"
    with Recorder("adjust_volume", {"result": "не должно вызваться"}) as rec:
        answer = intent_router.try_match(long_text)
    check("router отказался (фраза слишком длинная/сложная)", answer is None, answer)
    check("adjust_volume НЕ вызван", len(rec.calls) == 0, rec.calls)


def test_unrelated_question_returns_none():
    print("\n=== Обычный вопрос без совпадений -> None ===")
    answer = intent_router.try_match("какая сегодня погода в москве")
    check("router вернул None", answer is None, answer)


if __name__ == "__main__":
    test_volume_set_percent()
    test_volume_adjust_up()
    test_volume_adjust_down()
    test_media_pause()
    test_media_next_requires_context()
    test_media_next_with_context()
    test_timer_digits()
    test_timer_half_hour()
    test_open_app_confident()
    test_open_app_unconfident_falls_through()
    test_long_sentence_never_matches()
    test_unrelated_question_returns_none()
    print("\nВСЕ СЦЕНАРИИ ПРОШЛИ.")
