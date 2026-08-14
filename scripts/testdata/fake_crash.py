"""Фейковый процесс, который падает сразу же — для теста защиты от
restart-шторма в start.sh (JARVIS_MAX_RESTARTS/JARVIS_RESTART_WINDOW).

Использование: python3 fake_crash.py <name> <events_file>
"""
import os
import sys


def main() -> None:
    name = sys.argv[1]
    events_file = sys.argv[2]
    with open(events_file, "a") as f:
        f.write(f"start {name} {os.getpid()}\n")
        f.flush()
    sys.exit(1)


if __name__ == "__main__":
    main()
