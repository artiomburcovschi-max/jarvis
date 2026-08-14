"""Тесты для раунда 24 (C5) - AT-SPI инструменты.

AT-SPI требует живую графическую сессию с шиной accessibility, которой
нет в песочнице разработки - см. README, раздел "Раунд 24" и докстринг
atspi_control.py: этот код НЕ проверен на реальной системе. Здесь -
модульные тесты на ПОЛНОСТЬЮ фейковой реализации gi.repository.Atspi
(FakeAtspi ниже), проверяющие логику обхода дерева/fuzzy-поиска/действий,
а не сам API AT-SPI (его не с чем сверить без реальной шины).
"""
import sys

sys.path.insert(0, ".")

import agents.tools.atspi_control as atspi_control  # noqa: E402


class FakeStateSet:
    def __init__(self, active):
        self._active = active

    def contains(self, state):
        return state == "ACTIVE" and self._active


class FakeElement:
    def __init__(self, name="", role="push button", children=None, active=False,
                 text="", n_actions=1):
        self.name = name
        self.role = role
        self.children = children or []
        self.active = active
        self.text = text
        self.n_actions = n_actions
        self.clicked = False
        self.focused = False
        self.typed_text = None

    def get_name(self):
        return self.name

    def get_role_name(self):
        return self.role

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]

    def get_state_set(self):
        return FakeStateSet(self.active)


class FakeAtspi:
    """Мок gi.repository.Atspi - только то подмножество API, которое
    реально использует atspi_control.py."""

    class StateType:
        ACTIVE = "ACTIVE"

    class Text:
        @staticmethod
        def get_text(element, start, end):
            return element.text

    class Action:
        @staticmethod
        def get_n_actions(element):
            return element.n_actions

        @staticmethod
        def do_action(element, index):
            element.clicked = True

    class Component:
        @staticmethod
        def grab_focus(element):
            element.focused = True

    class EditableText:
        @staticmethod
        def set_text_contents(element, text):
            element.typed_text = text

    def __init__(self, desktop_apps):
        self._desktop = FakeElement(children=desktop_apps)

    def get_desktop(self, index):
        return self._desktop


def _with_fake_atspi(fake_instance, fn, *args, **kwargs):
    """Подменяет atspi_control._import_atspi() на время вызова fn(*args,
    **kwargs), гарантированно возвращая исходную функцию обратно даже при
    исключении - тот же паттерн save/restore, что и в
    test_environment_context.py для run_shell."""
    original = atspi_control._import_atspi
    atspi_control._import_atspi = lambda: fake_instance
    try:
        return fn(*args, **kwargs)
    finally:
        atspi_control._import_atspi = original


def _make_active_window(children):
    window = FakeElement(name="Тестовое окно", role="frame", active=True, children=children)
    app = FakeElement(role="application", children=[window])
    return FakeAtspi([app])


# --- atspi_read_active_window() ---------------------------------------------


def test_read_active_window_lists_named_elements():
    button = FakeElement(name="Сохранить", role="push button")
    field = FakeElement(name="Имя пользователя", role="entry", text="")
    fake = _make_active_window([button, field])

    result = _with_fake_atspi(fake, atspi_control.atspi_read_active_window)

    assert "result" in result
    assert "Сохранить" in result["result"]
    assert "Имя пользователя" in result["result"]


def test_read_active_window_no_active_window_found():
    # Ни одно окно не активно - десктоп есть, а взять нечего.
    window = FakeElement(name="Фон", role="frame", active=False)
    app = FakeElement(role="application", children=[window])
    fake = FakeAtspi([app])

    result = _with_fake_atspi(fake, atspi_control.atspi_read_active_window)

    assert "error" in result
    assert "активное окно" in result["error"]


def test_read_active_window_atspi_unavailable_returns_error_not_exception():
    original = atspi_control._import_atspi

    def _raise():
        raise RuntimeError("AT-SPI недоступен: нужны системные пакеты ...")

    atspi_control._import_atspi = _raise
    try:
        result = atspi_control.atspi_read_active_window()
    finally:
        atspi_control._import_atspi = original

    assert "error" in result
    assert "AT-SPI" in result["error"]


def test_read_active_window_refuses_on_windows(monkeypatch):
    monkeypatch.setattr(atspi_control, "IS_WINDOWS", True)
    result = atspi_control.atspi_read_active_window()
    assert "error" in result
    assert "Linux" in result["error"]


def test_read_active_window_respects_element_cap():
    # MAX_ELEMENTS ограничивает обход - не должно упасть/зависнуть на
    # окне с большим количеством элементов (например, IDE с деревом файлов).
    many_children = [FakeElement(name=f"файл_{i}.py") for i in range(500)]
    fake = _make_active_window(many_children)

    result = _with_fake_atspi(fake, atspi_control.atspi_read_active_window)

    assert "result" in result
    # Обход останавливается РОВНО на MAX_ELEMENTS - не больше (и само окно,
    # и его дети считаются в общий счётчик).
    assert result["result"].count("\n") <= atspi_control.MAX_ELEMENTS


# --- atspi_click_element() --------------------------------------------------


def test_click_element_matches_and_clicks():
    button = FakeElement(name="Сохранить", role="push button", n_actions=1)
    fake = _make_active_window([button])

    result = _with_fake_atspi(fake, atspi_control.atspi_click_element, "сохранить")

    assert "result" in result
    assert button.clicked is True


def test_click_element_low_confidence_does_not_click():
    button = FakeElement(name="Сохранить", role="push button", n_actions=1)
    fake = _make_active_window([button])

    result = _with_fake_atspi(fake, atspi_control.atspi_click_element, "полностью другое слово")

    assert "error" in result
    assert button.clicked is False


def test_click_element_without_actions_is_refused():
    label = FakeElement(name="Просто подпись", role="label", n_actions=0)
    fake = _make_active_window([label])

    result = _with_fake_atspi(fake, atspi_control.atspi_click_element, "просто подпись")

    assert "error" in result
    assert label.clicked is False


def test_click_element_refuses_on_windows(monkeypatch):
    monkeypatch.setattr(atspi_control, "IS_WINDOWS", True)
    result = atspi_control.atspi_click_element("что угодно")
    assert "error" in result
    assert "Linux" in result["error"]


# --- atspi_type_text() ------------------------------------------------------


def test_type_text_focuses_and_sets_text():
    field = FakeElement(name="Имя пользователя", role="entry")
    fake = _make_active_window([field])

    result = _with_fake_atspi(fake, atspi_control.atspi_type_text, "имя пользователя", "artem")

    assert "result" in result
    assert field.focused is True
    assert field.typed_text == "artem"


def test_type_text_low_confidence_does_not_type():
    field = FakeElement(name="Имя пользователя", role="entry")
    fake = _make_active_window([field])

    result = _with_fake_atspi(fake, atspi_control.atspi_type_text, "совершенно левое поле", "artem")

    assert "error" in result
    assert field.typed_text is None
    assert field.focused is False


# --- Общая проводка (схемы/имена/local-only) --------------------------------


def test_schema_names_match_implementations_and_local_only():
    schema_names = {s["function"]["name"] for s in atspi_control.TOOL_SCHEMAS}
    impl_names = set(atspi_control.TOOL_IMPLEMENTATIONS.keys())
    assert schema_names == impl_names == atspi_control.LOCAL_ONLY_TOOLS
