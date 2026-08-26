"""
Инструмент: управление YouTube и медиа (с поддержкой Linux и Windows).
"""

import subprocess
import sys
import urllib.parse
import urllib.request
import webbrowser
import re

try:
    import pyautogui
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False


def manage_media(action: str, query: str = "") -> dict:
    """Управляет медиа: ищет видео на YouTube или управляет воспроизведением."""
    action = action.lower()

    # ВЕТКА 1: Поиск и запуск видео на YouTube
    if action == "play_youtube":
        if not query:
            return {"error": "Для поиска на YouTube нужен запрос (параметр query)."}
        
        try:
            query_string = urllib.parse.urlencode({"search_query": query})
            url = f"https://www.youtube.com/results?{query_string}"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            req = urllib.request.Request(url, headers=headers)
            html_content = urllib.request.urlopen(req).read().decode()
            
            video_ids = re.findall(r"watch\?v=([a-zA-Z0-9_-]{11})", html_content)
            
            if not video_ids:
                return {"error": f"Не удалось найти видео по запросу: {query}"}
            
            video_url = f"https://www.youtube.com/watch?v={video_ids[0]}"
            webbrowser.open(video_url)
            
            return {"result": f"Нашел и открываю видео: {video_url}"}
        
        except Exception as e:
            return {"error": f"Ошибка при поиске на YouTube: {e}"}

    # ВЕТКА 2: Управление плеером (пауза, некст)
    elif action in ["pause", "resume", "play_pause", "next", "previous"]:
        # Нормализуем названия действий для плееров
        if action in ["pause", "resume", "play_pause"]:
            media_action = "play-pause"
            desc = "Пауза / Продолжить"
        elif action == "next":
            media_action = "next"
            desc = "Следующий трек"
        elif action == "previous":
            media_action = "previous"
            desc = "Предыдущий трек"
        else:
            media_action = "play-pause"
            desc = "Пауза / Продолжить"

        # ДЛЯ LINUX: используем playerctl через D-Bus (самый надежный способ)
        if sys.platform.startswith("linux"):
            try:
                result = subprocess.run(
                    ["playerctl", media_action],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                if result.returncode == 0:
                  return {"result": f"Команда '{desc}' успешно выполнена."}
                else:
                    err_msg = result.stderr.strip()
                    return {"error": f"Не найден активный медиаплеер или браузер с YouTube. Ошибка: {err_msg}"}
            except FileNotFoundError:
                return {"error": "В системе не установлен playerctl. Выполни: sudo apt install playerctl"}
            except Exception as e:
                return {"error": f"Ошибка playerctl: {e}"}

        # ДЛЯ WINDOWS / MACOS: используем pyautogui
        else:
            if not HAS_PYAUTOGUI:
                return {"error": "Не установлена библиотека pyautogui."}
            try:
                if action in ["pause", "resume", "play_pause"]:
                    pyautogui.press("playpause")
                elif action == "next":
                    pyautogui.press("nexttrack")
                elif action == "previous":
                    pyautogui.press("prevtrack")
                return {"result": f"Отправлена системная команда: {desc}"}
            except Exception as e:
                return {"error": f"Ошибка pyautogui: {e}"}
            
    else:
        return {"error": f"Неизвестное действие: {action}"}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "manage_media",
            "description": (
                "Ищет и включает видео на YouTube, а также управляет воспроизведением "
                "(пауза, продолжить, следующее видео, предыдущее видео). "
                "Используй, когда просят 'включи видео...', 'поставь на паузу', 'продолжи воспроизведение', 'следующее видео'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play_youtube", "pause", "resume", "play_pause", "next", "previous"],
                        "description": "Нужное действие: play_youtube (найти и включить), pause/resume/play_pause (пауза/плей), next (следующее), previous (предыдущее)."
                    },
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос. Обязателен только если action='play_youtube'.",
                    }
                },
                "required": ["action"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "manage_media": manage_media,
}