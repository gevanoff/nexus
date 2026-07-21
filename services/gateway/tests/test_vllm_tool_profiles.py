from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_profile_tool():
    path = REPO_ROOT / "deploy" / "scripts" / "vllm-tool-profile.py"
    spec = importlib.util.spec_from_file_location("nexus_vllm_tool_profile", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_production_vllm_tool_profiles_match_lane_env():
    profile_tool = _load_profile_tool()
    catalog = profile_tool.load_catalog(REPO_ROOT / "deploy" / "config" / "vllm-tool-profiles.json")
    topology = json.loads((REPO_ROOT / "deploy" / "topology" / "production.json").read_text(encoding="utf-8"))
    env = topology["defaults"]["env"]

    assert profile_tool.validate_selected_profiles(catalog, env) == []


def test_profile_validation_detects_gateway_and_parser_drift():
    profile_tool = _load_profile_tool()
    catalog = profile_tool.load_catalog(REPO_ROOT / "deploy" / "config" / "vllm-tool-profiles.json")
    profile = profile_tool.get_profile(catalog, "mistral_parallel")
    env = {
        "VLLM_FAST_TOOL_PROFILE": "mistral_parallel",
        **profile_tool.rendered_profile_env(profile, "VLLM_FAST"),
    }

    env["VLLM_FAST_NATIVE_TOOLS_ENABLED"] = "false"
    env["VLLM_FAST_TOOL_CALL_PARSER"] = "xlam"

    errors = profile_tool.validate_selected_profiles(catalog, env)
    assert any("VLLM_FAST_NATIVE_TOOLS_ENABLED" in error for error in errors)
    assert any("VLLM_FAST_TOOL_CALL_PARSER" in error for error in errors)


def test_mistral_profile_renders_gateway_alias_contract():
    catalog = json.loads(
        (REPO_ROOT / "deploy" / "config" / "vllm-tool-profiles.json").read_text(encoding="utf-8")
    )
    profile_alias = catalog["profiles"]["mistral_parallel"]["gateway_alias"]
    aliases = json.loads(
        (REPO_ROOT / "services" / "gateway" / "app" / "model_aliases.json").read_text(encoding="utf-8")
    )["aliases"]
    fast = aliases["fast"]

    for key, value in profile_alias.items():
        assert fast[key] == value
