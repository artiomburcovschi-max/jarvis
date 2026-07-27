
import json
import queue
import os
import tempfile
import threading
import time
import traceback
import wave

import zmq
from faster_whisper import WhisperModel
from rapidfuzz import fuzz

from agents import DialogManager, DialogManagerError

WAKE_WORD = "джарвис"
WAKE_WORD_THRESHOLD = 75
ACTIVE_WINDOW_SECONDS = 10.0      # окно "активного диалога" после wake-word
LATE_RESULT_WARN_SECONDS = 5.0    # если обработка дольше этого - предупреждаем в консоли

# Пороги для отсева "галлюцинаций" Whisper на тишине/шуме/щелчках.
NO_SPEECH_PROB_MAX = 0.6
AVG_LOGPROB_MIN = -1.0
COMPRESSION_RATIO_MAX = 2.4


def is_wake_word_present(text: str, target: str = WAKE_WORD, threshold: int = WAKE_WORD_THRESHOLD):
    """Ищет слово-активатор среди слов фразы (устойчиво к неточностям STT)."""
    words = text.lower().split()
    for word in words:
        similarity = fuzz.ratio(word, target)
        if similarity >= threshold:
            return True, word
    # Фолбэк: иногда STT склеивает "джарвис" со следующим словом без пробела.
    if fuzz.partial_ratio(text.lower(), target) >= threshold + 10:
        return True, target
    return False, None


def strip_wake_word(text: str, matched_word: str) -> str:
    """Убирает найденное слово-активатор из начала фразы, оставляя саму команду."""
    words = text.split()
    for i, w in enumerate(words):
        if fuzz.ratio(w.lower(), matched_word.lower()) >= WAKE_WORD_THRESHOLD:
            return " ".join(words[i + 1:]).strip()
    return text


def is_hallucinated_segment(segment) -> bool:
    """Штатная эвристика faster-whisper/whisper для отсева "галлюцинаций" на
    тишине/фоновом шуме: модель либо сама не уверена, что это речь
    (no_speech_prob высокий), либо результат статистически похож на мусор
    (низкий avg_logprob, ненормальный compression_ratio - признак того, что
    модель зациклилась на повторах или выдала бессвязный набор токенов)."""
    if getattr(segment, "no_speech_prob", 0.0) > NO_SPEECH_PROB_MAX:
        return True
    if getattr(segment, "avg_logprob", 0.0) < AVG_LOGPROB_MIN:
        return True
    if getattr(segment, "compression_ratio", 0.0) > COMPRESSION_RATIO_MAX:
        return True
    return False


class DialogState:
    """Хранит, находимся ли мы сейчас в "активном окне" диалога после wake-word.

    Проверка идёт по времени, КОГДА ФРАЗА БЫЛА ПРОИЗНЕСЕНА (ts из заголовка),
    а не по текущему времени - иначе долгая обработка одной фразы (Whisper
    "завис" на пару секунд) могла бы ошибочно закрыть окно ещё до того, как
    пользователь на самом деле замолчал.
    """

    def __init__(self, window_seconds: float):
        self.window_seconds = window_seconds
        self._active_until = 0.0
        self._lock = threading.Lock()

    def activate(self, at_time: float):
        with self._lock:
            self._active_until = max(self._active_until, at_time + self.window_seconds)

    def is_active(self, at_time: float) -> bool:
        with self._lock:
            return at_time < self._active_until


def transcribe_worker(model: WhisperModel, audio_queue: "queue.Queue", socket_ui: zmq.Socket,
                       dialog_manager: "DialogManager | None"):
    """Работает в отдельном потоке: последовательно распознаёт фразы из очереди.

    Даже если одна фраза вызовет исключение, поток не падает - ошибка
    логируется, обработка продолжается со следующей фразы.

    dialog_manager может быть None (если не настроен GEMINI_API_KEY) - в этом
    случае сервер продолжает работать как чистый STT-эхо (этап 0), просто
    без LLM-мозга. Это осознанное решение: LLM-слой - опциональная надстройка,
    а не обязательное условие для того, чтобы STT-конвейер вообще запускался.
    """
    dialog = DialogState(ACTIVE_WINDOW_SECONDS)
    phrase_counter = 0

    while True:
        item = audio_queue.get()
        if item is None:  # сигнал остановки
            break

        seq, capture_ts, raw_audio_bytes = item
        phrase_counter += 1
        temp_filename = None
        processing_started = time.time()

        try:
            fd, temp_filename = tempfile.mkstemp(suffix=".wav", prefix="jarvis_phrase_")
            os.close(fd)

            with wave.open(temp_filename, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(raw_audio_bytes)

            segments, info = model.transcribe(
                temp_filename,
                language="ru",
                beam_size=5,
                initial_prompt="Разговор с голосовым ассистентом по имени Джарвис.",
                condition_on_previous_text=False,
                no_speech_threshold=0.5,
            )

            clean_parts = []
            dropped_hallucination = False
            for segment in segments:
                if is_hallucinated_segment(segment):
                    dropped_hallucination = True
                    continue
                clean_parts.append(segment.text)
            text = "".join(clean_parts).strip()

            processing_seconds = time.time() - processing_started
            if processing_seconds > LATE_RESULT_WARN_SECONDS:
                print(f"[ПРЕДУПРЕЖДЕНИЕ] Фраза #{seq} обрабатывалась {processing_seconds:.1f}с "
                      f"(медленно - STT может не успевать за темпом речи)")

            if not text:
                reason = "похоже на галлюцинацию (тишина/шум/щелчок)" if dropped_hallucination else "пусто"
                print(f"⚪ [Фраза #{seq}, {reason}]: распознавать нечего")
                continue

            has_wake_word, matched_word = is_wake_word_present(text)

            if has_wake_word:
                command = strip_wake_word(text, matched_word) or text
                # Была ли уже активна сессия ДО этого произнесения "Джарвис"?
                # Если нет - это начало нового разговора, и LLM-историю нужно
                # сбросить, чтобы не тащить в контекст обрывки предыдущей,
                # никак не связанной беседы получасовой давности.
                is_continuation = dialog.is_active(capture_ts)
                dialog.activate(capture_ts)
                if not is_continuation and dialog_manager:
                    dialog_manager.reset()

                print(f"\n🟢 [Джарвис #{seq}] распознано: {text!r}")
                answer = dialog_manager.handle(command) if dialog_manager else command
                socket_ui.send_string(f"Джарвис: {answer}")
            elif dialog.is_active(capture_ts):
                # Активное окно после недавнего wake-word - считаем фразу
                # командой, даже если имя не повторили (перебивание/
                # продолжение диалога без повтора имени).
                dialog.activate(capture_ts)
                print(f"\n🟡 [Команда в диалоге #{seq}]: {text!r}")
                answer = dialog_manager.handle(text) if dialog_manager else text
                socket_ui.send_string(f"Джарвис (диалог): {answer}")
            else:
                print(f"\n⚪ [Игнор #{seq}]: {text!r}")
                socket_ui.send_string(f"Вы: {text}")

        except Exception:
            print(f"[ОШИБКА] Не удалось обработать фразу #{seq}:")
            traceback.print_exc()
        finally:
            if temp_filename and os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except OSError:
                    pass
            audio_queue.task_done()


def main():
    print("[Python] Загрузка модели Whisper (base)...")
    model = WhisperModel("base", device="cpu", compute_type="int8")
    print("[Python] Модель готова!")

    try:
        dialog_manager = DialogManager()
        print("[Python] LLM-слой (Gemini) готов.")
    except DialogManagerError as e:
        dialog_manager = None
        print(f"[Python] LLM-слой ОТКЛЮЧЁН: {e}")
        print("[Python] Сервер продолжит работать в режиме чистого STT (без LLM-ответов).")

    context = zmq.Context()
    socket_audio = context.socket(zmq.PAIR)
    socket_audio.bind("tcp://127.0.0.1:5555")

    socket_ui = context.socket(zmq.PAIR)
    socket_ui.bind("tcp://127.0.0.1:5556")

    print("[Python] Жду звук от C++ ядра...")

    audio_queue: "queue.Queue" = queue.Queue()
    worker = threading.Thread(
        target=transcribe_worker, args=(model, audio_queue, socket_ui, dialog_manager), daemon=True
    )
    worker.start()

    received_count = 0
    try:
        while True:
            parts = socket_audio.recv_multipart()
            if len(parts) != 2:
                print(f"[ZMQ] Пропускаю сообщение неожиданного формата ({len(parts)} частей)")
                continue

            header_bytes, raw_audio_bytes = parts
            if not raw_audio_bytes:
                continue

            try:
                header = json.loads(header_bytes.decode("utf-8"))
                seq = header.get("seq", -1)
                capture_ts = header.get("ts", time.time())
            except (ValueError, UnicodeDecodeError):
                # На случай рассинхронизации протокола (например, старая версия
                # C++ ядра без заголовков) - не роняем сервер, просто считаем,
                # что фраза только что произнесена.
                seq, capture_ts = -1, time.time()

            received_count += 1
            latency = time.time() - capture_ts
            print(f"[ZMQ] Получена фраза #{seq} ({len(raw_audio_bytes)} байт, "
                  f"долетела за {latency:.2f}с), в очереди на распознавание: {audio_queue.qsize()}")
            audio_queue.put((seq, capture_ts, raw_audio_bytes))

    except KeyboardInterrupt:
        print("\n[Python] Завершение работы...")
    finally:
        audio_queue.put(None)
        worker.join(timeout=5)
        socket_audio.close()
        socket_ui.close()
        context.term()


if __name__ == "__main__":
    main()
