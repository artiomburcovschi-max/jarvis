
import ast
import datetime
import operator


def get_current_datetime() -> dict:
    """Возвращает текущую дату, время и день недели.

    Используй эту функцию, когда пользователь спрашивает который час,
    какое сегодня число, какой сегодня день недели и т.п.

    Returns:
        Словарь с полями date (ГГГГ-ММ-ДД), time (ЧЧ:ММ:СС) и weekday
        (день недели на русском).
    """
    now = datetime.datetime.now()
    weekdays_ru = [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    ]
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": weekdays_ru[now.weekday()],
    }

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_ALLOWED_UNARYOPS = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_ast_node(node: ast.AST):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Недопустимое значение в выражении: {node.value!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](
            _eval_ast_node(node.left), _eval_ast_node(node.right)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_eval_ast_node(node.operand))
    raise ValueError("Выражение содержит недопустимую конструкцию")


def calculate(expression: str) -> dict:
    """Вычисляет арифметическое выражение.

    Используй для любых математических вычислений: сложение, вычитание,
    умножение, деление, возведение в степень, скобки. Например:
    "12 * (3 + 4)", "2 ** 10", "100 / 7".

    Args:
        expression: Арифметическое выражение в виде строки.

    Returns:
        Словарь с полем result (число) или error (текст ошибки, если
        выражение нельзя было безопасно вычислить).
    """
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_ast_node(tree.body)
        return {"result": result}
    except (ValueError, ZeroDivisionError, SyntaxError, TypeError) as e:
        return {"error": f"Не удалось вычислить выражение: {e}"}


# Список инструментов, которые видит Gemini. Порядок не важен.
ALL_TOOLS = [
    get_current_datetime,
    calculate,
]
