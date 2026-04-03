#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_LINE_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def die(message: str) -> "NoReturn":
    raise SystemExit(message)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"topology file not found: {path}")
    except json.JSONDecodeError as exc:
        die(f"invalid topology JSON at {path}: {exc}")
    if not isinstance(payload, dict):
        die(f"invalid topology payload at {path}: expected an object")
    return payload


def _validate_env_map(raw: Any, *, label: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        die(f"{label} must be an object")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not ENV_KEY_RE.match(key):
            die(f"{label} contains invalid env key: {key!r}")
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            out[key] = str(value)
        elif value is None:
            out[key] = ""
        elif isinstance(value, str):
            out[key] = value
        else:
            die(f"{label}.{key} must be a string/number/bool/null")
    return out


def _validate_components(raw: Any, *, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        die(f"{label} must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            die(f"{label} contains an invalid component entry")
        normalized = item.strip()
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _validate_repo_dir(raw: Any, *, label: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str) or not raw.strip():
        die(f"{label} must be a non-empty string when set")
    return raw.strip()


def load_host(topology_path: Path, host_name: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload = _read_json(topology_path)
    defaults = payload.get("defaults") or {}
    if defaults and not isinstance(defaults, dict):
        die("topology.defaults must be an object")
    hosts = payload.get("hosts")
    if not isinstance(hosts, dict):
        die("topology.hosts must be an object")
    host = hosts.get(host_name)
    if not isinstance(host, dict):
        die(f"unknown topology host: {host_name}")
    return payload, defaults, host


def merged_components(defaults: dict[str, Any], host: dict[str, Any]) -> list[str]:
    items = _validate_components(defaults.get("components"), label="defaults.components")
    host_items = _validate_components(host.get("components"), label="host.components")
    for component in host_items:
        if component not in items:
            items.append(component)
    return items


def merged_env(defaults: dict[str, Any], host: dict[str, Any]) -> dict[str, str]:
    env = _validate_env_map(defaults.get("env"), label="defaults.env")
    env.update(_validate_env_map(host.get("env"), label="host.env"))
    return env


def merged_repo_dir(defaults: dict[str, Any], host: dict[str, Any]) -> str:
    host_repo_dir = _validate_repo_dir(host.get("repo_dir"), label="host.repo_dir")
    if host_repo_dir:
        return host_repo_dir
    default_repo_dir = _validate_repo_dir(defaults.get("repo_dir"), label="defaults.repo_dir")
    if default_repo_dir:
        return default_repo_dir
    return "/opt/nexus"


def parse_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        values[match.group(2)] = match.group(3)
    return values


def render_env_file(template_path: Path, output_path: Path, env_values: dict[str, str]) -> tuple[int, int]:
    try:
        template_lines = template_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        die(f"template env file not found: {template_path}")

    existing_values = parse_env_values(output_path)
    known_keys: set[str] = set()
    output_lines: list[str] = []

    for line in template_lines:
        match = ENV_LINE_RE.match(line)
        if not match:
            output_lines.append(line)
            continue
        key = match.group(2)
        known_keys.add(key)
        if key in env_values:
            output_lines.append(f"{key}={env_values[key]}\n")
        elif key in existing_values:
            output_lines.append(f"{key}={existing_values[key]}\n")
        else:
            output_lines.append(line)

    topology_only_keys = sorted(key for key in env_values if key not in known_keys)
    existing_only_keys = sorted(key for key in existing_values if key not in known_keys and key not in env_values)

    if topology_only_keys or existing_only_keys:
        output_lines.append("\n# --- Additional values preserved/materialized by topology_tool.py ---\n")
        for key in topology_only_keys:
            output_lines.append(f"{key}={env_values[key]}\n")
        for key in existing_only_keys:
            output_lines.append(f"{key}={existing_values[key]}\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(output_lines), encoding="utf-8", newline="\n")
    return len(known_keys), len(topology_only_keys)


def cmd_components(args: argparse.Namespace) -> int:
    _payload, defaults, host = load_host(Path(args.topology_file), args.host)
    for component in merged_components(defaults, host):
        print(component)
    return 0


def cmd_ssh_target(args: argparse.Namespace) -> int:
    _payload, _defaults, host = load_host(Path(args.topology_file), args.host)
    ssh_target = host.get("ssh_target")
    if not isinstance(ssh_target, str) or not ssh_target.strip():
        die(f"topology host {args.host} does not define ssh_target")
    print(ssh_target.strip())
    return 0


def cmd_repo_dir(args: argparse.Namespace) -> int:
    _payload, defaults, host = load_host(Path(args.topology_file), args.host)
    print(merged_repo_dir(defaults, host))
    return 0


def cmd_render_env(args: argparse.Namespace) -> int:
    _payload, defaults, host = load_host(Path(args.topology_file), args.host)
    env_values = merged_env(defaults, host)
    known_count, topology_only_count = render_env_file(Path(args.template), Path(args.out), env_values)
    print(
        json.dumps(
            {
                "host": args.host,
                "output": args.out,
                "known_keys": known_count,
                "topology_extra_keys": topology_only_count,
            }
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read and materialize Nexus topology manifests.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_components = subparsers.add_parser("components", help="Print the merged component list for a host.")
    parser_components.add_argument("--topology-file", required=True)
    parser_components.add_argument("--host", required=True)
    parser_components.set_defaults(func=cmd_components)

    parser_ssh = subparsers.add_parser("ssh-target", help="Print the SSH target for a host.")
    parser_ssh.add_argument("--topology-file", required=True)
    parser_ssh.add_argument("--host", required=True)
    parser_ssh.set_defaults(func=cmd_ssh_target)

    parser_repo_dir = subparsers.add_parser("repo-dir", help="Print the repo directory for a host.")
    parser_repo_dir.add_argument("--topology-file", required=True)
    parser_repo_dir.add_argument("--host", required=True)
    parser_repo_dir.set_defaults(func=cmd_repo_dir)

    parser_render = subparsers.add_parser("render-env", help="Materialize a host env file from topology.")
    parser_render.add_argument("--topology-file", required=True)
    parser_render.add_argument("--host", required=True)
    parser_render.add_argument("--template", required=True)
    parser_render.add_argument("--out", required=True)
    parser_render.set_defaults(func=cmd_render_env)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
