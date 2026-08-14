"""
Ручной тест для confirmation.match_confirmation_reply() - самой
чувствительной к ошибкам части C3: от неё зависит, выполнится ли опасное
действие. Изолированный тест без каких-либо зависимостей от dialog_manager/
server.py - чистая функция "текст -> True/False/None".
"""

import sys
sys.path.insert(0, ".")

from agents.confirmation import match_confirmation_reply  # noqa: E402


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(f"{label}: {detail}")


CASES_YES = ["да", "Да.", "ага", "угу", "конечно", "давай", "точно", "делай", "ок", "окей", "да, точно"]
CASES_NO = ["нет", "Нет.", "не надо", "отмена", "стоп", "неа", "отставить"]
CASES_AMBIGUOUS = [
    "не знаю",  # содержит "не", но это не отказ по смыслу - и важно, что это НЕ должно давать False
    "да нет наверное",  # оба слова сразу - неоднозначно
    "открой браузер",  # явно новая, не связанная команда
    "какая сегодня погода",  # длинная реплика, явно не ответ на да/нет
    "",  # пусто
    "да, но подожди секунду, мне нужно ещё подумать над этим хорошенько",  # длинная - не пытаемся угадывать
]


def test_yes_cases():
    print("\n=== Однозначное 'да' ===")
    for text in CASES_YES:
        result = match_confirmation_reply(text)
        check(f"{text!r} -> True", result is True, result)


def test_no_cases():
    print("\n=== Однозначное 'нет' ===")
    for text in CASES_NO:
        result = match_confirmation_reply(text)
        check(f"{text!r} -> False", result is False, result)


def test_ambiguous_cases():
    print("\n=== Неоднозначное -> None (безопасный дефолт: не выполнять) ===")
    for text in CASES_AMBIGUOUS:
        result = match_confirmation_reply(text)
        check(f"{text!r} -> None", result is None, result)


if __name__ == "__main__":
    test_yes_cases()
    test_no_cases()
    test_ambiguous_cases()
    print("\nВСЕ СЦЕНАРИИ ПРОШЛИ.")
