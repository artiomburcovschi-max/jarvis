"""
Jarvis STT-сервер.

Архитектура:
  C++ ядро (VAD) --[аудио, tcp 5555]--> этот процесс --[текст, tcp 5556]--> UI

Что учтено в этой версии (см. README.md за полным списком):

1. Приём и распознавание разнесены на поток-приёмник + поток-worker через
   queue.Queue - ошибка на одной фразе (битые данные, сбой модели) не роняет
   весь сервер и не останавливает приём следующих фраз.

2. Каждая фраза приходит вместе с заголовком {"seq", "ts"} (ts - момент, когда
   фраза НАЧАЛА звучать, а не когда долетела). Это даёт две вещи:
   - честную метрику задержки речь->текст;
   - защиту от "зависла STT, ответ пришёл через 5 секунд после того, как
     диалоговая сессия уже закрылась по таймауту" - решение "продолжение
     диалога или нет" принимается по ts фразы, а не по моменту, когда Whisper
     закончил работу.

3. Whisper-hallucination guard: тишина/шум иногда заставляет STT-модели
   "галлюцинировать" случайные слова. Отбрасываем сегменты с высоким
   no_speech_prob / низким avg_logprob / аномальным compression_ratio - это
   штатные признаки галлюцинации в faster-whisper/whisper.

4. LLM-слой (agents.DialogManager, через OpenRouter/любой OpenAI-совместимый
   провайдер): распознанный текст команды больше не уходит в UI как есть -
   сначала идёт в LLM с function calling (см. agents/tools/), и в UI
   отправляется уже осмысленный ответ. Требует переменную окружения
   LLM_API_KEY (см. README.md). Если ключ не задан, сервер не падает -
   просто работает в режиме чистого STT-эхо, как раньше.

5. Голос (voice.TextToSpeech, Piper) + раунд 10 (стриминг + прерывание):
   ответ LLM теперь СТРИМИТСЯ по предложениям через voice.StreamingSpeaker -
   первая фраза начинает звучать, пока LLM ещё генерирует продолжение,
   вместо ожидания всего ответа целиком. На время озвучки в C++ ядро уходит
   команда MUTE (voice.ControlChannel, порт 5557) - но это уже не полное
   отключение микрофона (см. main.cpp): если пользователь начинает говорить
   достаточно уверенно, receive-цикл ниже зовёт speaker.abort() - текущее
   воспроизведение останавливается НЕМЕДЛЕННО (см. TextToSpeech.stop()),
   это и есть настоящее прерывание (barge-in), а не просто игнорирование
   голоса пользователя. Требует PIPER_MODEL_PATH (см. README.md). Если не
   задан - сервер работает без озвучки, только текстом в UI.

6. Раунд 13 (UI 2.0): канал 5556 (UI) теперь несёт структурированные JSON-
   события, а не голые строки - см. send_ui_event(): {"type": "state", ...}
   (idle/thinking/speaking), {"type": "user_message", ...} (распознанный
   текст, ДО ответа - показывается сразу, а не склеенным с ответом в конце),
   {"type": "assistant_message", ...}. Плюс НОВЫЙ канал 5558 - команды ОТ UI
   К серверу (см. ui_command_listener) - пока одна команда, "прервать"
   (кнопка в UI дёргает ровно тот же speaker.abort(), что и голосовой
   barge-in). main.py (UI) обновлён под этот протокол - старая версия UI
   (голые строки) с этим сервером работать не будет.

7. Раунд 22 (B3): порт 5555 теперь несёт не только полные фразы (2 zmq-
   фрейма), но и лёгкие СИГНАЛЫ без аудио (1 фрейм) - см. PhraseSender.h в
   ядре и parse_audio_message() здесь. "speech_started" (VAD увидел начало
   речи) -> состояние UI "listening_active" - обратная связь ПОКА
   пользователь ещё говорит, а не только после того, как фраза целиком
   закончится и распознается (это может занимать секунды). "speech_discarded"
   (фраза оказалась короче порога и была отброшена целиком) -> состояние
   возвращается в "idle" явно, иначе UI застряла бы в "слушаю вас" навсегда.
   Настоящего партиала ВНУТРИ одной фразы (текст по словам, пока человек
   говорит) это не даёт - Whisper всё ещё получает фразу только целиком,
   когда VAD решит, что она закончена. Полноценный партиал потребовал бы
   стриминга растущих кусков аудио ДО конца фразы - более крупное изменение
   в C++ ядре, отложено (см. README, раздел "Раунд 22").
"""

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

from agents import DialogManager, DialogManagerError, intent_router
from agents.confirmation import match_confirmation_reply
from agents.tools import register_timer_notifier, load_pending_timers, TOOL_IMPLEMENTATIONS
from audio_protocol import parse_audio_message
from jarvis_config import get
from stt_corrections import apply_corrections
from voice import ControlChannel, StreamingSpeaker, TextToSpeech, TTSError

WAKE_WORD = "джарвис"
WAKE_WORD_THRESHOLD = 75
ACTIVE_WINDOW_SECONDS = 10.0      # окно "активного диалога" после wake-word
LATE_RESULT_WARN_SECONDS = 5.0    # если обработка дольше этого - предупреждаем в консоли

# C3: сколько ждать ответа "да"/"нет" на вопрос подтверждения опасного
# действия, прежде чем тихо отменить его. Меньше, чем обычное окно диалога
# (DIALOG_WINDOW_SECONDS) - вопрос уже прозвучал и ждёт КОНКРЕТНОГО ответа,
# нет смысла держать его открытым так же долго, как открытый диалог вообще.
CONFIRMATION_TIMEOUT_SECONDS = 12.0

# Размер модели Whisper - настраивается через переменную окружения, чтобы
# менять точность/скорость без правки кода. "base" ощутимо путает слова
# (см. обсуждение с разработчиком - "темножить" вместо "умножить" и т.п.),
# "small" заметно точнее на русском при небольшом росте задержки на CPU.
# Если severity ошибок распознавания всё ещё мешает - попробуй "medium"
# (ощутимо медленнее на голом CPU, но точнее).
WHISPER_MODEL_SIZE = get("WHISPER_MODEL_SIZE", "whisper.model_size", "small").strip()
WHISPER_DEVICE = get("WHISPER_DEVICE", "whisper.device", "cpu").strip()
WHISPER_COMPUTE_TYPE = get("WHISPER_COMPUTE_TYPE", "whisper.compute_type", "int8").strip()

# Пороги для отсева "галлюцинаций" Whisper на тишине/шуме/щелчках.
NO_SPEECH_PROB_MAX = 0.6
AVG_LOGPROB_MIN = -1.0
COMPRESSION_RATIO_MAX = 2.4

# Раунд 11: мгновенный filler ("секунду"/"так"), пока LLM ещё думает - см.
# voice/streaming_speaker.py. Список через запятую, пустая переменная или
# явное "off" отключает filler совсем. Короткие бытовые слова-паразиты
# специально, а не длинные фразы - "секунду" звучит и синтезируется быстрее,
# чем "пожалуйста, подождите немного".
FILLER_PHRASES_RAW = get("FILLER_PHRASES", "filler.phrases", "Секунду.,Так.,Сейчас.").strip()
FILLER_DELAY_SECONDS = float(get("FILLER_DELAY_SECONDS", "filler.delay_seconds", 0.35))


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


def speak_full_text(speaker: StreamingSpeaker, text: str):
    """Озвучивает ГОТОВЫЙ текст целиком одним куском - для случаев, где нет
    настоящего LLM-стрима (эхо-режим без dialog_manager, или сообщение
    таймера). Через тот же StreamingSpeaker, что и обычные LLM-ответы, чтобы
    MUTE/UNMUTE и защита от наложения звука были едиными во всём проекте."""
    speaker.start_response()
    speaker.enqueue_sentence(text)
    speaker.end_response()


def send_ui_event(socket_ui: zmq.Socket, event: dict):
    """Раунд 13: единая точка отправки в UI. Раньше сюда уходили голые
    строки вида "Джарвис: ответ" - UI не мог отличить роль (кто сказал),
    состояние (слушаю/думаю/говорю) или распознанный-но-проигнорированный
    текст от настоящего ответа - всё было одним потоком текста в лог.
    Теперь это всегда JSON с полем "type" - см. main.py (UI), где эти же
    типы разбираются:
      {"type": "state", "value": "idle" | "thinking" | "speaking"}
      {"type": "user_message", "text": "...", "heard": true|false}
      {"type": "assistant_message", "text": "..."}
      {"type": "tool_call", "name": "...", "args": {...}, "result": {...}}  (раунд 18, C4)
    Всё ещё вызывается ТОЛЬКО из transcribe_worker (один поток - единый
    владелец socket_ui) - см. main(), где socket_ui больше никем не
    используется на запись."""
    socket_ui.send_string(json.dumps(event, ensure_ascii=False))


def notify_tool_call(socket_ui: zmq.Socket, name: str, args: dict, result: dict):
    """C4 (audit-лог): единая точка логирования РЕАЛЬНОГО вызова инструмента
    в UI - используется и из dialog_manager.handle_streaming() (обычный путь
    через LLM), и из детерминированного пути подтверждения ниже (см. C3) -
    чтобы в UI оба пути выглядели одинаково, а не как две разные системы."""
    send_ui_event(socket_ui, {"type": "tool_call", "name": name, "args": args, "result": result})


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
                       dialog_manager: "DialogManager | None", speaker: StreamingSpeaker):
    """Работает в отдельном потоке: последовательно распознаёт фразы из очереди.

    Даже если одна фраза вызовет исключение, поток не падает - ошибка
    логируется, обработка продолжается со следующей фразы.

    dialog_manager может быть None (если не настроен LLM_API_KEY) - в этом
    случае сервер продолжает работать как чистый STT-эхо (этап 0), просто
    без LLM-мозга.
    """
    dialog = DialogState(ACTIVE_WINDOW_SECONDS)
    phrase_counter = 0
    # C3: {"tool_name":..., "tool_args":..., "created_at":...} между фразами -
    # выставляется, когда dialog_manager перехватил опасный вызов (см.
    # agents/confirmation.py), и разбирается на СЛЕДУЮЩЕЙ фразе ниже, ДО
    # обычной обработки wake-word/диалога.
    pending_confirmation: "dict | None" = None

    while True:
        item = audio_queue.get()
        if item is None:  # сигнал остановки
            break

        seq, capture_ts, raw_audio_bytes = item
        phrase_counter += 1
        temp_filename = None
        processing_started = time.time()

        # Раунд 13: "думаю" выставляется МАКСИМАЛЬНО рано - сразу после того,
        # как фраза вообще взята в обработку, ДО распознавания. Пользователь
        # только что замолчал - Whisper ещё даже не начал работать, но UI уже
        # честно показывает "что-то происходит", а не молчит как раньше.
        send_ui_event(socket_ui, {"type": "state", "value": "thinking"})

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

            # Раунд 27 (E1): словарь исправлений типичных ошибок Whisper -
            # СРАЗУ после распознавания, ДО intent_router/confirmation/LLM -
            # весь код ниже видит уже исправленный текст и не должен ничего
            # специально знать про STT-ошибки. См. config/stt_corrections.yaml.
            text = apply_corrections(text)

            processing_seconds = time.time() - processing_started
            if processing_seconds > LATE_RESULT_WARN_SECONDS:
                print(f"[ПРЕДУПРЕЖДЕНИЕ] Фраза #{seq} обрабатывалась {processing_seconds:.1f}с "
                      f"(медленно - STT может не успевать за темпом речи)")

            if not text:
                reason = "похоже на галлюцинацию (тишина/шум/щелчок)" if dropped_hallucination else "пусто"
                print(f"⚪ [Фраза #{seq}, {reason}]: распознавать нечего")
                continue

            # C3: если ждём да/нет на вопрос подтверждения - разбираем ЭТУ
            # фразу СНАЧАЛА, до обычной логики wake-word/диалога. Делается
            # ДЕТЕРМИНИРОВАННО (см. confirmation.match_confirmation_reply),
            # без обращения к LLM - вопрос уже задан, нужен только да/нет.
            if pending_confirmation is not None:
                age_seconds = time.time() - pending_confirmation["created_at"]
                if age_seconds > CONFIRMATION_TIMEOUT_SECONDS:
                    print(f"[Подтверждение] Истекло время ожидания ({age_seconds:.1f}с) - отменено.")
                    pending_confirmation = None
                    # Не continue - фраза, которая пришла ПОСЛЕ истечения
                    # окна, не обязана быть ответом на уже неактуальный
                    # вопрос, обрабатываем её ниже как обычную новую реплику.
                else:
                    reply = match_confirmation_reply(text)
                    if reply is not None:
                        tool_name = pending_confirmation["tool_name"]
                        tool_args = pending_confirmation["tool_args"]
                        pending_confirmation = None
                        send_ui_event(socket_ui, {"type": "user_message", "text": text, "heard": True})
                        send_ui_event(socket_ui, {"type": "state", "value": "speaking"})

                        if reply:
                            print(f"🔴 [Подтверждено] Выполняю: {tool_name}")
                            impl = TOOL_IMPLEMENTATIONS.get(tool_name)
                            tool_result = impl(**tool_args) if impl else {"error": "инструмент исчез"}
                            notify_tool_call(socket_ui, tool_name, tool_args, tool_result)
                            answer = str(tool_result.get("result") or tool_result.get("error") or "Готово.")
                        else:
                            print(f"⚪ [Отменено] Не выполняю: {tool_name}")
                            notify_tool_call(socket_ui, tool_name, tool_args,
                                              {"status": "cancelled_by_user"})
                            answer = "Хорошо, отменил."

                        speak_full_text(speaker, answer)
                        send_ui_event(socket_ui, {"type": "assistant_message", "text": answer})
                        send_ui_event(socket_ui, {"type": "state", "value": "idle"})
                        dialog.activate(time.time())
                        continue
                    # reply is None - неоднозначный ответ ("угу наверное") ИЛИ
                    # явно новая, не связанная реплика. Безопасный дефолт -
                    # НЕ выполнять опасное действие на неуверенном основании -
                    # сбрасываем ожидание и обрабатываем фразу как обычную
                    # ниже (тот же принцип, что и в intent_router: "уверен
                    # или не лезу").
                    pending_confirmation = None

            has_wake_word, matched_word = is_wake_word_present(text)

            if has_wake_word:
                command = strip_wake_word(text, matched_word) or text
                # Была ли уже активна сессия ДО этого произнесения "Джарвис"?
                # Если нет - это начало нового разговора, и LLM-историю нужно
                # сбросить, чтобы не тащить в контекст обрывки предыдущей,
                # никак не связанной беседы получасовой давности.
                is_continuation = dialog.is_active(capture_ts)
                if not is_continuation and dialog_manager:
                    dialog_manager.reset()

                print(f"\n🟢 [Джарвис #{seq}] распознано: {text!r}")
                # Показываем распознанный текст в UI СРАЗУ - до ответа, а не
                # склеенным вместе с ним в конце (см. аудит: "нет отображения
                # распознанного текста до ответа"). heard=True - фраза
                # адресована Джарвису и будет обработана.
                send_ui_event(socket_ui, {"type": "user_message", "text": text, "heard": True})

                # Активируем окно ДО ответа тоже (не только после) - иначе
                # если пользователь перебьёт Джарвиса на середине ответа без
                # повтора "Джарвис", эта фраза попала бы в "else: игнор", а
                # не в "продолжение диалога", хотя пользователь явно
                # продолжает ТЕКУЩИЙ разговор, а не начинает новый.
                dialog.activate(time.time())

                send_ui_event(socket_ui, {"type": "state", "value": "speaking"})

                # Раунд 12: сначала пробуем "мгновенный" путь мимо LLM -
                # громкость/медиа/таймер/открыть приложение. Возвращает None
                # при малейшей неуверенности - тогда просто идём дальше по
                # ОБЫЧНОМУ пути, как будто router'а не было вовсе (см.
                # agents/intent_router.py). Это же работает ДАЖЕ если LLM не
                # настроена (dialog_manager is None) - закрывает часть
                # "нет локального fallback-мозга" из аудита.
                instant_answer = intent_router.try_match(command)
                if instant_answer is not None:
                    answer = instant_answer
                    speak_full_text(speaker, answer)
                elif dialog_manager:
                    speaker.start_response()
                    try:
                        answer = dialog_manager.handle_streaming(
                            command,
                            on_sentence_ready=speaker.enqueue_sentence,
                            should_abort=speaker.should_abort,
                            on_tool_call=lambda name, args, result: notify_tool_call(socket_ui, name, args, result),
                        )
                    finally:
                        speaker.end_response()
                    # C3: если dialog_manager перехватил опасный вызов (см.
                    # agents/confirmation.py) - запоминаем, ЧТО именно ждёт
                    # подтверждения, и КОГДА был задан вопрос (для тайм-аута).
                    # answer в этом случае - уже сам детерминированный вопрос
                    # ("Точно...?"), озвучивать его отдельно не нужно.
                    if dialog_manager.pending_confirmation is not None:
                        pending_confirmation = dict(dialog_manager.pending_confirmation)
                        pending_confirmation["created_at"] = time.time()
                else:
                    answer = command
                    speak_full_text(speaker, answer)

                send_ui_event(socket_ui, {"type": "assistant_message", "text": answer})
                # Продлеваем окно ЕЩЁ раз ПОСЛЕ ответа (текущим временем) -
                # распознавание+LLM+озвучка вместе могут съесть несколько
                # секунд, и пользователю нужны полные N секунд ПОСЛЕ того,
                # как он услышал ответ, а не с момента вопроса.
                dialog.activate(time.time())
            elif dialog.is_active(capture_ts):
                # Активное окно после недавнего wake-word (или мы всё ещё
                # внутри ответа/только что его закончили - см. комментарий
                # выше) - считаем фразу командой, даже если имя не
                # повторили (перебивание/продолжение диалога).
                print(f"\n🟡 [Команда в диалоге #{seq}]: {text!r}")
                send_ui_event(socket_ui, {"type": "user_message", "text": text, "heard": True})
                dialog.activate(time.time())

                send_ui_event(socket_ui, {"type": "state", "value": "speaking"})
                instant_answer = intent_router.try_match(text)
                if instant_answer is not None:
                    answer = instant_answer
                    speak_full_text(speaker, answer)
                elif dialog_manager:
                    speaker.start_response()
                    try:
                        answer = dialog_manager.handle_streaming(
                            text,
                            on_sentence_ready=speaker.enqueue_sentence,
                            should_abort=speaker.should_abort,
                            on_tool_call=lambda name, args, result: notify_tool_call(socket_ui, name, args, result),
                        )
                    finally:
                        speaker.end_response()
                    if dialog_manager.pending_confirmation is not None:
                        pending_confirmation = dict(dialog_manager.pending_confirmation)
                        pending_confirmation["created_at"] = time.time()
                else:
                    answer = text
                    speak_full_text(speaker, answer)

                send_ui_event(socket_ui, {"type": "assistant_message", "text": answer})
                dialog.activate(time.time())  # см. комментарий выше
            else:
                print(f"\n⚪ [Игнор #{seq}]: {text!r}")
                send_ui_event(socket_ui, {"type": "user_message", "text": text, "heard": False})

        except Exception:
            print(f"[ОШИБКА] Не удалось обработать фразу #{seq}:")
            traceback.print_exc()
        finally:
            # Раунд 13: ЧТО БЫ ни случилось с этой фразой - штатно завершилась,
            # упала с исключением, или текст оказался пуст ("continue" внутри
            # try выше тоже проходит через этот finally) - состояние UI
            # ГАРАНТИРОВАННО возвращается в "слушаю". Без этого один же
            # необработанный сбой навсегда заморозил бы индикатор на "думаю"
            # или "говорю", и пользователь не понял бы, что сервер вообще-то
            # жив и снова готов слушать.
            send_ui_event(socket_ui, {"type": "state", "value": "idle"})
            if temp_filename and os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except OSError:
                    pass
            audio_queue.task_done()


def ui_command_listener(socket_ui_commands: zmq.Socket, speaker: StreamingSpeaker):
    """Раунд 13: слушает команды ОТ UI (кнопка "прервать" - см. main.py).

    Единственная команда пока - {"type": "interrupt"}: пользователь нажал
    кнопку в UI, а не сказал что-то вслух. Дёргаем ТОТ ЖЕ speaker.abort(),
    что и обычный голосовой barge-in (см. main(): speaker.abort() на каждую
    входящую фразу от C++ ядра) - кнопка это просто ещё один СПОСОБ вызвать
    ровно то же самое прерывание, не отдельная логика.

    Работает в своём потоке и держит СВОЙ сокет (socket_ui_commands) -
    никогда не трогает socket_ui (тот - только для transcribe_worker)."""
    while True:
        try:
            raw = socket_ui_commands.recv_string()
        except zmq.ZMQError:
            break  # контекст закрывается при остановке сервера - выходим тихо

        try:
            command = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue

        if command.get("type") == "interrupt":
            print("[UI] Получена команда 'прервать' из интерфейса.")
            speaker.abort()


def main():
    print(f"[Python] Загрузка модели Whisper ({WHISPER_MODEL_SIZE})...")
    model = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
    print("[Python] Модель готова!")

    try:
        dialog_manager = DialogManager()
        print("[Python] LLM-слой готов.")
    except DialogManagerError as e:
        dialog_manager = None
        print(f"[Python] LLM-слой ОТКЛЮЧЁН: {e}")
        print("[Python] Сервер продолжит работать в режиме чистого STT (без LLM-ответов).")

    context = zmq.Context()
    socket_audio = context.socket(zmq.PAIR)
    socket_audio.bind("tcp://127.0.0.1:5555")

    socket_ui = context.socket(zmq.PAIR)
    socket_ui.bind("tcp://127.0.0.1:5556")

    # Раунд 13: ОТДЕЛЬНЫЙ сокет для команд ОТ UI К серверу (кнопка
    # "прервать") - специально не пытаемся слать/принимать в разных потоках
    # через ОДИН И ТОТ ЖЕ socket_ui (он уже занят потоком transcribe_worker
    # только на отправку) - у zmq-сокетов нет гарантии потокобезопасности
    # при использовании из нескольких потоков одновременно. Поэтому: один
    # сокет - один поток-владелец, как и с остальными каналами в проекте.
    socket_ui_commands = context.socket(zmq.PAIR)
    socket_ui_commands.bind("tcp://127.0.0.1:5558")

    try:
        tts = TextToSpeech()
        control = ControlChannel(context)
        print("[Python] Голос (Piper TTS) готов.")
    except TTSError as e:
        tts, control = None, None
        print(f"[Python] Голос ОТКЛЮЧЁН: {e}")
        print("[Python] Сервер продолжит работать без озвучки (только текст в UI).")

    # Раунд 11: filler-фразы синтезируются ЗАРАНЕЕ, один раз, при старте -
    # если синтезировать их "на лету" в момент, когда как раз не хватает
    # времени (LLM ещё не ответила), это свело бы на нет весь смысл filler'а.
    # off/пустая строка/отсутствие TTS - filler просто выключен, ничего не
    # ломается (StreamingSpeaker одинаково хорошо работает с filler_audio=[]).
    filler_audio = []
    if tts and FILLER_PHRASES_RAW and FILLER_PHRASES_RAW.lower() != "off":
        phrases = [p.strip() for p in FILLER_PHRASES_RAW.split(",") if p.strip()]
        for phrase in phrases:
            try:
                audio = tts.synthesize(phrase)
                if audio is not None:
                    filler_audio.append(audio)
            except Exception as e:
                print(f"[Python] Не удалось предсинтезировать filler {phrase!r}: {e!r}")
        if filler_audio:
            print(f"[Python] Filler готов ({len(filler_audio)} фраз, "
                  f"задержка {FILLER_DELAY_SECONDS}с): {phrases}")

    print("[Python] Жду звук от C++ ядра...")

    # StreamingSpeaker - единая точка озвучки для ВСЕГО: обычных ответов LLM
    # (по предложениям, по мере готовности - раунд 10), сообщений от
    # таймеров, и эхо-режима без LLM. Один поток внутри него - значит
    # наложение звука друг на друга физически невозможно (не нужен внешний
    # tts_lock, как было раньше).
    speaker = StreamingSpeaker(tts, control, filler_audio=filler_audio,
                                filler_delay_seconds=FILLER_DELAY_SECONDS)

    # Регистрируем "уведомитель" для set_timer (tools/timers.py) - когда
    # таймер срабатывает в СВОЁМ отдельном потоке (threading.Timer), он
    # зовёт эту функцию через тот же speaker, что и обычные ответы.
    register_timer_notifier(lambda message: speak_full_text(speaker, message))

    # Раунд 25 (C6): восстанавливаем таймеры, пережившие предыдущий
    # запуск (падение процесса или перезапуск watchdog'ом, см. раунд 19,
    # scripts/start.sh) - ОБЯЗАТЕЛЬНО после register_timer_notifier() выше,
    # иначе просроченному таймеру, который сработает почти сразу же,
    # некому будет позвонить.
    restored_count = load_pending_timers()
    if restored_count:
        print(f"[Python] Восстановлено таймеров после перезапуска: {restored_count}")

    audio_queue: "queue.Queue" = queue.Queue()
    worker = threading.Thread(
        target=transcribe_worker,
        args=(model, audio_queue, socket_ui, dialog_manager, speaker),
        daemon=True,
    )
    worker.start()

    ui_commands_thread = threading.Thread(
        target=ui_command_listener,
        args=(socket_ui_commands, speaker),
        daemon=True,
    )
    ui_commands_thread.start()

    received_count = 0
    try:
        while True:
            parts = socket_audio.recv_multipart()
            parsed = parse_audio_message(parts)

            if parsed["kind"] == "invalid":
                print(f"[ZMQ] Пропускаю сообщение неожиданного формата ({parsed['reason']})")
                continue

            if parsed["kind"] == "signal":
                # Раунд 22 (B3): лёгкая обратная связь в UI ДО того, как
                # фраза целиком закончится и уйдёт на распознавание - см.
                # PhraseSender.h за подробностями протокола.
                signal_type = parsed["signal_type"]
                if signal_type == "speech_started":
                    send_ui_event(socket_ui, {"type": "state", "value": "listening_active"})
                elif signal_type == "speech_discarded":
                    # Фраза оказалась слишком короткой и была отброшена ядром
                    # целиком - полной фразы вслед за сигналом не будет,
                    # значит вернуть "слушаю" нужно ЯВНО, иначе UI застряла
                    # бы в "слушаю вас" до следующей настоящей фразы.
                    send_ui_event(socket_ui, {"type": "state", "value": "idle"})
                # Неизвестный signal_type - тихо игнорируем (вперёд совместимо
                # с будущими типами сигналов от ядра).
                continue

            # parsed["kind"] == "phrase"
            seq = parsed["seq"]
            capture_ts = parsed["ts"]
            raw_audio_bytes = parsed["audio"]

            # Раунд 10 (barge-in): любая новая фраза, дошедшая от C++ ядра,
            # означает, что пользователь что-то сказал достаточно уверенно,
            # чтобы пройти повышенный порог VAD во время речи Джарвиса (см.
            # main.cpp) - abort() безопасно вызывать всегда, даже если
            # Джарвис сейчас молчит: тогда это просто no-op (см.
            # StreamingSpeaker.abort). Если же он как раз говорил - его
            # прервёт немедленно, а не после того как он доскажет мысль.
            speaker.abort()

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
        socket_ui_commands.close()
        if control:
            control.close()
        context.term()


if __name__ == "__main__":
    main()
