"""
Инструменты C7 (раунд 26): долгосрочная память о пользователе и его
проектах - ВНЕ истории диалога.

Зачем отдельно от истории: обычная история (dialog_manager.HISTORY_MAX_TURNS)
хранит только последние ~8 ходов и ПОЛНОСТЬЮ обнуляется при перезапуске
процесса (`self._history = []` при старте, раунд 11) - это осознанно,
чтобы после падения/рестарта не тянуть обрывок случайного старого диалога.
Факты отсюда переживают И перезапуск, И конец конкретного разговора -
фиксированный список коротких утверждений ("Артём работает над Джарвисом",
"любимый язык - Python"), которые dialog_manager подмешивает В КАЖДЫЙ
системный промпт (см. dialog_manager._build_system_prompt()), а не только
в текущую историю.

Два инструмента, и НИ ОДИН не требует голосового подтверждения (C3,
раунд 17): запись/удаление текстовой заметки - не разрушительное и легко
обратимое действие, в отличие от lock_computer/AT-SPI (раунд 17/24).
Постоянно переспрашивать "точно запомнить X?" сделало бы фичу неудобной
до бесполезности - соразмерность риска и трения, а не бездумное "если
можем спросить - спросим".

remember_fact - решение "запомнить или нет" оставлено САМОЙ МОДЕЛИ (явная
просьба "запомни, что..." или пользователь между делом сообщает что-то
похожее на долгоживущий факт о себе/проекте), а не жёсткому списку
триггер-слов, как в intent_router.py. Это осознанная асимметрия: там
неправильное решение - это ошибочно выполненное действие мимо LLM
(дорогая ошибка), здесь - лишняя строчка в файле с фактами (дешёвая,
поправимая через forget_fact).

forget_fact - нечёткий поиск по уже сохранённым фактам (fuzz.partial_ratio
из rapidfuzz - НЕ общий fuzzy_lookup из _shared.py, см. обоснование у
FORGET_FUZZY_THRESHOLD ниже) - "уверен или не лезу":
ниже порога уверенности НЕ удаляет ничего, сообщает, что не уверена, о
каком факте речь, и просит переформулировать.

Facts НЕ ограничены local-only (см. atspi_control.py, раунд 24, где это
принципиально) - они и так предназначены для того, чтобы модель их
использовала в ответах, а история диалога и так уже целиком уходит в
облако; отдельного нового риска утечки они не создают.
"""
import json
import threading
import time
import uuid
from pathlib import Path

from rapidfuzz import fuzz

# Старые факты вытесняются новыми при переполнении (FIFO), не отказ -
# лучше молча "забыть" самое старое и продолжать работать, чем внезапно
# сломать remember_fact для пользователя, который уже привык, что оно
# просто работает.
MAX_REMEMBERED_FACTS = 50
# Порог для fuzz.partial_ratio, НЕ для общего fuzzy_lookup() из _shared.py
# (тот использует fuzz.ratio - сравнение строк ЦЕЛИКОМ, что плохо подходит
# для этого случая: пользователь называет короткое ключевое слово
# ("Джарвис"), а не всю фразу факта целиком ("Артём работает над
# Джарвисом") - fuzz.ratio штрафует за разницу в длине и дал бы низкий
# скор даже для точного вхождения. fuzz.partial_ratio ищет наилучшее
# совпадение ПОДСТРОКИ - то, что здесь реально нужно.
FORGET_FUZZY_THRESHOLD = 70
MAX_FACT_LENGTH = 300

_storage_lock = threading.Lock()


def _storage_path() -> Path:
    """framework/agents/tools/memory.py -> framework/agents/tools ->
    framework/agents -> framework -> корень проекта -> data/memory.json.

    Функция, а не константа - чтобы тесты могли подменить путь (см.
    test_memory_persistence.py) и не трогать реальный файл проекта."""
    return Path(__file__).resolve().parent.parent.parent.parent / "data" / "memory.json"


def _load_all() -> list:
    path = _storage_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # Битый/недоступный файл НЕ должен ронять сервер - просто считаем,
        # что запомненных фактов нет (как при самом первом запуске).
        print(f"[memory] Файл {path} повреждён или недоступен ({e}) - считаю, что фактов нет.")
        return []
    if not isinstance(data, list):
        return []
    return data


def _save_all(facts: list) -> None:
    path = _storage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(facts, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[memory] Не удалось сохранить {path}: {e}")


def load_facts_summary() -> str:
    """Вызывается dialog_manager._build_system_prompt() на КАЖДЫЙ запрос -
    отдаёт факты уже готовым текстовым блоком для промпта (по строке на
    факт), или пустую строку, если фактов нет вовсе - тогда системный
    промпт не меняется по сравнению с тем, что было до этого раунда."""
    with _storage_lock:
        facts = _load_all()
    if not facts:
        return ""
    return "\n".join(f"- {entry['text']}" for entry in facts if entry.get("text"))


def remember_fact(text: str) -> dict:
    """Запоминает факт надолго (см. схему в TOOL_SCHEMAS)."""
    text = (text or "").strip()
    if not text:
        return {"error": "Пустой факт - нечего запоминать."}
    if len(text) > MAX_FACT_LENGTH:
        return {"error": f"Слишком длинно для одного факта (максимум {MAX_FACT_LENGTH} "
                          f"символов) - сформулируй короче."}

    with _storage_lock:
        facts = _load_all()
        facts.append({"id": uuid.uuid4().hex, "text": text, "created_at": time.time()})
        if len(facts) > MAX_REMEMBERED_FACTS:
            facts = facts[-MAX_REMEMBERED_FACTS:]
        _save_all(facts)

    return {"result": f"Запомнил: {text}"}


def forget_fact(text_hint: str) -> dict:
    """Забывает ранее запомненный факт по нечёткому совпадению (см. схему
    в TOOL_SCHEMAS)."""
    text_hint = (text_hint or "").strip()
    if not text_hint:
        return {"error": "Не указано, что именно забыть."}

    with _storage_lock:
        facts = _load_all()
        if not facts:
            return {"error": "Пока нечего забывать - память пуста."}

        best_entry, best_score = None, 0
        for entry in facts:
            score = fuzz.partial_ratio(text_hint.lower(), entry.get("text", "").lower())
            if score > best_score:
                best_entry, best_score = entry, score

        if best_entry is None or best_score < FORGET_FUZZY_THRESHOLD:
            best_text = best_entry["text"] if best_entry else "?"
            return {"error": f"Не уверен, какой именно факт забыть (лучшее совпадение "
                              f"«{best_text}», {best_score:.0f}%) - сформулируй точнее."}

        facts = [entry for entry in facts if entry["id"] != best_entry["id"]]
        _save_all(facts)

    return {"result": f"Забыл: {best_entry['text']}"}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": (
                "Запоминает короткий факт о пользователе или его проектах НАДОЛГО - "
                "переживает перезапуск сервера и не пропадает вместе с историей текущего "
                "разговора. Используй, когда пользователь явно просит что-то запомнить "
                "('запомни, что...'), или между делом сообщает долгоживущий факт о себе/"
                "своих проектах, полезный в будущих разговорах (имя, предпочтения, над чем "
                "работает). НЕ используй для мелочей, актуальных только в этом разговоре."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Короткая самодостаточная формулировка факта",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_fact",
            "description": (
                "Удаляет ранее запомненный факт по его примерному содержанию. Используй, "
                "когда пользователь просит что-то забыть или говорит, что запомненное "
                "раньше больше не актуально или было ошибкой."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text_hint": {
                        "type": "string",
                        "description": "Примерное содержание факта, который нужно забыть",
                    },
                },
                "required": ["text_hint"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "remember_fact": remember_fact,
    "forget_fact": forget_fact,
}
