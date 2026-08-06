from __future__ import annotations

import os

import pytest

os.environ.setdefault("GATEWAY_BEARER_TOKEN", "test-token")

from app import coding_agent
from app import coding_work_phases as phases


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["pytest", "tests/test_api.py"], True),
        (["python", "-m", "unittest", "tests.test_api"], True),
        (["node", "--check", "app.js"], True),
        (["npm", "run", "test:unit"], True),
        (["pnpm", "lint"], True),
        (["git", "diff", "--cached", "--check"], True),
        (["uv", "run", "--project", "services/gateway", "pytest", "tests/test_api.py"], True),
        (["uv", "run", "--no-sync", "--offline", "--", "git", "diff", "--check"], True),
        (["uv", "run", "-w", "dev", "pytest", "tests/test_api.py"], True),
        (["uv", "run", "--no-extra", "docs", "git", "diff", "--check"], True),
        (["uv", "run", "-m", "pytest"], True),
        (["uv", "--project", "services/gateway", "run", "pytest", "tests/test_api.py"], True),
        (["uv", "--offline", "run", "git", "diff", "--check"], True),
        (["npm", "install", "lint-staged"], False),
        (["yarn", "add", "check-deps"], False),
        (["uv", "add", "ruff"], False),
        (["uv", "run", "--project", "pytest", "python", "app.py"], False),
        (["uv", "run", "--future-option", "pytest", "python", "app.py"], False),
        (["uv", "run", "-w", "ruff", "python", "app.py"], False),
        (["uv", "run", "--no-extra", "pytest", "python", "app.py"], False),
        (["uv", "tool", "run", "pytest"], False),
        (["uv", "--project", "run", "pytest"], False),
        (["uv", "--future-option=value", "run", "pytest"], False),
        (["git", "status"], False),
    ],
)
def test_agent_and_phase_paths_share_validation_classification(argv: list[str], expected: bool):
    assert phases.is_validation_command(argv) is expected
    assert coding_agent._is_validation_command(argv) is expected


def test_python_validation_helper_is_shared_across_agent_and_phase_paths():
    valid = ["-m", "py_compile", "app/module.py"]
    invalid = ["app/module.py"]

    assert phases.is_python_validation_command(valid) is True
    assert coding_agent._is_python_validation_command(valid) is True
    assert phases.is_python_validation_command(invalid) is False
    assert coding_agent._is_python_validation_command(invalid) is False


def test_private_phase_aliases_remain_compatible_for_existing_callers():
    argv = ["uv", "run", "git", "diff", "--check"]

    assert phases._is_validation_command(argv) == phases.is_validation_command(argv)
    assert phases._python_validation_command(["-m", "pytest"]) == phases.is_python_validation_command(
        ["-m", "pytest"]
    )
