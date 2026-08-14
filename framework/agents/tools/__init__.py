"""
Автозагрузка инструментов (function calling) для Джарвиса.

Как добавить новый инструмент - ПРОСТО ДОБАВЬ ФАЙЛ в эту папку:
  1. Создай новый .py файл здесь (имя любое, НЕ начинающееся с "_").
  2. В нём: обычные Python-функции + список TOOL_SCHEMAS (схемы в формате
     OpenAI function calling) + словарь TOOL_IMPLEMENTATIONS (имя -> функция).
  3. Всё. Ничего больше не трогай - этот файл сам найдёт новый модуль при
     следующем запуске сервера и подключит его инструменты.

Пример минимального файла смотри в datetime_tools.py - это самый короткий
файл в папке, хорошая отправная точка чтобы скопировать структуру.

Почему так (а не один большой tools.py):
  - Разные инструменты обычно вообще не связаны друг с другом (открыть сайт
    и опросить погоду не имеют ничего общего) - отдельные файлы держат их
    независимыми и его проще найти/поправить один конкретный.
  - Можно прислать/получить ОДИН файл при добавлении одной новой
    возможности, вместо целого проекта - раз в файле уже есть всё
    необходимое (сам инструмент + его схема + регистрация).
  - Если в одном файле баг (опечатка, отсутствующий импорт) - ломается
    ТОЛЬКО он. Остальные инструменты как работали, так и работают - ошибка
    заносится в лог при старте, а не роняет весь Джарвис.

Файлы, начинающиеся с "_" (например _shared.py), автозагрузчик ПРОПУСКАЕТ -
это место для общего кода, переиспользуемого несколькими инструментами
(платформенные флаги, нечёткий поиск по названию и т.п.), не для инструментов
самих по себе.
"""

import importlib
import pkgutil
from pathlib import Path

TOOL_SCHEMAS: list[dict] = []
TOOL_IMPLEMENTATIONS: dict = {}

# Раунд 24 (C5): имена инструментов, которые НИКОГДА не должны попадать в
# схему, отправляемую облачному провайдеру (см. dialog_manager.py,
# _create_stream()) - модуль инструментов может ОПЦИОНАЛЬНО определить
# LOCAL_ONLY_TOOLS (set имён из своего TOOL_SCHEMAS), и они соберутся сюда.
# Большинство модулей это не определяют - тогда просто нечего добавлять.
LOCAL_ONLY_TOOLS: set = set()

_package_dir = Path(__file__).parent
_loaded_modules: list[str] = []
_failed_modules: list[str] = []

for _finder, _module_name, _is_pkg in sorted(pkgutil.iter_modules([str(_package_dir)])):
    if _module_name.startswith("_"):
        continue  # служебный модуль (см. докстринг выше) - пропускаем молча

    try:
        _module = importlib.import_module(f".{_module_name}", package=__name__)
    except Exception as e:
        print(f"[tools] ОШИБКА загрузки модуля инструментов '{_module_name}.py': {e!r}")
        print(f"[tools] Остальные инструменты продолжат работать в обычном режиме.")
        _failed_modules.append(_module_name)
        continue

    _module_schemas = getattr(_module, "TOOL_SCHEMAS", None)
    _module_impls = getattr(_module, "TOOL_IMPLEMENTATIONS", None)

    if not _module_schemas and not _module_impls:
        # Модуль без инструментов - например, вспомогательный код, который
        # решили не префиксовать "_". Не ошибка, просто нечего подключать.
        continue

    if not _module_schemas or not _module_impls:
        print(f"[tools] ВНИМАНИЕ: '{_module_name}.py' определяет только одно из "
              f"TOOL_SCHEMAS/TOOL_IMPLEMENTATIONS - пропускаю, нужны оба.")
        _failed_modules.append(_module_name)
        continue

    for _schema in _module_schemas:
        _tool_name = _schema["function"]["name"]

        if _tool_name in TOOL_IMPLEMENTATIONS:
            print(f"[tools] ВНИМАНИЕ: инструмент '{_tool_name}' из '{_module_name}.py' "
                  f"уже был зарегистрирован другим модулем - пропускаю дубликат.")
            continue

        if _tool_name not in _module_impls:
            print(f"[tools] ВНИМАНИЕ: '{_module_name}.py' описывает схему для "
                  f"'{_tool_name}', но не даёт для неё реализацию в "
                  f"TOOL_IMPLEMENTATIONS - пропускаю.")
            continue

        TOOL_SCHEMAS.append(_schema)
        TOOL_IMPLEMENTATIONS[_tool_name] = _module_impls[_tool_name]

    _module_local_only = getattr(_module, "LOCAL_ONLY_TOOLS", None)
    if _module_local_only:
        LOCAL_ONLY_TOOLS.update(_module_local_only)

    _loaded_modules.append(_module_name)

print(f"[tools] Загружено модулей: {len(_loaded_modules)} ({', '.join(_loaded_modules)}), "
      f"всего инструментов: {len(TOOL_IMPLEMENTATIONS)}"
      + (f", ОШИБКИ В: {', '.join(_failed_modules)}" if _failed_modules else ""))

# register_timer_notifier/load_pending_timers - особый случай: это не
# instrument сам по себе, а функции настройки, которые server.py вызывает
# один раз при старте (см. timers.py, раунд 25/C6 для второй). Реэкспортируем
# явно, чтобы `from agents.tools import register_timer_notifier,
# load_pending_timers` в server.py продолжал работать без изменений.
from .timers import register_timer_notifier, load_pending_timers  # noqa: E402

# load_facts_summary - аналогичный особый случай (раунд 26, C7): не
# instrument, а функция, которую dialog_manager зовёт на КАЖДЫЙ запрос при
# сборке системного промпта - см. memory.py.
from .memory import load_facts_summary  # noqa: E402

__all__ = [
    "TOOL_SCHEMAS", "TOOL_IMPLEMENTATIONS", "LOCAL_ONLY_TOOLS",
    "register_timer_notifier", "load_pending_timers", "load_facts_summary",
]
