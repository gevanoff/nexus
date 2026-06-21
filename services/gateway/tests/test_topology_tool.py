from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_topology_tool():
    roots = [Path(__file__).resolve().parents[3], Path(__file__).resolve().parents[1]]
    path = next(
        (root / "deploy" / "scripts" / "topology_tool.py" for root in roots if (root / "deploy" / "scripts" / "topology_tool.py").exists()),
        roots[0] / "deploy" / "scripts" / "topology_tool.py",
    )
    spec = importlib.util.spec_from_file_location("nexus_topology_tool", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_env_file_drops_deprecated_huge_model_inventory(tmp_path):
    topology_tool = _load_topology_tool()
    template = tmp_path / ".env.example"
    output = tmp_path / ".env"
    template.write_text("KEEP=template\nMLX_HUGE_MODELS=template-model\n", encoding="utf-8")
    output.write_text(
        "KEEP=existing\nMLX_HUGE_MODELS=stale-model\nMLX_HUGE_LANE_DEFAULT_MODEL=stale-default\n",
        encoding="utf-8",
    )

    topology_tool.render_env_file(
        template,
        output,
        {"KEEP": "topology", "MLX_HUGE_MODELS": "ignored-model"},
    )

    rendered = output.read_text(encoding="utf-8")
    assert "KEEP=topology" in rendered
    assert "MLX_HUGE_MODELS" not in rendered
    assert "MLX_HUGE_LANE_DEFAULT_MODEL" not in rendered
