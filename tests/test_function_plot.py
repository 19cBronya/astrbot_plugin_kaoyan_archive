from __future__ import annotations

import json
import math

import pytest

from kaoyan_archive.function_plot import (
    PlotSpecError,
    contains_plot_block,
    expand_plot_blocks,
    plot_fallback_markdown,
    render_plot_svg,
)


def test_sine_highlight_renders_as_safe_svg() -> None:
    svg, caption = render_plot_svg(
        {
            "x_range": ["-pi", "pi"],
            "y_range": [-1.2, 1.2],
            "curves": [
                {"expression": "sin(x)", "label": "y = sin(x)", "color": "#2563eb"}
            ],
            "highlights": [
                {
                    "curve": 0,
                    "x_range": ["pi/4", "pi/2"],
                    "label": "重点区间",
                    "color": "#dc2626",
                }
            ],
            "caption": "正弦函数及指定区间",
        }
    )

    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert "#2563eb" in svg
    assert "#dc2626" in svg
    assert "重点区间" in svg
    assert "script" not in svg.lower()
    assert caption == "正弦函数及指定区间"


def test_plot_block_is_replaced_in_its_original_markdown_position() -> None:
    spec = {
        "x_range": [-2, 2],
        "curves": [{"expression": "x^2", "label": "y=x²"}],
    }
    markdown = (
        "第一段讲解。\n\n```kaoyan-plot\n"
        + json.dumps(spec, ensure_ascii=False)
        + "\n```\n\n第二段讲解。"
    )

    assert contains_plot_block(markdown)
    expansion = expand_plot_blocks(markdown)

    assert expansion.plot_count == 1
    assert not expansion.errors
    assert expansion.markdown.index("第一段讲解") < expansion.markdown.index("<svg")
    assert expansion.markdown.index("</svg>") < expansion.markdown.index("第二段讲解")


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('id')",
        "open('/etc/passwd')",
        "x.__class__",
        "[x for x in (1, 2)]",
    ],
)
def test_arbitrary_code_is_rejected(expression: str) -> None:
    with pytest.raises(PlotSpecError):
        render_plot_svg({"curves": [{"expression": expression}]})


def test_non_finite_discontinuity_does_not_break_svg_generation() -> None:
    svg, _ = render_plot_svg(
        {
            "x_range": [-1, 1],
            "y_range": [-10, 10],
            "curves": [{"expression": "1/x"}],
        }
    )
    assert "<path" in svg
    assert "nan" not in svg.lower()
    assert math.isfinite(float(svg.split('viewBox="0 0 ', 1)[1].split()[0]))


def test_t2i_fallback_keeps_prose_without_leaking_plot_json_or_svg() -> None:
    markdown = (
        "前文。\n\n```kaoyan-plot\n"
        '{"curves":[{"expression":"sin(x)"}]}'
        "\n```\n\n后文。"
    )
    fallback = plot_fallback_markdown(markdown)

    assert "前文" in fallback and "后文" in fallback
    assert "sin(x)" not in fallback
    assert "<svg" not in fallback
    assert "暂时无法合成" in fallback
