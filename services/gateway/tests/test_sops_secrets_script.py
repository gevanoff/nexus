from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_sops_secrets_script_is_valid_bash() -> None:
    script = ROOT / "deploy/scripts/sops-secrets.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_sops_edit_forces_dotenv_format_for_env_sops_files() -> None:
    text = (ROOT / "deploy/scripts/sops-secrets.sh").read_text(encoding="utf-8")

    assert 'sops --input-type dotenv --output-type dotenv "$secret_file"' in text
    assert '\n    sops "$secret_file"\n' not in text
