#!/usr/bin/env bash
#
# scripts/start.sh — единая точка запуска Джарвиса (ядро + сервер + UI).
#
# Раунд 19 (A1-A2). Раньше три процесса запускались вручную в трёх
# терминалах, строго в таком порядке: server.py -> core -> main.py (см.
# README, "Как собрать и запустить"). Этот скрипт делает то же самое, плюс:
#
#   - следит, что все три процесса живы (watchdog-цикл, проверка раз в
#     JARVIS_POLL_INTERVAL секунд через `kill -0`);
#   - если ЛЮБОЙ из трёх падает — перезапускает ВСЕ ТРИ, а не только
#     упавший. Точечный рестарт одного процесса (например, только сервера)
#     сознательно не делаем: core общается с server по zmq на конкретных
#     сокетах (5555/5557), и если server поднимется "с нуля" с новым
#     контекстом, а core продолжит слать в старое соединение — есть шанс
#     тихо потерять фразы или зависнуть в неопределённом состоянии.
#     Перезапустить узел целиком — дороже по времени (доли секунды), зато
#     детерминированно. В духе принципа проекта "ложное срабатывание/
#     рассинхрон хуже, чем лишняя секунда ожидания";
#   - защита от restart-шторма: если перезапусков больше, чем
#     JARVIS_MAX_RESTARTS раз за JARVIS_RESTART_WINDOW секунд — скрипт
#     сдаётся и завершается с кодом 1, вместо того чтобы долбить перезапуск
#     впустую (типичный случай — ядро вообще не собрано, и без этой защиты
#     скрипт заспамил бы логи и CPU до ручной остановки);
#   - ротация логов (A3): раз в POLL_INTERVAL заодно с проверкой "живы ли
#     процессы" проверяется размер каждого файла в logs/ - если превышен
#     JARVIS_LOG_MAX_BYTES, файл архивируется (.log -> .log.1 -> .log.2 -> …
#     до JARVIS_LOG_BACKUPS штук) и обнуляется НА МЕСТЕ (copytruncate), без
#     перезапуска процесса-писателя - см. rotate_log() ниже, почему это
#     безопасно именно для файлов, открытых в append-режиме.
#
# Команды каждого процесса — в переменных CORE_CMD/SERVER_CMD/UI_CMD, по
# умолчанию реальные пути проекта. Переменные можно подменить (см.
# test_launch_watchdog.py) фейковыми процессами — так тестируется сама
# логика watchdog'а без реального железа, Whisper, Piper и т.п.
#
# Использование:
#   ./scripts/start.sh            # запуск на переднем плане (Ctrl+C — стоп)
#   ./scripts/stop.sh             # остановка из другого терминала
# Тот же скрипт используется как ExecStart в systemd-юните (см.
# deploy/systemd/jarvis.service) — второй уровень защиты: если сам этот
# скрипт умрёт целиком (а не один из трёх дочерних процессов), systemd
# перезапустит его снова (Restart=on-failure).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RUN_DIR="${JARVIS_RUN_DIR:-$PROJECT_ROOT/run}"
LOG_DIR="${JARVIS_LOG_DIR:-$PROJECT_ROOT/logs}"
mkdir -p "$RUN_DIR" "$LOG_DIR"

# Если есть venv в framework/venv (см. README) — используем его python.
VENV_ACTIVATE="$PROJECT_ROOT/framework/venv/bin/activate"
if [[ -f "$VENV_ACTIVATE" ]]; then
    # shellcheck disable=SC1090
    source "$VENV_ACTIVATE"
fi

CORE_DEFAULT="$PROJECT_ROOT/core/build/jarvis_core"
CORE_CMD="${CORE_CMD:-$CORE_DEFAULT}"
SERVER_CMD="${SERVER_CMD:-python3 $PROJECT_ROOT/framework/server.py}"
UI_CMD="${UI_CMD:-python3 $PROJECT_ROOT/framework/main.py}"

# Таймауты/пороги — все настраиваемые через env, без правки скрипта.
STARTUP_DELAY="${JARVIS_STARTUP_DELAY:-1.5}"   # пауза между стартом server и core+UI
POLL_INTERVAL="${JARVIS_POLL_INTERVAL:-2}"     # как часто проверяем, что все живы
MAX_RESTARTS="${JARVIS_MAX_RESTARTS:-5}"       # сколько рестартов терпим...
RESTART_WINDOW="${JARVIS_RESTART_WINDOW:-60}"  # ...за столько секунд, прежде чем сдаться

# Раунд 19 (A3) — ротация логов. Процессы держат файлы логов открытыми в
# режиме append на всё время своей жизни (что и через месяц работы под
# systemd), поэтому классический rename-based logrotate тут не подходит -
# используем copytruncate (см. rotate_log() ниже): проверяем размер раз в
# POLL_INTERVAL заодно с проверкой "живы ли процессы", архивируем и
# обнуляем файл ПО МЕСТУ, ничего не перезапуская.
LOG_MAX_BYTES="${JARVIS_LOG_MAX_BYTES:-5242880}"  # 5 МиБ по умолчанию
LOG_BACKUPS="${JARVIS_LOG_BACKUPS:-5}"            # сколько архивов *.log.N хранить

NAMES=(server core ui)
declare -A CMDS=([server]="$SERVER_CMD" [core]="$CORE_CMD" [ui]="$UI_CMD")
declare -A PIDS=()

log() {
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] $*" | tee -a "$LOG_DIR/watchdog.log"
}

preflight_check() {
    # Проверяем только дефолтный путь к бинарнику ядра — если CORE_CMD
    # подменена (тесты, кастомный запуск), эта проверка не мешает.
    if [[ "$CORE_CMD" == "$CORE_DEFAULT" && ! -x "$CORE_DEFAULT" ]]; then
        echo "Ошибка: не найден собранный core/build/jarvis_core." >&2
        echo "Сначала соберите ядро (см. README, раздел 'Как собрать и запустить')." >&2
        exit 1
    fi
}

is_alive() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start_one() {
    local name="$1"
    local cmd="${CMDS[$name]}"
    bash -c "$cmd" >>"$LOG_DIR/$name.log" 2>&1 &
    local pid=$!
    PIDS[$name]="$pid"
    echo "$pid" > "$RUN_DIR/$name.pid"
    log "запущен '$name' (pid $pid)"
}

start_all() {
    start_one server
    sleep "$STARTUP_DELAY"
    start_one core
    start_one ui
}

stop_one() {
    local name="$1"
    local pid="${PIDS[$name]:-}"
    if is_alive "$pid"; then
        kill -TERM "$pid" 2>/dev/null
        for _ in $(seq 1 15); do
            is_alive "$pid" || break
            sleep 0.2
        done
        is_alive "$pid" && kill -KILL "$pid" 2>/dev/null
    fi
    rm -f "$RUN_DIR/$name.pid"
    PIDS[$name]=""
}

stop_all() {
    # Останавливаем в обратном порядке относительно старта: сначала UI,
    # потом ядро (перестаёт писать в сокет), потом сервер.
    stop_one ui
    stop_one core
    stop_one server
}

rotate_log() {
    # copytruncate: файл АРХИВИРУЕТСЯ (переименовывается вбок) и ОБНУЛЯЕТСЯ
    # НА МЕСТЕ - имя не меняется. Процесс пишет в append-режиме, у append
    # каждая запись сама ищет конец файла перед записью - после truncate
    # "конец" окажется в начале, и следующий же write процесса попадёт уже
    # в свежий пустой файл. Никого не перезапускаем и не сигналим.
    local file="$1"
    [[ -f "$file" ]] || return 0

    local size
    size=$(stat -c%s "$file" 2>/dev/null) || return 0
    (( size < LOG_MAX_BYTES )) && return 0

    if (( LOG_BACKUPS > 0 )); then
        # Сдвигаем существующие архивы: .N-1 -> .N, самый старый (.LOG_BACKUPS) - вон.
        rm -f "$file.$LOG_BACKUPS"
        for ((i = LOG_BACKUPS - 1; i >= 1; i--)); do
            [[ -f "$file.$i" ]] && mv -f "$file.$i" "$file.$((i + 1))"
        done
        cp -f "$file" "$file.1"
    fi
    : > "$file"
    log "ротация лога $(basename "$file") (был >= ${LOG_MAX_BYTES} байт)"
}

rotate_all_logs() {
    for name in "${NAMES[@]}"; do
        rotate_log "$LOG_DIR/$name.log"
    done
    rotate_log "$LOG_DIR/watchdog.log"
}

shutdown_requested=0
on_signal() {
    shutdown_requested=1
}
trap on_signal SIGINT SIGTERM

restart_times=()

record_restart() {
    local now cutoff filtered=()
    now=$(date +%s)
    restart_times+=("$now")
    cutoff=$((now - RESTART_WINDOW))
    for t in "${restart_times[@]}"; do
        if (( t >= cutoff )); then
            filtered+=("$t")
        fi
    done
    restart_times=("${filtered[@]}")
}

too_many_restarts() {
    (( ${#restart_times[@]} > MAX_RESTARTS ))
}

preflight_check

echo $$ > "$RUN_DIR/watchdog.pid"
log "старт watchdog'а (pid $$)"
start_all

while (( shutdown_requested == 0 )); do
    sleep "$POLL_INTERVAL"
    (( shutdown_requested == 1 )) && break

    rotate_all_logs

    dead=""
    for name in "${NAMES[@]}"; do
        if ! is_alive "${PIDS[$name]:-}"; then
            dead="$name"
            break
        fi
    done

    if [[ -n "$dead" ]]; then
        log "процесс '$dead' упал — перезапускаю все три"
        record_restart
        if too_many_restarts; then
            log "слишком много рестартов (>$MAX_RESTARTS за ${RESTART_WINDOW}с) — сдаюсь"
            stop_all
            rm -f "$RUN_DIR/watchdog.pid"
            exit 1
        fi
        stop_all
        sleep 1
        start_all
    fi
done

log "получен сигнал остановки — завершаю все процессы"
stop_all
rm -f "$RUN_DIR/watchdog.pid"
exit 0
