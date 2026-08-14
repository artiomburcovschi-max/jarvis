"""
Инструмент: открыть папку в файловом менеджере по умолчанию.

Отдельно от apps.py, потому что открывается не своим бинарником, а через
"открой вот этот путь в файловом менеджере по умолчанию" (xdg-open на
Linux, explorer на Windows - оба сами знают, чем открыть папку, не нужно
угадывать конкретный файловый менеджер).
"""

import glob
import os
import subprocess

from ._shared import IS_WINDOWS, fuzzy_lookup

ALLOWED_FOLDERS = {
    "загрузки": "~/Downloads",
    "документы": "~/Documents",
    "рабочий стол": "~/Desktop",
    "домашняя папка": "~",
    # ДОПИШИ сюда реальные пути своих проектов - "Джарвис" (этот самый
    # проект) и route control server из памяти чата, но точные пути на
    # твоей машине я не знаю (эта песочница - не твой компьютер). Например:
    # "проект джарвис": "~/Projects/jarvis",
    # "роут контрол сервер": "~/Projects/route-control-server",
}


def _discover_volumes() -> dict[str, str]:
    """Возвращает {имя: путь} для ТЕКУЩЕГО состояния подключённых дисков и
    флешек - в отличие от ALLOWED_FOLDERS ВСЕГДА пересканируется заново при
    каждом вызове open_folder(), а не берётся из статического словаря:
    флешки подключаются/отключаются в любой момент между репликами, и
    закэшированный на старте сервера список неизбежно устарел бы."""
    volumes: dict[str, str] = {}

    if IS_WINDOWS:
        import string
        for letter in string.ascii_uppercase:
            path = f"{letter}:\\"
            if os.path.exists(path):
                volumes[f"диск {letter.lower()}"] = path
        return volumes

    # Linux: автоматически смонтированные съёмные носители (флешки, внешние
    # диски) в Cinnamon/GNOME/KDE обычно всплывают под /media/<user>/<метка>
    # или /run/media/<user>/<метка> - оба пути проверяем, окружение отличается
    # от дистрибутива к дистрибутиву.
    for base in ("/media", "/run/media"):
        if not os.path.isdir(base):
            continue
        for user_dir in glob.glob(os.path.join(base, "*")):
            for vol_dir in glob.glob(os.path.join(user_dir, "*")):
                if os.path.isdir(vol_dir):
                    volumes[os.path.basename(vol_dir).lower()] = vol_dir
    return volumes


def open_folder(folder_name: str) -> dict:
    """Открывает папку в файловом менеджере по умолчанию (см. TOOL_SCHEMAS)."""
    dynamic_volumes = _discover_volumes()
    # Статические папки (загрузки/документы/проекты) объединяем с ТЕКУЩИМИ
    # подключёнными дисками/флешками - fuzzy-поиск идёт по объединённому
    # списку, так что "открой флешку" и "открой загрузки" работают одним и
    # тем же путём в коде, а не двумя параллельными реализациями.
    all_candidates = {**ALLOWED_FOLDERS, **dynamic_volumes}

    best_name, best_score = fuzzy_lookup(folder_name, all_candidates.keys())
    if best_score < 60:
        available = ", ".join(all_candidates.keys()) or "(ничего не смонтировано)"
        return {
            "error": (
                f"'{folder_name}' нет в списке разрешённых папок и не похоже на "
                f"подключённый диск/флешку. Доступны: {available}."
            )
        }

    path = os.path.expanduser(all_candidates[best_name])
    if not os.path.isdir(path):
        return {"error": f"Папка не существует (или диск уже отключили): {path}"}

    import shutil
    opener = "explorer" if IS_WINDOWS else "xdg-open"
    if not IS_WINDOWS and shutil.which(opener) is None:
        return {"error": f"Не найдена команда '{opener}' для открытия папки."}

    try:
        subprocess.Popen([opener, path], start_new_session=True)
        return {"result": f"Открываю папку «{best_name}»."}
    except Exception as e:
        return {"error": f"Не удалось открыть папку: {e}"}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "open_folder",
            "description": (
                "Открывает папку (загрузки, документы, рабочий стол, домашняя папка, "
                "папки проектов) в файловом менеджере по умолчанию. Также умеет открывать "
                "ТЕКУЩИЕ подключённые диски и флешки (например 'открой диск D' на Windows "
                "или 'открой флешку' на Linux) - список актуализируется при каждом вызове."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_name": {
                        "type": "string",
                        "description": (
                            "Название папки/диска/флешки, например 'загрузки', "
                            "'диск d' или 'флешка'"
                        ),
                    }
                },
                "required": ["folder_name"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "open_folder": open_folder,
}
