"""Тесты для ротации логов (раунд 19, A3) в scripts/start.sh.

Переиспользует хелперы/фикстуры из test_launch_watchdog.py - тот же
паттерн, что и test_tool_call_audit_log.py, который переиспользует
FakeClient из test_abort_scenarios.py.

fake_worker.py умеет (см. сам файл) по третьему аргументу постоянно
печатать в stdout строку заданной длины - только так есть чем "нагонять"
файл logs/<name>.log до порога ротации без реального core/server/UI.
"""
import os
import signal
import subprocess
import sys
import time

import pytest

from test_launch_watchdog import (
    FAKE_WORKER,
    PROJECT_ROOT,
    START_SH,
    _wait_until,
)

ROTATION_POLL_TIMEOUT = 10.0


def _run_with_padding(tmp_path, pad_bytes, max_bytes, backups, poll_interval="0.1"):
    events_file = str(tmp_path / "events.log")
    log_dir = tmp_path / "logs"
    env = dict(os.environ)
    env["JARVIS_RUN_DIR"] = str(tmp_path / "run")
    env["JARVIS_LOG_DIR"] = str(log_dir)
    env["JARVIS_STARTUP_DELAY"] = "0.05"
    env["JARVIS_POLL_INTERVAL"] = poll_interval
    env["JARVIS_MAX_RESTARTS"] = "5"
    env["JARVIS_RESTART_WINDOW"] = "60"
    env["JARVIS_LOG_MAX_BYTES"] = str(max_bytes)
    env["JARVIS_LOG_BACKUPS"] = str(backups)
    env["SERVER_CMD"] = f"{sys.executable} {FAKE_WORKER} server {events_file} {pad_bytes}"
    env["CORE_CMD"] = f"{sys.executable} {FAKE_WORKER} core {events_file} 0"
    env["UI_CMD"] = f"{sys.executable} {FAKE_WORKER} ui {events_file} 0"

    proc = subprocess.Popen(
        ["bash", START_SH],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, log_dir, events_file


def _stop(proc):
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def _count_starts(events_file, name):
    if not os.path.exists(events_file):
        return 0
    with open(events_file) as f:
        return sum(1 for line in f if line.strip().startswith(f"start {name} "))


def test_log_gets_rotated_when_size_exceeded(tmp_path):
    # Маленький порог + быстрый спам в stdout - файл гарантированно
    # переваливает JARVIS_LOG_MAX_BYTES за пару циклов опроса.
    proc, log_dir, events_file = _run_with_padding(
        tmp_path, pad_bytes=64, max_bytes=500, backups=3
    )
    try:
        archive = log_dir / "server.log.1"
        ok = _wait_until(lambda: archive.exists(), timeout=ROTATION_POLL_TIMEOUT)
        assert ok, "server.log.1 не появился - ротация не сработала"
        assert archive.stat().st_size > 0

        # Процесс не должен был перезапускаться из-за ротации - это
        # операция ТОЛЬКО над файлом, не над самим процессом.
        assert _count_starts(events_file, "server") == 1

        # После обнуления файл продолжает расти - процесс жив и пишет в
        # то же самое имя файла (copytruncate, не перезапуск с новым fd).
        size_right_after = (log_dir / "server.log").stat().st_size
        ok = _wait_until(
            lambda: (log_dir / "server.log").stat().st_size > size_right_after,
            timeout=ROTATION_POLL_TIMEOUT,
        )
        assert ok, "server.log не растёт после ротации - процесс перестал в него писать"
    finally:
        _stop(proc)


def test_old_archives_are_capped(tmp_path):
    # Очень маленький порог -> много ротаций за короткое время ->
    # проверяем, что архивов больше backups+1 никогда не появляется.
    backups = 2
    proc, log_dir, events_file = _run_with_padding(
        tmp_path, pad_bytes=128, max_bytes=300, backups=backups, poll_interval="0.05"
    )
    try:
        target = log_dir / f"server.log.{backups}"
        ok = _wait_until(lambda: target.exists(), timeout=ROTATION_POLL_TIMEOUT)
        assert ok, f"не дождались накопления {backups} архивов"

        # Даём ещё немного покрутиться и проверяем, что лишний архив не появляется.
        time.sleep(1.0)
        overflow = log_dir / f"server.log.{backups + 1}"
        assert not overflow.exists(), "архивов больше JARVIS_LOG_BACKUPS - старые не вычищаются"
    finally:
        _stop(proc)
