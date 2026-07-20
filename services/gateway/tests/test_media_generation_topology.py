import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION = REPO_ROOT / "deploy" / "topology" / "production.json"
LIFECYCLE = REPO_ROOT / "deploy" / "topology" / "backend_lifecycle.json"
BACKENDS = REPO_ROOT / "services" / "gateway" / "app" / "backends_config.yaml"


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_media_models_are_placed_on_the_requested_hosts_without_default_startup():
    topology = _json(PRODUCTION)
    hosts = topology["hosts"]

    assert "ltx-video" in hosts["ada2"]["optional_components"]
    assert "ltx-video" not in hosts["ada2"]["components"]
    assert "hunyuan-video" in hosts["stackrot"]["optional_components"]
    assert "ace-step" in hosts["stackrot"]["optional_components"]
    assert "hunyuan-video" not in hosts["stackrot"]["components"]
    assert "ace-step" not in hosts["stackrot"]["components"]
    assert "skyreels-v2" not in hosts["ada2"].get("components", [])
    assert "skyreels-v2" not in hosts["ada2"].get("optional_components", [])

    defaults = topology["defaults"]["env"]
    assert defaults["VIDEO_BACKEND_CLASS"] == "ltx_video"
    assert defaults["MUSIC_BACKEND_CLASS"] == "ace_step_music"
    assert defaults["LTX_VIDEO_BASE_URL"] == "http://ada2:9180"
    assert defaults["HUNYUAN_VIDEO_BASE_URL"] == "http://stackrot:9185"
    assert defaults["ACE_STEP_BASE_URL"] == "http://stackrot:9195"


def test_media_models_have_assisted_lifecycle_policies():
    backends = _json(LIFECYCLE)["backends"]

    ltx = backends["ltx_video"]
    assert ltx["host"] == "ada2"
    assert ltx["component"] == "ltx-video"
    assert ltx["auto_start"] is False
    assert ltx["requires_confirmation"] is True
    assert ltx["estimated_vram_mb"] >= 32000

    for backend_class, component in (
        ("hunyuan_video", "hunyuan-video"),
        ("ace_step_music", "ace-step"),
    ):
        backend = backends[backend_class]
        assert backend["host"] == "stackrot"
        assert backend["component"] == component
        assert backend["auto_start"] is False
        assert backend["auto_stop"] is True
        assert backend["requires_confirmation"] is True

    assert "skyreels_v2" not in backends


def test_gateway_registry_exposes_new_media_capabilities():
    config = BACKENDS.read_text(encoding="utf-8")
    assert "  ltx_video:" in config
    assert "  hunyuan_video:" in config
    assert "  ace_step_music:" in config
    assert "${LTX_VIDEO_BASE_URL}" in config
    assert "${HUNYUAN_VIDEO_BASE_URL}" in config
    assert "${ACE_STEP_BASE_URL}" in config
    assert "skyreels_v2: ltx_video" in config


def test_compose_services_pin_stackrot_media_to_physical_gpu_one():
    hunyuan = (REPO_ROOT / "docker-compose.hunyuan-video.yml").read_text(encoding="utf-8")
    ace = (REPO_ROOT / "docker-compose.ace-step.yml").read_text(encoding="utf-8")
    ltx = (REPO_ROOT / "docker-compose.ltx-video.yml").read_text(encoding="utf-8")

    assert "HUNYUAN_VIDEO_CUDA_VISIBLE_DEVICES:-1" in hunyuan
    assert "ACE_STEP_CUDA_VISIBLE_DEVICES:-1" in ace
    assert "LTX_CUDA_VISIBLE_DEVICES:-0" in ltx
    assert "NEXUS_SERVICE_BACKEND_CLASS=hunyuan_video" in hunyuan
    assert "NEXUS_SERVICE_BACKEND_CLASS=ace_step_music" in ace
    assert "NEXUS_SERVICE_BACKEND_CLASS=ltx_video" in ltx


def test_skyreels_runtime_was_removed():
    assert not (REPO_ROOT / "docker-compose.skyreels-v2.yml").exists()
    assert not (REPO_ROOT / "services" / "skyreels-v2").exists()
    assert not (REPO_ROOT / "services" / "gateway" / "tools" / "skyreels_generate.py").exists()


def test_deploy_scripts_recognize_media_components():
    deploy = (REPO_ROOT / "deploy" / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    preflight = (REPO_ROOT / "deploy" / "scripts" / "preflight-check.sh").read_text(encoding="utf-8")
    for component in ("ltx-video", "hunyuan-video", "ace-step"):
        assert component in deploy
        assert component in preflight
