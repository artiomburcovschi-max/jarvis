"""audio_protocol.py - разбор multipart-сообщений от C++ ядра (порт 5555).

Раунд 22 (B3): вынесено из server.py в отдельный модуль БЕЗ тяжёлых
зависимостей (только json/time, стандартная библиотека) - специально,
чтобы протокол можно было протестировать без zmq, faster-whisper и всего
остального, что тянет за собой server.py при импорте (см.
test_streaming_feedback.py).
"""
import json
import time


def parse_audio_message(parts: "list[bytes]") -> dict:
    """Разбирает multipart-сообщение от core (порт 5555, PhraseSender) в
    структуру - независимо от того, сигнал это (1 фрейм, без аудио, раунд 22
    B3) или полная фраза (2 фрейма).

    Возвращает dict с одним из "kind":
      {"kind": "signal", "signal_type": ..., "seq": ..., "ts": ...}
      {"kind": "phrase", "seq": ..., "ts": ..., "audio": bytes}
      {"kind": "invalid", "reason": "..."}
    """
    if len(parts) == 1:
        try:
            header = json.loads(parts[0].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"kind": "invalid", "reason": "bad_signal_header"}
        return {
            "kind": "signal",
            "signal_type": header.get("type"),
            "seq": header.get("seq", -1),
            "ts": header.get("ts", time.time()),
        }

    if len(parts) != 2:
        return {"kind": "invalid", "reason": f"unexpected_frame_count:{len(parts)}"}

    header_bytes, raw_audio_bytes = parts
    if not raw_audio_bytes:
        return {"kind": "invalid", "reason": "empty_audio"}

    try:
        header = json.loads(header_bytes.decode("utf-8"))
        seq = header.get("seq", -1)
        capture_ts = header.get("ts", time.time())
    except (ValueError, UnicodeDecodeError):
        # На случай рассинхронизации протокола (например, старая версия
        # C++ ядра без заголовков) - не роняем сервер, просто считаем,
        # что фраза только что произнесена.
        seq, capture_ts = -1, time.time()

    return {"kind": "phrase", "seq": seq, "ts": capture_ts, "audio": raw_audio_bytes}
