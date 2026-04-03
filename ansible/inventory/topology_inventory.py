#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOPOLOGY_FILE = REPO_ROOT / "deploy" / "topology" / "production.json"


def _load_topology(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"topology inventory file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid topology inventory JSON at {path}: {exc}")
    if not isinstance(payload, dict):
        raise SystemExit(f"invalid topology payload at {path}: expected an object")
    hosts = payload.get("hosts")
    if not isinstance(hosts, dict):
        raise SystemExit(f"invalid topology payload at {path}: missing hosts object")
    return payload


def _split_ssh_target(raw_target: str, host_name: str) -> tuple[str, str]:
    candidate = (raw_target or "").strip()
    if "@" not in candidate:
        return "", candidate or host_name
    user, host = candidate.split("@", 1)
    return user.strip(), host.strip() or host_name


def _safe_group_name(prefix: str, value: str) -> str:
    normalized = (value or "").strip().lower().replace("-", "_").replace(".", "_")
    return f"{prefix}_{normalized}"


def _merged_env(defaults: dict[str, Any], host: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    defaults_env = defaults.get("env")
    if isinstance(defaults_env, dict):
        out.update(defaults_env)
    host_env = host.get("env")
    if isinstance(host_env, dict):
        out.update(host_env)
    return out


def _merged_repo_dir(defaults: dict[str, Any], host: dict[str, Any]) -> str:
    host_repo_dir = host.get("repo_dir")
    if isinstance(host_repo_dir, str) and host_repo_dir.strip():
        return host_repo_dir.strip()
    default_repo_dir = defaults.get("repo_dir")
    if isinstance(default_repo_dir, str) and default_repo_dir.strip():
        return default_repo_dir.strip()
    return ""


def _build_inventory(topology_file: Path, payload: dict[str, Any]) -> dict[str, Any]:
    defaults = payload.get("defaults") if isinstance(payload.get("defaults"), dict) else {}
    hosts = payload["hosts"]

    inventory: dict[str, Any] = {
        "_meta": {"hostvars": {}},
        "all": {"children": ["nexus"]},
        "nexus": {"hosts": [], "vars": {"nexus_topology_file": str(topology_file)}},
    }

    for host_name, raw_host in sorted(hosts.items()):
        if not isinstance(raw_host, dict):
            continue

        ssh_target = str(raw_host.get("ssh_target") or "").strip()
        ssh_user, ssh_host = _split_ssh_target(ssh_target, host_name)
        components = [item for item in raw_host.get("components", []) if isinstance(item, str) and item.strip()]
        native_services = [item for item in raw_host.get("native_services", []) if isinstance(item, str) and item.strip()]
        merged_env = _merged_env(defaults, raw_host)
        repo_dir = _merged_repo_dir(defaults, raw_host)

        hostvars: dict[str, Any] = {
            "ansible_host": ssh_host,
            "nexus_topology_host": host_name,
            "nexus_description": str(raw_host.get("description") or ""),
            "nexus_ssh_target": ssh_target,
            "nexus_components": components,
            "nexus_native_services": native_services,
            "nexus_topology_env": merged_env,
        }
        if ssh_user:
            hostvars["ansible_user"] = ssh_user
        if repo_dir:
            hostvars["nexus_repo_dir"] = repo_dir

        inventory["_meta"]["hostvars"][host_name] = hostvars
        inventory["nexus"]["hosts"].append(host_name)

        host_group = _safe_group_name("topology", host_name)
        inventory.setdefault(host_group, {"hosts": []})
        inventory[host_group]["hosts"].append(host_name)

        for component in components:
            group_name = _safe_group_name("component", component)
            inventory.setdefault(group_name, {"hosts": []})
            inventory[group_name]["hosts"].append(host_name)

        for service in native_services:
            group_name = _safe_group_name("native", service)
            inventory.setdefault(group_name, {"hosts": []})
            inventory[group_name]["hosts"].append(host_name)

    return inventory


def main(argv: list[str]) -> int:
    topology_file = Path(os.environ.get("NEXUS_TOPOLOGY_FILE", str(DEFAULT_TOPOLOGY_FILE)))
    payload = _load_topology(topology_file)
    inventory = _build_inventory(topology_file, payload)

    if len(argv) >= 2 and argv[1] == "--host":
        host_name = argv[2] if len(argv) >= 3 else ""
        print(json.dumps(inventory["_meta"]["hostvars"].get(host_name, {}), indent=2, sort_keys=True))
        return 0

    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
