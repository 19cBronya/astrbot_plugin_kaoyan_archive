from __future__ import annotations

import ast
import html
import json
import math
import re
from dataclasses import dataclass
from typing import Any


PLOT_PROMPT_MARKER = "KAOYAN_FUNCTION_PLOT_PROTOCOL_V1"
PLOT_PROTOCOL_PROMPT = r"""
【函数图像协议（KAOYAN_FUNCTION_PLOT_PROTOCOL_V1）】
当函数图像能明显帮助解释走势、交点、单调性、积分区间或指定区间时，可以在讲解的对应位置插入一个绘图块。没有必要时不要绘图。绘图块前后的 Markdown 会按原顺序与图像合成为一张最终答疑图片。

绘图块必须是严格 JSON，禁止输出 Python、JavaScript、Matplotlib 代码：
```kaoyan-plot
{
  "x_range": ["-pi", "pi"],
  "y_range": [-1.2, 1.2],
  "x_label": "x",
  "y_label": "y",
  "curves": [
    {"expression": "sin(x)", "label": "y = sin(x)", "color": "#2563eb"}
  ],
  "highlights": [
    {"curve": 0, "x_range": ["pi/4", "pi/2"], "label": "重点区间", "color": "#dc2626"}
  ]
}
```
允许的表达式包含 x、pi、e，四则运算、幂，以及 sin、cos、tan、asin、acos、atan、sinh、cosh、tanh、exp、log、log10、sqrt、abs、floor、ceil。每块最多 4 条曲线；需要把某段标红时使用 highlights，而不是生成图片或编写代码。只输出与讲解需要的字段。
""".strip()


_BLOCK_RE = re.compile(
    r"```(?:kaoyan-plot|function-plot)\s*\r?\n(?P<body>[\s\S]*?)\r?\n```",
    re.IGNORECASE,
)
_COLOR_RE = re.compile(r"^(?:#[0-9a-fA-F]{6}|[a-zA-Z]{1,20})$")
_ALLOWED_FUNCTIONS = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "exp": math.exp,
    "log": math.log,
    "ln": math.log,
    "log10": math.log10,
    "sqrt": math.sqrt,
    "abs": abs,
    "floor": math.floor,
    "ceil": math.ceil,
}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_DEFAULT_COLORS = ("#2563eb", "#059669", "#7c3aed", "#d97706")


class PlotSpecError(ValueError):
    pass


@dataclass(frozen=True)
class PlotExpansion:
    markdown: str
    plot_count: int
    errors: tuple[str, ...]


class _SafeExpression:
    def __init__(self, source: str, *, allow_x: bool = True) -> None:
        normalized = str(source).strip().replace("^", "**")
        if not normalized or len(normalized) > 160:
            raise PlotSpecError("表达式为空或过长")
        try:
            self.tree = ast.parse(normalized, mode="eval")
        except SyntaxError as exc:
            raise PlotSpecError(f"表达式语法错误: {source}") from exc
        self.allow_x = allow_x
        nodes = list(ast.walk(self.tree))
        if len(nodes) > 80:
            raise PlotSpecError("表达式过于复杂")
        self._validate(self.tree)

    def _validate(self, node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            self._validate(node.body)
            return
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
                raise PlotSpecError("表达式只能包含数值常量")
            if not math.isfinite(float(node.value)) or abs(float(node.value)) > 1e9:
                raise PlotSpecError("数值常量超出范围")
            return
        if isinstance(node, ast.Name):
            allowed = set(_CONSTANTS)
            if self.allow_x:
                allowed.add("x")
            if node.id not in allowed:
                raise PlotSpecError(f"不支持的变量: {node.id}")
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            self._validate(node.operand)
            return
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)
        ):
            self._validate(node.left)
            self._validate(node.right)
            return
        if isinstance(node, ast.Call):
            if (
                not isinstance(node.func, ast.Name)
                or node.func.id not in _ALLOWED_FUNCTIONS
                or node.keywords
                or len(node.args) != 1
            ):
                raise PlotSpecError("只允许调用单参数数学函数")
            self._validate(node.args[0])
            return
        raise PlotSpecError(f"不支持的表达式结构: {type(node).__name__}")

    def evaluate(self, x: float = 0.0) -> float:
        try:
            value = float(self._evaluate_node(self.tree.body, x))
        except (ArithmeticError, OverflowError, ValueError):
            return math.nan
        return value if math.isfinite(value) and abs(value) <= 1e100 else math.nan

    def _evaluate_node(self, node: ast.AST, x: float) -> float:
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            return x if node.id == "x" else _CONSTANTS[node.id]
        if isinstance(node, ast.UnaryOp):
            value = self._evaluate_node(node.operand, x)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left = self._evaluate_node(node.left, x)
            right = self._evaluate_node(node.right, x)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Mod):
                return left % right
            if abs(right) > 16:
                raise OverflowError
            return left**right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return float(_ALLOWED_FUNCTIONS[node.func.id](self._evaluate_node(node.args[0], x)))
        raise PlotSpecError("表达式未通过验证")


def contains_plot_block(markdown: str) -> bool:
    return bool(_BLOCK_RE.search(markdown or ""))


def plot_fallback_markdown(markdown: str) -> str:
    """Keep the explanation readable when the optional composite T2I fails."""
    return _BLOCK_RE.sub(
        "> ⚠️ 函数图暂时无法合成；讲解正文已保留。",
        str(markdown or ""),
    )


def expand_plot_blocks(markdown: str, *, max_blocks: int = 3) -> PlotExpansion:
    source = str(markdown or "")
    rendered = 0
    errors: list[str] = []

    def replace(match: re.Match[str]) -> str:
        nonlocal rendered
        if rendered >= max(1, min(int(max_blocks), 6)):
            message = "函数图数量超过插件限制"
            errors.append(message)
            return _error_html(message)
        try:
            payload = json.loads(match.group("body"))
            svg, caption = render_plot_svg(payload)
        except (json.JSONDecodeError, PlotSpecError, TypeError, ValueError) as exc:
            message = str(exc)[:240] or "绘图规格无效"
            errors.append(message)
            return _error_html(message)
        rendered += 1
        caption_html = (
            f'<figcaption style="margin-top:10px;color:#6e6e73;font-size:0.78em;">'
            f"{html.escape(caption)}</figcaption>"
            if caption
            else ""
        )
        return (
            '<figure class="kaoyan-function-plot" '
            'style="margin:1.15em 0;padding:18px;background:#fafafa;'
            'border:1px solid #d2d2d7;border-radius:20px;break-inside:avoid;">'
            f"{svg}{caption_html}</figure>"
        )

    expanded = _BLOCK_RE.sub(replace, source)
    return PlotExpansion(expanded, rendered, tuple(errors))


def render_plot_svg(spec: Any, *, width: int = 980, height: int = 520) -> tuple[str, str]:
    if not isinstance(spec, dict):
        raise PlotSpecError("绘图块必须是 JSON 对象")
    curves = spec.get("curves")
    if not isinstance(curves, list) or not curves or len(curves) > 4:
        raise PlotSpecError("curves 必须包含 1 到 4 条曲线")
    x_min, x_max = _parse_range(spec.get("x_range", [-10, 10]), "x_range")
    if x_max - x_min > 1e6:
        raise PlotSpecError("x_range 跨度过大")

    samples = min(max(int(spec.get("samples", 600)), 160), 900)
    xs = [x_min + (x_max - x_min) * index / (samples - 1) for index in range(samples)]
    curve_data: list[dict[str, Any]] = []
    all_finite: list[float] = []
    for index, curve in enumerate(curves):
        if not isinstance(curve, dict):
            raise PlotSpecError("每条曲线必须是 JSON 对象")
        expression = str(curve.get("expression") or "").strip()
        evaluator = _SafeExpression(expression)
        ys = [evaluator.evaluate(x) for x in xs]
        finite = [value for value in ys if math.isfinite(value)]
        if not finite:
            raise PlotSpecError(f"曲线 {index + 1} 在指定范围内没有可绘制点")
        all_finite.extend(finite)
        curve_data.append(
            {
                "expression": expression,
                "label": _short_text(curve.get("label") or f"y = {expression}", 80),
                "color": _color(curve.get("color"), _DEFAULT_COLORS[index]),
                "width": _bounded_float(curve.get("line_width", 3.2), 1.0, 8.0),
                "ys": ys,
            }
        )

    if "y_range" in spec and spec.get("y_range") is not None:
        y_min, y_max = _parse_range(spec["y_range"], "y_range")
    else:
        y_min, y_max = _automatic_y_range(all_finite)

    margin_left, margin_right, margin_top, margin_bottom = 92, 34, 32, 76
    inner_width = width - margin_left - margin_right
    inner_height = height - margin_top - margin_bottom

    def px(x_value: float) -> float:
        return margin_left + (x_value - x_min) / (x_max - x_min) * inner_width

    def py(y_value: float) -> float:
        return margin_top + (y_max - y_value) / (y_max - y_min) * inner_height

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="函数图像" style="display:block;width:100%;height:auto;">',
        '<rect width="100%" height="100%" rx="16" fill="#ffffff"/>',
        f'<rect x="{margin_left}" y="{margin_top}" width="{inner_width}" height="{inner_height}" '
        'fill="#ffffff" stroke="#d2d2d7" stroke-width="1.5"/>',
    ]

    x_ticks = _ticks(x_min, x_max, 7)
    y_ticks = _ticks(y_min, y_max, 6)
    for value in x_ticks:
        x_pos = px(value)
        parts.append(
            f'<line x1="{x_pos:.2f}" y1="{margin_top}" x2="{x_pos:.2f}" '
            f'y2="{margin_top + inner_height}" stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x_pos:.2f}" y="{margin_top + inner_height + 31}" text-anchor="middle" '
            'font-family="sans-serif" font-size="19" fill="#6e6e73">'
            f"{html.escape(_format_tick(value))}</text>"
        )
    for value in y_ticks:
        y_pos = py(value)
        parts.append(
            f'<line x1="{margin_left}" y1="{y_pos:.2f}" x2="{margin_left + inner_width}" '
            f'y2="{y_pos:.2f}" stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{margin_left - 15}" y="{y_pos + 6:.2f}" text-anchor="end" '
            'font-family="sans-serif" font-size="19" fill="#6e6e73">'
            f"{html.escape(_format_tick(value))}</text>"
        )

    if x_min <= 0 <= x_max:
        x_zero = px(0)
        parts.append(
            f'<line x1="{x_zero:.2f}" y1="{margin_top}" x2="{x_zero:.2f}" '
            f'y2="{margin_top + inner_height}" stroke="#4b5563" stroke-width="2"/>'
        )
    if y_min <= 0 <= y_max:
        y_zero = py(0)
        parts.append(
            f'<line x1="{margin_left}" y1="{y_zero:.2f}" x2="{margin_left + inner_width}" '
            f'y2="{y_zero:.2f}" stroke="#4b5563" stroke-width="2"/>'
        )

    for curve in curve_data:
        path = _path_for_points(xs, curve["ys"], px, py, y_min, y_max, inner_height)
        parts.append(
            f'<path d="{path}" fill="none" stroke="{curve["color"]}" '
            f'stroke-width="{curve["width"]:.1f}" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    highlights = spec.get("highlights", []) or []
    if not isinstance(highlights, list) or len(highlights) > 8:
        raise PlotSpecError("highlights 最多包含 8 段")
    legend_entries = [(curve["color"], curve["label"]) for curve in curve_data]
    for highlight in highlights:
        if not isinstance(highlight, dict):
            raise PlotSpecError("每段高亮必须是 JSON 对象")
        curve_index = int(highlight.get("curve", 0))
        if not 0 <= curve_index < len(curve_data):
            raise PlotSpecError("高亮引用了不存在的曲线")
        range_min, range_max = _parse_range(highlight.get("x_range"), "highlight.x_range")
        selected_ys = [
            y if range_min <= x <= range_max else math.nan
            for x, y in zip(xs, curve_data[curve_index]["ys"], strict=True)
        ]
        color = _color(highlight.get("color"), "#dc2626")
        path = _path_for_points(xs, selected_ys, px, py, y_min, y_max, inner_height)
        parts.append(
            f'<path d="{path}" fill="none" stroke="{color}" '
            f'stroke-width="{_bounded_float(highlight.get("line_width", 6), 2, 12):.1f}" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )
        label = _short_text(highlight.get("label") or "", 80)
        if label:
            legend_entries.append((color, label))

    markers = spec.get("markers", []) or []
    if not isinstance(markers, list) or len(markers) > 12:
        raise PlotSpecError("markers 最多包含 12 个点")
    for marker in markers:
        if not isinstance(marker, dict):
            raise PlotSpecError("每个标记点必须是 JSON 对象")
        x_value = _parse_number(marker.get("x"), "marker.x")
        if "y" in marker:
            y_value = _parse_number(marker.get("y"), "marker.y")
        else:
            curve_index = int(marker.get("curve", 0))
            if not 0 <= curve_index < len(curve_data):
                raise PlotSpecError("标记点引用了不存在的曲线")
            y_value = _SafeExpression(curve_data[curve_index]["expression"]).evaluate(x_value)
        if not (x_min <= x_value <= x_max and y_min <= y_value <= y_max):
            continue
        color = _color(marker.get("color"), "#dc2626")
        parts.append(
            f'<circle cx="{px(x_value):.2f}" cy="{py(y_value):.2f}" r="6" '
            f'fill="{color}" stroke="#ffffff" stroke-width="2"/>'
        )
        label = _short_text(marker.get("label") or "", 60)
        if label:
            parts.append(
                f'<text x="{px(x_value) + 10:.2f}" y="{py(y_value) - 10:.2f}" '
                'font-family="sans-serif" font-size="20" fill="#111111">'
                f"{html.escape(label)}</text>"
            )

    x_label = _short_text(spec.get("x_label") or "x", 30)
    y_label = _short_text(spec.get("y_label") or "y", 30)
    parts.extend(
        [
            f'<text x="{margin_left + inner_width}" y="{height - 18}" text-anchor="end" '
            f'font-family="sans-serif" font-size="22" fill="#111111">{html.escape(x_label)}</text>',
            f'<text x="24" y="{margin_top + 12}" font-family="sans-serif" font-size="22" '
            f'fill="#111111">{html.escape(y_label)}</text>',
        ]
    )

    legend_x = margin_left + 16
    legend_y = margin_top + 24
    for index, (color, label) in enumerate(legend_entries[:8]):
        row_y = legend_y + index * 27
        parts.append(
            f'<line x1="{legend_x}" y1="{row_y}" x2="{legend_x + 30}" y2="{row_y}" '
            f'stroke="{color}" stroke-width="5" stroke-linecap="round"/>'
        )
        parts.append(
            f'<text x="{legend_x + 40}" y="{row_y + 6}" font-family="sans-serif" '
            f'font-size="19" fill="#374151">{html.escape(label)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts), _short_text(spec.get("caption") or "", 160)


def _parse_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise PlotSpecError(f"{field} 必须是数值或数学表达式")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        number = _SafeExpression(str(value), allow_x=False).evaluate()
    if not math.isfinite(number):
        raise PlotSpecError(f"{field} 不是有限数值")
    return number


def _parse_range(value: Any, field: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise PlotSpecError(f"{field} 必须是两个端点组成的数组")
    lower = _parse_number(value[0], f"{field}[0]")
    upper = _parse_number(value[1], f"{field}[1]")
    if not lower < upper:
        raise PlotSpecError(f"{field} 下限必须小于上限")
    return lower, upper


def _automatic_y_range(values: list[float]) -> tuple[float, float]:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return -1.0, 1.0
    low = ordered[int((len(ordered) - 1) * 0.02)]
    high = ordered[int((len(ordered) - 1) * 0.98)]
    if math.isclose(low, high, rel_tol=1e-9, abs_tol=1e-9):
        padding = max(abs(low) * 0.2, 1.0)
    else:
        padding = (high - low) * 0.12
    low, high = low - padding, high + padding
    if low > 0:
        low = 0.0
    if high < 0:
        high = 0.0
    if math.isclose(low, high):
        return low - 1.0, high + 1.0
    return low, high


def _path_for_points(xs, ys, px, py, y_min, y_max, inner_height: float) -> str:
    commands: list[str] = []
    drawing = False
    previous_y_pixel: float | None = None
    guard = (y_max - y_min) * 0.08
    for x_value, y_value in zip(xs, ys, strict=True):
        if not math.isfinite(y_value) or not (y_min - guard <= y_value <= y_max + guard):
            drawing = False
            previous_y_pixel = None
            continue
        x_pixel, y_pixel = px(x_value), py(y_value)
        if previous_y_pixel is not None and abs(y_pixel - previous_y_pixel) > inner_height * 0.72:
            drawing = False
        commands.append(f'{"L" if drawing else "M"}{x_pixel:.2f},{y_pixel:.2f}')
        drawing = True
        previous_y_pixel = y_pixel
    return " ".join(commands)


def _ticks(lower: float, upper: float, count: int) -> list[float]:
    return [lower + (upper - lower) * index / (count - 1) for index in range(count)]


def _format_tick(value: float) -> str:
    if abs(value) < 1e-10:
        return "0"
    if abs(value) >= 1e5 or abs(value) < 1e-3:
        return f"{value:.2e}"
    return f"{value:.3g}"


def _color(value: Any, default: str) -> str:
    candidate = str(value or default).strip()
    return candidate if _COLOR_RE.fullmatch(candidate) else default


def _bounded_float(value: Any, lower: float, upper: float) -> float:
    try:
        return min(max(float(value), lower), upper)
    except (TypeError, ValueError):
        return lower


def _short_text(value: Any, limit: int) -> str:
    return str(value or "").strip().replace("\x00", "")[:limit]


def _error_html(message: str) -> str:
    return f"> ⚠️ 函数图渲染失败：{html.escape(message)}"
