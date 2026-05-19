from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from fastapi import HTTPException

from gateway.app import coding_workspace as cw
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

    def _scaffold(repo_path, plan):
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

    def _scaffold(repo_path, plan):
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

    def _scaffold(repo_path, plan):
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
    assert scaffold_paths == [str(tmp_path / "workspaces" / task["id"] / "repo")]

    labels = [item.get("label") for item in task.get("commands", [])]
    assert "github-repo-ensure" in labels
    assert "git-clone-base" in labels
    assert "git-branch-work" in labels
    assert "git-add" in labels
    assert "git-commit" in labels
    assert "git-push-branch" in labels
    assert "git-push-base" not in labels
    assert "git-remote-add" not in labels

    assert ["git", "clone", "--depth", "1", "--branch", "main", "https://github.com/example/existing-repo.git", str(tmp_path / "workspaces" / task["id"] / "repo")] in calls
    assert ["git", "push", "-u", "origin", "nexus-coder/qwen"] in calls


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
