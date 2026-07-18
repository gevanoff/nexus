from __future__ import annotations

from pathlib import Path

from app import images_backend


STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_gateway_image_batch_limit_matches_ui() -> None:
    assert images_backend.MAX_IMAGES_PER_REQUEST == 8
    assert images_backend.clamp_image_count(99) == 8
    assert images_backend.clamp_image_count(8) == 8
    assert images_backend.clamp_image_count(0) == 1


def test_image_ui_uses_stage_and_accessible_thumbnail_selector() -> None:
    html = (STATIC / "image.html").read_text(encoding="utf-8")
    script = (STATIC / "image_catalog_ui.js").read_text(encoding="utf-8")
    assert 'max="8"' in html
    assert "image-stage-frame" in html
    assert "thumbnail-strip" in html
    assert "image_catalog_ui.js?v=4" in html
    assert 'strip.setAttribute("role", "listbox")' in script
    assert 'button.setAttribute("role", "option")' in script
    assert 'button.setAttribute("aria-selected"' in script
    assert 'event.key === "ArrowRight"' in script
    assert 'previousButton.setAttribute("aria-label", "Previous image")' in script
    assert "Open full size" in script
    assert 'image.loading = "eager"' in script
    assert 'image.loading = "lazy"' not in script
