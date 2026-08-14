"""
Ручной тест (без сети, без реального Piper) на filler-логику в
StreamingSpeaker (раунд 11):

  1. Реальный ответ приходит МЕДЛЕННЕЕ, чем filler_delay -> filler должен
     прозвучать первым, реальное предложение - вторым.
  2. Реальный ответ приходит БЫСТРЕЕ, чем filler_delay -> filler вообще не
     должен звучать (не платим лишней задержкой за уже быстрый ответ).
  3. abort() до истечения filler_delay -> таймер отменяется, filler не
     звучит НИКОГДА, даже после abort (не "выстреливает в пустоту").

FakeTTS ведёт лог вызовов (speak/play_audio) с меткой времени - этого
достаточно, чтобы проверить и факт вызова, и порядок, без реального аудио.
"""

import sys
import threading
import time
import importlib.util
from pathlib import Path

# Импортируем модуль напрямую по файлу, А НЕ через "from voice.streaming_speaker
# import ..." - иначе Python сначала выполнит voice/__init__.py, который тянет
# tts.py -> sounddevice -> реальную библиотеку PortAudio. В этой тестовой
# песочнице PortAudio физически не установлена (и не должна быть - тест
# специально не трогает настоящий звук), а сам streaming_speaker.py не
# зависит от sounddevice напрямую - только queue/random/threading/traceback.
_spec = importlib.util.spec_from_file_location(
    "streaming_speaker_standalone", Path(__file__).parent / "voice" / "streaming_speaker.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
StreamingSpeaker = _module.StreamingSpeaker

FILLER_DELAY = 0.08  # маленькое значение специально, чтобы тест был быстрым


class FakeTTS:
    def __init__(self):
        self.log = []  # список (event, payload, t)
        self._t0 = time.monotonic()

    def speak(self, text):
        self.log.append(("speak", text, time.monotonic() - self._t0))

    def play_audio(self, audio):
        self.log.append(("play_audio", audio, time.monotonic() - self._t0))

    def stop(self):
        self.log.append(("stop", None, time.monotonic() - self._t0))


class FakeControl:
    def __init__(self):
        self.log = []

    def send_mute(self):
        self.log.append("MUTE")

    def send_unmute(self):
        self.log.append("UNMUTE")


def wait_for_queue_drain(speaker, timeout=2.0):
    """Ждёт, пока внутренняя очередь озвучки опустеет - простая замена
    join() для теста (внутренняя очередь не экспортируется наружу нарочно)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if speaker._queue.empty():
            time.sleep(0.05)  # маленький запас, чтобы _run() успел обработать последний item
            return
        time.sleep(0.01)
    raise AssertionError("Очередь озвучки не опустела за отведённое время")


def scenario_1_slow_answer_gets_filler():
    print("\n=== Сценарий 1: медленный ответ -> filler звучит первым ===")
    tts = FakeTTS()
    control = FakeControl()
    speaker = StreamingSpeaker(tts, control, filler_audio=["filler_audio_bytes"],
                                filler_delay_seconds=FILLER_DELAY)

    speaker.start_response()
    time.sleep(FILLER_DELAY * 3)  # имитируем "LLM думает" дольше, чем filler_delay
    speaker.enqueue_sentence("Настоящий ответ.")
    speaker.end_response()

    wait_for_queue_drain(speaker)

    events = [entry[0] for entry in tts.log]
    print(f"  события TTS: {events}")
    assert events == ["play_audio", "speak"], f"Ожидался порядок [play_audio, speak], а был: {events}"
    print("  OK: filler прозвучал первым, настоящий ответ - вторым.")


def scenario_2_fast_answer_skips_filler():
    print("\n=== Сценарий 2: быстрый ответ -> filler НЕ звучит ===")
    tts = FakeTTS()
    control = FakeControl()
    speaker = StreamingSpeaker(tts, control, filler_audio=["filler_audio_bytes"],
                                filler_delay_seconds=FILLER_DELAY)

    speaker.start_response()
    # Реальное предложение приходит СРАЗУ, задолго до filler_delay.
    speaker.enqueue_sentence("Мгновенный ответ.")
    speaker.end_response()

    # Ждём дольше, чем filler_delay, чтобы убедиться, что таймер точно не
    # выстрелил "с опозданием" уже после того, как контент начался.
    time.sleep(FILLER_DELAY * 3)
    wait_for_queue_drain(speaker)

    events = [entry[0] for entry in tts.log]
    print(f"  события TTS: {events}")
    assert events == ["speak"], f"Filler не должен был звучать, а события: {events}"
    print("  OK: filler пропущен, никакой лишней задержки для быстрого ответа.")


def scenario_3_abort_cancels_pending_filler():
    print("\n=== Сценарий 3: abort() до срабатывания filler -> filler не звучит никогда ===")
    tts = FakeTTS()
    control = FakeControl()
    speaker = StreamingSpeaker(tts, control, filler_audio=["filler_audio_bytes"],
                                filler_delay_seconds=FILLER_DELAY)

    speaker.start_response()
    speaker.abort()  # перебили почти сразу, до того как filler_delay истёк

    # Ждём дольше, чем filler_delay - таймер, если бы не был отменён, успел
    # бы сработать за это время.
    time.sleep(FILLER_DELAY * 3)

    events = [entry[0] for entry in tts.log]
    print(f"  события TTS: {events}")
    assert "play_audio" not in events, (
        f"Filler НЕ должен был прозвучать после abort(), а события: {events}"
    )
    print("  OK: отменённый filler-таймер не выстрелил задним числом (stop() от abort() - это норма).")


if __name__ == "__main__":
    scenario_1_slow_answer_gets_filler()
    scenario_2_fast_answer_skips_filler()
    scenario_3_abort_cancels_pending_filler()
    print("\nВСЕ СЦЕНАРИИ ПРОШЛИ.")
