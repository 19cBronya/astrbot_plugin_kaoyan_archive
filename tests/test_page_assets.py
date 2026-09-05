from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
PAGE = ROOT / "pages" / "archive"
KATEX = PAGE / "vendor" / "katex"


def test_katex_is_loaded_only_from_local_page_assets() -> None:
    document = (PAGE / "index.html").read_text(encoding="utf-8")
    assert "./vendor/katex/katex.min.css" in document
    assert "./vendor/katex/katex.min.js" in document
    assert "./vendor/katex/contrib/auto-render.min.js" in document
    assert "cdn.jsdelivr.net" not in document
    assert (KATEX / "LICENSE").is_file()


def test_page_uses_iframe_safe_confirmation_dialog() -> None:
    document = (PAGE / "index.html").read_text(encoding="utf-8")
    script = (PAGE / "app.js").read_text(encoding="utf-8")

    assert 'id="confirm-overlay"' in document
    assert "confirmAction(" in script
    assert "window.confirm" not in script


def test_image_preview_does_not_lazy_load_while_hidden() -> None:
    script = (PAGE / "app.js").read_text(encoding="utf-8")

    assert 'image.className = "attachment-image hidden"' not in script
    assert 'image.loading = "lazy"' not in script
    assert 'withTimeout(request, 15000, "预览请求超时")' in script


def test_page_has_batch_repair_center_and_collapsed_excluded_events() -> None:
    document = (PAGE / "index.html").read_text(encoding="utf-8")
    script = (PAGE / "app.js").read_text(encoding="utf-8")

    assert 'id="repair-view"' in document
    assert 'id="repair-select-all"' in document
    assert 'id="detail-excluded"' in document
    assert "pending_classifications" in script
    assert 'runRepairAction("manual_question")' in script
    assert 'runRepairAction("manual_instruction")' in script
    assert 'runRepairAction("manual_archive")' in script


def test_page_has_all_messages_membership_manager() -> None:
    document = (PAGE / "index.html").read_text(encoding="utf-8")
    script = (PAGE / "app.js").read_text(encoding="utf-8")

    assert 'id="messages-view"' in document
    assert 'id="message-ownership"' in document
    assert 'id="message-target"' in document
    assert 'id="message-create"' not in document
    assert 'id="message-unarchive"' in document
    assert '{ label: "全部消息", value: stats.events ?? 0, view: "messages" }' in script
    assert 'apiPost("messages/action"' in script
    assert 'runMessageAction("assign")' in script
    assert 'runMessageAction("unarchive")' in script
    assert 'document.createElement("details")' in script
    assert 'details.addEventListener("toggle"' in script
    assert 'actOnQuestion("rearchive_new")' not in script
    assert 'repair: "unarchived"' not in script


def test_question_detail_drawer_uses_most_of_desktop_viewport() -> None:
    stylesheet = (PAGE / "style.css").read_text(encoding="utf-8")

    assert ".detail-drawer { width: 84vw; max-width: 100%;" in stylesheet
    assert "@media (max-width: 980px)" in stylesheet
    assert ".detail-drawer { width: 100%; }" in stylesheet


def test_all_katex_fonts_referenced_by_css_are_vendored() -> None:
    stylesheet = (KATEX / "katex.min.css").read_text(encoding="utf-8")
    font_paths = set(re.findall(r"url\((fonts/[^)]+)\)", stylesheet))
    assert font_paths
    assert all((KATEX / relative).is_file() for relative in font_paths)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_katex_runtime_renders_the_reported_formula() -> None:
    script = r"""
const katex = require(process.argv[1]);
const html = katex.renderToString(
  String.raw`F\left(x+\frac{z}{y}, y+\frac{z}{x}\right)=0`,
  {displayMode: true, throwOnError: false, trust: false}
);
if (!html.includes('class="katex"') || !html.includes('mfrac')) process.exit(1);
"""
    subprocess.run(
        ["node", "-e", script, str(KATEX / "katex.min.js")],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_math_summary_parser_with_node_test_runner() -> None:
    subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "math_renderer.test.mjs")],
        check=True,
        capture_output=True,
        text=True,
    )
