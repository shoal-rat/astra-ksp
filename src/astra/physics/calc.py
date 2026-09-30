"""A safe arithmetic evaluator for free-form calculations.

Accepts a small worksheet: one or more statements separated by ``;`` or newlines, where each
statement is an expression or a simple assignment ``name = expression``. The value of the last
statement is the result; assigned names are reported too. Only numbers, names, arithmetic,
comparisons, boolean logic, conditional expressions and calls to whitelisted math functions are
allowed. Attribute access, subscripts, imports, lambdas, comprehensions and strings are rejected by
an AST whitelist before anything runs. All arithmetic is done in floats, so ``10**10**10`` overflows
cleanly instead of hanging on a giant integer. ``^`` is read as a power, as on a calculator.
"""

from __future__ import annotations

import ast
import math
import operator
import warnings
from typing import Any, Callable

from astra.physics.rocket import G0

FUNCTIONS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1.0 / 3.0), x),
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "asinh": math.asinh, "acosh": math.acosh, "atanh": math.atanh,
    "exp": math.exp, "log": math.log, "log10": math.log10, "log2": math.log2,
    "radians": math.radians, "degrees": math.degrees, "hypot": math.hypot,
    "floor": math.floor, "ceil": math.ceil, "abs": abs, "min": min, "max": max,
    "copysign": math.copysign,
}
CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf, "g0": G0}

_BINOPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_CMPOPS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
}
_ALLOWED_NODES: tuple[type, ...] = (
    ast.Module, ast.Expr, ast.Assign, ast.Name, ast.Load, ast.Store, ast.Constant, ast.BinOp,
    ast.UnaryOp, ast.Compare, ast.BoolOp, ast.IfExp, ast.Call, ast.And, ast.Or, ast.Not, ast.UAdd,
    ast.USub, *_BINOPS, *_CMPOPS,
)
MAX_LENGTH = 4000
MAX_NODES = 2000


class CalcError(ValueError):
    """The expression is not allowed or cannot be evaluated."""


def _num(x: Any) -> float:
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        return float(x)
    raise CalcError(f"expected a number, got {type(x).__name__}")


class _Evaluator:
    def __init__(self, names: dict[str, float]):
        self.names = names

    def run(self, node: ast.AST) -> Any:
        method = getattr(self, "_" + type(node).__name__, None)
        if method is None:
            raise CalcError(f"'{type(node).__name__}' is not allowed")
        return method(node)

    def _Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, bool):
            return node.value
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise CalcError(f"only numbers are allowed, not {type(node.value).__name__}")

    def _Name(self, node: ast.Name) -> Any:
        if node.id in self.names:
            return self.names[node.id]
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        if node.id in FUNCTIONS:
            raise CalcError(f"'{node.id}' is a function; call it like {node.id}(x)")
        raise CalcError(f"unknown name '{node.id}'")

    def _BinOp(self, node: ast.BinOp) -> float:
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise CalcError(f"operator '{type(node.op).__name__}' is not allowed")
        result = op(_num(self.run(node.left)), _num(self.run(node.right)))
        if isinstance(result, complex):
            raise CalcError("result is a complex number (e.g. a negative base to a fractional power)")
        return result

    def _UnaryOp(self, node: ast.UnaryOp) -> Any:
        val = self.run(node.operand)
        if isinstance(node.op, ast.USub):
            return -_num(val)
        if isinstance(node.op, ast.UAdd):
            return _num(val)
        if isinstance(node.op, ast.Not):
            return not val
        raise CalcError(f"operator '{type(node.op).__name__}' is not allowed")

    def _Compare(self, node: ast.Compare) -> bool:
        left = _num(self.run(node.left))
        for op_node, right_node in zip(node.ops, node.comparators):
            op = _CMPOPS.get(type(op_node))
            if op is None:
                raise CalcError(f"comparison '{type(op_node).__name__}' is not allowed")
            right = _num(self.run(right_node))
            if not op(left, right):
                return False
            left = right
        return True

    def _BoolOp(self, node: ast.BoolOp) -> Any:
        is_and = isinstance(node.op, ast.And)
        val: Any = is_and
        for v in node.values:
            val = self.run(v)
            if bool(val) != is_and:
                return val
        return val

    def _IfExp(self, node: ast.IfExp) -> Any:
        return self.run(node.body) if self.run(node.test) else self.run(node.orelse)

    def _Call(self, node: ast.Call) -> float:
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            name = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
            raise CalcError(f"function '{name}' is not available; allowed: {', '.join(sorted(FUNCTIONS))}")
        if node.keywords:
            raise CalcError("keyword arguments are not allowed")
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise CalcError("argument unpacking is not allowed")
        args = [_num(self.run(a)) for a in node.args]
        result = FUNCTIONS[node.func.id](*args)
        return _num(result)


def _check_name(name: str) -> None:
    if not name.isidentifier() or name.startswith("_"):
        raise CalcError(f"invalid variable name {name!r}")
    if name in FUNCTIONS:
        raise CalcError(f"'{name}' is a function name and cannot be a variable")


def evaluate(expression: str, variables: dict[str, float] | None = None) -> dict[str, Any]:
    """Evaluate a worksheet. Returns ``{"value": <last statement>, "assigned": {name: value}}``.

    Raises :class:`CalcError` (a ValueError) for disallowed syntax, unknown names, math domain
    errors, division by zero and overflow."""
    if not isinstance(expression, str) or not expression.strip():
        raise CalcError("empty expression")
    if len(expression) > MAX_LENGTH:
        raise CalcError(f"expression longer than {MAX_LENGTH} characters")
    names: dict[str, float] = {}
    for k, v in (variables or {}).items():
        _check_name(k)
        names[k] = _num(v)
    try:
        with warnings.catch_warnings():  # e.g. "1if x else 2": a SyntaxWarning on stderr, not a result
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(expression.replace("^", "**") if "^" in expression else expression, mode="exec")
    except SyntaxError as exc:
        raise CalcError(f"syntax error: {exc.msg} (column {exc.offset})") from exc
    except (RecursionError, MemoryError) as exc:
        raise CalcError("expression nested too deeply") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise CalcError("expression too complex")
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise CalcError(f"'{type(node).__name__}' is not allowed in a calculation")
    if not tree.body:
        raise CalcError("empty expression")
    ev = _Evaluator(names)
    assigned: dict[str, Any] = {}
    value: Any = None
    try:
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr):
                value = ev.run(stmt.value)
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                target = stmt.targets[0].id
                _check_name(target)
                if target in CONSTANTS:
                    raise CalcError(f"'{target}' is a constant and cannot be reassigned")
                value = ev.run(stmt.value)
                names[target] = value
                assigned[target] = value
            else:
                raise CalcError(f"statement '{type(stmt).__name__}' is not allowed; use expressions "
                                "or 'name = expression'")
    except ZeroDivisionError as exc:
        raise CalcError("division by zero") from exc
    except OverflowError as exc:
        raise CalcError("numeric overflow") from exc
    except RecursionError as exc:
        raise CalcError("expression nested too deeply") from exc
    except (ValueError, TypeError) as exc:
        if isinstance(exc, CalcError):
            raise
        raise CalcError(f"math error: {exc}") from exc
    return {"value": value, "assigned": assigned}
