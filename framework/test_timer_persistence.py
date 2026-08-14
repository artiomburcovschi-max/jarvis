"""Тесты для раунда 25 (C6) - персистентность таймеров через перезапуск.

Путь к файлу состояния (`_storage_path()`) подменяется на временный
(`tmp_path`) в каждом тесте - реальный `data/timers.json` проекта никогда
не трогается. Ожидание срабатывания таймера - через threading.Event с
таймаутом, а не голый time.sleep() - тест не флейкает от загруженности
машины и не ждёт дольше необходимого.
"""
import json
import sys
import threading
import time

import pytest

sys.path.insert(0, ".")

import agents.tools.timers as timers_module  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    storage_file = tmp_path / "timers.json"
    monkeypatch.setattr(timers_module, "_storage_path", lambda: storage_file)
    yield storage_file
    # На случай, если тест оставил notifier от предыдущего прогона.
    timers_module.register_timer_notifier(None)


def _make_capturing_notifier():
    received = []
    event = threading.Event()

    def notifier(message):
        received.append(message)
        event.set()

    return notifier, received, event


# --- set_timer() - постановка и запись на диск ------------------------------


def test_set_timer_persists_to_disk(_isolated_storage):
    result = timers_module.set_timer(300, "проверить духовку")
    assert "result" in result

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert len(data) == 1
    entry = next(iter(data.values()))
    assert entry["message"] == "проверить духовку"
    assert entry["fire_at_unix"] > time.time()  # в будущем, не в прошлом


def test_set_timer_rejects_non_positive_seconds():
    assert "error" in timers_module.set_timer(0, "тест")
    assert "error" in timers_module.set_timer(-5, "тест")


def test_set_timer_rejects_too_long_duration():
    result = timers_module.set_timer(7 * 3600, "тест")
    assert "error" in result


def test_multiple_timers_persist_independently(_isolated_storage):
    timers_module.set_timer(100, "первый")
    timers_module.set_timer(200, "второй")

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert len(data) == 2
    messages = {entry["message"] for entry in data.values()}
    assert messages == {"первый", "второй"}


# --- Срабатывание и удаление с диска -----------------------------------------


def test_timer_removed_from_disk_after_firing(_isolated_storage):
    notifier, received, event = _make_capturing_notifier()
    timers_module.register_timer_notifier(notifier)

    timer_id = "test-timer-1"
    timers_module._add_persisted(timer_id, time.time() + 0.05, "пора вставать")
    timers_module._schedule(timer_id, 0.05, "пора вставать")

    fired = event.wait(timeout=2.0)
    assert fired, "таймер не сработал вовремя"
    assert received == ["пора вставать"]

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert timer_id not in data


# --- load_pending_timers() - восстановление после "перезапуска" ------------


def test_load_pending_timers_reschedules_future_timer(_isolated_storage):
    notifier, received, event = _make_capturing_notifier()
    timers_module.register_timer_notifier(notifier)

    future_ts = time.time() + 0.1
    _isolated_storage.write_text(
        json.dumps({"abc": {"fire_at_unix": future_ts, "message": "будущий таймер"}}),
        encoding="utf-8",
    )

    restored = timers_module.load_pending_timers()
    assert restored == 1

    fired = event.wait(timeout=2.0)
    assert fired
    # Ещё не просроченный на момент загрузки таймер срабатывает в свой
    # обычный срок - без пометки "восстановлен", это неотличимо для
    # пользователя от таймера, поставленного в этой же сессии.
    assert received == ["будущий таймер"]


def test_load_pending_timers_fires_overdue_immediately_with_note(_isolated_storage):
    notifier, received, event = _make_capturing_notifier()
    timers_module.register_timer_notifier(notifier)

    past_ts = time.time() - 100  # должен был сработать 100 секунд назад
    _isolated_storage.write_text(
        json.dumps({"xyz": {"fire_at_unix": past_ts, "message": "просроченный таймер"}}),
        encoding="utf-8",
    )

    restored = timers_module.load_pending_timers()
    assert restored == 1

    fired = event.wait(timeout=2.0)
    assert fired
    assert len(received) == 1
    # Просроченный таймер, сработавший немедленно при старте, ДОЛЖЕН нести
    # пометку - иначе непонятно, почему Джарвис заговорил сам едва запустившись.
    assert "перезапуск" in received[0].lower()
    assert "просроченный таймер" in received[0]


def test_load_pending_timers_cleans_up_fired_entries(_isolated_storage):
    timers_module.register_timer_notifier(lambda message: None)

    past_ts = time.time() - 10
    _isolated_storage.write_text(
        json.dumps({"old": {"fire_at_unix": past_ts, "message": "старый"}}),
        encoding="utf-8",
    )

    timers_module.load_pending_timers()
    time.sleep(0.3)  # дать успеть сработать и удалиться с диска

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert "old" not in data


def test_load_pending_timers_with_no_file_returns_zero(_isolated_storage):
    # Файла ещё нет вовсе (первый запуск проекта) - не должно быть ошибки.
    assert not _isolated_storage.exists()
    assert timers_module.load_pending_timers() == 0


def test_load_pending_timers_with_corrupted_file_does_not_crash(_isolated_storage):
    _isolated_storage.write_text("это не json совсем {{{", encoding="utf-8")
    assert timers_module.load_pending_timers() == 0


def test_load_pending_timers_with_non_dict_json_does_not_crash(_isolated_storage):
    _isolated_storage.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert timers_module.load_pending_timers() == 0


def test_notifier_not_registered_does_not_crash_on_fire(_isolated_storage):
    # register_timer_notifier(None) - как если бы server.py ещё не успел
    # зарегистрировать колбэк (не должно случиться на практике, порядок
    # вызовов это гарантирует, но защититься дёшево).
    timers_module.register_timer_notifier(None)

    timer_id = "no-notifier-timer"
    timers_module._add_persisted(timer_id, time.time() + 0.05, "тест")
    timers_module._schedule(timer_id, 0.05, "тест")

    time.sleep(0.3)  # таймер сработал (или должен был) - не упало и ладно

    data = json.loads(_isolated_storage.read_text(encoding="utf-8"))
    assert timer_id not in data
