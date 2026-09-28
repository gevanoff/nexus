from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from fastapi import HTTPException

from app import coding_agent as ca
from app import coding_network_resilience
from app import coding_workspace as cw
from app import model_integration_workspace as miw


def test_create_model_integration_task_requires_github_repo_url(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(
        cw.miw,
        "build_integration_plan",
        lambda **kwargs: {
            "source_url": "https://huggingface.co/example/model",
            "prompt": "Integrate model",
        },
    )

    try:
        cw.create_model_integration_task(
            model="example/model",
            repo_url="https://gitlab.com/example/model-integration.git",
            preferred_runtime="auto",
            route_kind="chat",
            service_name="example-service",
            base_branch="main",
            branch_name="feature/test",
            prompt="Integrate model",
            owner="test",
        )
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "GitHub" in str(exc.detail)


def test_create_model_integration_task_defaults_destination_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(cw, "default_repo_url", lambda: "https://github.com/gevanoff/nexus.git")
    monkeypatch.setattr(
        cw.miw,
        "build_integration_plan",
        lambda **kwargs: {
            "source_url": "https://huggingface.co/nvidia/NVIDIA-Nemotron-Nano-9B-v2",
            "prompt": "Add backend integration",
            "service_name": "nemotron",
        },
    )

    def _scaffold(repo_path, plan, **_kwargs):
        readme = repo_path / "README.md"
        readme.write_text("seed", encoding="utf-8")
        return ["README.md"]

    monkeypatch.setattr(cw.miw, "scaffold_workspace", _scaffold)
    monkeypatch.setattr(
        cw,
        "_ensure_github_repo_available",
        lambda repo_url, *, git_token_value=None: {"ok": True, "created": True, "empty": True, "body": {"html_url": repo_url}},
    )
    monkeypatch.setattr(
        cw,
        "_run_process",
        lambda argv, **kwargs: {"ok": True, "returncode": 0, "argv": list(argv), "stdout": "", "stderr": "", "duration_ms": 1},
    )

    task = cw.create_model_integration_task(
        model="nvidia/NVIDIA-Nemotron-Nano-9B-v2",
        repo_url=None,
        preferred_runtime="vllm",
        route_kind="chat",
        service_name="nemotron",
        base_branch="main",
        branch_name="nexus-coder/nemotron",
        prompt="Add backend integration",
        owner="test",
    )

    assert task["status"] == "ready"
    assert task["repo_url"] == "https://github.com/gevanoff/nexus.git"


def test_create_model_integration_task_surfaces_plan_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")

    def _build_integration_plan(**kwargs):
        raise ValueError("model id could not be resolved")

    monkeypatch.setattr(cw.miw, "build_integration_plan", _build_integration_plan)

    try:
        cw.create_model_integration_task(
            model="nvidia/NVIDIA-Nemotron-Nano-9B-v2",
            repo_url="https://github.com/example/nemotron-integration.git",
            preferred_runtime="auto",
            route_kind="chat",
            service_name="nemotron",
            base_branch="main",
            branch_name="feature/test",
            prompt="Integrate model",
            owner="test",
        )
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "model id could not be resolved" in str(exc.detail)


def test_create_model_integration_task_attaches_remote_and_pushes_seed(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(
        cw.miw,
        "build_integration_plan",
        lambda **kwargs: {
            "source_url": "https://huggingface.co/nvidia/NVIDIA-Nemotron-Nano-9B-v2",
            "prompt": "Add backend integration",
            "service_name": "nemotron",
        },
    )

    def _scaffold(repo_path, plan, **_kwargs):
        readme = repo_path / "README.md"
        readme.write_text("seed", encoding="utf-8")
        return ["README.md"]

    monkeypatch.setattr(cw.miw, "scaffold_workspace", _scaffold)
    monkeypatch.setattr(
        cw,
        "_ensure_github_repo_available",
        lambda repo_url, *, git_token_value=None: {"ok": True, "created": True, "empty": True, "body": {"html_url": repo_url}},
    )

    calls: list[list[str]] = []

    def _run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[0:2] == ["git", "clone"]:
            Path(argv[-1]).joinpath(".git").mkdir()
        return {
            "ok": True,
            "returncode": 0,
            "argv": list(argv),
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", _run_process)

    task = cw.create_model_integration_task(
        model="nvidia/NVIDIA-Nemotron-Nano-9B-v2",
        repo_url="https://github.com/example/nemotron-integration.git",
        preferred_runtime="vllm",
        route_kind="chat",
        service_name="nemotron",
        base_branch="main",
        branch_name="nexus-coder/nemotron",
        prompt="Add backend integration",
        owner="test",
        git_token_value="ghp_test",
    )

    assert task["status"] == "ready"
    assert task["repo_url"] == "https://github.com/example/nemotron-integration.git"

    labels = [item.get("label") for item in task.get("commands", [])]
    assert "github-repo-ensure" in labels
    assert "git-remote-add" in labels
    assert "git-push-base" in labels
    assert "git-push-branch" in labels

    assert ["git", "remote", "add", "origin", "https://github.com/example/nemotron-integration.git"] in calls
    assert ["git", "push", "-u", "origin", "main"] in calls
    assert ["git", "push", "-u", "origin", "nexus-coder/nemotron"] in calls


def test_create_model_integration_task_clones_existing_repo_before_scaffolding(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(
        cw.miw,
        "build_integration_plan",
        lambda **kwargs: {
            "source_url": "https://huggingface.co/mlx-community/Qwen3.6-27B-4bit",
            "prompt": "Add backend integration",
            "service_name": "hf-qwen",
        },
    )

    scaffold_paths: list[str] = []

    def _scaffold(repo_path, plan, **_kwargs):
        scaffold_paths.append(str(repo_path))
        readme = repo_path / "README.md"
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_text("seed", encoding="utf-8")
        return ["README.md"]

    monkeypatch.setattr(cw.miw, "scaffold_workspace", _scaffold)
    monkeypatch.setattr(
        cw,
        "_ensure_github_repo_available",
        lambda repo_url, *, git_token_value=None: {"ok": True, "created": False, "empty": False, "body": {"html_url": repo_url}},
    )

    calls: list[list[str]] = []

    def _run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[0:2] == ["git", "clone"]:
            Path(argv[-1]).joinpath(".git").mkdir()
        return {
            "ok": True,
            "returncode": 0,
            "argv": list(argv),
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", _run_process)

    task = cw.create_model_integration_task(
        model="mlx-community/Qwen3.6-27B-4bit",
        repo_url="https://github.com/example/existing-repo.git",
        preferred_runtime="mlx",
        route_kind="json",
        service_name="hf-qwen",
        base_branch="main",
        branch_name="nexus-coder/qwen",
        prompt="Add backend integration",
        owner="test",
        git_token_value="ghp_test",
    )

    assert task["status"] == "ready"
    assert len(scaffold_paths) == 1
    assert scaffold_paths[0].startswith("/proc/self/fd/")

    labels = [item.get("label") for item in task.get("commands", [])]
    assert "github-repo-ensure" in labels
    assert "git-clone-base" in labels
    assert "git-branch-work" in labels
    assert "git-add" in labels
    assert "git-commit" in labels
    assert "git-push-branch" in labels
    assert "git-push-base" not in labels
    assert "git-remote-add" not in labels

    clone_calls = [argv for argv in calls if argv[0:2] == ["git", "clone"]]
    assert len(clone_calls) == 1
    assert clone_calls[0][:-1] == [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        "main",
        "https://github.com/example/existing-repo.git",
    ]
    assert clone_calls[0][-1].startswith("/proc/self/fd/")
    assert (tmp_path / "workspaces" / task["id"] / "repo" / ".git").is_dir()
    assert ["git", "push", "-u", "origin", "nexus-coder/qwen"] in calls


def test_model_integration_clone_remains_untrusted_until_provisioning_is_ready(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(
        cw.miw,
        "build_integration_plan",
        lambda **kwargs: {
            "source_url": "https://huggingface.co/example/model",
            "prompt": "Add backend integration",
        },
    )
    monkeypatch.setattr(
        cw,
        "_ensure_github_repo_available",
        lambda repo_url, *, git_token_value=None: {
            "ok": True,
            "created": False,
            "empty": False,
            "body": {},
        },
    )

    untrusted_saved = threading.Event()
    provisioning_started = threading.Event()
    allow_provisioning = threading.Event()
    task_ids: list[str] = []
    original_save_task = cw.save_task

    def save_task(task):
        result = original_save_task(task)
        if task.get("repo_path_untrusted") is True:
            task_ids[:] = [str(task["id"])]
            untrusted_saved.set()
        return result

    monkeypatch.setattr(cw, "save_task", save_task)

    def scaffold(repo_path, plan, **_kwargs):
        provisioning_started.set()
        assert allow_provisioning.wait(timeout=5)
        generated = repo_path / "generated.txt"
        generated.write_text("seed", encoding="utf-8")
        return [str(generated)]

    monkeypatch.setattr(cw.miw, "scaffold_workspace", scaffold)

    def run_process(argv, **_kwargs):
        if argv[0:2] == ["git", "clone"]:
            Path(argv[-1]).joinpath(".git").mkdir()
        return {
            "ok": True,
            "returncode": 0,
            "argv": list(argv),
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", run_process)
    result: dict[str, object] = {}

    def create_task():
        result["task"] = cw.create_model_integration_task(
            model="example/model",
            repo_url="https://github.com/example/model-integration.git",
            preferred_runtime="vllm",
            route_kind="chat",
            service_name="example-model",
            base_branch="main",
            branch_name="feature/model",
            prompt="Add backend integration",
            owner="test",
        )

    creator = threading.Thread(target=create_task)
    creator.start()
    assert untrusted_saved.wait(timeout=5)
    assert provisioning_started.wait(timeout=5)
    task_id = task_ids[0]
    persisted = cw.load_task(task_id)
    assert persisted["status"] == "initializing"
    assert persisted["repo_path_untrusted"] is True

    started: list[str] = []

    async def fake_start(task_id, **_kwargs):
        started.append(task_id)
        return cw.public_task(cw.load_task(task_id))

    monkeypatch.setattr(ca, "_start_agent_run_impl", fake_start)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(ca.start_agent_run(task_id))
    assert excinfo.value.status_code == 409
    assert "initializing" in str(excinfo.value.detail)
    assert started == []

    allow_provisioning.set()
    creator.join(timeout=5)
    assert not creator.is_alive()
    task = result["task"]
    assert isinstance(task, dict)
    assert task["status"] == "ready"
    assert "repo_path_untrusted" not in task
    assert asyncio.run(ca.start_agent_run(task_id))["status"] == "ready"
    assert started == [task_id]


def test_model_integration_clone_retries_in_fresh_preserved_stage(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(coding_network_resilience, "retry_base_delay_sec", lambda: 0)
    monkeypatch.setattr(
        cw.miw,
        "build_integration_plan",
        lambda **kwargs: {
            "source_url": "https://huggingface.co/example/model",
            "prompt": "Add backend integration",
            "service_name": "example-model",
        },
    )
    ensure_calls = []

    def ensure_remote(repo_url, *, git_token_value=None):
        ensure_calls.append((repo_url, git_token_value))
        return {
            "ok": True,
            "created": False,
            "empty": False,
            "body": {"html_url": repo_url},
        }

    monkeypatch.setattr(cw, "_ensure_github_repo_available", ensure_remote)
    scaffold_calls = []

    def scaffold(repo_path, plan, **_kwargs):
        scaffold_calls.append(str(repo_path))
        repo_path.joinpath("generated.txt").write_text("seed", encoding="utf-8")
        return ["generated.txt"]

    monkeypatch.setattr(cw.miw, "scaffold_workspace", scaffold)
    calls = []
    clone_targets = []
    clone_inodes = []

    def run_process(argv, **_kwargs):
        calls.append(list(argv))
        if argv[0:2] == ["git", "clone"]:
            target = Path(argv[-1])
            clone_targets.append(str(target.resolve()))
            clone_inodes.append(os.fstat(_kwargs["pass_fds"][-1]).st_ino)
            if len(clone_targets) == 1:
                target.joinpath("partial.txt").write_text(
                    "preserve",
                    encoding="utf-8",
                )
                return {
                    "ok": False,
                    "returncode": 128,
                    "argv": list(argv),
                    "stdout": "",
                    "stderr": (
                        "fatal: synthetic-token: Could not resolve host: github.com"
                    ),
                    "duration_ms": 1,
                }
            target.joinpath(".git").mkdir()
        return {
            "ok": True,
            "returncode": 0,
            "argv": list(argv),
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", run_process)

    task = cw.create_model_integration_task(
        model="example/model",
        repo_url="https://github.com/example/model-integration.git",
        preferred_runtime="vllm",
        route_kind="chat",
        service_name="example-model",
        base_branch="main",
        branch_name="feature/model",
        prompt="Add backend integration",
        owner="test",
        git_token_value="synthetic-token",
    )

    assert task["status"] == "ready"
    assert len(ensure_calls) == 1
    assert len(clone_targets) == 2
    assert clone_targets[0] == clone_targets[1]
    assert clone_inodes[0] != clone_inodes[1]
    assert len(scaffold_calls) == 1
    assert sum(argv[0:2] == ["git", "switch"] for argv in calls) == 1
    assert sum(argv[0:2] == ["git", "add"] for argv in calls) == 1
    assert sum(argv[0:2] == ["git", "commit"] for argv in calls) == 1
    assert sum(argv[0:2] == ["git", "push"] for argv in calls) == 1

    workspace = tmp_path / "workspaces" / task["id"]
    preserved = list(workspace.glob("repo.clone-partial-*"))
    assert len(preserved) == 1
    assert preserved[0].joinpath("repo", "partial.txt").read_text(
        encoding="utf-8"
    ) == "preserve"
    assert workspace.joinpath("repo", ".git").is_dir()
    clone_record = next(
        item for item in task["commands"] if item.get("label") == "git-clone-base"
    )
    assert clone_record["network_retry_attempts"] == 2
    assert clone_record["network_retry_count"] == 1
    assert clone_record["network_retry_recovered"] is True
    assert clone_record["network_retry_history"][0]["kind"] == "dns"
    assert "synthetic-token" not in json.dumps(task["commands"])


def test_model_integration_clone_does_not_retry_non_transient_failure(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(
        cw.miw,
        "build_integration_plan",
        lambda **kwargs: {
            "source_url": "https://huggingface.co/example/model",
            "prompt": "Add backend integration",
        },
    )
    ensure_calls = []
    monkeypatch.setattr(
        cw,
        "_ensure_github_repo_available",
        lambda repo_url, *, git_token_value=None: (
            ensure_calls.append(repo_url)
            or {"ok": True, "created": False, "empty": False, "body": {}}
        ),
    )
    scaffold_calls = []
    monkeypatch.setattr(
        cw.miw,
        "scaffold_workspace",
        lambda repo_path, plan, **_kwargs: scaffold_calls.append(str(repo_path)) or [],
    )
    calls = []

    def run_process(argv, **_kwargs):
        calls.append(list(argv))
        assert argv[0:2] == ["git", "clone"]
        return {
            "ok": False,
            "returncode": 128,
            "argv": list(argv),
            "stdout": "",
            "stderr": "fatal: Authentication failed",
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", run_process)

    task = cw.create_model_integration_task(
        model="example/model",
        repo_url="https://github.com/example/model-integration.git",
        preferred_runtime="vllm",
        route_kind="chat",
        service_name="example-model",
        base_branch="main",
        branch_name="feature/model",
        prompt="Add backend integration",
        owner="test",
    )

    assert task["status"] == "error"
    assert task["error"] == "git clone failed"
    assert len(ensure_calls) == 1
    assert len(calls) == 1
    assert scaffold_calls == []
    workspace = tmp_path / "workspaces" / task["id"]
    assert not workspace.joinpath("repo").exists()
    preserved = list(workspace.glob("repo.clone-partial-*"))
    assert len(preserved) == 1
    assert preserved[0].joinpath("repo").is_dir()


def test_model_integration_provisioning_stays_anchored_after_repo_path_swap(
    monkeypatch,
    tmp_path,
):
    workspace_root = tmp_path / "workspaces"
    monkeypatch.setattr(cw, "workspace_root", lambda: workspace_root)
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(
        cw.miw,
        "build_integration_plan",
        lambda **kwargs: {
            "source_url": "https://huggingface.co/example/model",
            "prompt": "Add backend integration",
        },
    )
    monkeypatch.setattr(
        cw,
        "_ensure_github_repo_available",
        lambda repo_url, *, git_token_value=None: {
            "ok": True,
            "created": False,
            "empty": False,
            "body": {},
        },
    )
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    preserved = tmp_path / "preserved-repo"
    lexical_repo = None

    def scaffold(repo_root, plan, **_kwargs):
        repo_root.joinpath("generated.txt").write_text("seed", encoding="utf-8")
        return [str(repo_root / "generated.txt")]

    monkeypatch.setattr(cw.miw, "scaffold_workspace", scaffold)

    def run_process(argv, *, cwd, **_kwargs):
        nonlocal lexical_repo
        if argv[0:2] == ["git", "clone"]:
            anchored_repo = Path(argv[-1])
            anchored_repo.joinpath(".git").mkdir()
            lexical_repo = anchored_repo.resolve()
        elif argv[0:2] == ["git", "switch"]:
            assert lexical_repo is not None
            lexical_repo.rename(preserved)
            lexical_repo.symlink_to(external, target_is_directory=True)
        return {
            "ok": True,
            "returncode": 0,
            "argv": list(argv),
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
        }

    monkeypatch.setattr(cw, "_run_process", run_process)

    task = cw.create_model_integration_task(
        model="example/model",
        repo_url="https://github.com/example/model-integration.git",
        preferred_runtime="vllm",
        route_kind="chat",
        service_name="example-model",
        base_branch="main",
        branch_name="feature/model",
        prompt="Add backend integration",
        owner="test",
    )

    assert task["status"] == "error"
    assert "identity changed" in task["error"]
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not external.joinpath("generated.txt").exists()
    assert preserved.joinpath("generated.txt").read_text(encoding="utf-8") == "seed"
    persisted = cw.load_task(task["id"])
    with pytest.raises(HTTPException) as excinfo:
        cw._repo_path(persisted)
    assert excinfo.value.status_code == 409


def test_model_integration_clone_validation_failure_remains_auditable(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(cw, "workspace_root", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(cw, "tasks_dir", lambda: tmp_path / "tasks")
    monkeypatch.setattr(
        cw.miw,
        "build_integration_plan",
        lambda **kwargs: {
            "source_url": "https://huggingface.co/example/model",
            "prompt": "Add backend integration",
        },
    )
    monkeypatch.setattr(
        cw,
        "_ensure_github_repo_available",
        lambda repo_url, *, git_token_value=None: {
            "ok": True,
            "created": False,
            "empty": False,
            "body": {},
        },
    )
    scaffold_calls = []
    monkeypatch.setattr(
        cw.miw,
        "scaffold_workspace",
        lambda *args, **kwargs: scaffold_calls.append((args, kwargs)) or [],
    )
    monkeypatch.setattr(
        cw,
        "_run_process",
        lambda argv, **kwargs: {
            "ok": True,
            "returncode": 0,
            "argv": list(argv),
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
        },
    )

    task = cw.create_model_integration_task(
        model="example/model",
        repo_url="https://github.com/example/model-integration.git",
        preferred_runtime="vllm",
        route_kind="chat",
        service_name="example-model",
        base_branch="main",
        branch_name="feature/model",
        prompt="Add backend integration",
        owner="test",
    )

    assert task["status"] == "error"
    clone_record = next(
        item for item in task["commands"] if item.get("label") == "git-clone-base"
    )
    assert clone_record["ok"] is True
    assert clone_record["network_retry_attempts"] == 1
    assert scaffold_calls == []
    persisted = cw.load_task(task["id"])
    assert persisted["repo_path_untrusted"] is True
    with pytest.raises(HTTPException):
        cw._repo_path(persisted)


def test_vllm_chat_model_integration_targets_existing_lane(monkeypatch):
    monkeypatch.setattr(
        miw,
        "fetch_model_metadata",
        lambda model_id: {
            "id": model_id,
            "library_name": "transformers",
            "pipeline_tag": "text-generation",
            "tags": ["text-generation"],
            "config": {
                "architectures": ["NemotronForCausalLM"],
                "model_type": "nemotron",
            },
        },
    )

    plan = miw.build_integration_plan(
        model="nvidia/NVIDIA-Nemotron-Nano-9B-v2",
        preferred_runtime="auto",
        route_kind="chat",
        service_name=None,
        prompt=None,
    )

    assert plan["runtime"] == "vllm"
    assert plan["integration_strategy"] == "existing_vllm_model"
    assert plan["backend_class"] == "local_vllm_fast"
    assert plan["target_backend_class"] == "local_vllm_fast"
    assert plan["containerize"] is False
    assert "Do not create a new backend class" in plan["prompt"]


def test_vllm_lane_scaffold_preserves_existing_readme_and_avoids_service(monkeypatch, tmp_path):
    monkeypatch.setattr(
        miw,
        "fetch_model_metadata",
        lambda model_id: {
            "id": model_id,
            "library_name": "transformers",
            "pipeline_tag": "text-generation",
            "tags": ["text-generation"],
            "config": {
                "architectures": ["NemotronForCausalLM"],
                "model_type": "nemotron",
            },
        },
    )
    plan = miw.build_integration_plan(
        model="nvidia/NVIDIA-Nemotron-Nano-9B-v2",
        preferred_runtime="auto",
        route_kind="chat",
        service_name=None,
        prompt=None,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Existing Nexus README\n", encoding="utf-8")

    created = miw.scaffold_workspace(repo, plan)

    assert (repo / "README.md").read_text(encoding="utf-8") == "# Existing Nexus README\n"
    assert not (repo / "services").exists()
    assert not (repo / "integration" / "backend-config-snippet.yaml").exists()
    assert not (repo / "integration" / "lifecycle.backend.json").exists()
    assert (repo / "integration" / "vllm-model-env-snippet.env").exists()
    assert (repo / "integration" / "model-alias-snippet.json").exists()
    assert any("readme.md" in path.lower() and "integration" in path for path in created)
    assert "VLLM_MODEL_FAST=nvidia/NVIDIA-Nemotron-Nano-9B-v2" in (repo / "integration" / "vllm-model-env-snippet.env").read_text(encoding="utf-8")
