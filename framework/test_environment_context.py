"""
Ручной тест (без реального X11/буфера обмена) для C2 (agents/tools/environment.py).

run_shell() - единственная точка выхода наружу для Linux-веток (subprocess) -
подменяется на фейк, который проверяет РОВНО КАКАЯ команда была вызвана и
возвращает заданный ответ. Для Windows-ветки get_active_window (единственная,
что использует ctypes напрямую, а не run_shell) - IS_WINDOWS выставляется
насильно, а ctypes.windll (которого в Linux-песочнице физически нет)
подменяется на фейковый объект.
"""

import sys
import types

sys.path.insert(0, ".")

import agents.tools.environment as env_module


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(f"{label}: {detail}")


def with_fake_run_shell(responses_by_first_arg):
    """responses_by_first_arg: {команда: (ok, output)} - фейк run_shell(),
    ключ - имя утилиты (args[0]), чтобы не зависеть от точного набора флагов."""
    def fake_run_shell(args, timeout=5.0):
        key = args[0]
        if key not in responses_by_first_arg:
            raise AssertionError(f"Неожиданный вызов run_shell с {args!r}")
        return responses_by_first_arg[key]
    return fake_run_shell


def with_fake_which(available_tools):
    def fake_which(name):
        return f"/usr/bin/{name}" if name in available_tools else None
    return fake_which


def test_active_window_linux_success():
    print("\n=== get_active_window (Linux, xdotool доступен) ===")
    original_run_shell = env_module.run_shell
    original_which = env_module.shutil.which
    env_module.run_shell = with_fake_run_shell({"xdotool": (True, "Mozilla Firefox\n")})
    env_module.shutil.which = with_fake_which({"xdotool"})
    try:
        result = env_module.get_active_window()
        check("результат успешный", "result" in result, result)
        check("содержит имя окна", "Mozilla Firefox" in result["result"], result)
    finally:
        env_module.run_shell = original_run_shell
        env_module.shutil.which = original_which


def test_active_window_linux_missing_tool():
    print("\n=== get_active_window (Linux, xdotool НЕ установлен) ===")
    original_which = env_module.shutil.which
    env_module.shutil.which = with_fake_which(set())
    try:
        result = env_module.get_active_window()
        check("честная ошибка про xdotool", "xdotool" in result.get("error", ""), result)
    finally:
        env_module.shutil.which = original_which


def test_clipboard_linux_xclip():
    print("\n=== get_clipboard (Linux, xclip доступен) ===")
    original_run_shell = env_module.run_shell
    original_which = env_module.shutil.which
    env_module.run_shell = with_fake_run_shell({"xclip": (True, "секретный пароль 12345\n")})
    env_module.shutil.which = with_fake_which({"xclip"})
    try:
        result = env_module.get_clipboard()
        check("результат успешный", "result" in result, result)
        check("содержит скопированный текст", "секретный пароль" in result["result"], result)
    finally:
        env_module.run_shell = original_run_shell
        env_module.shutil.which = original_which


def test_clipboard_truncates_long_text():
    print("\n=== get_clipboard обрезает длинный текст ===")
    long_text = "А" * 2000
    original_run_shell = env_module.run_shell
    original_which = env_module.shutil.which
    env_module.run_shell = with_fake_run_shell({"xclip": (True, long_text)})
    env_module.shutil.which = with_fake_which({"xclip"})
    try:
        result = env_module.get_clipboard()
        check("результат обрезан", len(result["result"]) < len(long_text), len(result["result"]))
        check("есть многоточие в конце", result["result"].endswith("..."), result["result"][-10:])
    finally:
        env_module.run_shell = original_run_shell
        env_module.shutil.which = original_which


def test_clipboard_empty():
    print("\n=== get_clipboard: пустой буфер ===")
    original_run_shell = env_module.run_shell
    original_which = env_module.shutil.which
    env_module.run_shell = with_fake_run_shell({"xclip": (True, "")})
    env_module.shutil.which = with_fake_which({"xclip"})
    try:
        result = env_module.get_clipboard()
        check("сообщение о пустом буфере", "пуст" in result["result"], result)
    finally:
        env_module.run_shell = original_run_shell
        env_module.shutil.which = original_which


def test_now_playing_linux():
    print("\n=== get_now_playing (Linux, playerctl доступен) ===")
    original_run_shell = env_module.run_shell
    original_which = env_module.shutil.which
    env_module.run_shell = with_fake_run_shell({"playerctl": (True, "Daft Punk - One More Time\n")})
    env_module.shutil.which = with_fake_which({"playerctl"})
    try:
        result = env_module.get_now_playing()
        check("результат успешный", "result" in result, result)
        check("содержит артиста и трек", "Daft Punk - One More Time" in result["result"], result)
    finally:
        env_module.run_shell = original_run_shell
        env_module.shutil.which = original_which


def test_now_playing_nothing_playing():
    print("\n=== get_now_playing: ничего не играет ===")
    original_run_shell = env_module.run_shell
    original_which = env_module.shutil.which
    env_module.run_shell = with_fake_run_shell({"playerctl": (False, "No players found")})
    env_module.shutil.which = with_fake_which({"playerctl"})
    try:
        result = env_module.get_now_playing()
        check("честное сообщение, не ошибка", "result" in result, result)
        check("сообщение адекватное", "не играет" in result["result"], result)
    finally:
        env_module.run_shell = original_run_shell
        env_module.shutil.which = original_which


def test_active_window_windows():
    print("\n=== get_active_window (Windows, через ctypes) ===")
    original_is_windows = env_module.IS_WINDOWS
    env_module.IS_WINDOWS = True

    fake_user32 = types.SimpleNamespace(
        GetForegroundWindow=lambda: 12345,
        GetWindowTextLengthW=lambda hwnd: len("Проводник"),
    )

    import ctypes as real_ctypes

    class FakeBuffer:
        def __init__(self, size):
            self.value = ""

    def fake_create_unicode_buffer(size):
        buf = FakeBuffer(size)
        return buf

    def fake_get_window_text_w(hwnd, buf, n):
        buf.value = "Проводник"
        return len(buf.value)

    fake_user32.GetWindowTextW = fake_get_window_text_w
    fake_windll = types.SimpleNamespace(user32=fake_user32)

    original_windll = getattr(real_ctypes, "windll", None)
    real_ctypes.windll = fake_windll
    original_create_buffer = real_ctypes.create_unicode_buffer
    real_ctypes.create_unicode_buffer = fake_create_unicode_buffer

    try:
        result = env_module.get_active_window()
        check("результат успешный", "result" in result, result)
        check("содержит имя окна", "Проводник" in result["result"], result)
    finally:
        env_module.IS_WINDOWS = original_is_windows
        if original_windll is not None:
            real_ctypes.windll = original_windll
        else:
            delattr(real_ctypes, "windll")
        real_ctypes.create_unicode_buffer = original_create_buffer


if __name__ == "__main__":
    test_active_window_linux_success()
    test_active_window_linux_missing_tool()
    test_clipboard_linux_xclip()
    test_clipboard_truncates_long_text()
    test_clipboard_empty()
    test_now_playing_linux()
    test_now_playing_nothing_playing()
    test_active_window_windows()
    print("\nВСЕ СЦЕНАРИИ ПРОШЛИ.")
