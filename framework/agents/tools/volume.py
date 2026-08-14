"""Инструменты: управление громкостью системы (PulseAudio/PipeWire через pactl)."""

import shutil

from ._shared import IS_WINDOWS, run_shell


def set_volume(level: int) -> dict:
    """Устанавливает громкость системы в процентах, 0-100 (см. TOOL_SCHEMAS)."""
    level = max(0, min(100, int(level)))
    if IS_WINDOWS:
        return {"error": "Управление громкостью на Windows пока не реализовано."}
    if shutil.which("pactl") is None:
        return {"error": "Не найдена команда 'pactl' (нужен PulseAudio/PipeWire)."}
    ok, err = run_shell(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
    if not ok:
        return {"error": f"Не удалось изменить громкость: {err}"}
    return {"result": f"Громкость установлена на {level}%."}


def adjust_volume(direction: str, step: int = 10) -> dict:
    """Изменяет громкость на шаг вверх/вниз (см. схему в TOOL_SCHEMAS)."""
    if IS_WINDOWS:
        return {"error": "Управление громкостью на Windows пока не реализовано."}
    if shutil.which("pactl") is None:
        return {"error": "Не найдена команда 'pactl' (нужен PulseAudio/PipeWire)."}
    step = max(1, min(50, int(step)))
    sign = "+" if direction.lower() in ("up", "вверх", "громче") else "-"
    ok, err = run_shell(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{sign}{step}%"])
    if not ok:
        return {"error": f"Не удалось изменить громкость: {err}"}
    return {"result": f"Готово, громкость изменена {'вверх' if sign == '+' else 'вниз'} на {step}%."}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": "Устанавливает громкость системы на конкретное значение в процентах (0-100).",
            "parameters": {
                "type": "object",
                "properties": {"level": {"type": "integer", "description": "Громкость от 0 до 100"}},
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_volume",
            "description": "Делает громкость тише/громче на шаг (используй вместо set_volume, когда просят 'сделай громче/тише', а не называют конкретное число).",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "description": "'up' (громче) или 'down' (тише)"},
                    "step": {"type": "integer", "description": "На сколько процентов изменить (по умолчанию 10)"},
                },
                "required": ["direction"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "set_volume": set_volume,
    "adjust_volume": adjust_volume,
}
