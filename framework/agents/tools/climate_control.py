import os

import requests

# --- Подключение к хабу ---
# Если в этой же папке (или где-то в framework/) уже есть общая функция вызова
# хаба (например у get_pc_status/get_climate_info в hub.py) — лучше вызвать
# её вместо HUB_API_BASE_URL/HUB_API_KEY/_hub_request ниже, чтобы URL/ключ
# задавались в одном месте. Я не видел тот файл, так что тут — самодостаточный
# вариант с теми же именами переменных окружения, что и на дашборде.
HUB_API_BASE_URL = os.getenv("HUB_API_BASE_URL", "http://localhost:8000/api/v1")
HUB_API_KEY = os.getenv("HUB_API_KEY") or os.getenv("API_KEY")
_TIMEOUT = 8.0

# Юзер может назвать юнит и slug'ом ("room"/"hall"), и по-русски — эти карты
# нормализуют что угодно к тому, что реально ждёт сервер. Юниты сейчас ровно
# два и оба уже настроены на хабе; если появится третий физический
# кондиционер — добавь его infrared_id/remote_id в .env хаба (см.
# TUYA_AC_HALL_*) и одну строку сюда, больше никаких правок не нужно.
UNIT_MAPPING = {
    "room": ["room", "комнат", "у себя", "моя комната", "спальн"],
    "hall": ["hall", "зал", "гостин", "папин", "у папы"],
}
MODE_MAPPING = {
    "cold": ["cold", "охлажд", "холод", "кондиционир", "cool"],
    "hot": ["hot", "обогрев", "тепл", "жар", "heat"],
    "dry": ["dry", "осуш", "сухо"],
    "wind": ["wind", "вентил", "обдув", "проветр", "fan"],
    "auto": ["auto", "авто"],
}


def _normalize(value: str, mapping: dict[str, list[str]], default: str) -> str:
    """Сопоставляет то, что сказал юзер/LLM, с одним из известных слагов."""

    value_lower = (value or "").strip().lower()
    if value_lower in mapping:
        return value_lower
    for slug, tokens in mapping.items():
        if any(token in value_lower for token in tokens):
            return slug
    return default


def _hub_request(method: str, endpoint: str, *, json: dict | None = None) -> dict:
    """Общий вызов хаба. Всегда возвращает dict — либо распарсенный ответ,
    либо {"error": "..."} с понятным текстом (429/502/недоступность сервера
    и т.д.), чтобы Джарвису было что сказать голосом, а не падать с трейсбеком.
    """

    headers = {"X-API-Key": HUB_API_KEY} if HUB_API_KEY else {}
    try:
        response = requests.request(
            method, f"{HUB_API_BASE_URL}{endpoint}", json=json, headers=headers, timeout=_TIMEOUT
        )
    except requests.RequestException:
        return {"error": "Хаб недоступен — проверь, запущен ли сервер."}

    if response.status_code == 429:
        # Анти-спам сервера — слишком частая команда по этому же юниту.
        try:
            detail = response.json().get("detail", "Подожди немного и попробуй снова.")
        except ValueError:
            detail = "Слишком частая команда, подожди немного."
        return {"error": detail}

    if not response.ok:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        return {"error": f"Хаб вернул ошибку ({response.status_code}): {detail}"}

    if not response.content:
        return {}
    return response.json()


def list_ac_units() -> dict:
    """Возвращает список настроенных кондиционеров (юнитов) в квартире."""

    result = _hub_request("GET", "/hub/climate-control/units")
    if "error" in result:
        return result
    if not result:
        return {"error": "На хабе не настроено ни одного кондиционера (пусто в .env)."}
    return {"units": [{"unit": u["unit"], "label": u["label"]} for u in result]}


def get_ac_status(unit: str = "room") -> dict:
    """Текущее состояние одного кондиционера: последняя отправленная команда
    (не факт. состояние — ИК-пульт односторонний, реального фидбека от AC нет)."""

    slug = _normalize(unit, UNIT_MAPPING, "room")
    result = _hub_request("GET", "/hub/climate-control/status")
    if "error" in result:
        return result

    state = (result.get("manual") or {}).get(slug)
    if not state or state.get("power") is None:
        return {"unit": slug, "status": "За это время команд на этот кондиционер ещё не было."}

    return {
        "unit": slug,
        "power": "включён" if state["power"] else "выключен",
        "mode": state.get("mode"),
        "temp": state.get("temp"),
        "last_command_ok": state.get("last_command_ok"),
        "last_error": state.get("last_error"),
        "sensor_online": result.get("sensor_online"),
    }


def control_ac(unit: str = "room", power: bool = True, mode: str | None = None, temp: float | None = None) -> dict:
    """Включает/выключает кондиционер и/или меняет режим и температуру — команда
    уходит сразу, без ожидания опроса. Если mode/temp не заданы явно, берутся
    последние отправленные для этого же юнита значения (а если их ещё не было
    в этом запуске сервера — cold/24°C по умолчанию), чтобы можно было
    сказать просто "включи кондиционер" без перечисления режима и градусов.
    """

    slug = _normalize(unit, UNIT_MAPPING, "room")

    if mode is None or temp is None:
        current = get_ac_status(slug)
        if mode is None:
            mode = current.get("mode") or "cold"
        if temp is None:
            temp = current.get("temp") or 24.0

    mode_slug = _normalize(mode, MODE_MAPPING, "cold")

    result = _hub_request(
        "POST",
        "/hub/climate-control/manual",
        json={"unit": slug, "power": bool(power), "mode": mode_slug, "temp": float(temp)},
    )
    if "error" in result:
        return result

    return {
        "status": "success",
        "message": (
            f"Кондиционер «{slug}» {'включён' if power else 'выключен'}, "
            f"режим {mode_slug}, {temp}°C."
        ),
    }


# --- Интеграция с автозагрузчиком Джарвиса ---

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "control_ac",
            "description": (
                "Включает, выключает или меняет режим/температуру кондиционера. "
                "В квартире может быть больше одного кондиционера — уточняй unit, "
                "если из разговора не ясно, какой имеется в виду."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "enum": ["room", "hall"],
                        "description": "Какой кондиционер: 'room' — своя комната, 'hall' — зал (папин).",
                    },
                    "power": {
                        "type": "boolean",
                        "description": "true — включить, false — выключить.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["cold", "hot", "dry", "wind", "auto"],
                        "description": "Режим работы. Необязателен — если не указан, останется прежний.",
                    },
                    "temp": {
                        "type": "number",
                        "description": "Целевая температура, °C (16-30). Необязательна — если не указана, останется прежняя.",
                    },
                },
                "required": ["unit", "power"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ac_status",
            "description": "Возвращает текущее состояние (последнюю отправленную команду) одного кондиционера.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {
                        "type": "string",
                        "enum": ["room", "hall"],
                        "description": "Какой кондиционер: 'room' — своя комната, 'hall' — зал (папин).",
                    }
                },
                "required": ["unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_ac_units",
            "description": "Возвращает список всех кондиционеров в квартире, которыми можно управлять.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "control_ac": control_ac,
    "get_ac_status": get_ac_status,
    "list_ac_units": list_ac_units,
}
