
import os

from google import genai
from google.genai import types

from .tools import ALL_TOOLS

DEFAULT_MODEL = "gemini-2.5-flash"

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
            )

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._history: list[types.Content] = []

    def reset(self):
        """Начать разговор с чистого листа (новая сессия после долгой паузы)."""
        self._history = []

    def _trim_history(self):
        if len(self._history) > HISTORY_MAX_TURNS:
            self._history = self._history[-HISTORY_MAX_TURNS:]

    def handle(self, user_text: str) -> str:
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
            print(f"[DialogManager] Ошибка запроса к Gemini: {e!r}")
            self._history.pop()
            return "Не могу сейчас связаться с сервером LLM, проверь интернет-соединение."
        if response.automatic_function_calling_history:
            self._history = list(response.automatic_function_calling_history)

        if response.candidates and response.candidates[0].content:
            self._history.append(response.candidates[0].content)

        self._trim_history()

        text = (response.text or "").strip()
        if not text:
            return "Понял, но ответить нечем - модель вернула пустой ответ."
        return text
