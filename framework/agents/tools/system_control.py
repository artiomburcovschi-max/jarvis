"""Инструмент: заблокировать экран компьютера."""

import subprocess

from ._shared import IS_WINDOWS


def lock_computer() -> dict:
    """Блокирует экран компьютера (см. схему в TOOL_SCHEMAS)."""
    import shutil

    if IS_WINDOWS:
        candidates = [["rundll32.exe", "user32.dll,LockWorkStation"]]
    else:
        # loginctl - современный способ (systemd), xdg-screensaver - более
        # старый универсальный запасной вариант на случай, если loginctl
        # недоступен или сессия не даёт заблокировать так.
        candidates = [["loginctl", "lock-session"], ["xdg-screensaver", "lock"]]

    for command in candidates:
        if not IS_WINDOWS and shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(command, timeout=5)
            return {"result": "Компьютер заблокирован."}
        except Exception:
            continue

    return {"error": "Не удалось заблокировать экран - ни один из способов не сработал."}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "lock_computer",
            "description": "Блокирует экран компьютера. Используй, если пользователь просит заблокировать компьютер или уходит.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "lock_computer": lock_computer,
}
