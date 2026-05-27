#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROFILES: dict[str, dict[str, Any]] = {
    "coding_fast": {
        "temperature": 0.15,
        "top_p": 0.90,
        "top_k": 40,
        "min_p": 0.05,
        "repetition_penalty": 1.05,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "max_tokens": 4096,
        "attempts": 1,
    },
    "coding_repo": {
        "temperature": 0.20,
        "top_p": 0.85,
        "top_k": 40,
        "min_p": 0.03,
        "repetition_penalty": 1.03,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "max_tokens": 8192,
        "attempts": 2,
    },
    "coding_reasoning": {
        "temperature": 0.30,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.02,
        "repetition_penalty": 1.02,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "max_tokens": 16384,
        "attempts": 2,
    },
    "greedy_probe": {
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 0,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "max_tokens": 4096,
        "attempts": 1,
    },
}

PATCH_SYSTEM_PROMPT = (
    "You are evaluating a local coding model. Inspect the provided repository files, "
    "make the smallest correct change, and return only a unified diff that can be "
    "applied with git apply. Do not wrap the diff in Markdown. Do not edit unrelated "
    "files. Do not invent paths, imports, functions, methods, variables, or config keys."
)

ANSWER_SYSTEM_PROMPT = (
    "You are evaluating a local coding model. Answer the repository question using only "
    "the provided files. Be concise and mention uncertainty when the evidence is missing."
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(item, separators=(",", ":"), sort_keys=True))
        handle.write("\n")


def _run(argv: list[str], *, cwd: Path, timeout_sec: float = 60.0, input_text: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "argv": argv,
            "stdout": proc.stdout[-8000:],
            "stderr": proc.stderr[-8000:],
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "argv": argv,
            "stdout": str(exc.stdout or "")[-8000:],
            "stderr": f"timeout after {timeout_sec}s\n{str(exc.stderr or '')[-8000:]}",
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
        }


def _safe_write(root: Path, rel: str, content: str) -> None:
    path = (root / rel).resolve()
    if root.resolve() not in path.parents and path != root.resolve():
        raise ValueError(f"path escapes workspace: {rel}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _setup_repo(task: dict[str, Any], root: Path) -> None:
    files = task.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError(f"task {task.get('id')} has no files")
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        _safe_write(root, str(rel), str(content))
    _run(["git", "init"], cwd=root)
    _run(["git", "config", "user.email", "coding-eval@example.invalid"], cwd=root)
    _run(["git", "config", "user.name", "Coding Eval"], cwd=root)
    _run(["git", "add", "."], cwd=root)
    _run(["git", "commit", "-m", "fixture"], cwd=root)


def _list_files(root: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        out.append(path.relative_to(root).as_posix())
    return out


def _build_context(task: dict[str, Any], root: Path, method: str, max_chars: int) -> tuple[str, list[str], list[str]]:
    all_files = _list_files(root)
    if method == "selected":
        requested = [str(item) for item in task.get("context_files") or []]
        files = [item for item in requested if item in all_files]
    elif method == "manifest":
        files = []
    else:
        files = all_files

    truncations: list[str] = []
    chunks = ["Repository file manifest:", *[f"- {item}" for item in all_files]]
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        header = f"\n--- {rel} ---\n"
        remaining = max_chars - sum(len(item) for item in chunks) - len(header)
        if remaining <= 0:
            truncations.append(rel)
            continue
        if len(text) > remaining:
            text = text[: max(0, remaining)] + "\n[TRUNCATED]\n"
            truncations.append(rel)
        chunks.append(header + text)
    return "\n".join(chunks), files, truncations


def _task_prompt(task: dict[str, Any], context: str) -> str:
    mode = str(task.get("mode") or "patch")
    if mode == "answer":
        return (
            f"Task class: {task.get('class')}\n"
            f"Question:\n{task.get('prompt')}\n\n"
            f"{context}\n\n"
            "Answer directly. Do not propose edits unless asked."
        )
    return (
        f"Task class: {task.get('class')}\n"
        f"Goal:\n{task.get('prompt')}\n\n"
        f"{context}\n\n"
        "Return only a unified diff. The diff must apply cleanly with git apply. "
        "Keep the patch minimal and preserve existing style."
    )


def _auth_headers(token: str) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def _chat_once(
    *,
    base_url: str,
    token: str,
    model: str,
    messages: list[dict[str, str]],
    profile: dict[str, Any],
    seed: int | None,
    stream: bool,
    timeout_sec: float,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": profile.get("temperature"),
        "top_p": profile.get("top_p"),
        "max_tokens": profile.get("max_tokens"),
        "stream": stream,
    }
    for key in ("top_k", "min_p", "repetition_penalty", "frequency_penalty", "presence_penalty", "stop"):
        if key in profile and profile.get(key) is not None:
            body[key] = profile[key]
    if seed is not None:
        body["seed"] = seed

    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url.rstrip("/") + "/chat/completions"
    data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    request = Request(url=url, data=data, headers=_auth_headers(token), method="POST")
    started = time.monotonic()
    text = ""
    finish_reason = ""
    usage: dict[str, Any] = {}
    ttft_ms: float | None = None

    try:
        with urlopen(request, timeout=timeout_sec) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            if stream:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or choice.get("text") or ""
                    if piece and ttft_ms is None:
                        ttft_ms = round((time.monotonic() - started) * 1000.0, 1)
                    text += str(piece)
                    if choice.get("finish_reason"):
                        finish_reason = str(choice.get("finish_reason"))
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
            else:
                payload = json.loads(resp.read().decode("utf-8"))
                choice = (payload.get("choices") or [{}])[0]
                msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                text = str(msg.get("content") or choice.get("text") or "")
                finish_reason = str(choice.get("finish_reason") or "")
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        wall_ms = round((time.monotonic() - started) * 1000.0, 1)
        completion_tokens = usage.get("completion_tokens")
        if not isinstance(completion_tokens, int) or completion_tokens <= 0:
            completion_tokens = max(1, int(len(text.split()) * 1.3))
        tokens_sec = round(completion_tokens / max(wall_ms / 1000.0, 0.001), 2)
        return {
            "ok": status == 200,
            "status": status,
            "content": text,
            "finish_reason": finish_reason,
            "usage": usage,
            "ttft_ms": ttft_ms,
            "wall_ms": wall_ms,
            "tokens_sec": tokens_sec,
        }
    except HTTPError as exc:
        body_text = exc.read(4000).decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "content": "", "error": body_text, "wall_ms": round((time.monotonic() - started) * 1000.0, 1)}
    except (URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": 0, "content": "", "error": f"{type(exc).__name__}: {exc}", "wall_ms": round((time.monotonic() - started) * 1000.0, 1)}


_FENCE_RE = re.compile(r"```(?:diff|patch)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def _extract_patch(text: str) -> tuple[str, bool]:
    raw = str(text or "").strip()
    markdown = False
    fenced = _FENCE_RE.findall(raw)
    if fenced:
        markdown = True
        for block in fenced:
            if "diff --git " in block or "\n--- " in f"\n{block}":
                raw = block.strip()
                break
    starts = [idx for idx in (raw.find("diff --git "), raw.find("--- ")) if idx >= 0]
    if starts:
        raw = raw[min(starts) :].strip()
    return raw, markdown


def _patch_paths(patch: str) -> set[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(("--- ", "+++ ")):
            value = line[4:].strip().split("\t", 1)[0]
            if value == "/dev/null":
                continue
            value = re.sub(r"^[ab]/", "", value)
            paths.add(value)
        elif line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                paths.add(re.sub(r"^[ab]/", "", parts[3]))
    return {item for item in paths if item and item != "/dev/null"}


def _git_changed_files(root: Path) -> list[str]:
    result = _run(["git", "status", "--porcelain"], cwd=root)
    files: list[str] = []
    for line in str(result.get("stdout") or "").splitlines():
        if len(line) >= 4:
            files.append(line[3:].strip())
    return files


def _evaluate_answer(task: dict[str, Any], content: str) -> tuple[bool, list[str]]:
    notes: list[str] = []
    ok = True
    for pattern in task.get("required_answer_patterns") or []:
        if not re.search(str(pattern), content, re.IGNORECASE | re.MULTILINE):
            ok = False
            notes.append(f"missing required answer pattern: {pattern}")
    for pattern in task.get("forbidden_answer_patterns") or []:
        if re.search(str(pattern), content, re.IGNORECASE | re.MULTILINE):
            ok = False
            notes.append(f"matched forbidden answer pattern: {pattern}")
    return ok, notes


def _run_task(args: argparse.Namespace, suite: dict[str, Any], task: dict[str, Any], profile: dict[str, Any], attempt: int) -> dict[str, Any]:
    task_id = str(task.get("id") or f"task-{uuid.uuid4().hex[:8]}")
    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    work_root = Path(args.workdir).resolve() / run_id / task_id
    _setup_repo(task, work_root)

    context, files_read, truncations = _build_context(task, work_root, str(args.context_method), int(args.max_context_chars))
    prompt = _task_prompt(task, context)
    mode = str(task.get("mode") or "patch")
    system_prompt = ANSWER_SYSTEM_PROMPT if mode == "answer" else PATCH_SYSTEM_PROMPT
    seed = None if args.seed is None else int(args.seed) + attempt

    chat = _chat_once(
        base_url=args.base_url,
        token=args.token,
        model=args.model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        profile=profile,
        seed=seed,
        stream=bool(args.stream),
        timeout_sec=float(args.timeout_sec),
    )

    notes: list[str] = []
    pass_fail = False
    patch_size = 0
    hallucinated_paths: list[str] = []
    unnecessary_edits: list[str] = []
    files_modified: list[str] = []
    test_result: dict[str, Any] | None = None
    lint_result: dict[str, Any] | None = None
    markdown_around_diff = False
    apply_check: dict[str, Any] | None = None
    apply_result: dict[str, Any] | None = None

    if not chat.get("ok"):
        notes.append(str(chat.get("error") or f"chat status {chat.get('status')}"))
    elif mode == "answer":
        pass_fail, answer_notes = _evaluate_answer(task, str(chat.get("content") or ""))
        notes.extend(answer_notes)
    else:
        patch, markdown_around_diff = _extract_patch(str(chat.get("content") or ""))
        patch_size = len([line for line in patch.splitlines() if line.strip()])
        paths = _patch_paths(patch)
        allowed_new = {str(item) for item in task.get("allowed_new_files") or []}
        existing = set(_list_files(work_root))
        hallucinated_paths = sorted(path for path in paths if path not in existing and path not in allowed_new)
        if not patch or not paths:
            notes.append("no unified diff detected")
        else:
            apply_check = _run(["git", "apply", "--check", "--whitespace=nowarn", "-"], cwd=work_root, input_text=patch)
            if apply_check.get("ok"):
                apply_result = _run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work_root, input_text=patch)
            else:
                notes.append("git apply --check failed")
            if apply_result and apply_result.get("ok"):
                files_modified = _git_changed_files(work_root)
                expected = {str(item) for item in task.get("expected_modified") or []}
                if expected:
                    unnecessary_edits = sorted(path for path in files_modified if path not in expected and path not in allowed_new)
                test_cmd = task.get("test_command")
                if isinstance(test_cmd, list) and test_cmd:
                    test_result = _run([str(item) for item in test_cmd], cwd=work_root, timeout_sec=float(task.get("test_timeout_sec") or args.command_timeout_sec))
                lint_cmd = task.get("lint_command")
                if isinstance(lint_cmd, list) and lint_cmd:
                    lint_result = _run([str(item) for item in lint_cmd], cwd=work_root, timeout_sec=float(task.get("lint_timeout_sec") or args.command_timeout_sec))
                tests_ok = test_result is None or bool(test_result.get("ok"))
                lint_ok = lint_result is None or bool(lint_result.get("ok"))
                pass_fail = bool(tests_ok and lint_ok and not hallucinated_paths and not unnecessary_edits)
            elif apply_result:
                notes.append("git apply failed")

    if markdown_around_diff:
        notes.append("model wrapped diff in Markdown")
    if str(chat.get("finish_reason") or "") == "length":
        truncations.append("completion_length")

    output = {
        "timestamp": _now_iso(),
        "suite": suite.get("name") or "",
        "task_id": task_id,
        "task_class": task.get("class") or "",
        "model": args.model,
        "quantization": args.quantization,
        "runtime": args.runtime,
        "host": args.host,
        "context_length": args.context_length,
        "temperature": profile.get("temperature"),
        "top_p": profile.get("top_p"),
        "top_k": profile.get("top_k"),
        "min_p": profile.get("min_p"),
        "repetition_penalty": profile.get("repetition_penalty"),
        "frequency_penalty": profile.get("frequency_penalty"),
        "presence_penalty": profile.get("presence_penalty"),
        "max_tokens": profile.get("max_tokens"),
        "seed": seed,
        "attempt": attempt,
        "prompt_template": "direct_patch_v1" if mode != "answer" else "direct_answer_v1",
        "repo_context_method": args.context_method,
        "tool_loop_count": 0,
        "files_read": files_read,
        "files_modified": files_modified,
        "test_command": task.get("test_command") or [],
        "test_result": test_result,
        "lint_command": task.get("lint_command") or [],
        "lint_result": lint_result,
        "pass": pass_fail,
        "patch_size": patch_size,
        "unnecessary_edits": unnecessary_edits,
        "hallucinated_paths": hallucinated_paths,
        "markdown_around_diff": markdown_around_diff,
        "truncation_events": truncations,
        "tokens_sec": chat.get("tokens_sec"),
        "time_to_first_token_ms": chat.get("ttft_ms"),
        "wall_clock_ms": chat.get("wall_ms"),
        "usage": chat.get("usage") or {},
        "finish_reason": chat.get("finish_reason") or "",
        "chat_status": chat.get("status"),
        "notes": notes,
    }
    if args.keep_responses:
        output["response"] = str(chat.get("content") or "")[:20000]
    return output


def _selected_tasks(suite: dict[str, Any], ids: set[str]) -> list[dict[str, Any]]:
    tasks = suite.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("suite must contain a tasks array")
    out = [item for item in tasks if isinstance(item, dict)]
    if ids:
        out = [item for item in out if str(item.get("id") or "") in ids]
    return out


def parse_args() -> argparse.Namespace:
    runtime_root = os.getenv("NEXUS_RUNTIME_ROOT", ".runtime")
    parser = argparse.ArgumentParser(description="Evaluate local coding models on patch and repo-question fixtures.")
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8800/v1"))
    parser.add_argument("--token", default=os.getenv("GATEWAY_BEARER_TOKEN") or os.getenv("OPENAI_API_KEY") or "")
    parser.add_argument("--model", required=False, default="coder")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="coding_repo")
    parser.add_argument("--suite", default=str(Path(__file__).with_name("coding_model_eval_suite.example.json")))
    parser.add_argument("--out", default=os.path.join(runtime_root, "coding-model-evals", "results.jsonl"))
    parser.add_argument("--workdir", default=os.path.join(runtime_root, "coding-model-evals", "work"))
    parser.add_argument("--task", action="append", default=[], help="Task id to run. May be repeated.")
    parser.add_argument("--attempts", type=int, default=None, help="Override profile attempts.")
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--stream", action="store_true", help="Use streaming to measure time-to-first-token.")
    parser.add_argument("--keep-responses", action="store_true")
    parser.add_argument("--context-method", choices=["all", "selected", "manifest"], default="all")
    parser.add_argument("--max-context-chars", type=int, default=120000)
    parser.add_argument("--context-length", type=int, default=0, help="Metadata only; records the served context length.")
    parser.add_argument("--runtime", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--quantization", default="")
    parser.add_argument("--timeout-sec", type=float, default=600.0)
    parser.add_argument("--command-timeout-sec", type=float, default=60.0)
    parser.add_argument("--list-profiles", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_profiles:
        print(json.dumps(PROFILES, indent=2, sort_keys=True))
        return 0

    suite_path = Path(args.suite).resolve()
    suite = _read_json(suite_path)
    profile = dict(PROFILES[args.profile])
    attempts = int(args.attempts if args.attempts is not None else profile.get("attempts") or 1)
    task_ids = {str(item) for item in args.task if str(item).strip()}
    tasks = _selected_tasks(suite, task_ids)
    if not tasks:
        raise SystemExit("no tasks selected")

    out_path = Path(args.out).resolve()
    if shutil.which("git") is None:
        raise SystemExit("git is required for patch application")

    total = 0
    passed = 0
    for task in tasks:
        for attempt in range(1, attempts + 1):
            result = _run_task(args, suite, task, profile, attempt)
            _write_jsonl(out_path, result)
            total += 1
            passed += 1 if result.get("pass") else 0
            status = "PASS" if result.get("pass") else "FAIL"
            print(f"{status} {result['task_id']} attempt={attempt} wall_ms={result.get('wall_clock_ms')} notes={'; '.join(result.get('notes') or [])}")
    print(f"wrote {total} results to {out_path} ({passed}/{total} passed)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
