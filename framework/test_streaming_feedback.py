"""Тесты для раунда 22 (B3) - лёгкая обратная связь "слушаю вас" ДО того,
как фраза целиком закончится и распознается.

Две независимые чистые функции, обе вынесены специально, чтобы их можно
было протестировать без поднятия реальных zmq-сокетов и без Qt:
  - audio_protocol.parse_audio_message() - разбор multipart-сообщения от core
    (обычная фраза (2 фрейма) vs сигнал (1 фрейм) - "speech_started"/
    "speech_discarded", см. PhraseSender.h);
  - ui_state.is_interruptible() - в каких состояниях кнопка "прервать" активна.

Живой сквозной прогон реального (скомпилированного) core/PhraseSender.h
против audio_protocol.parse_audio_message() был сделан вручную при
разработке этого раунда (см. README, раздел "Раунд 22") - здесь только
модульные тесты чистой логики, как и для всего остального в проекте.
"""
import json
import time

import audio_protocol
from ui_state import is_interruptible, STATE_LABELS, INTERRUPTIBLE_STATES


# --- audio_protocol.parse_audio_message() -----------------------------------


def test_phrase_message_parsed_correctly():
    header = json.dumps({"seq": 5, "ts": 111.5, "type": "phrase"}).encode("utf-8")
    audio = b"\x01\x02\x03\x04"

    result = audio_protocol.parse_audio_message([header, audio])

    assert result == {"kind": "phrase", "seq": 5, "ts": 111.5, "audio": audio}


def test_speech_started_signal_parsed_correctly():
    header = json.dumps({"seq": 7, "ts": 222.0, "type": "speech_started"}).encode("utf-8")

    result = audio_protocol.parse_audio_message([header])

    assert result["kind"] == "signal"
    assert result["signal_type"] == "speech_started"
    assert result["seq"] == 7
    assert result["ts"] == 222.0


def test_speech_discarded_signal_parsed_correctly():
    header = json.dumps({"seq": 8, "ts": 333.0, "type": "speech_discarded"}).encode("utf-8")

    result = audio_protocol.parse_audio_message([header])

    assert result["kind"] == "signal"
    assert result["signal_type"] == "speech_discarded"


def test_old_core_without_type_field_still_treated_as_phrase():
    # Обратная совместимость: если бы core был старой версии (без "type" в
    # заголовке, до раунда 22) - 2-фреймовое сообщение всё равно должно
    # разбираться как обычная фраза, просто "type" не будет использован.
    header = json.dumps({"seq": 1, "ts": 1.0}).encode("utf-8")
    audio = b"\x00\x00"

    result = audio_protocol.parse_audio_message([header, audio])

    assert result["kind"] == "phrase"
    assert result["seq"] == 1


def test_unknown_frame_count_is_invalid():
    result = audio_protocol.parse_audio_message([b"a", b"b", b"c"])
    assert result["kind"] == "invalid"
    assert "unexpected_frame_count" in result["reason"]


def test_empty_audio_frame_is_invalid():
    header = json.dumps({"seq": 1, "ts": 1.0, "type": "phrase"}).encode("utf-8")
    result = audio_protocol.parse_audio_message([header, b""])
    assert result["kind"] == "invalid"
    assert result["reason"] == "empty_audio"


def test_malformed_signal_header_is_invalid_not_a_crash():
    result = audio_protocol.parse_audio_message(["не json совсем".encode("utf-8")])
    assert result["kind"] == "invalid"
    assert result["reason"] == "bad_signal_header"


def test_malformed_phrase_header_falls_back_to_now(monkeypatch):
    # Битый заголовок у ПОЛНОЙ фразы (2 фрейма) не должен ронять сервер -
    # раньше эта ветка (см. старый server.py) тоже так себя вела.
    fixed_now = 999.0
    monkeypatch.setattr(time, "time", lambda: fixed_now)

    result = audio_protocol.parse_audio_message(["{не json".encode("utf-8"), b"\x01\x02"])

    assert result["kind"] == "phrase"
    assert result["seq"] == -1
    assert result["ts"] == fixed_now


def test_missing_seq_and_ts_default_gracefully():
    header = json.dumps({"type": "speech_started"}).encode("utf-8")
    result = audio_protocol.parse_audio_message([header])
    assert result["seq"] == -1
    assert isinstance(result["ts"], float)


# --- main.is_interruptible() ------------------------------------------------


def test_idle_is_not_interruptible():
    assert is_interruptible("idle") is False


def test_listening_active_is_not_interruptible():
    # Ключевой случай для этого раунда: пока пользователь просто говорит
    # (Джарвис молчит), кнопка "прервать" не должна включаться - раньше
    # (до этого раунда) логика была "state != idle", и это состояние тоже
    # ошибочно включило бы кнопку.
    assert is_interruptible("listening_active") is False


def test_thinking_and_speaking_are_interruptible():
    assert is_interruptible("thinking") is True
    assert is_interruptible("speaking") is True


def test_unknown_state_is_not_interruptible():
    assert is_interruptible("some_future_state") is False


def test_all_state_labels_have_an_interruptibility_verdict():
    # Каждое состояние, у которого есть подпись/цвет в UI, должно давать
    # определённый (не падающий) ответ от is_interruptible - защита от
    # будущего "добавили состояние в STATE_LABELS, забыли про кнопку".
    for state in STATE_LABELS:
        assert is_interruptible(state) in (True, False)


def test_interruptible_states_is_a_subset_of_known_labels():
    assert INTERRUPTIBLE_STATES.issubset(set(STATE_LABELS))
