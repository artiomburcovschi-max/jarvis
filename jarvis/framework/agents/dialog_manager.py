"""
Диалоговый менеджер поверх Gemini (google-genai SDK).

Архитектура:
  распознанный текст команды --> DialogManager.handle() --> текст ответа

Внутри используется Automatic Function Calling (AFC) SDK: инструменты из
tools.py передаются как обычные Python-функции, а сам SDK решает, вызывать
их или нет, выполняет вызов и достраивает финальный текстовый ответ. Это
сильно проще и надёжнее ручного парсинга function_call/function_response.

Управление историей диалога:
  - reset() вызывается сервером, когда открывается НОВАЯ сессия (сказано
    "Джарвис" после долгого перерыва) - предотвращает "раздувание контекста"
    старыми, не относящимися к делу репликами.
  - Внутри одной активной сессии история копится, но не бесконечно -
    HISTORY_MAX_TURNS ограничивает её сверху (старые реплики обрезаются),
    чтобы не раздувать промпт (и счёт за токены) в длинном разговоре.

Обработка сбоев:
  - Нет API-ключа: явная понятная ошибка при старте, а не туманный traceback
    при первом запросе.
  - Сеть/квота/таймаут недоступны: handle() не бросает исключение наружу -
    возвращает вежливый текст об ошибке, чтобы один сбой LLM не уронил весь
    сервер (тот же принцип изоляции ошибок, что и в transcribe_worker).
"""

import os

from google import genai
from google.genai import types

from .tools import ALL_TOOLS

DEFAULT_MODEL = "gemini-2.5-flash"
# ВНИМАНИЕ: Google планирует вывести gemini-2.5-flash из эксплуатации
# 16 октября 2026 - к этому моменту нужно будет переключиться на
# gemini-3-flash или gemini-3.1-flash-lite (смотри README).

HISTORY_MAX_TURNS = 16          # ~8 обменов репликами туда-обратно
MAX_TOOL_HOPS = 4               # предохранитель от слишком длинных цепочек вызовов инструментов
REQUEST_TIMEOUT_MS = 15_000     # таймаут запроса к Gemini

SYSTEM_INSTRUCTION = (
    "Ты Джарвис - голосовой ассистент. Тебе приходит текст, уже "
    "распознанный из речи пользователя (могут быть мелкие ошибки "
    "распознавания). Отвечай кратко и по-русски - твой ответ будет "
    "озвучен вслух через синтез речи, поэтому не используй списки, "
    "markdown, эмодзи и длинные пояснения. Если нужно посчитать или "
    "узнать точное время/дату - используй инструменты, а не считай в уме."
)


class DialogManagerError(Exception):
    """Ошибка конфигурации диалогового менеджера (например, нет API-ключа)."""


class DialogManager:
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise DialogManagerError(
                "Не найден GEMINI_API_KEY. Получи ключ на https://aistudio.google.com/apikey "
                "и установи переменную окружения: export GEMINI_API_KEY=... "
                "(см. README.md, раздел про LLM-слой)."
            )

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._history: list[types.Content] = []

    def reset(self):
        """Начать разговор с чистого листа (новая сессия после долгой паузы)."""
        self._history = []

    def _trim_history(self):
        if len(self._history) > HISTORY_MAX_TURNS:
            # Обрезаем с начала, оставляя только последние N реплик - это
            # именно то место, где решается "раздувание контекста" из
            # чек-листа: контекст не растёт бесконечно.
            self._history = self._history[-HISTORY_MAX_TURNS:]

    def handle(self, user_text: str) -> str:
        """Отправляет реплику пользователя в Gemini и возвращает текст ответа.

        Не бросает исключений наружу - при любой ошибке (сеть, квота,
        таймаут, неожиданный формат ответа) возвращает вежливое сообщение
        об ошибке, чтобы вызывающий код (server.py) не падал.
        """
        self._history.append(
            types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
        )

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=ALL_TOOLS,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=MAX_TOOL_HOPS,
            ),
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=self._history,
                config=config,
            )
        except Exception as e:
            # Сеть недоступна / квота исчерпана / таймаут / что угодно ещё -
            # не роняем сервер, отвечаем как есть.
            print(f"[DialogManager] Ошибка запроса к Gemini: {e!r}")
            # Откатываем последнюю реплику пользователя, раз ответа на неё
            # не будет - иначе следующий запрос пойдёт с "оборванной" историей.
            self._history.pop()
            return "Не могу сейчас связаться с сервером LLM, проверь интернет-соединение."

        # AFC-история уже включает исходные contents + все промежуточные
        # вызовы инструментов и их результаты - используем её как новую
        # историю диалога и вручную дописываем финальный текстовый ответ
        # модели (AFC его в historиЮ не кладёт, только промежуточные шаги).
        if response.automatic_function_calling_history:
            self._history = list(response.automatic_function_calling_history)

        if response.candidates and response.candidates[0].content:
            self._history.append(response.candidates[0].content)

        self._trim_history()

        text = (response.text or "").strip()
        if not text:
            return "Понял, но ответить нечем - модель вернула пустой ответ."
        return text
