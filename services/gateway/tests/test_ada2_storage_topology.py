from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_ada2_separates_nexus_runtime_and_backend_owned_data() -> None:
    topology = json.loads(
        (REPO_ROOT / "deploy" / "topology" / "production.json").read_text(
            encoding="utf-8"
        )
    )
    env = topology["hosts"]["ada2"]["env"]

    assert env["NEXUS_RUNTIME_ROOT"] == "/data/nexus-runtime"
    assert env["INVOKEAI_DATA_BIND_SOURCE"] == "/data/invokeai"
    assert env["SKYREELS_DATA_BIND_SOURCE"] == "/data/skyreels-v2"
    assert env["HEARTMULA_DATA_BIND_SOURCE"] == "/data/heartmula"
    assert env["FOLLOWYOURCANVAS_DATA_BIND_SOURCE"] == "/data/followyourcanvas"
    assert env["PERSONAPLEX_DATA_BIND_SOURCE"] == "/data/personaplex"
    assert env["HF_HOME"] == "/data/huggingface"
    assert env["HF_HOME_BIND_SOURCE"] == "/data/huggingface"
    assert env["HF_HUB_CACHE"] == "/data/huggingface/hub"
    assert env["HUGGINGFACE_HUB_CACHE"] == "/data/huggingface/hub"
    assert env["TRANSFORMERS_CACHE"] == "/data/huggingface/transformers"


def test_ada2_backend_compose_defaults_do_not_bind_var_lib_huggingface() -> None:
    compose_files = (
        "docker-compose.invokeai.yml",
        "docker-compose.lighton-ocr.yml",
        "docker-compose.skyreels-v2.yml",
        "docker-compose.vllm-strong.yml",
        "docker-compose.heartmula.yml",
    )

    for relative_path in compose_files:
        content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "/var/lib/huggingface" not in content, relative_path
        assert "/data/huggingface" in content, relative_path


def test_migrated_backends_have_independent_bind_sources() -> None:
    compose_bind_sources = {
        "docker-compose.invokeai.yml": "INVOKEAI_DATA_BIND_SOURCE",
        "docker-compose.skyreels-v2.yml": "SKYREELS_DATA_BIND_SOURCE",
        "docker-compose.heartmula.yml": "HEARTMULA_DATA_BIND_SOURCE",
        "docker-compose.followyourcanvas.yml": "FOLLOWYOURCANVAS_DATA_BIND_SOURCE",
        "docker-compose.personaplex.yml": "PERSONAPLEX_DATA_BIND_SOURCE",
    }

    for relative_path, bind_source in compose_bind_sources.items():
        content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert f"${{{bind_source}:-${{NEXUS_RUNTIME_ROOT:" in content, relative_path
