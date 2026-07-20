from __future__ import annotations

import importlib.util
import socket
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
MEDIA_APP = REPO_ROOT / "services" / "media-generation" / "app"
GATEWAY_TOOLS = REPO_ROOT / "services" / "gateway" / "tools"


def _load_module(path: Path):
    name = f"_nexus_test_{path.stem}_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_video_input_pins_the_validated_address(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(MEDIA_APP / "run_video.py")
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        output_index = command.index("--output") + 1
        Path(command[output_index]).write_bytes(b"image")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    output = module._download_input("https://example.com/input.png", tmp_path)
    command = captured["command"]

    assert Path(output).read_bytes() == b"image"
    assert "--resolve" in command
    assert "example.com:443:93.184.216.34" in command
    assert "--location" not in command


def test_remote_video_input_rejects_any_non_public_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module(MEDIA_APP / "run_video.py")
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )

    with pytest.raises(ValueError, match="non-public"):
        module._resolve_public_http_url("https://example.com/input.png")


def test_video_service_is_not_ready_without_a_supported_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXUS_MEDIA_ENGINE", raising=False)
    module = _load_module(MEDIA_APP / "video_main.py")

    errors = module._required_path_errors()
    response = module.readyz()

    assert any("NEXUS_MEDIA_ENGINE must be one of" in error for error in errors)
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("filename", "env_name"),
    [
        ("ltx_generate.py", "LTX_VIDEO_TIMEOUT_SEC"),
        ("hunyuan_video_generate.py", "HUNYUAN_VIDEO_TIMEOUT_SEC"),
        ("ace_step_generate.py", "ACE_STEP_TIMEOUT_SEC"),
    ],
)
def test_gateway_media_tools_reject_invalid_timeout_values(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    env_name: str,
) -> None:
    module = _load_module(GATEWAY_TOOLS / filename)
    monkeypatch.setenv(env_name, "not-a-number")

    with pytest.raises(ValueError, match=env_name):
        module._timeout_seconds(env_name, 60.0)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_gateway_media_tool_timeout_must_be_positive_and_finite(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    module = _load_module(GATEWAY_TOOLS / "ltx_generate.py")
    monkeypatch.setenv("LTX_VIDEO_TIMEOUT_SEC", value)

    with pytest.raises(ValueError, match="positive finite"):
        module._timeout_seconds("LTX_VIDEO_TIMEOUT_SEC", 60.0)
