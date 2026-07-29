from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
MEDIA_ROOT = REPO_ROOT / "services" / "media-generation"
MEDIA_APP = MEDIA_ROOT / "app"
GATEWAY_TOOLS = REPO_ROOT / "services" / "gateway" / "tools"


def _load_module(path: Path):
    name = f"_nexus_test_{path.stem}_{uuid4().hex}"
    original_path = list(sys.path)
    saved_app_modules: dict[str, object] = {}
    isolate_media_package = path.parent == MEDIA_APP and path.name == "video_main.py"
    if path.parent == MEDIA_APP:
        sys.path.insert(0, str(MEDIA_ROOT if isolate_media_package else MEDIA_APP))
    if isolate_media_package:
        saved_app_modules = {
            module_name: module
            for module_name, module in list(sys.modules.items())
            if module_name == "app" or module_name.startswith("app.")
        }
        for module_name in saved_app_modules:
            sys.modules.pop(module_name, None)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = original_path
        if isolate_media_package:
            for module_name in list(sys.modules):
                if module_name == "app" or module_name.startswith("app."):
                    sys.modules.pop(module_name, None)
            sys.modules.update(saved_app_modules)


def _public_resolution(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def test_remote_video_input_pins_the_validated_address_and_disables_proxies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(MEDIA_APP / "run_video.py")
    monkeypatch.setattr(module.socket, "getaddrinfo", _public_resolution)
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
    assert "--noproxy" in command
    assert command[command.index("--noproxy") + 1] == "*"
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


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/input.png",
        "http://localhost.localdomain/input.png",
        "http://anything.localhost/input.png",
    ],
)
def test_remote_video_input_rejects_localhost_names(url: str) -> None:
    module = _load_module(MEDIA_APP / "run_video.py")

    with pytest.raises(ValueError, match="localhost"):
        module._resolve_public_http_url(url)


def test_remote_video_input_removes_oversized_partial_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(MEDIA_APP / "run_video.py")
    monkeypatch.setattr(module.socket, "getaddrinfo", _public_resolution)
    monkeypatch.setenv("MEDIA_MAX_INPUT_BYTES", "4")

    def fake_run(command, **kwargs):
        output_index = command.index("--output") + 1
        Path(command[output_index]).write_bytes(b"12345")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        module._download_input("https://example.com/input.png", tmp_path)

    assert not (tmp_path / "input.png").exists()


def test_video_service_is_not_ready_without_a_supported_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXUS_MEDIA_ENGINE", raising=False)
    module = _load_module(MEDIA_APP / "video_main.py")

    errors = module._required_path_errors()
    response = module.readyz()

    assert any("NEXUS_MEDIA_ENGINE must be one of" in error for error in errors)
    assert response.status_code == 503


def test_video_output_content_type_is_inferred_from_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NEXUS_MEDIA_ENGINE", "ltx")
    module = _load_module(MEDIA_APP / "video_main.py")
    monkeypatch.setattr(module, "_output_root", lambda: tmp_path)

    job_id = f"ltx_{'a' * 32}"
    output_dir = tmp_path / job_id
    output_dir.mkdir()
    (output_dir / "result.json").write_text("{}", encoding="utf-8")

    response = module.get_output(job_id, "result.json")

    assert response.media_type == "application/json"


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
