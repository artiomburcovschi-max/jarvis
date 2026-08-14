"""Инструмент: управление воспроизведением медиа через playerctl (MPRIS)."""

import shutil

from ._shared import IS_WINDOWS, run_shell


def media_control(action: str) -> dict:
    """Управляет воспроизведением медиа: play_pause/next/previous (TOOL_SCHEMAS)."""
    if IS_WINDOWS:
        return {"error": "Управление медиа на Windows пока не реализовано."}
    if shutil.which("playerctl") is None:
        return {"error": "Не найдена команда 'playerctl' (установи: sudo apt install playerctl)."}

    action_map = {
        "play_pause": "play-pause", "пауза": "play-pause", "плей": "play-pause",
        "next": "next", "следующий": "next", "трек": "next",
        "previous": "previous", "предыдущий": "previous", "назад": "previous",
    }
    playerctl_action = action_map.get(action.lower())
    if not playerctl_action:
        return {"error": f"Неизвестное действие: {action}. Доступны: play_pause, next, previous."}

    ok, err = run_shell(["playerctl", playerctl_action])
    if not ok:
        return {"error": f"Не удалось управлять плеером (возможно, ничего не играет): {err}"}
    return {"result": f"Готово: {action}."}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": (
                "Управляет воспроизведением музыки/видео в любом плеере, "
                "поддерживающем MPRIS (Spotify, VLC, браузеры и т.д.): "
                "пауза/воспроизведение, следующий/предыдущий трек."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "'play_pause', 'next' или 'previous'"}
                },
                "required": ["action"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "media_control": media_control,
}
