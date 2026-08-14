"""
conversation_log.py - раунд 28 (E2): полный транскрипт разговора на диск.

В отличие от self._history в dialog_manager.py (живое окно на
HISTORY_MAX_TURNS ходов, участвует в ЗАПРОСАХ к LLM и обнуляется при
перезапуске процесса - осознанно, раунд 11, "не тянуть обрывок случайного
старого диалога") - это ЧИСТО дневник для юзера: каждое сообщение
(user/assistant/tool) дописывается сюда по мере разговора, НИКОГДА не
читается обратно в код и не влияет ни на что в работе Джарвиса.
Единственная цель - чтобы было что почитать/погрепать позже про то, что
вообще происходило, не подглядывая построчно в logs/server.log.

Формат и защита от бесконечного роста - тот же паттерн, что и в
gap_log.py (раунд 27): JSONL (один JSON-объект на строку, `tail -f`
удобно смотреть вживую), FIFO-вытеснение старых записей при переполнении
(MAX_CONVERSATION_LOG_ENTRIES) - это дневник для чтения человеком, не
архив на вечное хранение.
"""
import json
import threading
import time
from pathlib import Path

MAX_CONVERSATION_LOG_ENTRIES = 5000

_lock = threading.Lock()


def _log_path() -> Path:
    """framework/conversation_log.py -> framework -> корень проекта ->
    data/conversation_log.jsonl. Функция, а не константа - чтобы тесты
    могли подменить путь (см. test_conversation_log.py)."""
    return Path(__file__).resolve().parent.parent / "data" / "conversation_log.jsonl"


def log_message(role: str, content: str) -> None:
    """Дописывает одно сообщение в транскрипт. НИКОГДА не бросает
    исключение наружу - вызывается из горячего пути dialog_manager, сбой
    записи в дневник не должен ронять сам разговор."""
    entry = {"ts": time.time(), "role": role, "content": content}
    path = _log_path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
            lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
            if len(lines) > MAX_CONVERSATION_LOG_ENTRIES:
                lines = lines[-MAX_CONVERSATION_LOG_ENTRIES:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except OSError as e:
        print(f"[conversation_log] Не удалось записать в {path}: {e}")
