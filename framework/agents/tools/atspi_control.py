"""
Инструменты C5 (раунд 24): AT-SPI - чтение и управление интерфейсом
произвольных приложений через Linux accessibility API (та же шина,
которой пользуются экранные дикторы для незрячих).

Три инструмента, и ВСЕ ТРИ - "опасные" (см. confirmation.py,
DANGEROUS_TOOLS, C3) - подтверждение перед КАЖДЫМ вызовом, включая
ЧТЕНИЕ: полный текст активного окна может содержать пароли/переписку/что
угодно. Раз оно всё равно никогда не попадает в облако (см. ниже), лишнее
голосовое "да" не мешает, а единое правило "в этой зоне подтверждаем
всегда" безопаснее, чем разбираться, какое конкретно чтение "достаточно
безобидно", чтобы обойтись без вопроса.

РАЗДЕЛЕНИЕ ОБЛАКО/ЛОКАЛЬНО - главное архитектурное решение этого раунда,
не просто "ещё confirmation": эти три инструмента вообще НЕ попадают в
схему, которую видит облачный провайдер (см. dialog_manager.py,
LOCAL_ONLY_TOOLS / _create_stream() / _execute_tool_call()). Облачная
модель о них структурно не знает и не может их вызвать НИКАК - это не
инструкция "не используй", а отсутствие в списке. Доступны только когда
сам запрос к LLM обслуживается локально (форсированный офлайн-режим или
автоматический fallback раунда 23) - содержимое экрана в принципе никогда
не улетает наружу.

Linux-only - AT-SPI специфичен для Linux desktop accessibility stack.
Аналог для Windows (UI Automation, через pywin32) - отдельная, не
сделанная здесь задача (см. README, раздел "Раунд 24").

Требует системных пакетов gir1.2-atspi-2.0 + python3-gi (НЕ pip - тот же
принцип, что и в environment.py: системная утилита предпочтительнее
pip-зависимости, если система уже её предоставляет) и включённой
поддержки accessibility в самой системе. Если чего-то из этого нет -
каждый вызов возвращает понятную ошибку с подсказкой, а не падает при
импорте всего файла (см. _import_atspi() - импорт ЛЕНИВЫЙ, внутри
функции, а не на уровне модуля).

НЕ ПРОВЕРЕНО НА РЕАЛЬНОЙ СИСТЕМЕ (см. README, раздел "Раунд 24") - AT-SPI
требует живую графическую сессию с шиной accessibility, которой нет в
песочнице разработки. Код написан по официальному API
gi.repository.Atspi, но имена методов/сигнатуры могут потребовать мелкой
правки при первом реальном запуске - тесты (test_atspi_control.py) все на
моках самого Atspi-модуля, не на реальной шине.
"""
from ._shared import IS_WINDOWS, fuzzy_lookup

# Ограничения на глубину/ширину обхода дерева элементов - без этого
# сложное окно (например, IDE с открытым деревом файлов) могло бы отдать
# тысячи строк в контекст локальной модели.
MAX_TREE_DEPTH = 6
MAX_ELEMENTS = 80
MAX_TEXT_CHARS_PER_ELEMENT = 80

# Ниже какого совпадения имени с голосовой подсказкой НЕ кликаем и не
# вводим текст наугад - тот же принцип "уверен или не лезу", что и в
# apps.py/intent_router.py (тот же fuzzy_lookup, тот же дух порога).
ELEMENT_FUZZY_THRESHOLD = 65


def _import_atspi():
    """Лениво импортирует gi.repository.Atspi - НЕ на уровне модуля.

    Если бы импорт был на уровне модуля и системных пакетов не было бы -
    autoloader в tools/__init__.py отключил бы ВСЕ инструменты из этого
    файла разом (см. его докстринг: "ошибка при импорте отключает весь
    модуль"). Ленивый импорт внутри каждой функции превращает это в
    обычную ошибку выполнения ОДНОГО вызова - остальные инструменты
    Джарвиса продолжают работать как обычно.
    """
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        return Atspi
    except (ImportError, ValueError) as e:
        raise RuntimeError(
            "AT-SPI недоступен: нужны системные пакеты 'gir1.2-atspi-2.0' и "
            "'python3-gi' (sudo apt install gir1.2-atspi-2.0 python3-gi), "
            f"а также включённая поддержка accessibility в системе. ({e})"
        ) from e


def _find_active_window(Atspi):
    """Обходит desktop -> приложения -> их окна, ищет окно с состоянием
    ACTIVE. Каждый шаг обёрнут в try/except - AT-SPI объекты нередко
    "протухают" между вызовами (окно уже закрылось), и это не должно
    ронять весь обход, только пропускать проблемный узел."""
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
        except Exception:
            continue
        if app is None:
            continue
        try:
            n_windows = app.get_child_count()
        except Exception:
            continue
        for j in range(n_windows):
            try:
                window = app.get_child_at_index(j)
            except Exception:
                continue
            if window is None:
                continue
            try:
                if window.get_state_set().contains(Atspi.StateType.ACTIVE):
                    return window
            except Exception:
                continue
    return None


def _element_text(Atspi, element) -> str:
    try:
        text = Atspi.Text.get_text(element, 0, -1)
    except Exception:
        text = ""
    return (text or "").strip()


def _walk_tree(Atspi, element, depth, counter, on_element):
    """Обходит дерево элементов в глубину, вызывая on_element(element,
    depth) на каждый узел, с ограничениями MAX_TREE_DEPTH/MAX_ELEMENTS."""
    if counter[0] >= MAX_ELEMENTS or depth > MAX_TREE_DEPTH:
        return
    on_element(element, depth)
    counter[0] += 1
    try:
        n_children = element.get_child_count()
    except Exception:
        return
    for i in range(n_children):
        if counter[0] >= MAX_ELEMENTS:
            return
        try:
            child = element.get_child_at_index(i)
        except Exception:
            continue
        if child is not None:
            _walk_tree(Atspi, child, depth + 1, counter, on_element)


def _collect_named_elements(Atspi, window) -> list:
    results = []

    def _collect(element, depth):
        try:
            name = element.get_name() or ""
        except Exception:
            name = ""
        if name:
            results.append((name, element))

    _walk_tree(Atspi, window, 0, [0], _collect)
    return results


def atspi_read_active_window() -> dict:
    """Читает дерево доступных элементов активного окна: роли, подписи,
    видимый текст - контекст для последующего клика/ввода текста."""
    if IS_WINDOWS:
        return {"error": "AT-SPI поддерживается только на Linux."}

    try:
        Atspi = _import_atspi()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        window = _find_active_window(Atspi)
    except Exception as e:
        return {"error": f"Не удалось обратиться к AT-SPI: {e}"}

    if window is None:
        return {"error": "Не нашёл активное окно через AT-SPI - проверь, что "
                          "поддержка accessibility включена в системе."}

    lines = []

    def _describe(element, depth):
        try:
            role = element.get_role_name()
        except Exception:
            role = "?"
        try:
            name = element.get_name() or ""
        except Exception:
            name = ""
        text = _element_text(Atspi, element)
        if len(text) > MAX_TEXT_CHARS_PER_ELEMENT:
            text = text[:MAX_TEXT_CHARS_PER_ELEMENT] + "…"
        label = name or text
        if label:
            lines.append("  " * depth + f"- [{role}] {label}")

    try:
        _walk_tree(Atspi, window, 0, [0], _describe)
    except Exception as e:
        return {"error": f"Ошибка при обходе дерева элементов: {e}"}

    if not lines:
        return {"result": "Активное окно найдено, но в нём нет читаемых элементов."}
    return {"result": "Элементы активного окна:\n" + "\n".join(lines)}


def atspi_click_element(element_hint: str) -> dict:
    """Ищет в активном окне элемент, чья подпись похожа на element_hint, и
    кликает по нему (первое доступное действие - обычно click/press)."""
    if IS_WINDOWS:
        return {"error": "AT-SPI поддерживается только на Linux."}

    try:
        Atspi = _import_atspi()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        window = _find_active_window(Atspi)
    except Exception as e:
        return {"error": f"Не удалось обратиться к AT-SPI: {e}"}
    if window is None:
        return {"error": "Не нашёл активное окно через AT-SPI."}

    named = _collect_named_elements(Atspi, window)
    if not named:
        return {"error": "В активном окне нет элементов с подписями для поиска."}

    names = [n for n, _ in named]
    best_name, best_score = fuzzy_lookup(element_hint, names)
    if best_score < ELEMENT_FUZZY_THRESHOLD:
        return {"error": f"Не уверен, что «{element_hint}» - подходящий элемент "
                          f"(лучшее совпадение «{best_name}», {best_score}%) - не кликаю."}

    element = next(el for n, el in named if n == best_name)

    try:
        n_actions = Atspi.Action.get_n_actions(element)
    except Exception as e:
        return {"error": f"У «{best_name}» нет доступных действий: {e}"}
    if n_actions <= 0:
        return {"error": f"«{best_name}» не поддерживает клик (нет доступных действий)."}

    try:
        Atspi.Action.do_action(element, 0)
    except Exception as e:
        return {"error": f"Не удалось кликнуть «{best_name}»: {e}"}

    return {"result": f"Кликнул «{best_name}»."}


def atspi_type_text(element_hint: str, text: str) -> dict:
    """Ищет в активном окне текстовое поле, чья подпись похожа на
    element_hint, ставит на него фокус и вводит text."""
    if IS_WINDOWS:
        return {"error": "AT-SPI поддерживается только на Linux."}

    try:
        Atspi = _import_atspi()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        window = _find_active_window(Atspi)
    except Exception as e:
        return {"error": f"Не удалось обратиться к AT-SPI: {e}"}
    if window is None:
        return {"error": "Не нашёл активное окно через AT-SPI."}

    named = _collect_named_elements(Atspi, window)
    if not named:
        return {"error": "В активном окне нет элементов с подписями для поиска."}

    names = [n for n, _ in named]
    best_name, best_score = fuzzy_lookup(element_hint, names)
    if best_score < ELEMENT_FUZZY_THRESHOLD:
        return {"error": f"Не уверен, что «{element_hint}» - подходящее поле "
                          f"(лучшее совпадение «{best_name}», {best_score}%) - не ввожу текст."}

    element = next(el for n, el in named if n == best_name)

    try:
        Atspi.Component.grab_focus(element)
    except Exception as e:
        return {"error": f"Не удалось поставить фокус на «{best_name}»: {e}"}

    try:
        Atspi.EditableText.set_text_contents(element, text)
    except Exception as e:
        return {"error": f"Не удалось ввести текст в «{best_name}»: {e}"}

    return {"result": f"Ввёл текст в «{best_name}»."}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "atspi_read_active_window",
            "description": (
                "Читает дерево доступных элементов АКТИВНОГО окна (кнопки, поля, текст) "
                "через AT-SPI (Linux accessibility). Доступно ТОЛЬКО в локальном/офлайн "
                "режиме - облачная модель этот инструмент не видит. Требует голосового "
                "подтверждения перед выполнением."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "atspi_click_element",
            "description": (
                "Кликает по элементу интерфейса активного окна, чья подпись похожа на "
                "element_hint (например, кнопка \"Сохранить\"). Сначала используй "
                "atspi_read_active_window, чтобы узнать точные подписи элементов. "
                "Доступно ТОЛЬКО в локальном/офлайн режиме. Требует голосового подтверждения."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element_hint": {
                        "type": "string",
                        "description": "Подпись/название элемента для клика",
                    },
                },
                "required": ["element_hint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "atspi_type_text",
            "description": (
                "Вводит текст в текстовое поле активного окна, чья подпись похожа на "
                "element_hint. Доступно ТОЛЬКО в локальном/офлайн режиме. Требует "
                "голосового подтверждения."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element_hint": {
                        "type": "string",
                        "description": "Подпись/название текстового поля",
                    },
                    "text": {"type": "string", "description": "Текст для ввода"},
                },
                "required": ["element_hint", "text"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "atspi_read_active_window": atspi_read_active_window,
    "atspi_click_element": atspi_click_element,
    "atspi_type_text": atspi_type_text,
}

# Раунд 24 (C5): эти три инструмента НИКОГДА не должны попадать в схему,
# которую видит облачный провайдер - см. dialog_manager.py, _create_stream()
# и _execute_tool_call(). tools/__init__.py агрегирует это множество из
# всех модулей в agents.tools.LOCAL_ONLY_TOOLS.
LOCAL_ONLY_TOOLS = {"atspi_read_active_window", "atspi_click_element", "atspi_type_text"}
