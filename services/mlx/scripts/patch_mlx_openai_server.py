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
