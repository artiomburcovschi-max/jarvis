"""
Jarvis UI (раунд 13).

Раньше: статус-лейбл + один QTextEdit-лог, куда сваливались голые строки
("Джарвис: ответ", "Вы: текст") без разделения на роли и без состояния
"что сейчас происходит" - см. аудит: "UI - лог, не собеседник".

Теперь UI разбирает СТРУКТУРИРОВАННЫЙ протокол от server.py (см. там же,
send_ui_event() и докстринг модуля, раздел 6):
  {"type": "state", "value": "idle" | "listening_active" | "thinking" | "speaking"}
  {"type": "user_message", "text": "...", "heard": true|false}
  {"type": "assistant_message", "text": "..."}
  {"type": "tool_call", "name": "...", "args": {...}, "result": {...}}  (раунд 18, C4)

"listening_active" (раунд 22, B3) - VAD в ядре увидел начало речи, ЕЩЁ до
того, как фраза закончится и распознается.

И умеет отправлять команды ОБРАТНО серверу по ОТДЕЛЬНОМУ каналу (порт 5558,
см. server.py: ui_command_listener) - пока одна: "прервать" (кнопка).
Специально ДВА разных сокета в разные стороны, а не один PAIR туда-обратно
из разных потоков - см. комментарий в server.py про потокобезопасность zmq.
"""

import sys
import json
import time

import zmq
from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QVBoxLayout, QHBoxLayout,
    QWidget, QTextEdit, QPushButton,
)

from ui_state import STATE_LABELS, DEFAULT_STATE, is_interruptible

STATE_UI_ENDPOINT = "tcp://127.0.0.1:5556"
COMMANDS_ENDPOINT = "tcp://127.0.0.1:5558"



class ZMQListener(QThread):
    """Слушает СОСТОЯНИЯ И СООБЩЕНИЯ от сервера (порт 5556, только приём -
    отправка команд идёт через отдельный сокет, см. CommandSender ниже)."""

    event_received = Signal(dict)
    connection_lost = Signal(str)

    def run(self):
        context = zmq.Context()
        socket = context.socket(zmq.PAIR)
        socket.connect(STATE_UI_ENDPOINT)

        while True:
            try:
                raw = socket.recv_string()
            except zmq.ZMQError as e:
                self.connection_lost.emit(str(e))
                time.sleep(1)
                continue

            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                # Не роняем UI на неожиданном формате - показываем как есть,
                # завёрнутым в псевдо-событие, чтобы не потерять сообщение.
                event = {"type": "assistant_message", "text": raw}
            self.event_received.emit(event)


class CommandSender:
    """Отправляет команды УПРАВЛЕНИЯ серверу (кнопка "прервать" и т.п.) -
    порт 5558, ТОЛЬКО отправка. Живёт в GUI-потоке (в отличие от
    ZMQListener) - отправка редких коротких сообщений по локальному loopback
    не блокирует интерфейс заметным образом, отдельный поток не нужен."""

    def __init__(self):
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.PAIR)
        self._socket.connect(COMMANDS_ENDPOINT)

    def send_interrupt(self):
        try:
            self._socket.send_string(json.dumps({"type": "interrupt"}), flags=zmq.NOBLOCK)
        except zmq.ZMQError:
            pass  # сервер временно недоступен - не блокируем UI и не падаем

    def close(self):
        self._socket.close()
        self._context.term()


class JarvisUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Jarvis")
        self.resize(560, 480)

        self.status_label = QLabel(self)
        self.status_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 6px;")

        self.interrupt_button = QPushButton("⏹ Прервать", self)
        self.interrupt_button.setEnabled(False)  # включается, только пока идёт "думаю"/"говорю"
        self.interrupt_button.clicked.connect(self.on_interrupt_clicked)

        top_row = QHBoxLayout()
        top_row.addWidget(self.status_label, stretch=1)
        top_row.addWidget(self.interrupt_button)

        self.history = QTextEdit(self)
        self.history.setReadOnly(True)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addWidget(self.history)

        central_widget = QWidget()
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)

        self._set_state(DEFAULT_STATE)

        self.command_sender = CommandSender()

        self.zmq_thread = ZMQListener()
        self.zmq_thread.event_received.connect(self.handle_event)
        self.zmq_thread.connection_lost.connect(self.handle_connection_lost)
        self.zmq_thread.start()

    # --- обработка событий от сервера ---------------------------------

    @Slot(dict)
    def handle_event(self, event: dict):
        event_type = event.get("type")
        if event_type == "state":
            self._set_state(event.get("value", DEFAULT_STATE))
        elif event_type == "user_message":
            self._append_user_message(event.get("text", ""), heard=event.get("heard", True))
        elif event_type == "assistant_message":
            self._append_assistant_message(event.get("text", ""))
        elif event_type == "tool_call":
            self._append_tool_call(event.get("name", "?"), event.get("args", {}), event.get("result", {}))
        # Неизвестные типы событий тихо игнорируем - вперёд совместимо с
        # будущими версиями сервера, которые могут добавить новые типы.

    @Slot(str)
    def handle_connection_lost(self, error_text: str):
        self.status_label.setText(f"⚠️ Соединение потеряно, переподключаюсь... ({error_text})")
        self.status_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 6px; color: #b71c1c;")
        self.interrupt_button.setEnabled(False)

    # --- UI-состояние ----------------------------------------------------

    def _set_state(self, state: str):
        label, color = STATE_LABELS.get(state, STATE_LABELS[DEFAULT_STATE])
        self.status_label.setText(label)
        self.status_label.setStyleSheet(f"font-size: 16px; font-weight: bold; padding: 6px; color: {color};")
        # Прерывать есть смысл, только пока Джарвис что-то делает - пока
        # "слушаю"/"слушаю вас" - прерывать нечего.
        self.interrupt_button.setEnabled(is_interruptible(state))

    def _append_user_message(self, text: str, heard: bool):
        text = _escape_html(text)
        if heard:
            html = f'<div style="margin:4px 0;"><b style="color:#1565c0;">Вы:</b> {text}</div>'
        else:
            # Услышано, но не адресовано Джарвису (нет wake-word/не в активном
            # окне) - показываем бледным, чтобы не путать с настоящей командой.
            html = (
                f'<div style="margin:4px 0; color:#9e9e9e;">'
                f'<i>(фоновая речь, не Джарвису):</i> {text}</div>'
            )
        self.history.append(html)

    def _append_assistant_message(self, text: str):
        text = _escape_html(text)
        html = f'<div style="margin:4px 0 10px 0;"><b style="color:#2e7d32;">Джарвис:</b> {text}</div>'
        self.history.append(html)

    def _append_tool_call(self, name: str, args: dict, result: dict):
        # C4 (audit-лог): показываем КАЖДЫЙ реальный вызов инструмента -
        # что именно сделал Джарвис на компьютере, а не только что он
        # СКАЗАЛ. Мелким моноширинным текстом, отдельно от основной реплики -
        # это техническая деталь для тех, кому интересно, а не часть
        # разговора.
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
        status = result.get("status")
        if "error" in result:
            icon, color, outcome = "⚠️", "#c62828", _escape_html(str(result["error"]))
        elif status == "requires_user_confirmation":
            icon, color, outcome = "⏸", "#f9a825", "ждёт подтверждения..."
        elif status == "cancelled_by_user":
            icon, color, outcome = "🚫", "#9e9e9e", "отменено пользователем"
        else:
            icon, color, outcome = "🔧", "#546e7a", _escape_html(str(result.get("result", "готово")))

        html = (
            f'<div style="margin:2px 0; font-family:monospace; font-size:12px; color:{color};">'
            f'{icon} {_escape_html(name)}({_escape_html(args_str)}) → {outcome}</div>'
        )
        self.history.append(html)

    # --- действия пользователя -------------------------------------------

    def on_interrupt_clicked(self):
        self.command_sender.send_interrupt()

    def closeEvent(self, event):
        self.command_sender.close()
        super().closeEvent(event)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = JarvisUI()
    window.show()
    sys.exit(app.exec())
