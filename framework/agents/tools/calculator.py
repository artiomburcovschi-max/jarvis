"""
Инструмент: безопасный калькулятор.

Намеренно НЕ используем eval()/exec() - выражение приходит от LLM, которая,
в свою очередь, реагирует на текст от пользователя. eval() дал бы прямой
путь к выполнению произвольного Python-кода (классическая инъекция через
LLM-инструмент). Вместо этого разбираем выражение через ast и разрешаем
только числа и арифметические операции.
"""

import ast
import operator

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
    """Вычисляет арифметическое выражение (см. схему в TOOL_SCHEMAS)."""
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_ast_node(tree.body)
        return {"result": result}
    except (ValueError, ZeroDivisionError, SyntaxError, TypeError) as e:
        return {"error": f"Не удалось вычислить выражение: {e}"}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": (
                "Вычисляет арифметическое выражение: сложение, вычитание, "
                "умножение, деление, возведение в степень, скобки. Используй "
                "для любых математических вычислений вместо счёта в уме."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Арифметическое выражение, например '12 * (3 + 4)'",
                    }
                },
                "required": ["expression"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "calculate": calculate,
}
