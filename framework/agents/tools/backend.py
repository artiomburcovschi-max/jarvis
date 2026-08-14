"""
Инструменты: личный бэкенд пользователя (FastAPI: погода/курсы/новости/
избранное/файлы).

Отдельный проект пользователя (FastAPI + Streamlit), уже развёрнутый
самостоятельно. НЕ используем его /assistant/chat - это заглушка ("No model
is connected yet", см. app/services/assistant/service.py в бэкенде) -
настоящий мозг у Джарвиса уже есть, это DialogManager. Здесь используются
только /informer/* эндпоинты - это настоящие рабочие данные (погода через
Open-Meteo, котировки через yfinance, RSS-новости).

URL и ключ - переменные окружения, тем же принципом, что LLM_API_KEY: сейчас
бэкенд крутится локально на компе, потом переедет на телефон+SSD - поменяется
только JARVIS_BACKEND_URL, код трогать не придётся.
"""

import requests

from jarvis_config import get, get_secret

BACKEND_BASE_URL = get("JARVIS_BACKEND_URL", "backend.url", "http://127.0.0.1:8000/api/v1").strip().rstrip("/")
BACKEND_API_KEY = get_secret("JARVIS_BACKEND_API_KEY", "").strip()
BACKEND_TIMEOUT_SECONDS = 8.0


def _backend_request(method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> dict:
    """Общий HTTP-клиент к личному бэкенду. Всегда возвращает dict - либо с
    реальными данными, либо {"error": ...} - никогда не бросает исключение
    наружу (тот же принцип изоляции ошибок, что и везде в проекте)."""
    headers = {"X-API-Key": BACKEND_API_KEY} if BACKEND_API_KEY else {}
    url = f"{BACKEND_BASE_URL}{path}"

    try:
        response = requests.request(
            method, url, params=params, json=json_body, headers=headers, timeout=BACKEND_TIMEOUT_SECONDS
        )
    except requests.exceptions.ConnectionError:
        return {"error": f"Бэкенд недоступен по адресу {BACKEND_BASE_URL} - проверь, что сервер запущен."}
    except requests.exceptions.Timeout:
        return {"error": "Бэкенд не ответил вовремя (таймаут)."}
    except requests.exceptions.RequestException as e:
        return {"error": f"Ошибка запроса к бэкенду: {e}"}

    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        return {"error": f"Бэкенд ответил ошибкой ({response.status_code}): {detail}"}

    try:
        return response.json()
    except ValueError:
        return {"error": "Бэкенд вернул не-JSON ответ."}


def get_weather_report(city: str) -> dict:
    """Погода по городу через личный бэкенд (см. схему в TOOL_SCHEMAS)."""
    return _backend_request("GET", "/informer/weather", params={"city": city})


def get_market_quote(ticker: str = "BTC-USD") -> dict:
    """Котировка тикера через личный бэкенд (см. схему в TOOL_SCHEMAS)."""
    return _backend_request("GET", "/informer/finance", params={"ticker": ticker})


def get_news(category: str = "important", limit: int = 5) -> dict:
    """Свежие новости через личный бэкенд (см. схему в TOOL_SCHEMAS)."""
    limit = max(1, min(30, int(limit)))
    return _backend_request("GET", "/informer/news", params={"category": category, "limit": limit})


def get_daily_briefing(city: str = "") -> dict:
    """Сводка погода+избранные котировки+новости одним запросом (TOOL_SCHEMAS)."""
    params = {"city": city} if city.strip() else None
    return _backend_request("GET", "/informer/summary", params=params)


def list_favorite_tickers() -> dict:
    """Список избранных тикеров пользователя (см. схему в TOOL_SCHEMAS)."""
    return _backend_request("GET", "/informer/favorites")


def add_favorite_ticker(ticker: str) -> dict:
    """Добавляет тикер в избранное (см. схему в TOOL_SCHEMAS)."""
    return _backend_request("POST", "/informer/favorites", json_body={"ticker": ticker})


def remove_favorite_ticker(ticker: str) -> dict:
    """Убирает тикер из избранного (см. схему в TOOL_SCHEMAS)."""
    return _backend_request("DELETE", f"/informer/favorites/{ticker}")


def list_recent_files() -> dict:
    """Список файлов, загруженных в личный бэкенд (см. схему в TOOL_SCHEMAS)."""
    result = _backend_request("GET", "/files")
    if isinstance(result, dict) and "error" in result:
        return result
    # /files возвращает список напрямую, а не словарь - оборачиваем для
    # единообразия с остальными инструментами (все возвращают dict).
    names = [f.get("name", "?") for f in result] if isinstance(result, list) else []
    return {"count": len(names), "files": names}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather_report",
            "description": "Возвращает текущую погоду в указанном городе через личный бэкенд пользователя.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "Название города, например 'Москва'"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_quote",
            "description": (
                "Возвращает текущую цену тикера (акции, крипта, валюта, индекс). "
                "По умолчанию биткоин, если не указан другой."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Тикер, например 'BTC-USD', 'AAPL', 'EURUSD=X'"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Возвращает свежие новости по категории (important, politics, business, technology и т.д.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Категория новостей, по умолчанию 'important'"},
                    "limit": {"type": "integer", "description": "Сколько новостей вернуть (по умолчанию 5)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_daily_briefing",
            "description": (
                "Возвращает сводку одним запросом: погода (если указан город) + "
                "избранные тикеры + свежие новости. Используй, когда просят "
                "'сводку', 'что нового', 'введи в курс дела' и т.п."
            ),
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "Город для погоды (необязательно)"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_favorite_tickers",
            "description": "Возвращает список избранных тикеров пользователя.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_favorite_ticker",
            "description": "Добавляет тикер в список избранных.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": "Тикер, например 'BTC-USD'"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_favorite_ticker",
            "description": "Убирает тикер из списка избранных.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": "Тикер, который нужно убрать"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_files",
            "description": "Возвращает список файлов, сохранённых в личном бэкенде пользователя.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "get_weather_report": get_weather_report,
    "get_market_quote": get_market_quote,
    "get_news": get_news,
    "get_daily_briefing": get_daily_briefing,
    "list_favorite_tickers": list_favorite_tickers,
    "add_favorite_ticker": add_favorite_ticker,
    "remove_favorite_ticker": remove_favorite_ticker,
    "list_recent_files": list_recent_files,
}
