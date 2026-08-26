import json
import subprocess

SMART_MAPPING = {
    "наушники": ["headphone", "headset", "bluez", "usb", "wireless"],
    "колонки": ["speaker", "analog", "line", "hdmi", "stereo"],
    "динамики": ["speaker", "analog", "line", "hdmi", "stereo"],
    "микрофон": ["mic", "capture", "usb", "analog", "bluez"],
    "вебка": ["webcam", "usb", "camera"],
    "блютуз": ["bluez", "a2dp", "bluetooth"]
}

def _run_pactl(args: list[str]) -> str:
    res = subprocess.run(["pactl"] + args, capture_output=True, text=True, check=True)
    return res.stdout

def _get_pactl_json(kind: str) -> list[dict]:
    try:
        return json.loads(_run_pactl(["-f", "json", "list", kind]))
    except Exception as e:
        print(f"[audio_device] Ошибка получения {kind}: {e}")
        return []

def list_audio_devices(device_type: str = "all") -> dict:
    """Возвращает список всех подключенных аудиоустройств (входов и/или выходов)."""
    result = {}
    
    if device_type in ["all", "output", "outputs"]:
        sinks = _get_pactl_json("sinks")
        result["outputs"] = [
            {
                "name": d.get("name"),
                "description": d.get("description", d.get("name")),
                "state": d.get("state")
            }
            for d in sinks
        ]
        
    if device_type in ["all", "input", "inputs"]:
        sources = _get_pactl_json("sources")
        result["inputs"] = [
            {
                "name": d.get("name"),
                "description": d.get("description", d.get("name")),
                "state": d.get("state")
            }
            for d in sources
            if "monitor" not in d.get("name", "").lower()  # Исключаем виртуальные мониторы выходов
        ]
        
    return result

def switch_audio_device(device_type: str, search_name: str) -> dict:
    is_output = device_type.lower() in ["output", "out", "выход", "динамики", "колонки"]
    
    target_kind = "sinks" if is_output else "sources"
    streams_kind = "sink-inputs" if is_output else "source-outputs"
    
    devices = _get_pactl_json(target_kind)
    if not devices:
        return {"error": "Не удалось получить список аудиоустройств. Проверь PulseAudio/PipeWire."}

    search_lower = search_name.lower()
    search_tokens = SMART_MAPPING.get(search_lower, [search_lower])
    
    matched = None
    for dev in devices:
        name = dev.get("name", "").lower()
        desc = dev.get("description", "").lower()
        if any(token in name or token in desc for token in search_tokens):
            matched = dev
            break

    if not matched:
        available = [d.get("description", d.get("name")) for d in devices]
        return {
            "error": f"Устройство '{search_name}' не найдено. Доступные: {', '.join(available)}"
        }

    target_name = matched["name"]
    display_name = matched.get("description", target_name)
    
    try:
        cmd_default = "set-default-sink" if is_output else "set-default-source"
        _run_pactl([cmd_default, target_name])
        
        cmd_mute = "set-sink-mute" if is_output else "set-source-mute"
        _run_pactl([cmd_mute, target_name, "0"])
        
        active_streams = _get_pactl_json(streams_kind)
        moved_count = 0
        cmd_move = "move-sink-input" if is_output else "move-source-output"
        
        for stream in active_streams:
            stream_id = str(stream.get("index"))
            try:
                _run_pactl([cmd_move, stream_id, target_name])
                moved_count += 1
            except Exception:
                pass

        msg = f"Аудио{'выход' if is_output else 'вход'} переключен на: {display_name}."
        if moved_count > 0:
            msg += f" Перенесено активных потоков: {moved_count}."
            
        return {"status": "success", "message": msg}

    except Exception as e:
        return {"error": f"Ошибка при переключении звука: {e}"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "switch_audio_device",
            "description": "Переключает аудиовыход (колонки/наушники) или аудиовход (микрофон).",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_type": {"type": "string", "enum": ["output", "input"]},
                    "search_name": {"type": "string", "description": "Название или ключевое слово."}
                },
                "required": ["device_type", "search_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_audio_devices",
            "description": "Возвращает список доступных звуковых выходов и входов.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_type": {
                        "type": "string",
                        "enum": ["all", "output", "input"],
                        "description": "Тип устройств для вывода: 'output' (динамики/наушники), 'input' (микрофоны) или 'all'."
                    }
                }
            }
        }
    }
]

TOOL_IMPLEMENTATIONS = {
    "switch_audio_device": switch_audio_device,
    "list_audio_devices": list_audio_devices
}