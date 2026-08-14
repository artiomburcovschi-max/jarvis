"""
Инструмент: открыть сайт в браузере по умолчанию.

Через стандартный модуль webbrowser - работает одинаково на Linux, Windows
и macOS без веток по ОС (в отличие от большинства других файлов в tools/,
которым для управления системой нужны разные команды под разные ОС).
"""

import webbrowser

# Короткие бытовые названия сайтов -> реальный домен. Без этого словаря
# "открой ютуб" ушло бы в open_website("ютуб") - а там нет точки в первом
# слове, значит сработала бы ветка "это не URL, ищи в Google запрос 'ютуб'"
# вместо перехода на youtube.com. LLM обычно и сама достаточно сообразительна,
# чтобы передать сюда "youtube.com", а не "ютуб" - но полагаться только на её
# добрую волю в КАЖДОМ вызове не нужно, когда можно закрыть самые частые
# случаи одной таблицей.
SITE_ALIASES = {
    "ютуб": "youtube.com",
    "youtube": "youtube.com",
    "гугл": "google.com",
    "google": "google.com",
    "почта": "mail.google.com",
    "гмейл": "mail.google.com",
    "дискорд": "discord.com/app",
}


def open_website(url_or_query: str) -> dict:
    """Открывает сайт в браузере по умолчанию (см. схему в TOOL_SCHEMAS)."""
    text = url_or_query.strip()
    if not text:
        return {"error": "Пустой запрос - не знаю, что открывать."}

    alias_target = SITE_ALIASES.get(text.lower())
    if alias_target:
        url = f"https://{alias_target}"
    else:
        looks_like_url = "." in text.split()[0] and " " not in text.split()[0]
        url = text if looks_like_url else f"https://www.google.com/search?q={text}"
        if looks_like_url and not url.startswith(("http://", "https://")):
            url = "https://" + url

    try:
        opened = webbrowser.open(url)
        if not opened:
            return {"error": "Не удалось найти браузер по умолчанию в системе."}
        return {"result": f"Открываю: {url}"}
    except Exception as e:
        return {"error": f"Не удалось открыть браузер: {e}"}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "open_website",
            "description": (
                "Открывает сайт в браузере по умолчанию. Если дана не ссылка, "
                "а просто тема/вопрос - откроет поиск Google по этому запросу. "
                "Используй, когда просят открыть сайт, погуглить, найти что-то в интернете."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url_or_query": {
                        "type": "string",
                        "description": "Адрес сайта ('youtube.com') или поисковый запрос ('погода в Москве')",
                    }
                },
                "required": ["url_or_query"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "open_website": open_website,
}
