"""
gap_log.py - раунд 27: "лог трения".

НЕ автокоррекция. Джарвис сам НЕ решает, что где-то ошибся, и не правит
себя сам - это осознанное решение (см. обсуждение в чате): надёжно
определить программно "здесь была ошибка STT, а не просто странная фраза
пользователя или баг в tool'е" - трудно, и система, которая сама себе
верит, что была неправа, может с тем же успехом убедить себя в этом там,
где была права. Вместо этого - просто ФИКСАЦИЯ: каждый значимый затык
(LLM недоступна целиком, инструмент упал с неожиданным исключением, агент
уткнулся в лимit шагов) - одна строка в data/gaps.jsonl. Юзер сам потом
читает и решает: словарная замена (stt_corrections.py, E1), баг, недостающий
tool - тот же паттерн, что и в раундах 3/6 ("фикс по логам с реального
теста"), просто автоматизированный и постоянный, а не разовый ручной сбор.

Формат - JSONL (один JSON-объект на строку) - можно `tail -f` смотреть
вживую, можно грепать по kind, не нужен специальный парсер для чтения по
одной записи за раз.

Ограничен по размеру (MAX_GAP_LOG_ENTRIES, FIFO - старые записи
вытесняются новыми при переполнении, тот же принцип, что у memory.py,
раунд 26) - это диагностический журнал для юзера, не архив на вечное
хранение, и не должен расти бесконечно на удалённой от присмотра машине.
"""
import json
import threading
import time
from pathlib import Path

MAX_GAP_LOG_ENTRIES = 500

_lock = threading.Lock()


def _log_path() -> Path:
    """framework/gap_log.py -> framework -> корень проекта ->
    data/gaps.jsonl. Функция, а не константа - чтобы тесты могли
    подменить путь (см. test_gap_log.py)."""
    return Path(__file__).resolve().parent.parent / "data" / "gaps.jsonl"


def log_gap(kind: str, detail: str, user_text: str = "") -> None:
    """Дописывает одну строку в лог трения. НИКОГДА не бросает исключение
    наружу - вызывается из мест, которые сами уже обрабатывают ошибку
    (dialog_manager), и сбой самого логирования не должен добавлять
    вторую, более серьёзную проблему поверх первой."""
    entry = {
        "ts": time.time(),
        "kind": kind,
        "detail": detail,
        "user_text": user_text,
    }
    path = _log_path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
            lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
            if len(lines) > MAX_GAP_LOG_ENTRIES:
                lines = lines[-MAX_GAP_LOG_ENTRIES:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except OSError as e:
        print(f"[gap_log] Не удалось записать в {path}: {e}")


def read_recent_gaps(limit: int = 20) -> list:
    """Читает последние N записей - для отладки/ручного просмотра, не
    используется в горячем пути. Битые строки (например, файл читался в
    момент незавершённой записи) тихо пропускаются, не роняют вызов."""
    path = _log_path()
    if not path.exists():
        return []
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries[-limit:]
