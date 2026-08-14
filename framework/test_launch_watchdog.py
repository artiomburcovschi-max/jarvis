"""Тесты для scripts/start.sh (раунд 19, A1-A2).

Раздел "Общие принципы" в README требует тест с фейками для любого нового
куска логики - для bash-скрипта это означает: реальный core/server/main.py
подменяются фейковыми python-процессами (scripts/testdata/fake_worker.py,
fake_crash.py) через переменные окружения CORE_CMD/SERVER_CMD/UI_CMD,
которые start.sh как раз для этого и читает.

Проверяем:
  1. все три процесса стартуют в правильном количестве;
  2. падение ОДНОГО процесса перезапускает ВСЕ ТРИ (не точечно) - это
     сознательное архитектурное решение (см. комментарий в start.sh);
  3. чистая остановка по SIGTERM (нет "осиротевших" дочерних процессов);
  4. защита от restart-шторма - если процесс валится сразу же на каждом
     старте, watchdog не долбит рестарты бесконечно, а сдаётся с кодом 1.

Всё через subprocess с временными RUN_DIR/LOG_DIR (никогда не трогаем
реальные run/ и logs/ проекта) и урезанными таймаутами
(JARVIS_STARTUP_DELAY/JARVIS_POLL_INTERVAL), чтобы тесты были быстрыми.
"""
import os
import signal
import subprocess
import sys
import time

import pytest

FRAMEWORK_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(FRAMEWORK_DIR)
START_SH = os.path.join(PROJECT_ROOT, "scripts", "start.sh")
FAKE_WORKER = os.path.join(PROJECT_ROOT, "scripts", "testdata", "fake_worker.py")
FAKE_CRASH = os.path.join(PROJECT_ROOT, "scripts", "testdata", "fake_crash.py")

POLL_TIMEOUT = 8.0
POLL_STEP = 0.05


def _wait_until(predicate, timeout=POLL_TIMEOUT, step=POLL_STEP):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def _read_events(events_file):
    if not os.path.exists(events_file):
        return []
    with open(events_file) as f:
        return [line.strip() for line in f if line.strip()]


def _count_starts(events, name):
    return sum(1 for e in events if e.startswith(f"start {name} "))


def _pid_of(events, name, occurrence=-1):
    starts = [e for e in events if e.startswith(f"start {name} ")]
    if not starts:
        return None
    return int(starts[occurrence].split()[2])


def _is_alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _base_env(tmp_path, events_file):
    env = dict(os.environ)
    env["JARVIS_RUN_DIR"] = str(tmp_path / "run")
    env["JARVIS_LOG_DIR"] = str(tmp_path / "logs")
    env["JARVIS_STARTUP_DELAY"] = "0.1"
    env["JARVIS_POLL_INTERVAL"] = "0.2"
    env["JARVIS_MAX_RESTARTS"] = "5"
    env["JARVIS_RESTART_WINDOW"] = "60"
    env["SERVER_CMD"] = f"{sys.executable} {FAKE_WORKER} server {events_file}"
    env["CORE_CMD"] = f"{sys.executable} {FAKE_WORKER} core {events_file}"
    env["UI_CMD"] = f"{sys.executable} {FAKE_WORKER} ui {events_file}"
    return env


@pytest.fixture
def watchdog(tmp_path):
    """Запускает start.sh в своей группе процессов и гарантированно чистит
    её за собой, даже если тест упал на середине assert'а."""
    events_file = str(tmp_path / "events.log")
    env = _base_env(tmp_path, events_file)
    proc = subprocess.Popen(
        ["bash", START_SH],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc, events_file, env
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def test_all_three_start(watchdog):
    proc, events_file, _ = watchdog

    ok = _wait_until(lambda: len(_read_events(events_file)) >= 3)
    assert ok, f"не дождались старта всех трёх процессов: {_read_events(events_file)}"

    events = _read_events(events_file)
    for name in ("server", "core", "ui"):
        assert _count_starts(events, name) == 1


def test_one_crash_restarts_all_three(watchdog):
    proc, events_file, env = watchdog

    ok = _wait_until(lambda: len(_read_events(events_file)) >= 3)
    assert ok

    events = _read_events(events_file)
    server_pid_before = _pid_of(events, "server")
    ui_pid_before = _pid_of(events, "ui")
    core_pid_before = _pid_of(events, "core")

    # "Роняем" core, как будто он упал сам.
    os.kill(core_pid_before, signal.SIGKILL)

    # Ждём, пока watchdog перезапустит все три - у каждого должно стать
    # по 2 записи "start" в журнале, а не только у core.
    def all_restarted():
        ev = _read_events(events_file)
        return (
            _count_starts(ev, "server") >= 2
            and _count_starts(ev, "core") >= 2
            and _count_starts(ev, "ui") >= 2
        )

    ok = _wait_until(all_restarted, timeout=10.0)
    events = _read_events(events_file)
    assert ok, f"не все процессы перезапустились после падения core: {events}"

    # И это именно НОВЫЕ процессы, а не те же самые pid.
    assert _pid_of(events, "server") != server_pid_before
    assert _pid_of(events, "ui") != ui_pid_before
    assert _pid_of(events, "core") != core_pid_before


def test_sigterm_stops_everything_cleanly(watchdog):
    proc, events_file, _ = watchdog

    ok = _wait_until(lambda: len(_read_events(events_file)) >= 3)
    assert ok

    events = _read_events(events_file)
    pids = {
        name: _pid_of(events, name) for name in ("server", "core", "ui")
    }
    for pid in pids.values():
        assert _is_alive(pid)

    proc.send_signal(signal.SIGTERM)

    def watchdog_exited():
        return proc.poll() is not None

    ok = _wait_until(watchdog_exited, timeout=10.0)
    assert ok, "watchdog не завершился после SIGTERM"
    assert proc.returncode == 0

    # Ни один из трёх дочерних процессов не должен остаться "сиротой".
    for name, pid in pids.items():
        assert not _is_alive(pid), f"{name} (pid {pid}) остался жить после остановки"


def test_restart_storm_gives_up(tmp_path):
    events_file = str(tmp_path / "events.log")
    env = _base_env(tmp_path, events_file)
    # ui падает сразу при каждом запуске - имитируем несобранный/битый
    # процесс, а не реальный крэш посреди работы.
    env["UI_CMD"] = f"{sys.executable} {FAKE_CRASH} ui {events_file}"
    env["JARVIS_MAX_RESTARTS"] = "2"
    env["JARVIS_RESTART_WINDOW"] = "60"

    proc = subprocess.Popen(
        ["bash", START_SH],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=5)
        pytest.fail("watchdog не сдался после restart-шторма - завис в цикле рестартов")

    assert proc.returncode == 1

    # И никого из фейковых server/core не осталось висеть.
    events = _read_events(events_file)
    for name in ("server", "core"):
        pid = _pid_of(events, name)
        assert not _is_alive(pid)
