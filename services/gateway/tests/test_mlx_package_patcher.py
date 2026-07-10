from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_patcher():
    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / "services" / "mlx" / "scripts" / "patch_mlx_openai_server.py"
    spec = importlib.util.spec_from_file_location("nexus_mlx_patcher", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mlx_patcher_hardens_minimax_tool_parser():
    patcher = _load_patcher()
    source = "prefix\n" + patcher.GLM_TOOL_BEFORE + "\nsuffix"

    patched, changes = patcher._patch_glm4_moe_text(source)
    patched_again, changes_again = patcher._patch_glm4_moe_text(patched)

    assert changes == ["defensive GLM/MiniMax tool parser"]
    assert changes_again == []
    assert "if tc_detail is None:" in patched
    assert 'tc_args = tc_detail.group(2) or ""' in patched
    assert 'return {"content": model_output}' in patched
    assert patched_again == patched


def test_mlx_patcher_hardens_kimi_tool_parser():
    patcher = _load_patcher()
    source = "prefix\n" + patcher.KIMI_TOOL_BEFORE + "\nsuffix"

    patched, changes = patcher._patch_kimi_k2_text(source)
    patched_again, changes_again = patcher._patch_kimi_k2_text(patched)

    assert changes == ["defensive Kimi K2 tool parser"]
    assert changes_again == []
    assert "if name_match is None:" in patched
    assert patched_again == patched


def test_mlx_patcher_adds_glm_dsa_indexshare_model():
    patcher = _load_patcher()
    source = patcher.GLM_DSA_ORIGINAL_MARKER

    patched, changes = patcher._patch_glm_moe_dsa_model_text(source)
    patched_again, changes_again = patcher._patch_glm_moe_dsa_model_text(patched)

    assert changes == ["GLM DSA IndexShare model"]
    assert changes_again == []
    assert patcher.GLM_DSA_PATCH_MARKER in patched
    assert "indexer_types: Optional[list[str]] = None" in patched
    assert 'if self.indexer_type != "full":' in patched
    assert "shared_topk_indices" in patched
    assert "if layer.self_attn.indexer is not None:" in patched
    assert "else CacheList(KVCache())" in patched
    assert patched_again == patched


def test_mlx_patcher_upgrades_glm_dsa_handler_thread_guard():
    patcher = _load_patcher()
    source = "\n".join(
        [
            patcher.HELPER_LEGACY_AFTER,
            patcher.CONTEXT_AFTER,
            patcher.CACHE_AFTER,
            patcher.STREAM_AFTER,
            patcher.NONSTREAM_AFTER,
            "            if not force_handler_thread and not self.model.cache_is_trimmable:\n",
            "        if not force_handler_thread and not self.model.cache_is_trimmable:\n",
        ]
    )

    patched, changes = patcher._patch_text(source)

    assert "handler-thread GLM DSA helper upgrade" in changes
    assert 'model_type == "glm_moe_dsa"' in patched


def test_mlx_patcher_upgrades_glm_dsa_shared_cache_shape():
    patcher = _load_patcher()
    source = "\n".join([patcher.GLM_DSA_PATCH_MARKER, patcher.GLM_DSA_LEGACY_CACHE])

    patched, changes = patcher._patch_glm_moe_dsa_model_text(source)

    assert changes == ["GLM DSA shared-layer cache shape"]
    assert "else CacheList(KVCache())" in patched


def test_mlx_patcher_extends_large_model_ready_timeout():
    patcher = _load_patcher()
    source = "\n".join(
        [
            "import os",
            patcher.HANDLER_READY_TIMEOUT_CONSTANT_MARKER.rstrip(),
            f"                {patcher.HANDLER_READY_TIMEOUT_CALL}",
            f"            {patcher.HANDLER_READY_TIMEOUT_CALL}",
        ]
    )

    patched, changes = patcher._patch_handler_process_text(source)
    patched_again, changes_again = patcher._patch_handler_process_text(patched)

    assert changes == ["configurable large-model ready timeout"]
    assert changes_again == []
    assert 'os.getenv("MLX_MODEL_READY_TIMEOUT_SEC", "900")' in patched
    assert patched.count("timeout=_nexus_model_ready_timeout_seconds()") == 2
    assert patched_again == patched
