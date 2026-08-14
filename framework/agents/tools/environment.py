"""
Инструменты C2 (read-only контекст среды): активное окно, буфер обмена,
что сейчас играет. Всё это только ЧТЕНИЕ состояния системы - никаких
побочных эффектов, поэтому, в отличие от будущих "опасных" действий (C3),
не нуждается ни в каком подтверждении - дать LLM это как контекст ничем не
рискованнее, чем дать ей время суток.

Даёт возможность фраз вида "закрой это" (после get_active_window LLM знает,
какое "это"), "что за трек" (get_now_playing), "вставь то, что я скопировал"
(get_clipboard).

Linux: используются штатные X11-утилиты (xdotool, xclip/xsel) - как и
остальной проект, через subprocess с whitelist-командами, а не через новую
pip-зависимость. Если утилита не установлена - честная ошибка с подсказкой
apt install, как и в остальных файлах tools/ (см. media.py: 'playerctl').

Windows: активное окно и буфер обмена - через ctypes/штатный powershell, без
дополнительных pip-пакетов (та же причина, что и с C++ ONNX Runtime -
не тащим лишние зависимости туда, где хватает stdlib/системных утилит).
"""

import shutil

from ._shared import IS_WINDOWS, run_shell

# Ограничение длины буфера обмена, попадающей в ответ LLM/озвучку - без
# этого случайно скопированный огромный текст (или бинарный мусор,
# распознанный как текст) улетел бы ЦЕЛИКОМ в контекст LLM и/или в TTS.
CLIPBOARD_MAX_CHARS = 500


def get_active_window() -> dict:
    """Возвращает заголовок и (где возможно) имя процесса активного окна."""
    if IS_WINDOWS:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title:
                return {"result": "Активное окно не удалось определить (нет заголовка)."}
            return {"result": f"Активное окно: {title}"}
        except Exception as e:
            return {"error": f"Не удалось получить активное окно: {e}"}

    if shutil.which("xdotool") is None:
        return {
            "error": "Не найдена команда 'xdotool' (установи: sudo apt install xdotool) - "
                     "нужна, чтобы узнать активное окно."
        }

    ok, output = run_shell(["xdotool", "getactivewindow", "getwindowname"])
    if not ok or not output.strip():
        return {"error": f"Не удалось определить активное окно: {output}"}
    return {"result": f"Активное окно: {output.strip()}"}


def get_clipboard() -> dict:
    """Возвращает текущее содержимое буфера обмена (обрезано до разумной длины)."""
    if IS_WINDOWS:
        ok, output = run_shell(["powershell", "-NoProfile", "-Command", "Get-Clipboard"])
        if not ok:
            return {"error": f"Не удалось прочитать буфер обмена: {output}"}
        text = output.strip()
    else:
        tool = None
        args = None
        if shutil.which("xclip"):
            tool, args = "xclip", ["xclip", "-selection", "clipboard", "-o"]
        elif shutil.which("xsel"):
            tool, args = "xsel", ["xsel", "--clipboard", "--output"]
        if tool is None:
            return {
                "error": "Не найдена команда 'xclip' ни 'xsel' (установи: "
                         "sudo apt install xclip) - нужна, чтобы прочитать буфер обмена."
            }
        ok, output = run_shell(args)
        if not ok:
            return {"error": f"Не удалось прочитать буфер обмена: {output}"}
        text = output.strip()

    if not text:
        return {"result": "Буфер обмена пуст."}

    truncated = text[:CLIPBOARD_MAX_CHARS]
    suffix = "..." if len(text) > CLIPBOARD_MAX_CHARS else ""
    return {"result": f"В буфере обмена: {truncated}{suffix}"}


def get_now_playing() -> dict:
    """Возвращает артиста и трек из текущего MPRIS-плеера (Linux) - см. media.py."""
    if IS_WINDOWS:
        return {"error": "Определение текущего трека на Windows пока не реализовано."}
    if shutil.which("playerctl") is None:
        return {"error": "Не найдена команда 'playerctl' (установи: sudo apt install playerctl)."}

    ok, output = run_shell(["playerctl", "metadata", "--format", "{{artist}} - {{title}}"])
    if not ok or not output.strip():
        return {"result": "Сейчас ничего не играет (или плеер не поддерживает MPRIS)."}
    return {"result": f"Сейчас играет: {output.strip()}"}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_active_window",
            "description": (
                "Возвращает заголовок активного (текущего) окна на экране. Используй, "
                "когда пользователь ссылается на что-то без явного названия "
                "(\"закрой это\", \"что это за окно\")."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clipboard",
            "description": (
                "Возвращает текущее содержимое буфера обмена. Используй, когда просят "
                "прочитать/использовать то, что скопировано."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_now_playing",
            "description": (
                "Возвращает исполнителя и название текущего трека в плеере (Spotify/VLC/"
                "браузер и т.п.). Используй для вопросов вида \"что за трек играет\"."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "get_active_window": get_active_window,
    "get_clipboard": get_clipboard,
    "get_now_playing": get_now_playing,
}
