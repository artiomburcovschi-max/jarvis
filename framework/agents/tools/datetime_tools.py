"""
Инструмент: текущая дата/время.

Это пример МИНИМАЛЬНОГО независимого файла-инструмента - вся "конвенция"
для автозагрузки: файл лежит в этой папке (agents/tools/), его имя не
начинается с "_", и в нём есть две переменные верхнего уровня -
TOOL_SCHEMAS (список схем в формате OpenAI function calling) и
TOOL_IMPLEMENTATIONS (словарь "имя функции" -> сама функция). Всё остальное -
детали реализации, автозагрузчик в __init__.py их не трогает.
"""

import datetime

WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]


def get_current_datetime() -> dict:
    """Возвращает текущую дату, время и день недели."""
    now = datetime.datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": WEEKDAYS_RU[now.weekday()],
    }


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_datetime",
            "description": (
                "Возвращает текущую дату, время и день недели. Используй, "
                "когда пользователь спрашивает который час, какое сегодня "
                "число, какой сегодня день недели."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "get_current_datetime": get_current_datetime,
}
