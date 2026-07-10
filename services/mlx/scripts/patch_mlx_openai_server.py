#!/usr/bin/env python3
"""Apply Nexus compatibility patches to the installed mlx-openai-server package."""

from __future__ import annotations

import argparse
import importlib.util
import py_compile
import shutil
import sys
import time
from pathlib import Path
from typing import Callable


HELPER_BEFORE = """        logger.info(f"Initialized MLXHandler with model path: {model_path}")

    async def get_models(self) -> list[dict[str, Any]]:
"""

HELPER_AFTER = """        logger.info(f"Initialized MLXHandler with model path: {model_path}")

    def _requires_handler_thread_generation(self) -> bool:
        \"\"\"Return True for model families whose MLX stream state is thread-local.\"\"\"
        model_type = str(self.model_type)
        return model_type.startswith("deepseek_v") or model_type == "glm_moe_dsa"

    async def get_models(self) -> list[dict[str, Any]]:
"""

HELPER_LEGACY_AFTER = """        logger.info(f"Initialized MLXHandler with model path: {model_path}")

    def _requires_handler_thread_generation(self) -> bool:
        \"\"\"Return True for model families whose MLX stream state is thread-local.\"\"\"
        return str(self.model_type).startswith("deepseek_v")

    async def get_models(self) -> list[dict[str, Any]]:
"""

CONTEXT_BEFORE = """        input_ids = self.model.encode_prompt(input_prompt)

        if self._is_request_batchable(request):
"""

CONTEXT_AFTER = """        input_ids = self.model.encode_prompt(input_prompt)
        force_handler_thread = self._requires_handler_thread_generation()

        if self._is_request_batchable(request) and not force_handler_thread:
"""

CACHE_BEFORE = """        with self._generation_lock:
            cache, rest_input_ids = self.prompt_cache.fetch_nearest_cache(
                input_ids,
                allowed_sources={"nonbatch"},
            )
            cache, rest_input_ids = self._normalize_nonbatch_cache_hit(
                input_ids,
                cache,
                rest_input_ids,
            )
            if cache is None:
                cache = self.model.create_prompt_cache()

        # Cache key must be the FULL input_ids, not rest_input_ids.
"""

CACHE_AFTER = """        if force_handler_thread:
            # DeepSeek MLX cache/stream state is bound to the thread that creates it.
            # Let mlx_lm create and own the prompt cache inside direct generation.
            cache, rest_input_ids = None, input_ids
        else:
            with self._generation_lock:
                cache, rest_input_ids = self.prompt_cache.fetch_nearest_cache(
                    input_ids,
                    allowed_sources={"nonbatch"},
                )
                cache, rest_input_ids = self._normalize_nonbatch_cache_hit(
                    input_ids,
                    cache,
                    rest_input_ids,
                )
                if cache is None:
                    cache = self.model.create_prompt_cache()

        # Cache key must be the FULL input_ids, not rest_input_ids.
"""

STREAM_BEFORE = """            use_batch = self._is_request_batchable(request) and ctx.checkpoint_position is None
            if use_batch:
                scheduler = await self._get_or_start_scheduler()
                response_generator = self._submit_batched_stream(scheduler, ctx)
            else:
                response_generator = self.inference_worker.submit_stream(
"""

STREAM_AFTER = """            force_handler_thread = self._requires_handler_thread_generation()
            use_batch = (
                self._is_request_batchable(request)
                and ctx.checkpoint_position is None
                and not force_handler_thread
            )
            if use_batch:
                scheduler = await self._get_or_start_scheduler()
                response_generator = self._submit_batched_stream(scheduler, ctx)
            elif force_handler_thread:
                sync_response_generator = self._stream_with_lock(
                    input_ids=ctx.rest_input_ids,
                    prompt_cache=ctx.cache,
                    stream=True,
                    **request_data,
                )

                async def _response_generator() -> AsyncGenerator[Any, None]:
                    for item in sync_response_generator:
                        yield item
                        await asyncio.sleep(0)

                response_generator = _response_generator()
            else:
                response_generator = self.inference_worker.submit_stream(
"""

NONSTREAM_BEFORE = """        if self._is_request_batchable(request) and ctx.checkpoint_position is None:
            scheduler = await self._get_or_start_scheduler()
            return await self._collect_batched_response(scheduler, ctx)
"""

NONSTREAM_AFTER = """        if self._requires_handler_thread_generation():
            return self._generate_with_lock(
                input_ids=ctx.rest_input_ids,
                prompt_cache=ctx.cache,
                stream=False,
                **request_data,
            )
        if self._is_request_batchable(request) and ctx.checkpoint_position is None:
            scheduler = await self._get_or_start_scheduler()
            return await self._collect_batched_response(scheduler, ctx)
"""

NONTRIMMABLE_GUARDS = [
    (
        "            if not self.model.cache_is_trimmable:\n",
        "            if not force_handler_thread and not self.model.cache_is_trimmable:\n",
        "batched non-trimmable cache guard",
    ),
    (
        "        if not self.model.cache_is_trimmable:\n",
        "        if not force_handler_thread and not self.model.cache_is_trimmable:\n",
        "non-batched non-trimmable cache guard",
    ),
]

GLM_TOOL_BEFORE = """        tool_calls = []
        for match in matches:
            tc_detail = self.func_detail_regex.search(match)
            tc_name = tc_detail.group(1)
            tc_args = tc_detail.group(2)
            pairs = self.func_arg_regex.findall(tc_args)
            arg_dct = {}
            for key, value in pairs:
                arg_key = key.strip()
                arg_value = value.strip()
                arg_dct[arg_key] = arg_value
            tool_calls.append(
                {"name": tc_name.strip(), "arguments": json.dumps(arg_dct, ensure_ascii=False)}
            )
        return {"tool_calls": tool_calls}
"""

GLM_TOOL_AFTER = """        tool_calls = []
        malformed_blocks = []
        for match in matches:
            tc_detail = self.func_detail_regex.search(match)
            if tc_detail is None:
                malformed_blocks.append(match)
                continue
            tc_name = tc_detail.group(1)
            tc_args = tc_detail.group(2) or ""
            pairs = self.func_arg_regex.findall(tc_args)
            arg_dct = {}
            for key, value in pairs:
                arg_key = key.strip()
                arg_value = value.strip()
                arg_dct[arg_key] = arg_value
            tool_calls.append(
                {"name": tc_name.strip(), "arguments": json.dumps(arg_dct, ensure_ascii=False)}
            )
        if malformed_blocks or not tool_calls:
            return {"content": model_output}
        return {"tool_calls": tool_calls}
"""

KIMI_TOOL_BEFORE = """                name_match = self.tool_name_regex.search(header)
                name = name_match.group(1)
"""

KIMI_TOOL_AFTER = """                name_match = self.tool_name_regex.search(header)
                if name_match is None:
                    continue
                name = name_match.group(1)
"""

GLM_DSA_PATCH_MARKER = "class GlmMoeDsaAttention(DeepseekV32Attention):"

GLM_DSA_ORIGINAL_MARKER = """from .base import BaseModelArgs
from .deepseek_v32 import Model as DSV32Model
"""

GLM_DSA_LEGACY_CACHE = """    def make_cache(self):
        return [CacheList(KVCache(), KVCache()) for _ in self.layers]
"""

GLM_DSA_INDEXSHARE_CACHE = """    def make_cache(self):
        return [
            CacheList(KVCache(), KVCache())
            if layer.self_attn.indexer is not None
            else CacheList(KVCache())
            for layer in self.layers
        ]
"""

HANDLER_READY_TIMEOUT_IMPORT_MARKER = "import os\n"
HANDLER_READY_TIMEOUT_CONSTANT_MARKER = '_CANCEL = "__CANCEL__"\n'
HANDLER_READY_TIMEOUT_CALL = "response = await self._wait_for_ready(ready_queue, timeout=300)"
HANDLER_READY_TIMEOUT_HELPER = '''

def _nexus_model_ready_timeout_seconds() -> float:
    """Allow very large local models enough time to initialize."""
    raw = os.getenv("MLX_MODEL_READY_TIMEOUT_SEC", "900")
    try:
        return max(300.0, float(raw))
    except (TypeError, ValueError):
        return 900.0
'''

GLM_DSA_PATCHED_TEXT = '''# Copyright © 2025 Apple Inc.

from dataclasses import dataclass
from typing import Any, Dict, Optional

import mlx.core as mx
import mlx.nn as nn

from .base import BaseModelArgs, create_attention_mask, scaled_dot_product_attention
from .cache import CacheList, KVCache
from .deepseek_v32 import (
    DeepseekV32Attention,
    DeepseekV32MLP,
    DeepseekV32MoE,
    Model as DSV32Model,
)


@dataclass
class ModelArgs(BaseModelArgs):
    model_type: str
    vocab_size: int
    hidden_size: int
    index_head_dim: int
    index_n_heads: int
    index_topk: int
    intermediate_size: int
    moe_intermediate_size: int
    num_hidden_layers: int
    num_attention_heads: int
    num_key_value_heads: int
    n_shared_experts: Optional[int]
    n_routed_experts: Optional[int]
    routed_scaling_factor: float
    kv_lora_rank: int
    q_lora_rank: int
    qk_rope_head_dim: int
    v_head_dim: int
    qk_nope_head_dim: int
    topk_method: str
    scoring_func: str
    norm_topk_prob: bool
    n_group: int
    topk_group: int
    num_experts_per_tok: int
    moe_layer_freq: int
    first_k_dense_replace: int
    max_position_embeddings: int
    rms_norm_eps: float
    rope_parameters: Dict
    attention_bias: bool
    indexer_types: Optional[list[str]] = None
    rope_scaling: Dict = None
    rope_theta: Optional[float] = None

    def __post_init__(self):
        self.rope_scaling = self.rope_parameters
        self.rope_theta = self.rope_parameters["rope_theta"]


def _indexer_type(config: ModelArgs, layer_idx: int) -> str:
    indexer_types = config.indexer_types
    if not indexer_types:
        return "full"
    if layer_idx >= len(indexer_types):
        return "full"
    return str(indexer_types[layer_idx] or "full").lower()


class GlmMoeDsaAttention(DeepseekV32Attention):
    def __init__(self, config: ModelArgs, layer_idx: int):
        super().__init__(config)
        self.indexer_type = _indexer_type(config, layer_idx)
        if self.indexer_type != "full":
            self.indexer = None

    def __call__(
        self,
        x: mx.array,
        mask: Optional[mx.array] = None,
        cache: Optional[Any] = None,
        shared_topk_indices: Optional[mx.array] = None,
    ) -> tuple[mx.array, Optional[mx.array]]:
        B, L, D = x.shape

        qr = self.q_a_layernorm(self.q_a_proj(x))
        q = self.q_b_proj(qr)

        q = q.reshape(B, L, self.num_heads, self.q_head_dim).transpose(0, 2, 1, 3)
        q_nope, q_pe = mx.split(q, [self.qk_nope_head_dim], axis=-1)
        compressed_kv = self.kv_a_proj_with_mqa(x)
        compressed_kv, k_pe = mx.split(compressed_kv, [self.kv_lora_rank], axis=-1)
        k_pe = k_pe.reshape(B, L, 1, self.qk_rope_head_dim).transpose(0, 2, 1, 3)
        kv_latent = self.kv_a_layernorm(compressed_kv)

        offset = cache[0].offset if cache is not None else 0
        q_pe = self.rope(q_pe, offset)
        k_pe = self.rope(k_pe, offset)

        kv_latent = mx.expand_dims(kv_latent, axis=1)

        if cache is not None:
            kv_latent, k_pe = cache[0].update_and_fetch(kv_latent, k_pe)
        else:
            cache = [None] * 2

        if self.indexer is not None:
            topk_indices = self.indexer(x, qr, mask, cache=cache[1])
        else:
            topk_indices = shared_topk_indices

        if topk_indices is not None:
            if L == 1:
                idx = topk_indices[:, :, 0, :, None]
                kv_latent = mx.take_along_axis(
                    kv_latent,
                    mx.broadcast_to(idx, idx.shape[:-1] + (kv_latent.shape[-1],)),
                    axis=2,
                )
                k_pe = mx.take_along_axis(
                    k_pe,
                    mx.broadcast_to(idx, idx.shape[:-1] + (k_pe.shape[-1],)),
                    axis=2,
                )
                if mask is not None:
                    mask = mx.take_along_axis(mask, topk_indices, axis=-1)
            else:
                shape = list(topk_indices.shape)
                shape[-1] = kv_latent.shape[2]
                sparse_mask = mx.zeros(shape, dtype=mx.bool_)
                sparse_mask = mx.put_along_axis(
                    sparse_mask, topk_indices, mx.array(True), axis=-1
                )
                if mask is not None:
                    sparse_mask = sparse_mask & mask
                mask = sparse_mask

        if self.indexer is not None and cache is not None and cache[0] is not None:
            cache[0].keys = mx.depends(cache[0].keys, (cache[1].keys, cache[1].values))

        pe_scores = (q_pe * self.scale) @ k_pe.swapaxes(-1, -2)
        if mask is not None:
            pe_scores = mx.where(
                mask,
                pe_scores,
                mx.array(mx.finfo(pe_scores.dtype).min, pe_scores.dtype),
            )

        if L == 1:
            q_nope = self.embed_q(q_nope)
            k = v = kv_latent
        else:
            k = self.embed_q(kv_latent, transpose=False)
            v = self.unembed_out(kv_latent)

        output = scaled_dot_product_attention(
            q_nope, k, v, cache=cache, scale=self.scale, mask=pe_scores
        )
        if L == 1:
            output = self.unembed_out(output)

        output = output.transpose(0, 2, 1, 3).reshape(B, L, -1)
        return self.o_proj(output), topk_indices


class GlmMoeDsaDecoderLayer(nn.Module):
    def __init__(self, config: ModelArgs, layer_idx: int):
        super().__init__()
        self.self_attn = GlmMoeDsaAttention(config, layer_idx)
        self.mlp = (
            DeepseekV32MoE(config)
            if (
                config.n_routed_experts is not None
                and layer_idx >= config.first_k_dense_replace
                and layer_idx % config.moe_layer_freq == 0
            )
            else DeepseekV32MLP(config)
        )
        self.input_layernorm = nn.RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = nn.RMSNorm(
            config.hidden_size, eps=config.rms_norm_eps
        )

    def __call__(
        self,
        x: mx.array,
        mask: Optional[mx.array] = None,
        cache: Optional[Any] = None,
        shared_topk_indices: Optional[mx.array] = None,
    ) -> tuple[mx.array, Optional[mx.array]]:
        r, topk_indices = self.self_attn(
            self.input_layernorm(x), mask, cache, shared_topk_indices
        )
        h = x + r
        r = self.mlp(self.post_attention_layernorm(h))
        return h + r, topk_indices


class GlmMoeDsaModel(nn.Module):
    def __init__(self, config: ModelArgs):
        super().__init__()
        self.vocab_size = config.vocab_size
        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)
        self.layers = [
            GlmMoeDsaDecoderLayer(config, idx)
            for idx in range(config.num_hidden_layers)
        ]
        self.start_idx = 0
        self.end_idx = len(self.layers)
        self.num_layers = self.end_idx

        self.norm = nn.RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.pipeline_rank = 0
        self.pipeline_size = 1

    def pipeline(self, group):
        self.pipeline_rank = group.rank()
        self.pipeline_size = group.size()
        layers_per_rank = len(self.layers) // self.pipeline_size
        extra = len(self.layers) - layers_per_rank * self.pipeline_size
        if self.pipeline_rank < extra:
            layers_per_rank += 1
        self.start_idx = (self.pipeline_size - self.pipeline_rank - 1) * layers_per_rank
        self.end_idx = self.start_idx + layers_per_rank
        self.layers = self.layers[: self.end_idx]
        self.layers[: self.start_idx] = [None] * self.start_idx
        self.num_layers = len(self.layers) - self.start_idx

    def __call__(
        self,
        x: mx.array,
        cache: Optional[Any] = None,
    ) -> mx.array:
        h = self.embed_tokens(x)

        pipeline_rank = self.pipeline_rank
        pipeline_size = self.pipeline_size

        if cache is None:
            cache = [None] * self.num_layers
        mask = create_attention_mask(
            h, cache[0][0] if cache[0] else None, return_array=True
        )

        if pipeline_rank < pipeline_size - 1:
            h = mx.distributed.recv_like(h, (pipeline_rank + 1))

        shared_topk_indices = None
        for i in range(self.num_layers):
            layer = self.layers[self.start_idx + i]
            h, layer_topk_indices = layer(h, mask, cache[i], shared_topk_indices)
            if layer.self_attn.indexer is not None:
                shared_topk_indices = layer_topk_indices

        if pipeline_rank != 0:
            h = mx.distributed.send(h, (pipeline_rank - 1) % pipeline_size)
            if cache[-1] is not None:
                cache[-1][0].keys = mx.depends(cache[-1][0].keys, h)

        if pipeline_size > 1:
            h = mx.distributed.all_gather(h)[: h.shape[0]]

        return self.norm(h)


class Model(DSV32Model):
    def __init__(self, config: ModelArgs):
        nn.Module.__init__(self)
        self.args = config
        self.model_type = config.model_type
        self.model = GlmMoeDsaModel(config)
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

    def make_cache(self):
        return [
            CacheList(KVCache(), KVCache())
            if layer.self_attn.indexer is not None
            else CacheList(KVCache())
            for layer in self.layers
        ]
'''


class PatchError(RuntimeError):
    """Raised when the installed package is not compatible with this patch."""


def _replace_once(text: str, before: str, after: str, description: str) -> tuple[str, bool]:
    if after in text:
        return text, False
    if before not in text:
        raise PatchError(f"could not find patch point: {description}")
    return text.replace(before, after, 1), True


def _patch_text(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []

    if HELPER_AFTER not in text and HELPER_LEGACY_AFTER in text:
        text = text.replace(HELPER_LEGACY_AFTER, HELPER_AFTER, 1)
        changes.append("handler-thread GLM DSA helper upgrade")

    replacements = [
        ("handler-thread model-family helper", HELPER_BEFORE, HELPER_AFTER),
        ("handler-thread context flag", CONTEXT_BEFORE, CONTEXT_AFTER),
        ("handler-thread direct prompt-cache ownership", CACHE_BEFORE, CACHE_AFTER),
        ("handler-thread streaming dispatch", STREAM_BEFORE, STREAM_AFTER),
        ("handler-thread non-stream dispatch", NONSTREAM_BEFORE, NONSTREAM_AFTER),
    ]
    for description, before, after in replacements:
        text, changed = _replace_once(text, before, after, description)
        if changed:
            changes.append(description)

    guard_changed = False
    for before, after, description in NONTRIMMABLE_GUARDS:
        text, changed = _replace_once(text, before, after, description)
        guard_changed = guard_changed or changed
    if guard_changed:
        changes.append("handler-thread non-trimmable cache guards")

    required_markers = [
        '_requires_handler_thread_generation(self) -> bool',
        'force_handler_thread = self._requires_handler_thread_generation()',
        'elif force_handler_thread:',
        'if self._requires_handler_thread_generation():',
    ]
    missing = [marker for marker in required_markers if marker not in text]
    if missing:
        raise PatchError("patched file is missing required markers: " + ", ".join(missing))

    return text, changes


def _patch_glm4_moe_text(text: str) -> tuple[str, list[str]]:
    text, changed = _replace_once(
        text,
        GLM_TOOL_BEFORE,
        GLM_TOOL_AFTER,
        "defensive GLM/MiniMax tool parser",
    )
    return text, ["defensive GLM/MiniMax tool parser"] if changed else []


def _patch_kimi_k2_text(text: str) -> tuple[str, list[str]]:
    text, changed = _replace_once(
        text,
        KIMI_TOOL_BEFORE,
        KIMI_TOOL_AFTER,
        "defensive Kimi K2 tool parser",
    )
    return text, ["defensive Kimi K2 tool parser"] if changed else []


def _patch_glm_moe_dsa_model_text(text: str) -> tuple[str, list[str]]:
    if GLM_DSA_PATCH_MARKER in text:
        if "else CacheList(KVCache())" not in text:
            text, changed = _replace_once(
                text,
                GLM_DSA_LEGACY_CACHE,
                GLM_DSA_INDEXSHARE_CACHE,
                "GLM DSA shared-layer cache shape",
            )
            return text, ["GLM DSA shared-layer cache shape"] if changed else []
        return text, []
    if "indexer_types" in text and "shared_topk_indices" in text:
        return text, []
    if GLM_DSA_ORIGINAL_MARKER not in text:
        raise PatchError("could not find patch point: GLM DSA IndexShare model")
    compile(GLM_DSA_PATCHED_TEXT, "glm_moe_dsa.py", "exec")
    return GLM_DSA_PATCHED_TEXT, ["GLM DSA IndexShare model"]


def _patch_handler_process_text(text: str) -> tuple[str, list[str]]:
    if "def _nexus_model_ready_timeout_seconds()" in text:
        return text, []
    if HANDLER_READY_TIMEOUT_IMPORT_MARKER not in text:
        raise PatchError("could not find handler-process os import")
    if HANDLER_READY_TIMEOUT_CONSTANT_MARKER not in text:
        raise PatchError("could not find handler-process IPC constants")
    if text.count(HANDLER_READY_TIMEOUT_CALL) != 2:
        raise PatchError("expected two handler-process model-ready timeout calls")

    text = text.replace(
        HANDLER_READY_TIMEOUT_CONSTANT_MARKER,
        HANDLER_READY_TIMEOUT_CONSTANT_MARKER + HANDLER_READY_TIMEOUT_HELPER,
        1,
    )
    text = text.replace(
        HANDLER_READY_TIMEOUT_CALL,
        "response = await self._wait_for_ready(ready_queue, timeout=_nexus_model_ready_timeout_seconds())",
    )
    return text, ["configurable large-model ready timeout"]


def _find_package_path(venv: Path | None, relative_path: str, import_name: str) -> Path:
    if venv is not None:
        matches = sorted(venv.glob(f"lib/python*/site-packages/{relative_path}"))
        if matches:
            return matches[-1]
        raise PatchError(f"could not find {relative_path} under venv: {venv}")

    spec = importlib.util.find_spec(import_name)
    if spec is None or spec.origin is None:
        raise PatchError(f"could not import-locate {import_name}")
    return Path(spec.origin)


def _find_handler_path(venv: Path | None) -> Path:
    return _find_package_path(venv, "app/handler/mlx_lm.py", "app.handler.mlx_lm")


def _patch_file(
    target: Path,
    patcher: Callable[[str], tuple[str, list[str]]],
    *,
    backup_label: str,
    dry_run: bool,
) -> list[str]:
    source = target.read_text(encoding="utf-8")
    patched, changes = patcher(source)
    if not changes:
        return []

    compile(patched, str(target), "exec")
    if dry_run:
        return changes

    backup = target.with_name(f"{target.name}.bak-nexus-{backup_label}-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target, backup)
    target.write_text(patched, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"patched mlx-openai-server file: {target}")
    print(f"backup: {backup}")
    print("changes: " + ", ".join(changes))
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch mlx-openai-server so DeepSeek MLX generation stays on one thread."
    )
    parser.add_argument("--venv", type=Path, default=None, help="MLX virtualenv root")
    parser.add_argument("--target", type=Path, default=None, help="handler file to patch")
    parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    args = parser.parse_args()

    patches: list[tuple[Path, Callable[[str], tuple[str, list[str]]], str]] = [
        (args.target or _find_handler_path(args.venv), _patch_text, "deepseek-thread"),
    ]
    if args.target is None:
        patches.extend(
            [
                (
                    _find_package_path(args.venv, "app/parsers/glm4_moe.py", "app.parsers.glm4_moe"),
                    _patch_glm4_moe_text,
                    "tool-parser",
                ),
                (
                    _find_package_path(args.venv, "app/parsers/kimi_k2.py", "app.parsers.kimi_k2"),
                    _patch_kimi_k2_text,
                    "tool-parser",
                ),
                (
                    _find_package_path(
                        args.venv,
                        "mlx_lm/models/glm_moe_dsa.py",
                        "mlx_lm.models.glm_moe_dsa",
                    ),
                    _patch_glm_moe_dsa_model_text,
                    "glm-dsa-indexshare",
                ),
                (
                    _find_package_path(
                        args.venv,
                        "app/core/handler_process.py",
                        "app.core.handler_process",
                    ),
                    _patch_handler_process_text,
                    "model-ready-timeout",
                ),
            ]
        )

    all_changes: list[str] = []
    for target, patcher, backup_label in patches:
        changes = _patch_file(target, patcher, backup_label=backup_label, dry_run=args.dry_run)
        if changes:
            all_changes.extend(f"{target}: {change}" for change in changes)

    if not all_changes:
        print("mlx-openai-server patches already present")
        return 0

    if args.dry_run:
        print("mlx-openai-server patch would update:")
        for change in all_changes:
            print(f"  - {change}")
        return 0

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
