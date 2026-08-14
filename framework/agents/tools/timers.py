"""
Инструмент: таймер/напоминание.

В отличие от остальных инструментов, таймер не может просто "вернуть
результат" - результат должен прозвучать ПОЗЖЕ, когда время выйдет, а не
сразу. Для этого server.py при старте регистрирует "уведомитель"
(register_timer_notifier) - функцию, которая знает, как озвучить и
показать сообщение в UI. Этот файл сам ничего не знает про TTS/ZMQ - это
осознанное разделение ответственности (инструменты остаются простыми
функциями, а не тянут в себя половину server.py).

Раунд 25 (C6) - персистентность через перезапуск:
  До этого раунда таймер был чистым threading.Timer в памяти процесса -
  если server.py упадёт сам или его перезапустит watchdog (раунд 19,
  scripts/start.sh - а он теперь перезапускает ВСЕ ТРИ процесса при
  падении любого) - все ожидающие таймеры молча исчезали без следа.

  Теперь каждый таймер при постановке сразу пишется на диск
  (data/timers.json) с АБСОЛЮТНЫМ временем срабатывания (не "осталось
  секунд" - так переживает любое число перезапусков без пересчёта на
  каждом шаге) и стирается оттуда, когда реально сработал.
  load_pending_timers() зовётся server.py ОДИН РАЗ при старте, СРАЗУ
  ПОСЛЕ register_timer_notifier() (иначе перепланированному таймеру
  некому будет позвонить, если он окажется просроченным и сработает
  почти сразу) - перечитывает файл и перепланирует всё, что ещё не
  прошло. То, что должно было сработать, ПОКА процесс не работал -
  срабатывает немедленно, с пометкой в сообщении, что это восстановленный
  после перезапуска таймер (иначе непонятно, почему Джарвис вдруг
  заговорил сам, едва запустившись).
"""

import json
import threading
import time
import uuid
from pathlib import Path

_timer_notifier = None
_storage_lock = threading.Lock()


def register_timer_notifier(callback):
    """Вызывается один раз из server.py при старте - регистрирует функцию,
    которая озвучит сообщение, когда таймер сработает."""
    global _timer_notifier
    _timer_notifier = callback


def _storage_path() -> Path:
    """framework/agents/tools/timers.py -> framework/agents/tools ->
    framework/agents -> framework -> корень проекта -> data/timers.json.

    Функция, а не константа на уровне модуля, - специально, чтобы тесты
    могли подменить путь (см. test_timer_persistence.py) и не трогать
    реальный файл состояния проекта."""
    return Path(__file__).resolve().parent.parent.parent.parent / "data" / "timers.json"


def _load_all() -> dict:
    path = _storage_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # Битый/недоступный файл состояния НЕ должен ронять запуск
        # сервера - просто считаем, что сохранённых таймеров нет.
        print(f"[timers] Файл {path} повреждён или недоступен ({e}) - считаю, что таймеров нет.")
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_all(timers: dict) -> None:
    path = _storage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(timers, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[timers] Не удалось сохранить {path}: {e} - таймер продолжит "
              f"работать в этой сессии, но не переживёт перезапуск.")


def _add_persisted(timer_id: str, fire_at_unix: float, message: str) -> None:
    with _storage_lock:
        timers = _load_all()
        timers[timer_id] = {"fire_at_unix": fire_at_unix, "message": message}
        _save_all(timers)


def _remove_persisted(timer_id: str) -> None:
    with _storage_lock:
        timers = _load_all()
        if timer_id in timers:
            del timers[timer_id]
            _save_all(timers)


def _schedule(timer_id: str, delay_seconds: float, message: str, restored: bool = False) -> None:
    def _fire():
        _remove_persisted(timer_id)
        if _timer_notifier:
            text = message
            if restored and delay_seconds <= 0:
                # Сработал немедленно при старте, потому что просрочен -
                # без этой пометки было бы непонятно, почему Джарвис вдруг
                # заговорил сам через долю секунды после запуска.
                text = f"(таймер, поставленный до перезапуска) {message}"
            _timer_notifier(text)

    timer = threading.Timer(max(delay_seconds, 0), _fire)
    timer.daemon = True  # не мешает процессу завершиться, если таймер ещё не сработал
    timer.start()


def set_timer(seconds: int, message: str) -> dict:
    """Ставит таймер на N секунд (см. схему в TOOL_SCHEMAS)."""
    seconds = int(seconds)
    if seconds <= 0:
        return {"error": "Таймер должен быть больше 0 секунд."}
    if seconds > 6 * 3600:
        return {"error": "Слишком длинный таймер (максимум 6 часов)."}

    timer_id = uuid.uuid4().hex
    fire_at_unix = time.time() + seconds
    _add_persisted(timer_id, fire_at_unix, message)
    _schedule(timer_id, seconds, message)
    return {"result": f"Таймер поставлен на {seconds} секунд."}


def load_pending_timers() -> int:
    """Раунд 25 (C6): вызывается server.py ОДИН РАЗ при старте, СРАЗУ
    ПОСЛЕ register_timer_notifier() - перечитывает data/timers.json и
    перепланирует всё, что там сохранено. Возвращает количество
    восстановленных таймеров (для лога при старте).

    Таймеры, чьё время уже прошло, ПОКА процесс не работал, срабатывают
    почти немедленно (с пометкой в сообщении - см. _schedule()), а не
    тихо теряются и не переносятся молча."""
    with _storage_lock:
        timers = _load_all()

    now = time.time()
    for timer_id, entry in timers.items():
        fire_at_unix = entry.get("fire_at_unix", now)
        message = entry.get("message", "")
        delay = fire_at_unix - now
        _schedule(timer_id, delay, message, restored=True)

    return len(timers)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": (
                "Ставит таймер/напоминание на указанное количество секунд. "
                "Когда время выйдет, пользователь услышит сообщение вслух. "
                "Переведи время из слов пользователя в секунды сам "
                "(например, '5 минут' -> 300, 'полчаса' -> 1800)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer", "description": "Через сколько секунд сработает таймер"},
                    "message": {"type": "string", "description": "Что сказать, когда таймер сработает"},
                },
                "required": ["seconds", "message"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "set_timer": set_timer,
}
