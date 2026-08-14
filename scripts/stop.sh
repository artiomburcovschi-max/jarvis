#!/usr/bin/env bash
#
# scripts/stop.sh — остановить watchdog из start.sh (и вместе с ним все три
# дочерних процесса), не переключаясь в терминал, где он запущен на
# переднем плане. Полезно при ручном запуске и как ExecStop в systemd-юните.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="${JARVIS_RUN_DIR:-$PROJECT_ROOT/run}"
PIDFILE="$RUN_DIR/watchdog.pid"

if [[ -f "$PIDFILE" ]]; then
    pid="$(cat "$PIDFILE")"
    if kill -0 "$pid" 2>/dev/null; then
        echo "Останавливаю watchdog (pid $pid)..."
        kill -TERM "$pid"
        exit 0
    fi
fi

echo "Watchdog не запущен (нет живого pid в $PIDFILE)."
exit 1
