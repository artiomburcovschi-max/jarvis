"""
Синтез и воспроизведение речи через Piper (полностью локально, бесплатно).

Piper отдаёт аудио кусками (AudioChunk) с сырыми PCM16-сэмплами - собираем их
в один массив и проигрываем через sounddevice (обёртка над той же PortAudio,
что уже используется в C++ ядре для записи - общая нативная библиотека,
ничего лишнего не тянем).

Про выбор устройства вывода: специально НЕ выбираем конкретную колонку/устройство
в коде - sounddevice.play() без device= использует системное устройство
вывода по умолчанию, точно так же как AudioRecorder.cpp использует системное
устройство ВВОДА по умолчанию. Это значит: если в настройках звука ОС
default input = микрофон от наушников, а default output = колонка - всё
само маршрутизируется правильно без единой строчки кода про конкретные
устройства. Именно то разделение "микрофон отдельно, колонка отдельно",
которое обсуждали - оно уже работает на уровне ОС, а не нашего кода.
"""

from pathlib import Path

import numpy as np
import sounddevice as sd
from piper import PiperVoice, SynthesisConfig

from jarvis_config import get


class TTSError(Exception):
    """Ошибка конфигурации TTS (например, не найден файл голосовой модели)."""


class TextToSpeech:
    def __init__(self, model_path: str | None = None, config_path: str | None = None):
        model_path = model_path or get("PIPER_MODEL_PATH", "piper.model_path")
        if model_path:
            model_path = model_path.strip()  # та же защита от невидимого \n при копипасте
        if not model_path:
            raise TTSError(
                "Не найден PIPER_MODEL_PATH. Скачай голосовую модель Piper "
                "(см. README.md, раздел про голос) и укажи путь к .onnx файлу: "
                "export PIPER_MODEL_PATH=/путь/к/ru_RU-dmitri-medium.onnx"
            )
        if not Path(model_path).exists():
            raise TTSError(f"Файл голосовой модели не найден: {model_path}")

        self._voice = PiperVoice.load(model_path, config_path)
        self._sample_rate = self._voice.config.sample_rate
        self._syn_config = SynthesisConfig(
            volume=float(get("PIPER_VOLUME", "piper.volume", 1.0)),
            length_scale=float(get("PIPER_SPEED", "piper.speed", 1.0)),  # >1.0 = медленнее
        )

    def synthesize(self, text: str) -> "np.ndarray | None":
        """Только синтез, без воспроизведения - возвращает PCM16 numpy-массив
        или None для пустого текста. Нужен отдельно от speak(), чтобы можно
        было ЗАРАНЕЕ (при старте сервера) синтезировать короткие filler-фразы
        ("секунду", "сейчас" и т.п.) один раз и переиспользовать их аудио
        много раз без повторного обращения к Piper - см. server.py,
        FILLER_PHRASES. Без кэша каждый filler синтезировался бы заново
        прямо в момент, когда и так уже не хватает времени (это свело бы на
        нет весь смысл filler'а - мгновенно закрыть паузу тишины)."""
        text = text.strip()
        if not text:
            return None

        chunks = []
        for audio_chunk in self._voice.synthesize(text, syn_config=self._syn_config):
            chunks.append(np.frombuffer(audio_chunk.audio_int16_bytes, dtype=np.int16))

        if not chunks:
            return None

        return np.concatenate(chunks)

    def play_audio(self, audio: "np.ndarray | None"):
        """Воспроизводит УЖЕ готовый (например, закэшированный) PCM16-массив,
        блокируясь до конца звучания - без обращения к синтезу вообще."""
        if audio is None or len(audio) == 0:
            return
        sd.play(audio, samplerate=self._sample_rate, blocking=True)

    def speak(self, text: str):
        """Синтезирует речь и ВОСПРОИЗВОДИТ её, блокируясь до конца звучания.

        Блокирующий вызов - это осознанное решение для этапа 2 (полудуплекс):
        вызывающий код должен точно знать момент, когда озвучка закончилась,
        чтобы снять MUTE с микрофона (см. server.py). Не глотает исключения -
        вызывающий код сам решает, как реагировать на сбой TTS (см. try/finally
        вокруг MUTE/UNMUTE в server.py, чтобы микрофон не завис замьюченным
        навсегда, даже если синтез упал).
        """
        self.play_audio(self.synthesize(text))

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def stop(self):
        """Немедленно останавливает текущее воспроизведение (раунд 10:
        настоящее прерывание/barge-in). Безопасно вызывать в любой момент,
        даже если сейчас ничего не звучит - sd.stop(ignore_errors=True)
        просто ничего не делает в этом случае."""
        sd.stop(ignore_errors=True)
