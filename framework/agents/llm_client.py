"""
Тонкая обёртка над LLM-провайдером через OpenAI-совместимый протокол.

Работает с ЛЮБЫМ провайдером, у которого есть OpenAI-совместимый эндпоинт:
OpenRouter (рекомендуется - один ключ, доступ к десяткам моделей, есть
бесплатные), напрямую OpenAI, напрямую DeepSeek, локальный Ollama и т.д.
Чтобы сменить провайдера/модель - не нужно менять код, только переменные
окружения (см. README.md).

Обязательно только LLM_API_KEY. Всё остальное - с разумными дефолтами.
"""

from openai import OpenAI

from jarvis_config import get, get_secret

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-2.5-flash"
# ВНИМАНИЕ: список бесплатных (":free") моделей на OpenRouter меняется
# буквально каждый месяц - провайдеры добавляют и убирают модели без
# предупреждения. Не полагайся на конкретный ":free"-id из старых гайдов
# (включая этот файл, если ты его давно не обновлял) - актуальный список
# смотри на https://openrouter.ai/models?max_price=0 и подставляй нужный
# id в LLM_MODEL.


class LLMClientError(Exception):
    """Ошибка конфигурации LLM-клиента (например, нет API-ключа)."""


def create_client() -> tuple[OpenAI, str]:
    """Создаёт OpenAI-совместимый клиент из переменных окружения.

    Returns:
        (client, model_name)

    Raises:
        LLMClientError: если LLM_API_KEY не задан.
    """
    # .strip() специально: скопированный откуда-нибудь ключ нередко тащит с
    # собой невидимый перевод строки в конце (частый артефакт копипаста с
    # сайтов) - а HTTP-заголовок с переносом строки внутри значения невалиден
    # и httpcore рубит запрос ДО отправки с "Illegal header value", что снаружи
    # выглядит как загадочная "Connection error." Лучше подстраховаться здесь
    # раз и навсегда, чем каждый раз объяснять этот один и тот же баг.
    api_key = get_secret("LLM_API_KEY")
    if api_key:
        api_key = api_key.strip()
    if not api_key:
        raise LLMClientError(
            "Не найден LLM_API_KEY. Получи бесплатный ключ на "
            "https://openrouter.ai/keys и установи переменную окружения: "
            "export LLM_API_KEY=sk-or-... (см. README.md, раздел про LLM-слой)."
        )

    base_url = get("LLM_BASE_URL", "llm.base_url", DEFAULT_BASE_URL).strip()
    model = get("LLM_MODEL", "llm.model", DEFAULT_MODEL).strip()

    # OpenRouter-специфичные заголовки для их дашборда/рейтинга моделей -
    # опциональны, ни на что не влияют, если не заданы.
    extra_headers = {}
    site_url = get("LLM_SITE_URL", "llm.site_url", "")
    if site_url:
        extra_headers["HTTP-Referer"] = site_url.strip()
    app_name = get("LLM_APP_NAME", "llm.app_name", "")
    if app_name:
        extra_headers["X-Title"] = app_name.strip()

    client = OpenAI(base_url=base_url, api_key=api_key, default_headers=extra_headers or None)
    return client, model


def create_fallback_client() -> "tuple[OpenAI, str] | None":
    """Раунд 23 (B6) - опциональный локальный fallback (LM Studio/Ollama/
    llama-server и т.п. - любой OpenAI-совместимый локальный сервер).

    В отличие от create_client(), НИЧЕГО не обязательно: если
    LLM_FALLBACK_BASE_URL или LLM_FALLBACK_MODEL не заданы - просто
    возвращает None, и dialog_manager работает как раньше (без fallback,
    единственная ошибка при недоступности облака). Ничего не проверяет
    по сети при создании - как и create_client(), это просто конструктор
    клиента, реальный вызов будет позже в dialog_manager.

    API-ключ НЕ обязателен - большинство локальных серверов (LM Studio,
    Ollama, llama-server) его не проверяют вообще, но конструктору
    OpenAI-клиента формально нужна непустая строка.
    """
    base_url = get("LLM_FALLBACK_BASE_URL", "llm_fallback.base_url", "").strip()
    model = get("LLM_FALLBACK_MODEL", "llm_fallback.model", "").strip()
    if not base_url or not model:
        return None

    api_key = get_secret("LLM_FALLBACK_API_KEY", "not-needed")
    if api_key:
        api_key = api_key.strip() or "not-needed"

    client = OpenAI(base_url=base_url, api_key=api_key)
    return client, model
