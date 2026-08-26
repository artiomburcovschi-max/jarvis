import subprocess
import re
import socket
import uuid

# --- ВНУТРЕННИЕ ФУНКЦИИ (Скрыты от Джарвиса) ---

def _scan_usb_devices() -> list[dict]:
    devices = []
    try:
        res = subprocess.run(["lsusb"], capture_output=True, text=True, check=True)
        for line in res.stdout.strip().split("\n"):
            if not line:
                continue
            match = re.search(r"ID\s+([0-9a-fA-F:]+)\s+(.+)", line)
            if match:
                device_id = match.group(1)
                name = match.group(2).strip()
                name_lower = name.lower()
                
                skip_keywords = ["root hub", "hub", "controller", "virtual", "audio"]
                if any(kw in name_lower for kw in skip_keywords) and "picun" not in name_lower:
                    continue
                    
                devices.append({"name": name, "id": device_id})
    except Exception:
        pass
    return devices

def _scan_network_devices() -> list[dict]:
    devices = []
    try:
        res = subprocess.run(["ip", "neigh"], capture_output=True, text=True, check=True)
        for line in res.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 5 and "lladdr" in parts:
                ip = parts[0]
                mac = parts[parts.index("lladdr") + 1]
                status = parts[-1]
                
                if status not in ["REACHABLE", "DELAY", "STALE"]:
                    continue
                
                hostname = "Неизвестное устройство"
                try:
                    socket.setdefaulttimeout(0.5)
                    hostname = socket.gethostbyaddr(ip)[0].split(".")[0]
                except Exception:
                    pass
                
                devices.append({"name": hostname, "ip": ip, "mac": mac})
    except Exception:
        pass
    return devices


# --- ИНСТРУМЕНТЫ (Доступны Джарвису) ---

def scan_hardware_and_network(target: str = "all", detail_level: str = "summary") -> dict:
    """
    Основной сканер. 
    Если detail_level == 'summary', возвращает только имена (чтобы не грузить эфир).
    Если detail_level == 'detailed', возвращает полные данные с IP/MAC.
    """
    result = {}
    
    if target in ["all", "usb"]:
        usb = _scan_usb_devices()
        if detail_level == "summary":
            result["usb_devices"] = [d["name"] for d in usb] if usb else ["Ничего не найдено"]
        else:
            result["usb_devices"] = usb
            
    if target in ["all", "network"]:
        net = _scan_network_devices()
        if detail_level == "summary":
            # Возвращаем только имена или IP, если имя неизвестно, но без MAC-адресов
            result["network_devices"] = [d["name"] if d["name"] != "Неизвестное устройство" else f"Устройство ({d['ip']})" for d in net]
        else:
            result["network_devices"] = net
            
    return result

def get_local_network_info() -> dict:
    """Возвращает IP и MAC адрес ПК, на котором запущен Джарвис."""
    ip = "127.0.0.1"
    try:
        # Хитрый способ получить свой локальный IP без парсинга ifconfig
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except Exception:
        pass
        
    # Получаем MAC-адрес и форматируем его в XX:XX:XX...
    mac = ':'.join(['{:02x}'.format((uuid.getnode() >> ele) & 0xff) for ele in range(0,8*6,8)][::-1])
    return {"my_ip": ip, "my_mac": mac}

def ping_host(host: str) -> dict:
    """Пингует указанный IP или домен."""
    try:
        # Отправляем 2 пакета (-c 2) с таймаутом ожидания 2 секунды (-W 2)
        res = subprocess.run(["ping", "-c", "2", "-W", "2", host], capture_output=True, text=True)
        if res.returncode == 0:
            return {"status": "alive", "host": host, "message": f"Сервер {host} доступен. Пинг прошел успешно."}
        else:
            return {"status": "dead", "host": host, "message": f"Сервер {host} недоступен (таймаут)."}
    except Exception as e:
        return {"error": f"Ошибка при выполнении пинга: {e}"}


# --- РЕГИСТРАЦИЯ ИНСТРУМЕНТОВ ---

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "scan_hardware_and_network",
            "description": "Сканирует USB-периферию и устройства в локальной сети. По умолчанию используй detail_level='summary'. Используй 'detailed' ТОЛЬКО если пользователь прямо просит технические подробности, IP или MAC адреса.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["all", "usb", "network"],
                        "description": "'usb' для девайсов, 'network' для сети, 'all' для всего."
                    },
                    "detail_level": {
                        "type": "string",
                        "enum": ["summary", "detailed"],
                        "description": "Уровень деталей. 'summary' вернет только имена. 'detailed' вернет IP и MAC."
                    }
                },
                "required": ["target", "detail_level"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_local_network_info",
            "description": "Возвращает локальный IP и MAC-адрес твоего собственного компьютера."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ping_host",
            "description": "Пингует сервер, IP-адрес или домен (например, 192.168.1.100), чтобы проверить, жив ли он (доступен ли в сети).",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "IP-адрес или домен для проверки (например: 192.168.100.93)."
                    }
                },
                "required": ["host"]
            }
        }
    }
]

TOOL_IMPLEMENTATIONS = {
    "scan_hardware_and_network": scan_hardware_and_network,
    "get_local_network_info": get_local_network_info,
    "ping_host": ping_host
}