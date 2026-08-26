"""
Инструменты: локальный хаб (FastAPI, /hub/*) - статус ПК, климат в комнате,
прочие устройства (замок, датчики и т.п. без своей выделенной схемы).

Тот же бэкенд, что и в backend.py (тот же URL/ключ), но другой раздел API -
/hub/* вместо /informer/*. Данные туда шлют test_pc.py/мониторинг (статус
ПК) и метеостанция/будущий ESP32-шлюз (климат), в память сервера - см.
app/services/hub/service.py в бэкенде. Это значит: если ничего не прислало
данные ещё, эндпоинт вернёт null, а не 404 - это не ошибка, а "устройство
пока молчит", и озвучивать это надо соответствующе, а не как сбой.

received_at - это когда сервер получил репорт, а не когда его снял датчик.
Здесь считаем "сколько секунд назад" и кладём как seconds_since_update,
чтобы LLM не озвучивал старые данные как свежие, если клиент-отправитель
(мониторинг/метеостанция) внезапно отвалился.
"""

from datetime import datetime, timezone

import requests

from jarvis_config import get, get_secret

BACKEND_BASE_URL = get("JARVIS_BACKEND_URL", "backend.url", "http://127.0.0.1:8000/api/v1").strip().rstrip("/")
BACKEND_API_KEY = get_secret("JARVIS_BACKEND_API_KEY", "").strip()
BACKEND_TIMEOUT_SECONDS = 8.0

# Порог, после которого показания считаются протухшими и об этом стоит
# явно сказать в ответе, а не молча озвучить как текущие.
STALE_AFTER_SECONDS = 30 * 60


def _backend_request(method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> dict | None:
    """Общий HTTP-клиент к личному бэкенду. Возвращает dict, None (сервер явно
    сказал "данных нет") или {"error": ...} - никогда не бросает исключение
    наружу (тот же принцип изоляции ошибок, что и в backend.py)."""
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

    if not response.content:
        return {}

    try:
        return response.json()
    except ValueError:
        return {"error": "Бэкенд вернул не-JSON ответ."}


def _with_freshness(reading: dict) -> dict:
    """Добавляет seconds_since_update и is_stale, считая от received_at (UTC)."""
    received_at_raw = reading.get("received_at")
    if not received_at_raw:
        return reading
    try:
        received_at = datetime.fromisoformat(received_at_raw.replace("Z", "+00:00"))
        seconds_ago = (datetime.now(timezone.utc) - received_at).total_seconds()
    except ValueError:
        return reading
    reading = dict(reading)
    reading["seconds_since_update"] = round(seconds_ago)
    reading["is_stale"] = seconds_ago > STALE_AFTER_SECONDS
    return reading


def get_pc_status() -> dict:
    """Текущая нагрузка ПК: CPU/RAM/GPU (см. схему в TOOL_SCHEMAS)."""
    result = _backend_request("GET", "/hub/pc/status")
    if result is None:
        return {"error": "Мониторинг ПК ещё ничего не присылал - проверь, что скрипт мониторинга запущен."}
    if "error" in result:
        return result
    return _with_freshness(result)


def get_climate_info() -> dict:
    """Температура/влажность/давление в комнате (см. схему в TOOL_SCHEMAS)."""
    result = _backend_request("GET", "/hub/climate/info")
    if result is None:
        return {"error": "Датчик климата ещё ничего не присылал - проверь метеостанцию/ESP32-шлюз."}
    if "error" in result:
        return result
    return _with_freshness(result)


def get_device_status(name: str) -> dict:
    """Состояние произвольного устройства без своей схемы, например 'door_lock'
    (см. схему в TOOL_SCHEMAS)."""
    result = _backend_request("GET", f"/hub/device/{name}")
    if result is None:
        return {"error": f"Устройство '{name}' ещё ни разу не отчитывалось."}
    if "error" in result:
        return result
    return _with_freshness(result)


def list_hub_devices() -> dict:
    """Список всех устройств хаба, которые хоть раз отчитались, с их текущим
    состоянием (см. схему в TOOL_SCHEMAS)."""
    result = _backend_request("GET", "/hub/devices")
    if isinstance(result, dict) and "error" in result:
        return result
    devices = [_with_freshness(d) for d in result] if isinstance(result, list) else []
    return {"count": len(devices), "devices": devices}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_pc_status",
            "description": "Возвращает текущую загрузку ПК (CPU, RAM, GPU) по данным локального мониторинга.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_climate_info",
            "description": "Возвращает температуру, влажность и давление в комнате по данным датчика климата.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_status",
            "description": (
                "Возвращает текущее состояние конкретного устройства хаба по имени "
                "(например, дверного замка). Используй, когда спрашивают про устройство, "
                "у которого нет отдельного инструмента вроде get_pc_status/get_climate_info."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Имя устройства, например 'door_lock'"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_hub_devices",
            "description": "Возвращает список всех устройств хаба, которые хоть раз отчитались, с их состоянием.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "get_pc_status": get_pc_status,
    "get_climate_info": get_climate_info,
    "get_device_status": get_device_status,
    "list_hub_devices": list_hub_devices,
}
