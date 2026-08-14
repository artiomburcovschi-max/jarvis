"""
Управляющий канал Python -> C++ ядро: MUTE/UNMUTE вокруг воспроизведения
TTS-ответа.

Зачем: как только Джарвис начинает говорить через динамик, микрофон
(особенно если физически недалеко от колонки) начнёт слышать его же
собственный голос. Полноценное эхоподавление (AEC) - отдельная большая
задача, отложенная на будущее (см. README). Для этапа 2 делаем простое и
надёжное: на время озвучки ответа C++ ядро полностью игнорирует микрофон
(грубый полудуплекс), а как только звук закончился - снова слушает.

Протокол - тот же принцип, что и остальные каналы (Python - хаб, C++
подключается): PAIR-сокет, Python bind, C++ connect. Сообщения - простые
строки "MUTE"/"UNMUTE".
"""

import zmq

DEFAULT_ENDPOINT = "tcp://127.0.0.1:5557"


class ControlChannel:
    def __init__(self, context: zmq.Context, endpoint: str = DEFAULT_ENDPOINT):
        self._socket = context.socket(zmq.PAIR)
        self._socket.bind(endpoint)

    def send_mute(self):
        self._socket.send_string("MUTE")

    def send_unmute(self):
        self._socket.send_string("UNMUTE")

    def close(self):
        self._socket.close()
