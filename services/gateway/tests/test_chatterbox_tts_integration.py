from __future__ import annotations

import json
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_chatterbox_turbo_backend_is_registered_and_deployable() -> None:
    backends = yaml.safe_load(_read("services/gateway/app/backends_config.yaml"))["backends"]
    config = backends["chatterbox_tts"]

    assert config["class"] == "chatterbox_tts"
    assert config["base_url"] == "${CHATTERBOX_TTS_BASE_URL}"
    assert config["supported_capabilities"] == ["tts"]
    assert config["concurrency_limits"]["tts"] == 1
    assert config["health"]["readiness"] == "/readyz"

    compose = _read("docker-compose.chatterbox-tts.yml")
    assert "nexus-chatterbox-tts" in compose
    assert "chatterbox-tts==0.1.7" in _read("services/chatterbox-tts/requirements.txt")
    assert 'device_ids: ["${CHATTERBOX_TTS_CUDA_VISIBLE_DEVICES:-1}"]' in compose
    assert "/var/lib/tts_refs:ro" in compose
    assert "NEXUS_SERVICE_BACKEND_CLASS=chatterbox_tts" in compose


def test_chatterbox_turbo_is_in_stackrot_topology_and_lifecycle() -> None:
    topology = json.loads(_read("deploy/topology/production.json"))
    lifecycle = json.loads(_read("deploy/topology/backend_lifecycle.json"))

    stackrot = topology["hosts"]["stackrot"]
    defaults = topology["defaults"]["env"]

    assert "chatterbox-tts" in stackrot["components"]
    assert stackrot["env"]["CHATTERBOX_TTS_CUDA_VISIBLE_DEVICES"] == "1"
    assert defaults["CHATTERBOX_TTS_BASE_URL"] == "http://stackrot:9188"
    assert defaults["CHATTERBOX_TTS_ADVERTISE_BASE_URL"] == "http://stackrot:9188"

    backend = lifecycle["backends"]["chatterbox_tts"]
    assert backend["host"] == "stackrot"
    assert backend["component"] == "chatterbox-tts"
    assert backend["compose_file"] == "docker-compose.chatterbox-tts.yml"
    assert "tts" in backend["capabilities"]


def test_chatterbox_ui_exposes_only_supported_turbo_controls() -> None:
    html = _read("services/gateway/app/static/tts.html")
    js = _read("services/gateway/app/static/tts.js")

    for control_id in (
        "temperature",
        "topP",
        "topK",
        "repetitionPenalty",
        "seed",
        "normLoudness",
    ):
        assert f'id="{control_id}"' in html

    assert 'backendClass === "chatterbox_tts"' in js
    assert "body.temperature" in js
    assert "body.top_p" in js
    assert "body.top_k" in js
    assert "body.repetition_penalty" in js
    assert "body.seed" in js
    assert "body.norm_loudness" in js

    # Turbo explicitly ignores these original Chatterbox controls.
    assert 'id="cfgWeight"' not in html
    assert 'id="exaggeration"' not in html
    assert 'id="minP"' not in html


def test_chatterbox_service_contract_matches_gateway_expectations() -> None:
    source = _read("services/chatterbox-tts/app/main.py")

    assert '@app.post("/v1/audio/speech")' in source
    assert '@app.get("/v1/voices")' in source
    assert '@app.get("/v1/metadata")' in source
    assert '@app.get("/readyz")' in source
    assert "ChatterboxTurboTTS.from_pretrained" in source
    assert "audio_prompt_path" in source
    assert "librosa.effects.time_stretch" in source


def test_public_tts_route_can_select_backend_per_request() -> None:
    source = _read("services/gateway/app/tts_routes.py")

    assert 'body.pop("backend_class", None)' in source
    assert 'body.pop("backend", None)' in source
    assert "requested_backend or" in source


def test_core_toolset_exposes_provider_neutral_tts_generation() -> None:
    source = _read("services/gateway/app/tool_calling/registry.py")
    docs = _read("docs/TOOL_CALLING.md")

    assert '"nexus_tts_generate"' in source
    assert '"chatterbox_tts"' in source
    assert '"pocket_tts"' in source
    assert '"luxtts"' in source
    assert '"qwen3_tts"' in source
    assert '"core"' in source
    assert "check_backend_ready(backend, route_kind=\"tts\")" in source
    assert 'await check_capability(backend, "tts")' in source
    assert "nexus_tts_generate" in docs
    assert "vLLM or MLX" in docs
