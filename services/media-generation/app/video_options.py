from __future__ import annotations

import re
from typing import Any


LTX_RESOLUTION_PRESETS: dict[str, tuple[int, int]] = {
    "480p": (704, 384),
    "540p": (768, 512),
    "720p": (1280, 704),
}
HUNYUAN_RESOLUTION_PRESETS: tuple[str, ...] = ("480p", "720p")

LTX_MAX_EDGE = 1280
LTX_MAX_PIXELS = 1280 * 704
LTX_MAX_FRAMES = 257
HUNYUAN_MAX_FRAMES = 241
DEFAULT_VIDEO_FPS = 24
_EXPLICIT_RESOLUTION_RE = re.compile(r"^(\d+)x(\d+)$")


def _strict_positive_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{name} must be a positive integer")
        parsed = int(value)
    else:
        text = str(value or "").strip()
        if not text.isdigit():
            raise ValueError(f"{name} must be a positive integer")
        parsed = int(text)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def normalize_resolution(value: Any, default: str) -> str:
    normalized = str(value or default).strip().lower()
    aliases = {
        "480": "480p",
        "540": "540p",
        "720": "720p",
    }
    return aliases.get(normalized, normalized)


def _explicit_dimensions(payload: dict[str, Any]) -> tuple[int, int] | None:
    width_present = payload.get("width") not in (None, "")
    height_present = payload.get("height") not in (None, "")
    if not width_present and not height_present:
        return None
    if not width_present or not height_present:
        raise ValueError("custom video resolutions require both width and height")
    return (
        _strict_positive_int(payload.get("width"), name="width"),
        _strict_positive_int(payload.get("height"), name="height"),
    )


def _resolution_dimensions(value: str) -> tuple[int, int] | None:
    match = _EXPLICIT_RESOLUTION_RE.fullmatch(value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _validate_ltx_custom_dimensions(width: int, height: int) -> tuple[int, int]:
    invalid = [name for name, value in (("width", width), ("height", height)) if value % 64 != 0]
    if invalid:
        joined = " and ".join(invalid)
        raise ValueError(
            f"LTX two-stage generation requires {joined} to be divisible by 64; "
            f"received {width}x{height}"
        )
    if max(width, height) > LTX_MAX_EDGE or width * height > LTX_MAX_PIXELS:
        raise ValueError(
            "LTX resolution exceeds the configured safety limit; "
            f"received {width}x{height}, maximum edge {LTX_MAX_EDGE} and "
            f"maximum area {LTX_MAX_PIXELS} pixels"
        )
    return width, height


def ltx_dimensions(payload: dict[str, Any]) -> tuple[int, int]:
    resolution_was_supplied = payload.get("resolution") not in (None, "")
    resolution = normalize_resolution(payload.get("resolution"), "540p")

    # A symbolic profile is authoritative. The Gateway may add conventional
    # dimensions such as 1280x720 while normalizing an advanced request; those
    # dimensions are not valid for the LTX two-stage pipeline and must not
    # override the backend-specific profile.
    preset = LTX_RESOLUTION_PRESETS.get(resolution)
    if preset is not None:
        return preset

    dimensions = _explicit_dimensions(payload)
    if dimensions is None:
        dimensions = _resolution_dimensions(resolution)
    if dimensions is not None:
        return _validate_ltx_custom_dimensions(*dimensions)

    if resolution_was_supplied:
        supported = ", ".join(LTX_RESOLUTION_PRESETS)
        raise ValueError(
            f"unsupported LTX resolution {resolution!r}; choose one of: {supported}, "
            "or provide a WIDTHxHEIGHT value divisible by 64"
        )
    return LTX_RESOLUTION_PRESETS["540p"]


def _validate_timing(engine: str, payload: dict[str, Any]) -> None:
    fps = _strict_positive_int(payload.get("fps", DEFAULT_VIDEO_FPS), name="fps")
    if fps > 60:
        raise ValueError("fps must not exceed 60")

    explicit_frames = payload.get("num_frames") not in (None, "")
    if explicit_frames:
        requested_frames = _strict_positive_int(payload.get("num_frames"), name="num_frames")
    else:
        duration_value = payload.get("duration_seconds", payload.get("duration", 5))
        duration = _strict_positive_int(duration_value, name="duration_seconds")
        requested_frames = duration * fps + 1

    maximum = LTX_MAX_FRAMES if engine == "ltx" else HUNYUAN_MAX_FRAMES
    if requested_frames > maximum:
        max_seconds = max(1, (maximum - 1) // fps)
        raise ValueError(
            f"{engine} request would generate {requested_frames} frames, above the "
            f"{maximum}-frame limit at {fps} fps; use at most {max_seconds} seconds"
        )


def validate_video_payload(engine: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if engine == "ltx":
        width, height = ltx_dimensions(normalized)
        normalized["width"] = width
        normalized["height"] = height
        requested_resolution = normalize_resolution(normalized.get("resolution"), "540p")
        matching_profile = next(
            (name for name, dimensions in LTX_RESOLUTION_PRESETS.items() if dimensions == (width, height)),
            None,
        )
        normalized["resolution"] = matching_profile or (
            requested_resolution if requested_resolution in LTX_RESOLUTION_PRESETS else "custom"
        )
        _validate_timing("ltx", normalized)
        return normalized
    if engine == "hunyuan":
        resolution = normalize_resolution(normalized.get("resolution"), "720p")
        if resolution not in HUNYUAN_RESOLUTION_PRESETS:
            supported = ", ".join(HUNYUAN_RESOLUTION_PRESETS)
            raise ValueError(f"unsupported Hunyuan resolution {resolution!r}; choose one of: {supported}")
        # Width and height may have been attached by a generic Gateway
        # normalizer. Hunyuan consumes the symbolic profile instead.
        normalized.pop("width", None)
        normalized.pop("height", None)
        normalized["resolution"] = resolution
        _validate_timing("hunyuan", normalized)
        return normalized
    return normalized
