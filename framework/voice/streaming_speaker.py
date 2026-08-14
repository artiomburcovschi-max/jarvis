"""
Раунд 10: озвучивает ответ LLM по предложениям, по мере готовности, а не
дожидаясь всего ответа целиком - первая фраза начинает звучать, пока LLM
ещё генерирует продолжение (см. agents/dialog_manager.py: handle_streaming).

Также даёт настоящее прерывание (barge-in): если пользователь начал
говорить, пока Джарвис ещё договаривает ответ, вызывающий код зовёт abort() -
текущее воспроизведение останавливается НЕМЕДЛЕННО (см. TextToSpeech.stop() -
подтверждено по исходникам sounddevice, что stop() из другого потока
прерывает блокирующий speak() без задержки), а все ещё не озвученные
предложения в очереди отбрасываются, не озвучиваясь.

Раунд 11: мгновенный filler ("секунду" / короткий звук) сразу после конца
фразы, пока STT уже определил команду, а LLM ещё думает. Главная причина
ощущения "мёртвой паузы" в живом разговоре - не сам факт задержки, а полная
тишина в это время: пользователь не знает, услышали его вообще или нет.
Если реальный ответ LLM не успел прийти за FILLER_DELAY_SECONDS - в очередь
озвучки подставляется ЗАРАНЕЕ закэшированное (см. TextToSpeech.synthesize(),
server.py) короткое аудио - без обращения к синтезу в этот самый момент,
когда и так уже не хватает времени. Если реальный ответ успевает раньше
таймера - filler вообще не звучит, никакой лишней задержки для быстрых
ответов не добавляется.
"""

import queue
import random
import threading
import traceback


class _FillerItem:
    """Маркер в очереди озвучки: уже готовое аудио (см. TextToSpeech.play_audio()),
    без обращения к синтезу - в отличие от обычных строк-предложений, которые
    _run() синтезирует через tts.speak(). Отдельный тип, а не просто пустая
    строка/None, чтобы _run() не путал filler с "конец ответа" (None) или с
    обычным пустым предложением."""

    __slots__ = ("audio",)

    def __init__(self, audio):
        self.audio = audio


class StreamingSpeaker:
    def __init__(self, tts, control, filler_audio: "list | None" = None,
                 filler_delay_seconds: float = 0.35):
        self._tts = tts
        self._control = control
        # Раунд 11: заранее засинтезированные короткие фразы (см. server.py,
        # TextToSpeech.synthesize()) - пустой список означает "filler выключен"
        # (например, TTS вообще не настроен, или список специально пуст).
        self._filler_audio = list(filler_audio) if filler_audio else []
        self._filler_delay_seconds = filler_delay_seconds
        self._filler_timer: "threading.Timer | None" = None
        self._content_started = threading.Event()  # первое РЕАЛЬНОЕ предложение уже в очереди?
        self._queue: "queue.Queue" = queue.Queue()
        self._abort_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _cancel_filler_timer(self):
        if self._filler_timer is not None:
            self._filler_timer.cancel()
            self._filler_timer = None

    def _maybe_enqueue_filler(self):
        """Вызывается ПО ТАЙМЕРУ, спустя filler_delay_seconds после начала
        ответа. Если к этому моменту реальный текст ещё не подоспел (и
        ответ не был отменён перебиванием) - подставляем короткую готовую
        фразу, чтобы не тянуть полную тишину, пока LLM всё ещё генерирует
        первое предложение."""
        if self._content_started.is_set() or self._abort_event.is_set():
            return
        self._queue.put(_FillerItem(random.choice(self._filler_audio)))

    def start_response(self):
        """Вызывать один раз в начале ответа, до первого enqueue_sentence."""
        self._abort_event.clear()
        self._content_started.clear()
        if self._control:
            self._control.send_mute()
        if self._filler_audio:
            self._filler_timer = threading.Timer(self._filler_delay_seconds, self._maybe_enqueue_filler)
            self._filler_timer.daemon = True
            self._filler_timer.start()

    def enqueue_sentence(self, sentence: str):
        """Кладёт готовое предложение в очередь на озвучку. Если ответ уже
        прерван - молча ничего не делает (не копим то, что не озвучится)."""
        sentence = sentence.strip()
        if not sentence or self._abort_event.is_set():
            return
        # Реальный текст уже пошёл - filler по таймеру (если ещё не сработал)
        # больше не нужен, отменяем его немедленно.
        self._content_started.set()
        self._cancel_filler_timer()
        self._queue.put(sentence)

    def end_response(self):
        """Вызывать один раз, когда LLM закончила генерацию (или поток был
        прерван) - сигнализирует, что больше предложений не будет."""
        self._cancel_filler_timer()
        self._queue.put(None)

    def abort(self):
        """Прерывает текущее воспроизведение и всё, что ещё не озвучено.

        Безопасно вызывать в ЛЮБОЙ момент, даже если сейчас ничего не
        звучит - тогда это просто no-op. Именно поэтому receive-цикл в
        server.py может звать это на КАЖДУЮ входящую фразу без проверки
        "а точно ли сейчас идёт ответ" - лишний вызов ничего не ломает.
        """
        self._abort_event.set()
        self._cancel_filler_timer()
        if self._tts:
            self._tts.stop()
        if self._control:
            self._control.send_unmute()  # не ждём own end_response - разговор явно уже не наш
        # Чистим очередь от всего, что ещё не озвучено
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def should_abort(self) -> bool:
        return self._abort_event.is_set()

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                if self._control:
                    self._control.send_unmute()
                continue
            if self._abort_event.is_set():
                continue  # прервано между постановкой в очередь и озвучкой
            if self._tts:
                try:
                    if isinstance(item, _FillerItem):
                        self._tts.play_audio(item.audio)
                    else:
                        self._tts.speak(item)
                except Exception:
                    print("[TTS] Ошибка синтеза/воспроизведения:")
                    traceback.print_exc()

