from __future__ import annotations

from typing import Any


LTX_RESOLUTION_PRESETS: dict[str, tuple[int, int]] = {
    # Keep the existing request names for compatibility, but expose the actual
    # model-safe dimensions in the UI. Two-stage LTX requires both axes to be
    # divisible by 64.
    "480p": (704, 384),
    "540p": (768, 512),
    "720p": (1280, 704),
}

HUNYUAN_RESOLUTION_PRESETS: tuple[str, ...] = ("480p", "720p")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_resolution(value: Any, default: str) -> str:
    normalized = str(value or default).strip().lower()
    aliases = {
        "480": "480p",
        "540": "540p",
        "720": "720p",
    }
    return aliases.get(normalized, normalized)


def ltx_dimensions(payload: dict[str, Any]) -> tuple[int, int]:
    width = _as_int(payload.get("width"))
    height = _as_int(payload.get("height"))
    if width > 0 or height > 0:
        if width <= 0 or height <= 0:
            raise ValueError("LTX custom resolutions require both width and height")
        invalid = [name for name, value in (("width", width), ("height", height)) if value % 64 != 0]
        if invalid:
            joined = " and ".join(invalid)
            raise ValueError(
                f"LTX two-stage generation requires {joined} to be divisible by 64; "
                f"received {width}x{height}"
            )
        return width, height

    resolution = normalize_resolution(payload.get("resolution"), "540p")
    dimensions = LTX_RESOLUTION_PRESETS.get(resolution)
    if dimensions is None:
        supported = ", ".join(LTX_RESOLUTION_PRESETS)
        raise ValueError(f"unsupported LTX resolution {resolution!r}; choose one of: {supported}")
    return dimensions


def validate_video_payload(engine: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if engine == "ltx":
        width, height = ltx_dimensions(normalized)
        normalized["width"] = width
        normalized["height"] = height
        normalized["resolution"] = normalize_resolution(normalized.get("resolution"), "540p")
        return normalized
    if engine == "hunyuan":
        resolution = normalize_resolution(normalized.get("resolution"), "720p")
        if resolution not in HUNYUAN_RESOLUTION_PRESETS:
            supported = ", ".join(HUNYUAN_RESOLUTION_PRESETS)
            raise ValueError(f"unsupported Hunyuan resolution {resolution!r}; choose one of: {supported}")
        normalized["resolution"] = resolution
        return normalized
    return normalized
