"""
Intent-router: распознаёт САМЫЕ частые бытовые команды (громкость, медиа,
таймер, "открой X") и выполняет их НАПРЯМУЮ, в обход dialog_manager/LLM.

Почему это отдельный слой, а не просто "быстрый tool":
  Обычный путь команды - Whisper -> LLM (сетевой запрос, обычно 1-3+ секунды
  до первого токена) -> tool -> TTS. Для "тише"/"пауза"/"таймер 5 минут" эта
  задержка не нужна и не ощущается как "живой" ассистент - именно к этому
  относится жалоба в аудите ("даже идеальный LLM будет ощущаться как
  медленный чат с колонкой", "бытовая команда никогда не должна ждать
  полный цикл Whisper -> OpenRouter -> tool -> Piper").

Дизайн - "уверен или не лезу":
  try_match() возвращает None при малейшей неуверенности (длинная/сложная
  фраза, нет чёткого совпадения, не распознан аргумент) - и вызывающий код
  (server.py) просто идёт по ОБЫЧНОМУ пути через LLM, как будто router'а не
  было вообще. Это принципиально: router не пытается быть NLU общего
  назначения и не обязан покрывать все формулировки - он должен быть ПРАВ
  почти всегда для тех формулировок, которые всё-таки распознал, а не
  покрывать максимум случаев ценой ложных срабатываний. Ложное срабатывание
  здесь хуже, чем лишняя секунда ожидания LLM: он либо выполнит не то
  действие (тише вместо ответа на вопрос), либо разрежет составную фразу
  (тише, потому что: за 5 минут перезвоню) на два неверных tool-вызова.

  Отсюда же ограничение по длине фразы (MAX_WORDS_FOR_INSTANT_COMMAND) -
  длинные/составные фразы почти всегда сложнее простой команды и должны
  решаться LLM, а не грубым regex.

Полностью независим от dialog_manager/LLM_API_KEY - работает даже если LLM
вообще не настроен (см. server.py: intent_router вызывается ДО проверки
"dialog_manager is None"). Тем самым закрывает часть пункта "нет локального
fallback-мозга" из аудита: пусть не "который час" (это не intent, а вопрос),
но громкость/медиа/таймер/открыть приложение будут работать без единого
обращения к сети даже без API-ключа.

Раунд 23 (B6): единственное исключение из "полностью независим" -
llm_mode.py, общий флаг "форсированный офлайн" между этим модулем (голосом
переключает) и dialog_manager (читает перед каждым запросом к LLM). Это
НЕ импорт dialog_manager - отдельный модуль без логики, специально ради
того, чтобы независимость от dialog_manager, описанная выше, не сломалась.
"""

import re

from . import llm_mode
from .tools._shared import fuzzy_lookup
from .tools.apps import ALLOWED_APPLICATIONS_LINUX, ALLOWED_APPLICATIONS_WINDOWS, open_application
from .tools.media import media_control
from .tools.timers import set_timer
from .tools.volume import adjust_volume, set_volume
from .tools._shared import IS_WINDOWS

# Длинную/составную фразу НЕ пытаемся резать по regex - отдаём LLM целиком.
# 6 слов покрывает подавляющее большинство реальных команд ("сделай
# погромче", "поставь таймер на 5 минут", "открой браузер пожалуйста") и
# при этом достаточно коротко, чтобы не поймать случайно кусок содержательной
# фразы, где просто МЕЛЬКНУЛО похожее слово.
MAX_WORDS_FOR_INSTANT_COMMAND = 6

# Порог fuzzy-совпадения для "открой X" СТРОЖЕ, чем внутри самого tool'а
# (там 60 - потому что там уже нет пути назад, LLM его вызвала осознанно).
# Здесь совпадение находим "втихую", по regex + fuzzy, без участия LLM,
# поэтому нужна более высокая уверенность, иначе рискуем открыть не то,
# что имелось в виду, при этом LLM (с её собственным порогом 60 и
# способностью переспросить) даже не увидит фразу.
APP_OPEN_FUZZY_THRESHOLD = 75

_TIMER_UNIT_SECONDS = {
    "сек": 1, "секунд": 1, "секунда": 1, "секунды": 1,
    "мин": 60, "минут": 60, "минута": 60, "минуты": 60,
    "час": 3600, "часа": 3600, "часов": 3600,
}

_TIMER_NUMBER_UNIT_RE = re.compile(
    r"(\d{1,4})\s*(секунд\w*|сек\b|минут\w*|мин\b|час\w*)"
)

_VOLUME_PERCENT_RE = re.compile(r"(\d{1,3})\s*процент")

_VOLUME_UP_WORDS = ("громче", "погромче", "прибавь", "увеличь")
_VOLUME_DOWN_WORDS = ("тише", "потише", "убавь", "уменьши")
_VOLUME_CONTEXT_WORDS = ("громкост", "звук")

_MEDIA_PAUSE_WORDS = ("пауза", "останови", "стоп")
_MEDIA_PLAY_WORDS = ("продолжи", "воспроизведи", "плей", "играй")
_MEDIA_NEXT_WORDS = ("следующ", "дальше", "переключи")
_MEDIA_PREV_WORDS = ("предыдущ", "прошлый", "назад")
_MEDIA_CONTEXT_WORDS = ("трек", "песн", "музык", "видео")

_OPEN_APP_RE = re.compile(r"(?:открой|запусти)\s+(.+)")


def _word_count(text: str) -> int:
    return len(text.split())


def _is_short_enough(text: str) -> bool:
    return _word_count(text) <= MAX_WORDS_FOR_INSTANT_COMMAND


def _tool_message(tool_result: dict) -> str:
    """Инструменты возвращают {"result": ...} или {"error": ...} - в обоих
    случаях это уже готовый для озвучки русский текст (см. tools/*.py) -
    роутеру не нужно придумывать свою формулировку."""
    return str(tool_result.get("result") or tool_result.get("error") or "Готово.")


def _match_timer(text: str) -> "str | None":
    if "таймер" not in text and "напомни" not in text:
        return None

    if re.search(r"пол\s*час", text):
        seconds = 1800
    else:
        m = _TIMER_NUMBER_UNIT_RE.search(text)
        if not m:
            # "таймер на минуту" / "напомни через час" - без числа, но с
            # единственным числом единицы измерения. Проверяем по отдельным
            # словам, иначе не распознаем - и это НОРМАЛЬНО, просто отдаём
            # LLM (она справится лучше грубого regex).
            for word, base_seconds in (("минуту", 60), ("час", 3600), ("секунду", 1)):
                if re.search(rf"\b{word}\b", text):
                    seconds = base_seconds
                    break
            else:
                return None
        else:
            number = int(m.group(1))
            unit_word = m.group(2)
            unit_seconds = None
            for prefix, mult in _TIMER_UNIT_SECONDS.items():
                if unit_word.startswith(prefix):
                    unit_seconds = mult
                    break
            if unit_seconds is None:
                return None
            seconds = number * unit_seconds

    if seconds <= 0 or seconds > 6 * 3600:
        return None  # выход за разумные пределы - пусть разбирается LLM (и объяснит пользователю, почему нет)

    result = set_timer(seconds=seconds, message="Время вышло!")
    return _tool_message(result)


def _match_volume(text: str) -> "str | None":
    percent_match = _VOLUME_PERCENT_RE.search(text)
    if percent_match and any(w in text for w in _VOLUME_CONTEXT_WORDS):
        result = set_volume(level=int(percent_match.group(1)))
        return _tool_message(result)

    if any(w in text for w in _VOLUME_UP_WORDS):
        # "громче"/"погромче" сами по себе достаточно однозначны даже без
        # слова "звук"/"громкость" рядом - короткая фраза уровня "сделай
        # погромче" уже прошла фильтр MAX_WORDS, доп. контекст не обязателен.
        result = adjust_volume(direction="up")
        return _tool_message(result)
    if any(w in text for w in _VOLUME_DOWN_WORDS):
        result = adjust_volume(direction="down")
        return _tool_message(result)
    return None


def _match_media(text: str) -> "str | None":
    has_context = any(w in text for w in _MEDIA_CONTEXT_WORDS)
    if any(w in text for w in _MEDIA_PAUSE_WORDS) or any(w in text for w in _MEDIA_PLAY_WORDS):
        result = media_control(action="play_pause")
        return _tool_message(result)
    if any(w in text for w in _MEDIA_NEXT_WORDS) and has_context:
        # "следующий"/"дальше" САМИ ПО СЕБЕ слишком неоднозначны без
        # музыкального контекста ("дальше" может значить что угодно в
        # разговоре) - здесь контекстное слово обязательно, в отличие от
        # громкости выше.
        result = media_control(action="next")
        return _tool_message(result)
    if any(w in text for w in _MEDIA_PREV_WORDS) and has_context:
        result = media_control(action="previous")
        return _tool_message(result)
    return None


def _match_open_app(text: str) -> "str | None":
    m = _OPEN_APP_RE.match(text)
    if not m:
        return None
    app_name = m.group(1).strip(" .!,")
    if not app_name:
        return None

    apps = ALLOWED_APPLICATIONS_WINDOWS if IS_WINDOWS else ALLOWED_APPLICATIONS_LINUX
    best_name, best_score = fuzzy_lookup(app_name, apps.keys())
    if best_score < APP_OPEN_FUZZY_THRESHOLD:
        # Недостаточно уверены - НЕ открываем наугад. Отдаём LLM: у неё
        # порог ниже (60) и она может либо всё равно попробовать открыть
        # правильно понятое приложение, либо переспросить.
        return None

    result = open_application(app_name=app_name)
    return _tool_message(result)


# Раунд 23 (B6): "офлайн"/"онлайн" - слова достаточно однозначные в
# бытовой русской речи для голосового ассистента (в отличие, скажем, от
# "дальше" в _match_media, которому нужен доп. контекст) - отдельного
# контекстного слова не требуем, как и для "громче"/"тише" в _match_volume.
_OFFLINE_WORDS = ("офлайн", "оффлайн")
_ONLINE_WORDS = ("онлайн",)


def _match_llm_mode(text: str) -> "str | None":
    if any(w in text for w in _OFFLINE_WORDS):
        llm_mode.set_forced_offline(True)
        return "Перехожу в офлайн-режим, буду отвечать локальной моделью."
    if any(w in text for w in _ONLINE_WORDS):
        llm_mode.set_forced_offline(False)
        return "Возвращаюсь в обычный режим, снова использую облачную модель."
    return None


# Порядок проверок - от наиболее специфичных к общим: таймер и volume-set
# с процентом требуют чисел (маловероятны как ложные срабатывания), потом
# open_app (тоже достаточно специфичный паттерн "открой/запусти"), и в конце
# volume-adjust/media - самые короткие триггерные слова, где риск ложного
# срабатывания на не по адресу сказанное слово чуть выше.
_MATCHERS = (_match_timer, _match_volume, _match_media, _match_open_app, _match_llm_mode)


def try_match(raw_text: str) -> "str | None":
    """Пытается распознать и СРАЗУ ВЫПОЛНИТЬ бытовую команду.

    Возвращает готовый текст для озвучки, если распознал и выполнил, или
    None, если не уверен - в этом случае вызывающий код (server.py) должен
    передать raw_text в обычный путь через dialog_manager/LLM без каких-либо
    следов того, что router вообще пытался её разобрать."""
    text = raw_text.strip().lower().strip(".!?")
    if not text or not _is_short_enough(text):
        return None

    for matcher in _MATCHERS:
        answer = matcher(text)
        if answer is not None:
            return answer
    return None
