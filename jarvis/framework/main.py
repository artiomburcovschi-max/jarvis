import sys
import time

import zmq
from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QWidget, QTextEdit


class ZMQListener(QThread):
    message_received = Signal(str)
    connection_lost = Signal(str)

    def run(self):
        context = zmq.Context()
        socket = context.socket(zmq.PAIR)
        # ВАЖНО: connect, а не bind, и порт 5556!
        socket.connect("tcp://127.0.0.1:5556")

        while True:
            try:
                text = socket.recv_string()
                self.message_received.emit(text)
            except zmq.ZMQError as e:
                # Не даём потоку тихо умереть при разрыве соединения -
                # сообщаем в UI и пробуем переподключиться.
                self.connection_lost.emit(str(e))
                time.sleep(1)


class JarvisUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Jarvis Framework")
        self.resize(500, 350)

        self.status_label = QLabel("Ожидание ядра C++...", self)
        self.log = QTextEdit(self)
        self.log.setReadOnly(True)

        layout = QVBoxLayout()
        layout.addWidget(self.status_label)
        layout.addWidget(self.log)

        central_widget = QWidget()
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)

        self.zmq_thread = ZMQListener()
        self.zmq_thread.message_received.connect(self.update_log)
        self.zmq_thread.connection_lost.connect(self.update_status_error)
        self.zmq_thread.start()

    @Slot(str)
    def update_log(self, text):
        self.status_label.setText(f"Получена команда: {text}")
        self.log.append(text)

    @Slot(str)
    def update_status_error(self, error_text):
        self.status_label.setText(f"[Соединение потеряно, переподключаюсь] {error_text}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = JarvisUI()
    window.show()
    sys.exit(app.exec())
