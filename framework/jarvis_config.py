"""
config/config.yaml + .env — единая точка настройки Джарвиса (раунд 21, A4).

До этого раунда все настройки (кроме путей ядра/скриптов) были россыпью
export'ов в шелле, разбросанной по README и по умолчаниям внутри четырёх
разных .py-файлов. Теперь:

  - config/config.yaml — НЕсекретные настройки со значениями по умолчанию,
    видны и редактируются одним файлом, можно спокойно класть в git;
  - .env (по образцу .env.example) — ТОЛЬКО секреты (API-ключи) - этот
    файл в git не кладём;
  - переменная окружения, если её явно экспортировали в shell — главнее
    всех. Это обратная совместимость: старые `export LLM_API_KEY=...` из
    старого README по-прежнему работают один в один, ничего не сломалось
    для тех, кто уже так настроил.

Порядок приоритета (выше — главнее):
  1. os.environ (уже установленная переменная окружения в текущем shell)
  2. .env (в корне проекта, если файл есть)
  3. config.yaml (в config.yaml, если ключ там есть)
  4. дефолт, переданный вызывающим кодом

ВАЖНО, что сюда сознательно НЕ входит:
  - WAKEWORD_* — их читает C++ ядро напрямую через getenv (раунд 14), а
    не Python. Заводить сюда YAML-парсер на стороне C++ - слишком большая
    и рискованная задача для этого раунда;
  - JARVIS_RUN_DIR/JARVIS_LOG_DIR/JARVIS_POLL_INTERVAL и другие настройки
    scripts/start.sh (раунд 19-20) — это bash, а не Python, у него нет
    доступа к этому модулю.
  Для обеих групп единственный способ настройки по-прежнему —
  переменные окружения (см. README).
"""
import os
from pathlib import Path
from typing import Any

import yaml

_dotenv_loaded = False
_yaml_cache: dict | None = None


def _project_root() -> Path:
    # framework/jarvis_config.py -> framework/ -> корень проекта
    return Path(__file__).resolve().parent.parent


def _load_dotenv_once() -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True

    env_path = _project_root() / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Снимаем один слой обрамляющих кавычек, если есть (VAR="значение").
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            continue
        # .env НЕ перебивает уже установленную переменную окружения — тот
        # же приоритет, что описан в докстринге модуля.
        os.environ.setdefault(key, value)


def _load_yaml_once() -> dict:
    global _yaml_cache
    if _yaml_cache is not None:
        return _yaml_cache

    config_path = _project_root() / "config" / "config.yaml"
    if not config_path.exists():
        _yaml_cache = {}
        return _yaml_cache

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        data = {}
    _yaml_cache = data
    return _yaml_cache


def _lookup_yaml(yaml_path: str) -> Any:
    node: Any = _load_yaml_once()
    for part in yaml_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def get(env_var: str, yaml_path: str, default: Any = None) -> Any:
    """Достаёт несекретную настройку по правилам приоритета из докстринга
    модуля: os.environ -> .env -> config.yaml -> default.

    env_var — имя переменной окружения (для обратной совместимости со
    старыми export'ами).
    yaml_path — путь в config.yaml через точку, например "llm.model_name".
    """
    _load_dotenv_once()

    value = os.environ.get(env_var)
    if value:
        return value

    value = _lookup_yaml(yaml_path)
    if value is not None and value != "":
        return value

    return default


def get_secret(env_var: str, default: Any = None) -> Any:
    """Как get(), но только для секретов (API-ключи и т.п.) — НЕ смотрит
    config.yaml вообще, только os.environ и .env. Секреты в config.yaml
    класть нельзя (этот файл предполагается пригодным для git)."""
    _load_dotenv_once()
    value = os.environ.get(env_var)
    if value:
        return value
    return default


def reset_cache_for_tests() -> None:
    """Только для тестов. Сбрасывает кэш .env/config.yaml между тест-кейсами,
    которые подсовывают свои временные файлы (см. test_jarvis_config.py) -
    без этого второй тест в том же процессе увидел бы конфиг, закэшированный
    первым."""
    global _dotenv_loaded, _yaml_cache
    _dotenv_loaded = False
    _yaml_cache = None
