"""
Инструмент: открыть приложение из белого списка.

НАМЕРЕННО whitelist, а не произвольная строка в subprocess: команда
приходит от LLM, реагирующей на текст пользователя (в т.ч. искажённый
распознаванием речи) - если бы мы просто запускали subprocess.Popen(любая
строка от модели), это была бы прямая дыра для выполнения произвольных
команд в системе через промпт-инъекцию (тот же принцип, что и с
calculate() - там ast вместо eval(), здесь whitelist вместо произвольного
запуска).

Каждое приложение - СПИСОК кандидатов-бинарников, а не одно жёстко зашитое
имя (см. _shared.find_installed_candidate). Список редактируемый - допиши
сюда своё ПО под то, что реально стоит у тебя.
"""

import subprocess

from ._shared import IS_WINDOWS, find_installed_candidate, fuzzy_lookup

ALLOWED_APPLICATIONS_LINUX = {
    "браузер": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "firefox"],
    "гугл": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "firefox"],
    "хром": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"],
    "калькулятор": ["gnome-calculator", "galculator", "qalculate-gtk", "kcalc"],
    "терминал": ["gnome-terminal", "xfce4-terminal", "konsole", "x-terminal-emulator"],
    "текстовый редактор": ["xed", "gedit", "kate", "leafpad", "mousepad"],
    "файловый менеджер": ["nemo", "nautilus", "dolphin", "thunar", "pcmanfm"],
    "настройки": ["cinnamon-settings", "gnome-control-center", "systemsettings5"],
    "календарь": ["gnome-calendar", "korganizer"],
    "код": ["code", "codium"],
    "вс код": ["code", "codium"],
    "vs code": ["code", "codium"],
    "spotify": ["spotify"],
    "steam": ["steam"],
    "discord": ["discord"],
    # Roblox/Roblox Studio - официально только под Windows (см.
    # ALLOWED_APPLICATIONS_WINDOWS ниже); в Linux-словаре НЕ добавляем, чтобы
    # не создавать ложное впечатление поддержки через Wine/Proton "из
    # коробки" - если реально используешь Roblox через Steam Proton/Lutris/
    # нативный лаунчер под Linux, допиши сюда актуальный бинарник вручную.
}
ALLOWED_APPLICATIONS_WINDOWS = {
    "калькулятор": ["calc.exe"],
    "блокнот": ["notepad.exe"],
    "проводник": ["explorer.exe"],
    "браузер": ["chrome.exe", "msedge.exe", "firefox.exe"],
    "гугл": ["chrome.exe", "msedge.exe", "firefox.exe"],
    "хром": ["chrome.exe"],
    "код": ["code.exe"],
    "вс код": ["code.exe"],
    "vs code": ["code.exe"],
    # Steam обычно НЕ добавляет себя в PATH - но всегда ставится в
    # предсказуемое место (в отличие от Discord/Roblox, которым нужен glob
    # из-за версионированных подпапок - см. find_installed_candidate).
    "steam": ["steam.exe", r"%PROGRAMFILES(X86)%\Steam\steam.exe", r"%PROGRAMFILES%\Steam\steam.exe"],
    # Discord ставится в версионированную подпапку (app-1.2.3) под
    # LOCALAPPDATA, версия меняется при каждом автообновлении - поэтому "*"
    # вместо конкретного номера версии (см. find_installed_candidate: паттерн
    # с "*" разворачивается через glob, берётся самая свежая по времени
    # изменения версия).
    "discord": ["discord.exe", r"%LOCALAPPDATA%\Discord\app-*\Discord.exe"],
    "spotify": ["spotify.exe", r"%APPDATA%\Spotify\Spotify.exe"],
    # Тоже версионированная подпапка (version-abc123, буквенно-цифровой хэш,
    # не номер версии - тем более нельзя зашить как константу).
    "roblox": ["robloxplayerbeta.exe", r"%LOCALAPPDATA%\Roblox\Versions\version-*\RobloxPlayerBeta.exe"],
    "roblox studio": ["robloxstudiobeta.exe", r"%LOCALAPPDATA%\Roblox\Versions\version-*\RobloxStudioBeta.exe"],
    "студия роблокс": ["robloxstudiobeta.exe", r"%LOCALAPPDATA%\Roblox\Versions\version-*\RobloxStudioBeta.exe"],
}


def open_application(app_name: str) -> dict:
    """Открывает приложение из разрешённого списка (см. схему в TOOL_SCHEMAS)."""
    apps = ALLOWED_APPLICATIONS_WINDOWS if IS_WINDOWS else ALLOWED_APPLICATIONS_LINUX

    best_name, best_score = fuzzy_lookup(app_name, apps.keys())
    if best_score < 60:
        return {
            "error": (
                f"'{app_name}' нет в списке разрешённых приложений. "
                f"Доступны: {', '.join(apps.keys())}."
            )
        }

    candidates = apps[best_name]
    # РАНЬШЕ на Windows тут стояло "candidates[0]" без всякой проверки -
    # то есть код слепо верил, что первый кандидат установлен, и просто
    # пытался его запустить. Для calc.exe/notepad.exe/explorer.exe (всегда
    # есть в Windows) это работало по счастливой случайности, но для
    # Steam/Discord/Spotify/Roblox (которые НЕ добавляют себя в PATH и не
    # всегда лежат по стандартному пути) это гарантированно падало бы с
    # "не удалось запустить". Теперь find_installed_candidate реально
    # проверяет ВСЕ ОС одинаково (включая glob-паттерны для версионированных
    # путей - см. _shared.py).
    binary = find_installed_candidate(candidates)
    if binary is None:
        return {
            "error": (
                f"'{best_name}' не найден - проверил кандидатов: {', '.join(candidates)}. "
                f"Ни один не установлен, либо допиши актуальное имя в ALLOWED_APPLICATIONS "
                f"в agents/tools/apps.py."
            )
        }

    try:
        subprocess.Popen([binary], start_new_session=True)
        return {"result": f"Открываю {best_name} ({binary})."}
    except Exception as e:
        return {"error": f"Не удалось запустить {best_name}: {e}"}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": (
                "Открывает приложение на компьютере из разрешённого списка "
                "(браузер/гугл/хром, калькулятор, терминал, текстовый редактор, файловый "
                "менеджер, настройки, календарь, код/VS Code, spotify, steam, discord, "
                "roblox, roblox studio [только Windows]). "
                "Используй, когда просят что-то открыть/запустить на компьютере."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Название приложения, например 'калькулятор' или 'терминал'",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "open_application": open_application,
}
