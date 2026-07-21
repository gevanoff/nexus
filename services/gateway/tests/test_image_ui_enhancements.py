from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_image_prompt_uses_full_width_editor() -> None:
    html = (STATIC / "image.html").read_text(encoding="utf-8")

    assert 'class="prompt-field"' in html
    assert ".prompt-field { width: 100%" in html
    assert "#prompt { width: 100%; min-height: 180px; }" in html


def test_image_ui_has_optional_set_download_naming() -> None:
    html = (STATIC / "image.html").read_text(encoding="utf-8")
    script = (STATIC / "image_ui_enhancements.js").read_text(encoding="utf-8")

    assert 'id="nameScheme"' in html
    assert 'placeholder="example01"' in html
    assert "example01.png, example02.png" in html
    assert 'raw || "example01"' in script
    assert "parseNamingScheme" in script
    assert "data-named-image-download" in script
    assert "anchor.download = filename" in script


def test_image_ui_unwraps_nested_gateway_backend_errors() -> None:
    html = (STATIC / "image.html").read_text(encoding="utf-8")
    script = (STATIC / "image_ui_enhancements.js").read_text(encoding="utf-8")

    assert "image_ui_enhancements.js?v=1" in html
    assert "unwrapBackendDetail" in script
    assert "Image model/workflow mismatch" in script
    assert "Required workflow family" in script
    assert "raw response remains available" in script


def test_image_ui_enhancement_javascript_parses() -> None:
    node = shutil.which("node")
    if not node:
        return
    script_path = STATIC / "image_ui_enhancements.js"
    result = subprocess.run(
        [node, "--check", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_image_catalog_javascript_parses() -> None:
    node = shutil.which("node")
    if not node:
        return
    script_path = STATIC / "image_catalog_ui.js"
    result = subprocess.run(
        [node, "--check", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
