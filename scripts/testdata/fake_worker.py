"""Фейковый долгоживущий процесс для теста watchdog-цикла в start.sh.

Использование: python3 fake_worker.py <name> <events_file> [pad_bytes]

При старте дописывает строку "start <name> <pid>" в events_file. При
получении SIGTERM дописывает "stop <name> <pid>" и завершается кодом 0.
Никакой реальной работы не делает — только живёт, пока его не остановят,
и оставляет след, по которому тест может отличить "тот же процесс" от
"перезапущенный".

Если передан необязательный третий аргумент pad_bytes (> 0) - процесс
дополнительно печатает в stdout строку такой длины каждые ~10мс. Это
нужно только тесту ротации логов (A3) - start.sh перенаправляет stdout
каждого процесса в logs/<name>.log, и без чего-то, что реально пишет в
этот файл, его нечем "нагонять" до порога ротации.
"""
import os
import signal
import sys
import time


def main() -> None:
    name = sys.argv[1]
    events_file = sys.argv[2]
    pad_bytes = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    with open(events_file, "a") as f:
        f.write(f"start {name} {os.getpid()}\n")
        f.flush()

    def handle_term(signum, frame):  # noqa: ANN001
        with open(events_file, "a") as f:
            f.write(f"stop {name} {os.getpid()}\n")
            f.flush()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_term)

    padding = ("x" * pad_bytes) if pad_bytes > 0 else None
    while True:
        if padding is not None:
            print(padding, flush=True)
        time.sleep(0.01)


if __name__ == "__main__":
    main()
