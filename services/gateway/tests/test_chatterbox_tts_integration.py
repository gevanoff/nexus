from __future__ import annotations

import importlib.util
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
    assert 'await ensure_tts_backend_ready(backend, reason="tool_tts", route_kind="tts")' in source
    assert 'getattr(S, "TTS_BACKEND_CLASS", "")' in source
    assert 'or "pocket_tts"' in source
    assert 'await check_capability(backend, "tts")' in source
    assert "nexus_tts_generate" in docs
    assert "vLLM or MLX" in docs



def test_chatterbox_reference_resolution_is_confined_to_library() -> None:
    source = _read("services/chatterbox-tts/app/main.py")
    assert "_VOICE_STEM_RE.fullmatch(raw)" in source
    assert "_refs_dir().expanduser().resolve()" in source
    assert "root not in candidate.parents" in source
    assert "candidate = Path(raw)" not in source


def test_chatterbox_seed_is_serialized_with_generation() -> None:
    source = _read("services/chatterbox-tts/app/main.py")
    lock_at = source.index("with _SYNTH_LOCK, torch.inference_mode():")
    seed_at = source.index("random.seed(seed)", lock_at)
    generate_at = source.index("model.generate(text, **kwargs)", seed_at)
    assert lock_at < seed_at < generate_at


def test_chatterbox_is_allowed_by_deployment_control() -> None:
    compose = _read("docker-compose.deployment-control.yml")
    control = _read("services/deployment-control/app/main.py")
    preflight = _read("deploy/scripts/preflight-check.sh")
    assert "chatterbox-tts" in compose
    assert "chatterbox-tts" in control
    assert "tts|chatterbox-tts|luxtts" in preflight
    assert "append_component_unique chatterbox-tts" in preflight
    assert 'check_port_required CHATTERBOX_TTS_PORT 9188 "Chatterbox TTS"' in preflight


def test_tts_tool_balances_admission_and_lifecycle() -> None:
    source = _read("services/gateway/app/tool_calling/registry.py")
    assert "acquired = False" in source
    assert 'await admission.acquire(backend, "tts")' in source
    assert "acquired = True" in source
    assert "if acquired:" in source
    assert 'await _notify_tts_lifecycle(backend, "start")' in source
    assert 'await _notify_tts_lifecycle(backend, "finish")' in source


def test_tts_tool_uses_shared_cache_and_bearer_artifact_route() -> None:
    registry = _read("services/gateway/app/tool_calling/registry.py")
    audio_routes = _read("services/gateway/app/audio_routes.py")
    cache = _read("services/gateway/app/audio_cache.py")
    ui_routes = _read("services/gateway/app/ui_routes.py")
    assert "save_audio_cache" in registry
    assert 'relative_url = f"/v1/audio/artifacts/{name}"' in registry
    assert '@router.get("/v1/audio/artifacts/{filename}")' in audio_routes
    assert "require_bearer(req)" in audio_routes
    assert "resolve_audio_cache_path(filename)" in audio_routes
    assert "UI_AUDIO_MAX_BYTES" in cache
    assert "UI_AUDIO_TTL_SEC" in cache
    assert "save_audio_cache(audio_bytes=audio_bytes, mime_hint=mime_hint)" in ui_routes


def test_tts_ui_refreshes_backend_controls_after_restore() -> None:
    source = _read("services/gateway/app/static/tts.js")
    restore_at = source.index("const serverSettings = await loadUserSettings();")
    refresh_at = source.index("updateBackendSpecificControls();", restore_at)
    voice_restore_at = source.index("serverSettings.tts.voice", restore_at)
    assert restore_at < refresh_at < voice_restore_at


def test_gateway_tool_budgets_cover_tts_synthesis() -> None:
    config = _read("services/gateway/app/config.py")
    compose = _read("docker-compose.gateway.yml")
    env_example = _read(".env.example")
    docs = _read("docs/TOOL_CALLING.md")
    assert "NEXUS_TOOL_TIMEOUT_SEC: float = 300.0" in config
    assert "NEXUS_TOOL_LOOP_TIMEOUT_SEC: float = 360.0" in config
    assert "NEXUS_TOOL_TIMEOUT_SEC=${NEXUS_TOOL_TIMEOUT_SEC:-300}" in compose
    assert "NEXUS_TOOL_LOOP_TIMEOUT_SEC=${NEXUS_TOOL_LOOP_TIMEOUT_SEC:-360}" in compose
    assert "NEXUS_TOOL_TIMEOUT_SEC=300" in env_example
    assert "NEXUS_TOOL_LOOP_TIMEOUT_SEC=360" in env_example
    assert "NEXUS_TOOL_TIMEOUT_SEC=300" in docs
    assert "NEXUS_TOOL_LOOP_TIMEOUT_SEC=360" in docs



def test_lifecycle_manager_maps_chatterbox_advertise_url() -> None:
    source = _read("services/lifecycle-manager/app/main.py")
    assert '"chatterbox_tts": "CHATTERBOX_TTS_ADVERTISE_BASE_URL"' in source


def test_tts_surfaces_share_policy_aware_activation() -> None:
    runtime = _read("services/gateway/app/tts_backend.py")
    registry = _read("services/gateway/app/tool_calling/registry.py")
    routes = _read("services/gateway/app/tts_routes.py")
    ui = _read("services/gateway/app/ui_routes.py")
    health = _read("services/gateway/app/health_checker.py")

    assert '"/v1/lifecycle/ensure"' in runtime
    assert "requires_confirmation" in runtime
    assert "blocked" in runtime
    assert "observe_only" in runtime
    assert "await checker.refresh_backend(backend_class)" in runtime
    assert "status.raw_ready is False" in runtime
    assert "async def refresh_backend" in health
    assert 'ensure_tts_backend_ready(backend, reason="tool_tts"' in registry
    assert 'ensure_tts_backend_ready(backend_class, reason="api_tts"' in routes
    assert 'ensure_tts_backend_ready(backend_class, reason="ui_tts"' in ui
    assert 'ensure_tts_backend_ready(backend_class, reason="ui_chat_tts"' in ui


def test_reference_generation_restores_default_conditioning() -> None:
    module_path = REPO_ROOT / "services/chatterbox-tts/app/conditioning.py"
    spec = importlib.util.spec_from_file_location("chatterbox_conditioning_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeModel:
        def __init__(self) -> None:
            self.conds = "default"
            self.seen: list[str] = []

        def generate(self, text: str, **kwargs):
            self.seen.append(self.conds)
            if kwargs.get("audio_prompt_path"):
                self.conds = "reference"
            return text

    model = FakeModel()
    assert module.generate_preserving_conditioning(
        model,
        "reference request",
        {"audio_prompt_path": "/refs/alice.wav"},
        restore_after=True,
    ) == "reference request"
    assert model.conds == "default"

    assert module.generate_preserving_conditioning(
        model,
        "default request",
        {},
        restore_after=False,
    ) == "default request"
    assert model.seen == ["default", "default"]
    assert model.conds == "default"



def test_chatterbox_service_name_is_canonicalized() -> None:
    source = _read("services/gateway/app/backends.py")
    assert '"chatterbox-tts": "chatterbox_tts"' in source
    assert '"chatterbox_tts": "chatterbox_tts"' in source
    assert '"chatterbox_tts": "chatterbox-tts"' in source


def test_tool_calling_docs_do_not_render_literal_paragraph_escapes() -> None:
    docs = _read("docs/TOOL_CALLING.md")
    assert "falsely advertised as capable.\\n\\nAll built-in schemas" not in docs
    assert "falsely advertised as capable.\n\nAll built-in schemas" in docs
