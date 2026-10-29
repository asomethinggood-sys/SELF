"""A restricted arithmetic evaluator — never :func:`eval`.

Grammar
-------
::

    expr    := term (('+' | '-') term)*
    term    := unary (('*' | '/' | '//' | '%') unary)*
    unary   := ('+' | '-') unary | power
    power   := atom ('**' unary)?
    atom    := number | constant | function '(' expr ')' | '(' expr ')'

Supported: ``+ - * / // % **``, parentheses, the constants ``pi``/``e``, and
the functions ``abs``, ``sqrt``, ``round``, ``floor``, ``ceil``, ``min``,
``max``, ``log``. Nothing else is evaluated; names are looked up in a fixed
table and expression length/depth is bounded.

Feature area: 68 (safe calculator).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

MAX_EXPRESSION_LENGTH = 400
MAX_DEPTH = 24
MAX_RESULT = 1e100

_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "abs": abs,
    "sqrt": math.sqrt,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "min": min,
    "max": max,
    "log": math.log,
    "ln": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}
_ARITY: dict[str, int] = {
    "abs": 1, "sqrt": 1, "round": 2, "floor": 1, "ceil": 1,
    "min": -1, "max": -1, "log": 1, "ln": 1, "log10": 1, "exp": 1,
    "sin": 1, "cos": 1, "tan": 1,
}

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>\*\*|//|[+\-*/%(),])"
    r")"
)


class CalcError(ValueError):
    def __init__(self, message: str, code: str = "calc.invalid") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    position: int


def tokenize(expression: str) -> list[Token]:
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise CalcError("expression is too long", "calc.too_long")
    tokens: list[Token] = []
    position = 0
    while position < len(expression):
        match = _TOKEN_RE.match(expression, position)
        if not match:
            raise CalcError(f"unexpected character {expression[position]!r} at {position}", "calc.bad_token")
        position = match.end()
        kind = match.lastgroup or ""
        tokens.append(Token(kind, match.group(kind), match.start(kind)))
    return tokens


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0
        self.depth = 0

    # -- helpers -----------------------------------------------------
    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> Token:
        token = self.peek()
        if token is None:
            raise CalcError("unexpected end of expression", "calc.unexpected_end")
        self.index += 1
        return token

    def accept(self, value: str) -> bool:
        token = self.peek()
        if token is not None and token.value == value:
            self.index += 1
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise CalcError(f"expected {value!r}", "calc.expected")

    # -- grammar -----------------------------------------------------
    def parse(self) -> float:
        result = self.expression()
        if self.peek() is not None:
            raise CalcError(f"unexpected token {self.peek().value!r}", "calc.trailing")
        return result

    def expression(self) -> float:
        value = self.term()
        while True:
            token = self.peek()
            if token is None or token.value not in {"+", "-"}:
                return value
            self.next()
            right = self.term()
            value = value + right if token.value == "+" else value - right

    def term(self) -> float:
        value = self.unary()
        while True:
            token = self.peek()
            if token is None or token.value not in {"*", "/", "//", "%"}:
                return value
            self.next()
            right = self.unary()
            if token.value in {"/", "//"} and right == 0:
                raise CalcError("division by zero", "calc.division_by_zero")
            if token.value == "*":
                value *= right
            elif token.value == "/":
                value /= right
            elif token.value == "//":
                value //= right
            else:
                value %= right

    def unary(self) -> float:
        token = self.peek()
        if token is not None and token.value in {"+", "-"}:
            self.next()
            value = self.unary()
            return value if token.value == "+" else -value
        return self.power()

    def power(self) -> float:
        base = self.atom()
        if self.accept("**"):
            exponent = self.unary()
            if abs(exponent) > 1000 or abs(base) > 1e6:
                raise CalcError("result is out of range", "calc.range")
            value = base**exponent
            return value
        return base

    def atom(self) -> float:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CalcError("expression is nested too deeply", "calc.depth")
        try:
            token = self.next()
            if token.kind == "number":
                return float(token.value)
            if token.kind == "name":
                name = token.value.lower()
                if self.accept("("):
                    return self.call(name)
                if name in _CONSTANTS:
                    return _CONSTANTS[name]
                raise CalcError(f"unknown name {name!r}", "calc.unknown_name")
            if token.value == "(":
                value = self.expression()
                self.expect(")")
                return value
            raise CalcError(f"unexpected token {token.value!r}", "calc.unexpected_token")
        finally:
            self.depth -= 1

    def call(self, name: str) -> float:
        if name not in _FUNCTIONS:
            raise CalcError(f"unknown function {name!r}", "calc.unknown_function")
        arguments: list[float] = []
        if not self.accept(")"):
            arguments.append(self.expression())
            while self.accept(","):
                arguments.append(self.expression())
            self.expect(")")
        arity = _ARITY[name]
        # -1 means "variadic" (min/max accept any positive number of arguments)
        if arity == -1:
            if not arguments:
                raise CalcError(f"{name}() needs at least one argument", "calc.arity")
        elif len(arguments) != arity:
            raise CalcError(f"{name}() takes {arity} argument(s)", "calc.arity")
        try:
            return float(_FUNCTIONS[name](*arguments))
        except (ValueError, OverflowError, ZeroDivisionError) as exc:
            raise CalcError(f"{name}() failed: {exc}", "calc.math") from exc


def evaluate(expression: str) -> float:
    """Evaluate ``expression`` safely and return a float."""
    if not expression or not expression.strip():
        raise CalcError("empty expression", "calc.empty")
    # Fraction-style input: "1/3" is handled by the parser, but a bare "%"
    # would be confusing, so normalise a leading percent away.
    text = expression.strip().replace("×", "*").replace("÷", "/").replace("^", "**")
    text = text.replace("٫", ".").replace("٬", "")
    text = re.sub(r"[۰-۹]", lambda m: str(ord(m.group(0)) - ord("۰")), text)
    text = re.sub(r"[٠-٩]", lambda m: str(ord(m.group(0)) - ord("٠")), text)
    parser = _Parser(tokenize(text))
    result = parser.parse()
    if math.isnan(result) or math.isinf(result):
        raise CalcError("result is not a finite number", "calc.range")
    if abs(result) > MAX_RESULT:
        raise CalcError("result is out of range", "calc.range")
    return result


def format_result(value: float) -> str:
    """Render a result without floating-point noise for integers."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    if abs(value) >= 1e6 or (value != 0 and abs(value) < 1e-4):
        return f"{value:.6e}"
    return f"{value:.10g}"
