"""
Ручной тест (без сети, без реальных Steam/Discord/Roblox) для раунда 15
(C1 - расширение whitelist):

  1. find_installed_candidate() с glob-паттерном (эмулирует версионированные
     папки Discord/Roblox на Windows) - находит САМУЮ СВЕЖУЮ версию среди
     нескольких.
  2. open_application() на Windows реально проверяет наличие бинарника
     (было: слепо брало candidates[0] без проверки - см. apps.py, коммент
     "РАНЬШЕ на Windows тут стояло...") - имитируем ОТСУТСТВИЕ Steam и
     проверяем, что возвращается честная ошибка, а не попытка запустить
     несуществующий файл.
  3. _discover_volumes()/open_folder() на Linux - подключённая "флешка"
     (временная директория, имитирующая /media/user/USBDRIVE) находится и
     открывается; после "отключения" (удаления папки) - исчезает из списка
     на СЛЕДУЮЩЕМ вызове (не кэшируется).
  4. SITE_ALIASES в web.py - "ютуб" не улетает в Google-поиск, а открывает
     youtube.com напрямую.
"""

import os
import sys
import shutil
import tempfile
import types

sys.path.insert(0, ".")

import agents.tools._shared as shared_module
import agents.tools.apps as apps_module
import agents.tools.folders as folders_module
import agents.tools.web as web_module


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(f"{label}: {detail}")


def test_glob_candidate_picks_newest_version():
    print("\n=== find_installed_candidate: glob-паттерн, несколько версий ===")
    tmp = tempfile.mkdtemp()
    try:
        old_dir = os.path.join(tmp, "app-1.0.0")
        new_dir = os.path.join(tmp, "app-2.0.0")
        os.makedirs(old_dir)
        os.makedirs(new_dir)
        old_exe = os.path.join(old_dir, "Discord.exe")
        new_exe = os.path.join(new_dir, "Discord.exe")
        with open(old_exe, "w") as f:
            f.write("old")
        os.utime(old_exe, (1000, 1000))  # искусственно старое время изменения
        with open(new_exe, "w") as f:
            f.write("new")
        os.utime(new_exe, (2000, 2000))  # искусственно новое время изменения

        pattern = os.path.join(tmp, "app-*", "Discord.exe")
        result = shared_module.find_installed_candidate([pattern])
        check("найден именно НОВЫЙ exe (по mtime)", result == new_exe, result)
    finally:
        shutil.rmtree(tmp)


def test_glob_candidate_no_match_falls_through():
    print("\n=== find_installed_candidate: glob без совпадений -> None ===")
    result = shared_module.find_installed_candidate([
        "/tmp/definitely_does_not_exist_xyz/app-*/Foo.exe",
        "definitely_not_a_real_binary_xyz",
    ])
    check("вернул None (ничего не найдено)", result is None, result)


def test_windows_open_application_checks_existence():
    print("\n=== open_application на Windows реально проверяет наличие (был баг) ===")
    original_is_windows = apps_module.IS_WINDOWS
    apps_module.IS_WINDOWS = True
    try:
        result = apps_module.open_application("steam")
        check("вернулась ОШИБКА (Steam не 'установлен' в тестовом окружении)",
              "error" in result, result)
        check("сообщение об ошибке упоминает, что не найден",
              "не найден" in result["error"], result["error"])
    finally:
        apps_module.IS_WINDOWS = original_is_windows


def test_windows_open_application_finds_glob_installed_app():
    print("\n=== open_application на Windows находит приложение через glob ===")
    original_is_windows = apps_module.IS_WINDOWS
    original_apps = dict(apps_module.ALLOWED_APPLICATIONS_WINDOWS)
    apps_module.IS_WINDOWS = True
    tmp = tempfile.mkdtemp()
    try:
        version_dir = os.path.join(tmp, "version-abc123")
        os.makedirs(version_dir)
        fake_exe = os.path.join(version_dir, "RobloxPlayerBeta.exe")
        with open(fake_exe, "w") as f:
            f.write("fake")

        apps_module.ALLOWED_APPLICATIONS_WINDOWS["roblox"] = [
            os.path.join(tmp, "version-*", "RobloxPlayerBeta.exe")
        ]

        # Popen реально запускать не нужно (это не .exe, который можно
        # выполнить в Linux-песочнице) - подменяем на фейк, чтобы проверить
        # только ЛОГИКУ ПОИСКА, а не запуск процесса.
        original_popen = apps_module.subprocess.Popen
        launched = {}

        def fake_popen(args, **kwargs):
            launched["binary"] = args[0]
            return types.SimpleNamespace()

        apps_module.subprocess.Popen = fake_popen
        try:
            result = apps_module.open_application("roblox")
        finally:
            apps_module.subprocess.Popen = original_popen

        check("результат успешный", "result" in result, result)
        check("запущен именно найденный через glob файл",
              launched.get("binary") == fake_exe, launched)
    finally:
        apps_module.IS_WINDOWS = original_is_windows
        apps_module.ALLOWED_APPLICATIONS_WINDOWS.clear()
        apps_module.ALLOWED_APPLICATIONS_WINDOWS.update(original_apps)
        shutil.rmtree(tmp)


def test_dynamic_volume_discovery_and_open():
    print("\n=== Динамическое обнаружение флешки (Linux, /media/user/...) ===")
    tmp = tempfile.mkdtemp()
    media_root = os.path.join(tmp, "media")
    user_dir = os.path.join(media_root, "artem")
    volume_dir = os.path.join(user_dir, "USBDRIVE")
    os.makedirs(volume_dir)

    original_media_bases = None
    # _discover_volumes жёстко смотрит на "/media" и "/run/media" - патчим
    # через monkeypatch самой функции, чтобы не трогать реальную ФС хоста.
    original_discover = folders_module._discover_volumes

    def fake_discover():
        volumes = {}
        for base in (media_root,):
            if not os.path.isdir(base):
                continue
            import glob as glob_mod
            for user_d in glob_mod.glob(os.path.join(base, "*")):
                for vol_d in glob_mod.glob(os.path.join(user_d, "*")):
                    if os.path.isdir(vol_d):
                        volumes[os.path.basename(vol_d).lower()] = vol_d
        return volumes

    folders_module._discover_volumes = fake_discover
    try:
        original_popen = folders_module.subprocess.Popen
        launched = {}

        def fake_popen(args, **kwargs):
            launched["path"] = args[1]
            return types.SimpleNamespace()

        folders_module.subprocess.Popen = fake_popen
        try:
            result = folders_module.open_folder("usbdrive")
        finally:
            folders_module.subprocess.Popen = original_popen

        check("флешка найдена и 'открыта'", "result" in result, result)
        check("открыт правильный путь", launched.get("path") == volume_dir, launched)

        # "Отключаем" флешку (удаляем папку) - следующий вызов НЕ должен её найти,
        # т.к. список пересканируется каждый раз, а не кэшируется с первого вызова.
        shutil.rmtree(volume_dir)
        result2 = folders_module.open_folder("usbdrive")
        check("после 'отключения' флешка больше не находится", "error" in result2, result2)
    finally:
        folders_module._discover_volumes = original_discover
        shutil.rmtree(tmp)


def test_youtube_alias_bypasses_google_search():
    print("\n=== web.py: 'ютуб' открывает youtube.com напрямую, не через Google ===")
    original_open = web_module.webbrowser.open
    opened = {}

    def fake_open(url):
        opened["url"] = url
        return True

    web_module.webbrowser.open = fake_open
    try:
        result = web_module.open_website("ютуб")
    finally:
        web_module.webbrowser.open = original_open

    check("результат успешный", "result" in result, result)
    check("открыт именно youtube.com, а не google-поиск",
          opened.get("url") == "https://youtube.com", opened)


if __name__ == "__main__":
    test_glob_candidate_picks_newest_version()
    test_glob_candidate_no_match_falls_through()
    test_windows_open_application_checks_existence()
    test_windows_open_application_finds_glob_installed_app()
    test_dynamic_volume_discovery_and_open()
    test_youtube_alias_bypasses_google_search()
    print("\nВСЕ СЦЕНАРИИ ПРОШЛИ.")
