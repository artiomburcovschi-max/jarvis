"""
Диалоговый менеджер поверх OpenAI-совместимого API (OpenRouter и т.п.).

Архитектура:
  распознанный текст команды --> DialogManager.handle_streaming() -->
  callback на каждое готовое предложение (для немедленной озвучки) -->
  полный текст ответа в конце (для истории/UI)

В отличие от Gemini SDK, здесь нет "автоматического" вызова инструментов -
цикл написан вручную: модель просит вызвать функцию -> мы её вызываем ->
результат уходит обратно -> модель либо просит ещё один вызов, либо
отвечает текстом. Это стандартный протокол OpenAI function calling, который
одинаково работает через OpenRouter с любым провайдером.

Раунд 10 - стриминг ответа: раньше handle() ждал ПОЛНЫЙ ответ модели,
прежде чем что-либо озвучить - на ответе из нескольких предложений это
несколько секунд тишины, прежде чем Джарвис открывал рот. Теперь
handle_streaming() читает ответ по кусочкам (Server-Sent Events) и зовёт
on_sentence_ready() на каждое законченное предложение сразу, как оно
готово - первая фраза начинает звучать, пока LLM ещё генерирует продолжение.

Управление историей диалога:
  - reset() вызывается сервером, когда открывается НОВАЯ сессия (сказано
    "Джарвис" после долгого перерыва) - предотвращает "раздувание контекста"
    старыми, не относящимися к делу репликами.
  - Внутри одной сессии история копится, но обрезается по HISTORY_MAX_TURNS
    ЦЕЛЫМИ ходами (никогда не разрывая tool_call и его tool-ответ посередине -
    это сломало бы протокол и провайдер вернул бы ошибку).
  - Раунд 28 (E2): выпадающие при обрезке реплики не пропадают молча - см.
    _update_summary()/self._history_summary ниже, и conversation_log.py -
    полный транскрипт отдельно пишется на диск (НЕ читается обратно, только
    для юзера почитать/погрепать позже).

Обработка сбоев:
  - Нет API-ключа: явная понятная ошибка при старте, а не туманный traceback
    при первом запросе.
  - Сеть/квота/таймаут недоступны: handle_streaming() не бросает исключение
    наружу - зовёт on_sentence_ready() с вежливым текстом об ошибке и
    откатывает историю, чтобы не оставить в ней реплику без ответа.

Раунд 23 (B6) - локальный fallback без интернета:
  Если облачный провайдер недоступен (см. _create_stream()) И настроен
  локальный OpenAI-совместимый сервер (LM Studio/Ollama/llama-server и
  т.п. - LLM_FALLBACK_BASE_URL/_MODEL) - автоматически пробуем ЕГО, прежде
  чем сдаться с ошибкой. Плюс ручной офлайн-режим - голосовая команда
  "перейди в офлайн" (intent_router.py, флаг в llm_mode.py) заранее
  пропускает попытку облака вовсе. Если fallback не настроен - поведение
  не отличается от того, что было до этого раунда (единственная ошибка).
  Локальная модель обычно заметно слабее облачной - это осознанный
  компромисс "что-то ответит" против "тишина/явная ошибка", а не попытка
  притвориться, что качество не изменилось.

Раунд 24 (C5) - AT-SPI-инструменты доступны ТОЛЬКО локально:
  _create_stream() отправляет облачному провайдеру УРЕЗАННЫЙ список
  инструментов - без всего из agents.tools.LOCAL_ONLY_TOOLS (см.
  atspi_control.py) - облачная модель структурно не видит эти инструменты.
  _execute_tool_call(is_local=...) - второй, защитный слой: отказывает в
  выполнении local-only инструмента, даже если он почему-то был запрошен
  во время облачного хопа (на случай, если какой-то провайдер пропустит
  "галлюцинированный" вызов инструмента вне присланного списка).

Раунд 29 (E3) - "мультиагент" (рутина локально / тяжёлое в облако):
  _is_routine_query() - грубая эвристика по длине фразы в словах
  (config.yaml, multi_agent.routine_max_words). Если fallback настроен и
  запрос похож на рутину - _create_stream() пробует ЛОКАЛЬНЫЙ клиент
  ПЕРВЫМ (быстрее/дешевле для простых вещей), эскалирует в облако только
  при технической неудаче (сеть/модель недоступна) - НЕ судит качество
  ответа, это был бы ещё один LLM-вызов. Тяжёлые запросы и любой запрос
  без настроенного fallback идут ровно тем же путём, что и до этого
  раунда (облако первым) - E3 не меняет поведение без сконфигурированного
  fallback вообще ни на йоту.
"""

import json
import re
from typing import Callable

from .llm_client import LLMClientError, create_client, create_fallback_client
from . import llm_mode
from .tools import TOOL_IMPLEMENTATIONS, TOOL_SCHEMAS, LOCAL_ONLY_TOOLS, load_facts_summary
from .confirmation import DANGEROUS_TOOLS
import conversation_log
import gap_log
from jarvis_config import get as config_get

HISTORY_MAX_TURNS = 8            # сколько последних ходов (user->...->assistant) хранить
MAX_TOOL_HOPS = 4                # предохранитель от зацикливания на вызовах инструментов
SUMMARY_MAX_TOKENS = 150         # раунд 28 (E2) - лимит для сжатия выпадающих реплик в резюме
REQUEST_TIMEOUT_SECONDS = 15.0

# Без явного max_tokens запрос по умолчанию просит "сколько модель может"
# (у Gemini 2.5 Flash это 65535) - на аккаунте с нулевым/маленьким балансом
# OpenRouter отвечает 402 "requires more credits", даже если реальный ответ
# был бы коротким: провайдер резервирует бюджет под ЗАПРОШЕННЫЙ максимум, а
# не под фактически использованный. Голосовому ассистенту и не нужны тысячи
# токенов на ответ - ограничиваем сами, заодно это чуть быстрее и дешевле.
MAX_RESPONSE_TOKENS = 500

SYSTEM_PROMPT = (
    "Ты Джарвис - голосовой ассистент. Тебе приходит текст, уже "
    "распознанный из речи пользователя (могут быть мелкие ошибки "
    "распознавания). Отвечай кратко и по-русски - твой ответ будет "
    "озвучен вслух через синтез речи, поэтому не используй списки, "
    "markdown, эмодзи и длинные пояснения. Если нужно посчитать или "
    "узнать точное время/дату - используй инструменты, а не считай в уме."
)


def _is_routine_query(user_text: str) -> bool:
    """Раунд 29 (E3) - "мультиагент": грубая, ДЕШЁВАЯ эвристика "рутина
    или тяжёлое", без обращения к LLM (само обращение к LLM ради решения
    "спросить ли LLM" бессмысленно). Юзер явно попросил "по сложности,
    чтобы долго не думал" - значит простой, предсказуемый критерий, а не
    попытка тонко угадать намерение по ключевым словам (список триггер-слов
    для "сложности" был бы хрупким и непрозрачным - откуда ни возьмись
    отказ распознавать "тяжёлый" запрос как тяжёлый).

    Критерий - длина фразы в словах (config.yaml, multi_agent.routine_max_words,
    по умолчанию 8). Короткие фразы ЧАСТО рутина ("включи Спотифай",
    "который час"), длинные - часто задачи с реальным рассуждением - это
    не идеально (короткое "почему небо синее" тоже попадёт в "рутину"), но
    предсказуемо и прозрачно, глядя в конфиг."""
    max_words = int(config_get("ROUTINE_MAX_WORDS", "multi_agent.routine_max_words", 8))
    return len(user_text.split()) <= max_words


def _build_system_prompt(history_summary: str = "") -> str:
    """Раунд 26 (C7): подмешивает долгосрочные факты (remember_fact/
    forget_fact, agents/tools/memory.py) в системный промпт КАЖДОГО
    запроса - в отличие от self._history, эти факты переживают и
    перезапуск процесса, и конец текущего разговора.

    Раунд 28 (E2): вторым отдельным блоком подмешивает history_summary -
    сжатое резюме более РАННЕЙ части ЭТОГО ЖЕ разговора (см.
    DialogManager._update_summary()), если она уже выпала из живого окна
    self._history (HISTORY_MAX_TURNS). В отличие от фактов - это НЕ
    переживает перезапуск (тот же принцип "не тянуть обрывок случайного
    старого диалога", что и вся self._history, раунд 11) и не про
    пользователя вообще, а конкретно про то, что уже обсуждали В ЭТОМ
    разговоре, но что больше не помещается в окно дословной истории.

    Если ни фактов, ни резюме нет - промпт совпадает с тем, что было до
    раунда 26, один в один."""
    facts_block = load_facts_summary()
    prompt = SYSTEM_PROMPT
    if facts_block:
        prompt += (
            "\n\nЧто ты уже знаешь о пользователе и его проектах (запомнено "
            "в прошлых разговорах, а не только в этом):\n" + facts_block
        )
    if history_summary:
        prompt += (
            "\n\nКраткое резюме более ранней части ЭТОГО разговора (детали "
            "уже не хранятся дословно, только общий смысл):\n" + history_summary
        )
    return prompt

# Ищем границу предложения: пунктуация конца предложения + пробел/конец
# строки. Не идеально (не отличает "3.14" от "3. Дальше" - редкий и
# безобидный краевой случай для голосового ответа), но простое и надёжное
# решение без внешних NLP-библиотек ради разбивки на предложения.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")


def _extract_ready_sentence(buffer: str) -> tuple[str | None, str]:
    """Если в буфере есть законченное предложение - возвращает (предложение,
    остаток), иначе (None, буфер_как_есть)."""
    match = _SENTENCE_END_RE.search(buffer)
    if not match:
        return None, buffer
    return buffer[: match.start() + 1].strip(), buffer[match.end():]


class DialogManagerError(Exception):
    """Ошибка конфигурации диалогового менеджера (например, нет API-ключа)."""


class DialogManager:
    def __init__(self):
        try:
            self._client, self._model = create_client()
        except LLMClientError as e:
            raise DialogManagerError(str(e)) from e

        # Раунд 23 (B6): необязательный локальный fallback (LM Studio и
        # т.п.) - None, если не настроен (LLM_FALLBACK_BASE_URL/_MODEL не
        # заданы) - тогда поведение при недоступности облака не меняется:
        # единственная понятная ошибка, как и раньше этого раунда.
        fallback = create_fallback_client()
        if fallback is not None:
            self._fallback_client, self._fallback_model = fallback
        else:
            self._fallback_client, self._fallback_model = None, None

        self._history: list[dict] = []
        # Раунд 28 (E2): сжатое резюме той части этой сессии, которая уже
        # выпала из self._history при обрезке (_trim_history/_update_summary
        # ниже) - НЕ переживает перезапуск, тот же принцип, что и вся
        # self._history (раунд 11).
        self._history_summary: str = ""
        # C3: если не None - модель попросила вызвать "опасный" инструмент
        # (см. confirmation.DANGEROUS_TOOLS), но он ЕЩЁ НЕ выполнен - ждём
        # явного да/нет от пользователя следующей репликой. server.py читает
        # это поле сразу после handle_streaming() и решает, что делать со
        # следующей фразой (см. server.py: match_confirmation_reply).
        self.pending_confirmation: "dict | None" = None

    def reset(self):
        """Начать разговор с чистого листа (новая сессия после долгой паузы)."""
        self._history = []
        self._history_summary = ""
        self.pending_confirmation = None

    def _append_history(self, entry: dict) -> None:
        """Единая точка добавления в self._history - централизует запись
        полного транскрипта на диск (раунд 28, E2, conversation_log.py)
        рядом с самим добавлением, чтобы будущие новые точки append() не
        забыли про лог."""
        self._history.append(entry)
        conversation_log.log_message(entry.get("role", "?"), entry.get("content") or "")

    def _trim_history(self):
        # Обрезаем ЦЕЛЫМИ ходами с начала, а не как попало: находим индекс
        # начала второго по счёту хода (role == "user") и отрезаем всё до
        # него. Так мы никогда не оторвём tool_call от его tool-ответа -
        # оторванный tool_call без ответа - это ошибка протокола, а не
        # просто "немного потерянный контекст".
        while len(self._history) > HISTORY_MAX_TURNS * 2:
            user_indices = [i for i, m in enumerate(self._history) if m["role"] == "user"]
            if len(user_indices) < 2:
                break
            # Раунд 28 (E2): раньше выпадающие реплики просто отбрасывались
            # без следа - теперь сжимаются в резюме (_update_summary), а не
            # молча теряются, пока разговор не станет длиннее HISTORY_MAX_TURNS.
            dropped = self._history[:user_indices[1]]
            self._history = self._history[user_indices[1]:]
            self._update_summary(dropped)

    def _pick_summary_client(self):
        """Раунд 28 (E2): для сжатия истории в резюме - тот же выбор
        облако/локально, что и llm_mode.is_forced_offline() даёт для
        основных ответов (B6), но БЕЗ автоматического фолбэка при сбое -
        это вспомогательная, необязательная функция: если выбранный
        вариант не сработал, резюме просто не обновляется в этот раз
        (см. _update_summary), не стоит того, чтобы дублировать всю
        retry-логику _create_stream() ради второстепенной фичи."""
        if llm_mode.is_forced_offline():
            if self._fallback_client is not None:
                return self._fallback_client, self._fallback_model
            return None, None
        return self._client, self._model

    def _update_summary(self, dropped_messages: list[dict]) -> None:
        """Раунд 28 (E2): сжимает выпадающие из окна истории реплики в
        короткое резюме (отдельный маленький LLM-вызов - БЕЗ инструментов,
        БЕЗ остального контекста разговора) и объединяет с уже накопленным
        self._history_summary в одно обновлённое резюме, а не накапливает
        цепочку резюме одно поверх другого.

        Если вызов не удался (нет ни облака, ни доступного fallback, сеть
        упала) - резюме остаётся как было, выпавшие реплики теряются
        молча в ЭТОТ раз, но не роняем основной поток ответа пользователю
        ради вспомогательной фичи - раунд 27 gap_log фиксирует случай для
        видимости, что резюмирование деградировало."""
        client, model = self._pick_summary_client()
        if client is None:
            return

        dropped_text = "\n".join(
            f"{m['role']}: {m.get('content') or '[вызов инструмента]'}" for m in dropped_messages
        )
        prompt = (
            "Сожми это в краткое резюме (2-4 предложения, по-русски, без "
            "markdown) того, что важно помнить из этой части разговора для "
            "дальнейшего общения. Если ниже уже есть предыдущее резюме - "
            "объедини его с новыми репликами в одно короткое обновлённое "
            "резюме, не создавай два отдельных.\n\n"
        )
        if self._history_summary:
            prompt += f"Предыдущее резюме: {self._history_summary}\n\n"
        prompt += f"Новые реплики:\n{dropped_text}"

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=SUMMARY_MAX_TOKENS,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            summary = (response.choices[0].message.content or "").strip()
            if summary:
                self._history_summary = summary
        except Exception as e:
            print(f"[DialogManager] Не удалось сжать историю в резюме: {e!r}")
            gap_log.log_gap("summary_failed", repr(e))

    def _execute_tool_call(self, tool_call_name: str, tool_call_arguments: str,
                            is_local: bool = False) -> str:
        try:
            args = json.loads(tool_call_arguments or "{}")
        except json.JSONDecodeError:
            return json.dumps({"error": "Модель передала некорректные аргументы (не JSON)"})

        # Раунд 24 (C5): защита "на всякий случай", ВТОРЫМ слоем поверх
        # фильтрации схемы в _create_stream() - облачный запрос вообще не
        # получает схему этих инструментов, так что модель о них не знает,
        # но некоторые провайдеры пропускают "галлюцинированные" вызовы
        # инструментов, имени которых не было в списке. Если такое
        # произошло на облачном хопе - отказываем прямо здесь, а не молча
        # выполняем локально-only действие по итогам облачного запроса.
        if tool_call_name in LOCAL_ONLY_TOOLS and not is_local:
            return json.dumps({
                "error": f"Инструмент {tool_call_name} доступен только в локальном/"
                         f"офлайн режиме, а не через облачную модель.",
            }, ensure_ascii=False)

        if tool_call_name in DANGEROUS_TOOLS:
            # C3: НЕ выполняем - откладываем до явного подтверждения "да" от
            # пользователя следующей репликой (см. handle_streaming ниже,
            # где по факту установки pending_confirmation обрывается цикл
            # хопов и задаётся детерминированный вопрос вместо ответа
            # модели). Возвращаем ЧТО-ТО как tool-ответ, потому что протокол
            # OpenAI требует ответ на каждый tool_call - но реального
            # значения этот текст не имеет, модель его всё равно не увидит
            # (мы обрываем цикл раньше, чем модель прочтёт этот ответ).
            self.pending_confirmation = {"tool_name": tool_call_name, "tool_args": args}
            return json.dumps({"status": "requires_user_confirmation"}, ensure_ascii=False)

        impl = TOOL_IMPLEMENTATIONS.get(tool_call_name)
        if impl is None:
            return json.dumps({"error": f"Неизвестный инструмент: {tool_call_name}"})

        try:
            result = impl(**args)
        except Exception as e:
            result = {"error": f"Ошибка при выполнении инструмента {tool_call_name}: {e}"}
            # Раунд 27: НЕПРЕДВИДЕННОЕ исключение из tool'а (не обычный
            # {"error": ...} ответ, который инструмент вернул сам, а
            # реальный краш реализации) - стоит записи в лог трения, юзер
            # разберётся, баг ли это или недостающая проверка входных данных.
            gap_log.log_gap("tool_exception", f"{tool_call_name}: {e}")

        return json.dumps(result, ensure_ascii=False)

    def _create_stream(self, messages: list[dict], user_text: str = ""):
        """Раунд 23 (B6): создаёт стрим у облачного провайдера, а при
        неудаче (или в форсированном офлайн-режиме) - у локального
        fallback, если он настроен.

        Возвращает (stream, used_fallback: bool). Бросает исключение
        наружу, только если ВСЕ доступные варианты не сработали (или
        единственный доступный - облако, и оно не сработало, как и до
        этого раунда) - вызывающий код (handle_streaming) ловит его одним
        try/except, как раньше.

        llm_mode.is_forced_offline() (голосовая команда "перейди в
        офлайн") пропускает попытку облака вовсе - незачем ждать таймаут
        заведомо не нужного запроса, если пользователь явно попросил
        работать локально.

        Раунд 24 (C5): облако получает УРЕЗАННЫЙ список инструментов -
        без всего из LOCAL_ONLY_TOOLS (AT-SPI и подобное) - облачная
        модель структурно не видит эти инструменты и не может их
        вызвать. Fallback/локальная модель получает ПОЛНЫЙ список.

        Раунд 29 (E3) - "мультиагент": ЕСЛИ fallback настроен и запрос
        похож на рутину (_is_routine_query) - пробуем ЛОКАЛЬНО ПЕРВЫМ, а
        не облако. Дешевле и быстрее для простых вещей. НЕ судим качество
        локального ответа (это был бы ещё один LLM-вызов, противоречащий
        самой цели экономии) - эскалируем в облако, только если локальный
        вызов реально не удался ТЕХНИЧЕСКИ (сеть/модель недоступна), точно
        так же, как облако эскалирует в fallback при сбое. Тяжёлые запросы
        и любой запрос без настроенного fallback идут ровно тем же путём,
        что и до этого раунда (облако первым) - E3 не меняет поведение,
        если fallback не настроен, вообще ни на йоту.
        """
        forced_offline = llm_mode.is_forced_offline()

        routine_first_to_local = (
            not forced_offline
            and self._fallback_client is not None
            and _is_routine_query(user_text)
        )
        if routine_first_to_local:
            try:
                stream = self._fallback_client.chat.completions.create(
                    model=self._fallback_model,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    max_tokens=MAX_RESPONSE_TOKENS,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    stream=True,
                )
                return stream, True
            except Exception as e:
                print(f"[DialogManager] Локальная модель недоступна для рутинного "
                      f"запроса ({e!r}) - эскалирую в облако...")
                # Падаем ниже, в обычную ветку "облако первым" - НЕ raise
                # здесь: рутинный запрос просто ведёт себя как тяжёлый,
                # если локалка не ответила.

        if not forced_offline:
            cloud_tools = [s for s in TOOL_SCHEMAS if s["function"]["name"] not in LOCAL_ONLY_TOOLS]
            try:
                stream = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=cloud_tools,
                    tool_choice="auto",
                    max_tokens=MAX_RESPONSE_TOKENS,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    stream=True,
                )
                return stream, False
            except Exception as e:
                if self._fallback_client is None:
                    raise
                print(f"[DialogManager] Облако недоступно ({e!r}), "
                      f"пробую локальный fallback ({self._fallback_model})...")
        elif self._fallback_client is None:
            # Форсированный офлайн, но fallback не настроен - облако даже
            # не пробуем (пользователь явно попросил не ходить в сеть),
            # сразу честная ошибка вместо непонятного зависания.
            raise RuntimeError(
                "включён офлайн-режим, но локальный fallback (LLM_FALLBACK_BASE_URL) не настроен"
            )

        stream = self._fallback_client.chat.completions.create(
            model=self._fallback_model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            max_tokens=MAX_RESPONSE_TOKENS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            stream=True,
        )
        return stream, True

    def handle(self, user_text: str) -> str:
        """Не-стриминговая версия - удобна для тестов/скриптов. Собирает все
        предложения из handle_streaming() в одну строку и возвращает её."""
        parts: list[str] = []
        return self.handle_streaming(user_text, on_sentence_ready=parts.append) or " ".join(parts)

    def handle_streaming(
        self,
        user_text: str,
        on_sentence_ready: Callable[[str], None],
        should_abort: Callable[[], bool] = lambda: False,
        on_tool_call: Callable[[str, dict, dict], None] = lambda name, args, result: None,
    ) -> str:
        """Отправляет реплику пользователя в LLM, стримит ответ по кусочкам.

        on_sentence_ready(sentence) зовётся на каждое законченное предложение
        сразу, как оно готово - ДО того, как весь ответ модели завершится.
        Это и даёт быструю озвучку: первая фраза начинает звучать, пока LLM
        ещё генерирует продолжение (см. voice/streaming_speaker.py).

        on_tool_call(name, args, result) - раунд 18 (C4, audit-лог) - зовётся
        на КАЖДЫЙ реальный вызов инструмента (name - имя, args - разобранные
        аргументы, result - разобранный JSON-результат: {"result": ...} или
        {"error": ...}). Зовётся и для перехваченных "опасных" вызовов тоже
        (см. DANGEROUS_TOOLS) - result в этом случае будет
        {"status": "requires_user_confirmation"}, чтобы в UI было видно, что
        действие ЗАПРОШЕНО, а не молча выполнено. По умолчанию no-op - вызов
        без колбэка (например, из тестов) ничего не ломает.

        should_abort() проверяется в трёх местах, а не только "после каждого
        кусочка от LLM" - это важно для честной отмены:
          1. В начале КАЖДОГО хопа, до сетевого запроса - если пользователя
             уже перебило между хопами (например, во время выполнения
             инструмента), не тратим ещё один запрос к LLM впустую.
          2. Внутри чтения потока - как и раньше, после каждого чанка.
          3. Перед выполнением КАЖДОГО инструмента - если прервали ровно в
             момент "модель попросила вызвать функцию, но мы её ещё не
             выполнили", не дёргаем реальное действие (открыть браузер,
             громкость и т.п.) ради ответа, который уже никто не услышит.

        Не бросает исключений наружу - при любой ошибке или отмене зовёт
        (для ошибок) on_sentence_ready() с вежливым текстом, и откатывает
        историю ПОЛНОСТЬЮ до состояния "как будто этой реплики не было" -
        через снимок длины истории (_history_len_before), а не через
        pop() одного элемента. Это принципиально при отмене на позднем
        хопе: к этому моменту в истории уже могут лежать этот user-ход,
        assistant-ход с tool_calls и несколько tool-ответов - одиночный
        pop() убрал бы не то, что нужно, и мог бы оставить "подвешенный"
        tool_call без ответа в постоянной истории, что сломало бы протокол
        (и любой следующий запрос к LLM) на ровном месте.
        """
        history_len_before = len(self._history)
        # C3: сбрасываем с чистого листа - pending_confirmation отражает
        # РЕЗУЛЬТАТ именно ЭТОГО вызова (server.py читает его сразу после
        # возврата), а не что-то, что могло остаться от предыдущего хода.
        self.pending_confirmation = None

        def _rollback():
            """Откатывает историю целиком до состояния до этой реплики -
            используется и при ошибках, и при отмене (barge-in)."""
            del self._history[history_len_before:]

        self._append_history({"role": "user", "content": user_text})
        messages = [{"role": "system", "content": _build_system_prompt(self._history_summary)}] + self._history
        full_text_parts: list[str] = []

        for hop in range(MAX_TOOL_HOPS):
            if should_abort():
                print("[DialogManager] Отменено ДО начала хопа "
                      f"#{hop} (перебили между шагами) - запрос к LLM не отправляется.")
                _rollback()
                return "".join(full_text_parts)

            try:
                stream, used_fallback = self._create_stream(messages, user_text)
                if used_fallback:
                    print(f"[DialogManager] Хоп #{hop} обслужен локальным fallback "
                          f"({self._fallback_model}), не облаком.")
            except Exception as e:
                print(f"[DialogManager] Ошибка запроса к LLM ({self._model}): {e!r}")
                _rollback()
                if self._fallback_client is not None:
                    error_text = ("Не могу связаться ни с облачной, ни с локальной моделью - "
                                   "проверь интернет и что локальный сервер (LM Studio и т.п.) запущен.")
                else:
                    error_text = "Не могу сейчас связаться с LLM-сервером, проверь интернет или ключ API."
                # Раунд 27: LLM полностью недоступна (и облако, и локальный
                # fallback, если он был) - самый серьёзный вид затыка,
                # обязательно в лог трения.
                gap_log.log_gap("llm_unavailable", repr(e), user_text)
                on_sentence_ready(error_text)
                return error_text

            content_buffer = ""
            tool_calls_accum: dict[int, dict] = {}
            finish_reason = None
            aborted = False

            try:
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

                    if delta.content:
                        content_buffer += delta.content
                        full_text_parts.append(delta.content)
                        while True:
                            sentence, content_buffer = _extract_ready_sentence(content_buffer)
                            if sentence is None:
                                break
                            on_sentence_ready(sentence)

                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            entry = tool_calls_accum.setdefault(
                                tc_delta.index, {"id": None, "name": "", "arguments": ""}
                            )
                            if tc_delta.id:
                                entry["id"] = tc_delta.id
                            if tc_delta.function and tc_delta.function.name:
                                entry["name"] += tc_delta.function.name
                            if tc_delta.function and tc_delta.function.arguments:
                                entry["arguments"] += tc_delta.function.arguments

                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                    if should_abort():
                        aborted = True
                        print("[DialogManager] Ответ прерван пользователем (barge-in) - "
                              "прекращаю читать поток раньше времени.")
                        break
            except Exception as e:
                print(f"[DialogManager] Ошибка при чтении потока от LLM: {e!r}")
                if not content_buffer.strip() and not full_text_parts:
                    _rollback()
                    error_text = "Соединение с LLM оборвалось на середине ответа, попробуй ещё раз."
                    on_sentence_ready(error_text)
                    return error_text
                # Частичный ответ уже был - просто заканчиваем, что успели
                finish_reason = finish_reason or "stop"

            # Остаток буфера без завершающей пунктуации - тоже озвучиваем
            # (иначе последний обрывок фразы без точки в конце потерялся бы).
            if content_buffer.strip() and not aborted:
                on_sentence_ready(content_buffer.strip())

            full_content = "".join(full_text_parts)

            if aborted:
                # ВАЖНО: если модель успела начать накапливать tool_calls в
                # ЭТОМ прерванном хопе - НЕ кладём assistant-сообщение с ними
                # в историю. Мы их не выполнили (и не выполним - ответ уже
                # никому не нужен), а assistant-сообщение с tool_calls без
                # немедленно следующих за ним tool-ответов - невалидная
                # история с точки зрения протокола: следующий же запрос к
                # LLM с такой историей вернётся ошибкой (dangling tool_call).
                # Откатываем всю реплику целиком - как будто её не было,
                # следующая (перебивающая) фраза придёт как новая, чистая.
                _rollback()
                return full_content

            assistant_entry: dict = {"role": "assistant", "content": full_content or None}
            if tool_calls_accum:
                assistant_entry["tool_calls"] = [
                    {
                        "id": entry["id"],
                        "type": "function",
                        "function": {"name": entry["name"], "arguments": entry["arguments"]},
                    }
                    for entry in tool_calls_accum.values()
                ]
            self._append_history(assistant_entry)
            messages.append(assistant_entry)

            if not tool_calls_accum:
                self._trim_history()
                return full_content or "Понял, но ответить нечем - модель вернула пустой ответ."

            # Модель попросила вызвать один или несколько инструментов -
            # выполняем и кладём результаты обратно в историю, затем идём
            # на следующий круг цикла (снова стримим).
            #
            # should_abort() проверяется ПЕРЕД каждым вызовом (а не после) -
            # если перебили ровно в этот момент, дальнейшие инструменты в
            # списке (и их реальные побочные эффекты - открыть браузер,
            # поменять громкость и т.п.) не выполняются вовсе. Уже
            # выполненные к этому моменту инструменты (и их tool-ответы) в
            # истории уже есть - откатываем ЦЕЛИКОМ через _rollback(), а не
            # оставляем "наполовину отвеченный" ход в истории.
            for entry in tool_calls_accum.values():
                if should_abort():
                    print("[DialogManager] Отменено перед вызовом инструмента "
                          f"'{entry['name']}' (перебили) - инструмент НЕ вызывается.")
                    _rollback()
                    return full_content
                tool_result_json = self._execute_tool_call(
                    entry["name"], entry["arguments"], is_local=used_fallback,
                )
                tool_entry = {"role": "tool", "tool_call_id": entry["id"], "content": tool_result_json}
                self._append_history(tool_entry)
                messages.append(tool_entry)

                # C4 (audit-лог): сообщаем о РЕАЛЬНО случившемся вызове -
                # даже для перехваченных опасных вызовов (см. ниже) тоже,
                # с result={"status": "requires_user_confirmation"} - в UI
                # должно быть видно, что действие ЗАПРОШЕНО, а не тихо
                # выполнено само по себе.
                try:
                    parsed_args = json.loads(entry["arguments"] or "{}")
                except json.JSONDecodeError:
                    parsed_args = {}
                try:
                    parsed_result = json.loads(tool_result_json)
                except json.JSONDecodeError:
                    parsed_result = {"error": "не удалось разобрать результат"}
                on_tool_call(entry["name"], parsed_args, parsed_result)

                if self.pending_confirmation is not None:
                    # C3: этот конкретный вызов - "опасный" инструмент
                    # (см. _execute_tool_call/DANGEROUS_TOOLS) - он НЕ
                    # выполнен. Останавливаемся ЗДЕСЬ, не идём ни на
                    # следующий хоп (не даём модели самой решать, как
                    # сформулировать вопрос - см. docstring confirmation.py),
                    # ни к оставшимся tool_calls в ЭТОМ ЖЕ батче, если их
                    # несколько - пока не подтверждено ПЕРВОЕ опасное
                    # действие, остальное подождёт.
                    tool_display_name = DANGEROUS_TOOLS.get(entry["name"], entry["name"])
                    question = f"Точно {tool_display_name}? Скажи «да» или «нет»."
                    confirmation_entry = {"role": "assistant", "content": question}
                    self._append_history(confirmation_entry)
                    self._trim_history()
                    on_sentence_ready(question)
                    return question

            full_text_parts = []  # начинаем собирать текст СЛЕДУЮЩЕГО круга с нуля

        self._trim_history()
        error_text = "Не получилось довести задачу до конца за разумное число шагов, попробуй переформулировать."
        # Раунд 27: агент сдался, не решив задачу за MAX_TOOL_HOPS шагов -
        # либо задача реально требует больше шагов (можно поднять лимит),
        # либо модель зациклилась - в любом случае стоит записи в лог трения.
        gap_log.log_gap("hop_limit_reached", f"исчерпано {MAX_TOOL_HOPS} шагов", user_text)
        on_sentence_ready(error_text)
        return error_text
