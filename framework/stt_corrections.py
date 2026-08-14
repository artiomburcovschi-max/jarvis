"""
stt_corrections.py - раунд 27 (E1): словарь типичных ошибок распознавания.

Применяется к тексту СРАЗУ после Whisper (server.py, transcribe_worker),
ДО intent_router/LLM - порядок важен: неправильно распознанное слово
никогда не долетает до логики команд, значит все дальнейшие компоненты
(intent_router, dialog_manager, LLM) видят уже исправленный текст и
ничего специально под STT-ошибки знать не должны.

Формат - config/stt_corrections.yaml, редактируемый юзером словарь
"как Whisper слышит" -> "что имелось в виду" (см. сам файл). Пусто/нет
файла - ничего не меняется, текст проходит как есть (та же философия
"опционально по умолчанию", что и config.yaml/A4, llm_fallback/B6).

Замена - по границам слова (\\b), без учёта регистра, а не голая
подстрока - иначе "темножить" заменило бы кусок случайно похожего более
длинного слова. Порядок правил в файле - это порядок применения; если
несколько правил пересекаются на одном и том же куске текста, применяется
первое подошедшее (простая, предсказуемая семантика, не самая "умная",
но зато однозначно понятно, что происходит, глядя в файл).

Это НЕ автокоррекция Джарвисом самого себя (см. обсуждение в чате) - это
статический словарь, который редактирует ЮЗЕР, глядя в gap_log.py
(раунд 27, E1-продолжение) или просто заметив на слух повторяющуюся
ошибку. Джарвис сам НЕ решает, что и на что менять.
"""
import re
import threading
from pathlib import Path

import yaml

_lock = threading.Lock()
_cache: "dict[str, str] | None" = None
_compiled_cache: "list[tuple[re.Pattern, str]] | None" = None
_cached_mtime: "float | None" = None


def _config_path() -> Path:
    """framework/stt_corrections.py -> framework -> корень проекта ->
    config/stt_corrections.yaml. Функция, а не константа - чтобы тесты
    могли подменить путь (см. test_stt_corrections.py)."""
    return Path(__file__).resolve().parent.parent / "config" / "stt_corrections.yaml"


def reset_cache_for_tests() -> None:
    global _cache, _compiled_cache, _cached_mtime
    with _lock:
        _cache = None
        _compiled_cache = None
        _cached_mtime = None


def _load_raw() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        print(f"[stt_corrections] Файл {path} повреждён или недоступен ({e}) - "
              f"исправления не применяются.")
        return {}
    if not isinstance(data, dict):
        return {}
    # Значения должны быть строками - тихо игнорируем мусор (например,
    # вложенный словарь по ошибке), а не роняем весь файл из-за одной
    # опечатки в YAML.
    return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def _current_mtime() -> "float | None":
    path = _config_path()
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _get_compiled_rules() -> "list[tuple[re.Pattern, str]]":
    global _cache, _compiled_cache, _cached_mtime
    with _lock:
        mtime = _current_mtime()
        # Раунд 27: файл можно редактировать прямо во время работы сервера -
        # правило заработает на следующей же фразе, без перезапуска.
        # Проверка mtime - дешёвый stat(), не полное перечитывание файла
        # на каждый вызов; перечитываем содержимое, только когда файл
        # реально поменялся (или пропал/появился - mtime тогда None/новый).
        if _compiled_cache is not None and mtime == _cached_mtime:
            return _compiled_cache

        _cache = _load_raw()
        _cached_mtime = mtime
        compiled = []
        for wrong, right in _cache.items():
            if not wrong.strip():
                continue
            pattern = re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE)
            compiled.append((pattern, right))
        _compiled_cache = compiled
        return _compiled_cache


def apply_corrections(text: str) -> str:
    """Применяет словарь замен к тексту. Пустой текст/пустой словарь -
    без изменений, дёшево и безопасно вызывать всегда, без предварительной
    проверки "а есть ли вообще файл"."""
    if not text:
        return text
    for pattern, right in _get_compiled_rules():
        text = pattern.sub(right, text)
    return text
