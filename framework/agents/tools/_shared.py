"""
Общие хелперы, переиспользуемые несколькими файлами в tools/.

Имя файла начинается с подчёркивания НАМЕРЕННО: автозагрузчик в
tools/__init__.py специально пропускает такие файлы при сборе инструментов
(см. комментарий там) - это способ иметь "служебные" модули внутри пакета,
которые сами по себе не добавляют ни одного instrument'а, а только
переиспользуемый код для других файлов.
"""

import glob
import os
import platform
import shutil

from rapidfuzz import fuzz

IS_LINUX = platform.system() == "Linux"
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"


def find_installed_candidate(candidates: list[str]) -> str | None:
    """Возвращает первый реально установленный бинарник из списка кандидатов.

    Разные дистрибутивы/окружения ставят одно и то же ПО под разными
    именами (например, календарь - gnome-calendar на GNOME, но не факт, что
    стоит на Cinnamon/Mint) - поэтому кандидатов несколько, пробуем по
    очереди через shutil.which(), первый найденный - используем.

    Кандидат, содержащий "*" или "%" (например
    "%LOCALAPPDATA%\\Discord\\app-*\\Discord.exe"), трактуется НЕ как имя
    бинарника для shutil.which(), а как путь-паттерн для glob - это нужно
    для приложений на Windows, которые НЕ добавляют себя в PATH и ставятся
    в версионированные папки (Discord: app-1.2.3, Roblox: version-abc123) -
    обычный shutil.which() для них никогда ничего не найдёт, а хардкодить
    конкретную версию в путь бессмысленно, она меняется при каждом
    обновлении приложения.
    """
    for candidate in candidates:
        if "*" in candidate or "%" in candidate:
            expanded = os.path.expandvars(candidate)
            matches = glob.glob(expanded)
            if matches:
                # Несколько версий сразу (старая не всегда удаляется при
                # обновлении) - берём самую свежую по времени изменения, а
                # не первую по алфавиту (алфавитный порядок версий не
                # гарантированно совпадает с хронологическим).
                matches.sort(key=os.path.getmtime, reverse=True)
                return matches[0]
            continue
        if shutil.which(candidate):
            return candidate
    return None


def fuzzy_lookup(name: str, known_names) -> tuple[str | None, int]:
    """Ищет ближайшее по звучанию известное имя (устойчиво к неточностям STT)."""
    best_name, best_score = None, 0
    for known_name in known_names:
        score = fuzz.ratio(name.lower(), known_name)
        if score > best_score:
            best_name, best_score = known_name, score
    return best_name, best_score


def run_shell(command: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    """Запускает системную команду, возвращает (успех, вывод_или_ошибка)."""
    import subprocess

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, (result.stderr or result.stdout).strip()
    except FileNotFoundError:
        return False, f"команда '{command[0]}' не найдена"
    except subprocess.TimeoutExpired:
        return False, "команда не ответила вовремя"
