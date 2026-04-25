#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_LINE_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


FAMILY_SPECS: dict[str, dict[str, list[str]]] = {
    "vllm": {
        "components": ["vllm", "vllm-strong", "vllm-fast", "vllm-embeddings"],
        "default_env_keys": [
            "VLLM_BASE_URL",
            "VLLM_ADVERTISE_BASE_URL",
            "VLLM_FAST_BASE_URL",
            "VLLM_FAST_ADVERTISE_BASE_URL",
            "VLLM_EMBEDDINGS_BASE_URL",
            "VLLM_EMBEDDINGS_ADVERTISE_BASE_URL",
        ],
        "host_env_keys": [],
    },
    "tts": {
        "components": ["tts", "luxtts"],
        "default_env_keys": [
            "POCKET_TTS_BASE_URL",
            "POCKET_TTS_ADVERTISE_BASE_URL",
            "LUXTTS_BASE_URL",
            "LUXTTS_ADVERTISE_BASE_URL",
        ],
        "host_env_keys": [
            "LUXTTS_UPSTREAM_BASE_URL",
            "LUXTTS_PROMPT_AUDIO",
        ],
    },
    "qwen3-tts": {
        "components": ["qwen3-tts"],
        "default_env_keys": [
            "QWEN3_TTS_BASE_URL",
            "QWEN3_TTS_ADVERTISE_BASE_URL",
        ],
        "host_env_keys": [
            "QWEN3_TTS_DEVICE_MAP",
            "QWEN3_TTS_DTYPE",
            "QWEN3_TTS_ATTN_IMPL",
            "QWEN3_TTS_MODEL_ID",
            "QWEN3_TTS_TASK",
            "QWEN3_TTS_LANGUAGE",
            "QWEN3_TTS_SPEAKER",
            "QWEN3_TTS_VOICE_MAP_JSON",
            "QWEN3_TTS_REF_MAP_JSON",
            "QWEN3_TTS_INSTRUCT",
            "QWEN3_TTS_REF_AUDIO",
            "QWEN3_TTS_REF_TEXT",
            "QWEN3_TTS_REFS_REF_TEXT",
        ],
    },
}


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


def _validate_platform(raw: Any, *, label: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str) or not raw.strip():
        die(f"{label} must be a non-empty string when set")
    return raw.strip().lower()


def _validate_repo_dir_map(raw: Any, *, label: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        die(f"{label} must be an object")
    out: dict[str, str] = {}
    for key, value in raw.items():
        platform = _validate_platform(key, label=f"{label}.<key>")
        out[platform] = _validate_repo_dir(value, label=f"{label}.{key}")
    return out


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


def merged_platform(defaults: dict[str, Any], host: dict[str, Any]) -> str:
    host_platform = _validate_platform(host.get("platform"), label="host.platform")
    if host_platform:
        return host_platform
    default_platform = _validate_platform(defaults.get("platform"), label="defaults.platform")
    return default_platform


def merged_repo_dir(defaults: dict[str, Any], host: dict[str, Any]) -> str:
    host_repo_dir = _validate_repo_dir(host.get("repo_dir"), label="host.repo_dir")
    if host_repo_dir:
        return host_repo_dir
    platform = merged_platform(defaults, host)
    repo_dir_by_platform = _validate_repo_dir_map(defaults.get("repo_dir_by_platform"), label="defaults.repo_dir_by_platform")
    if platform and platform in repo_dir_by_platform:
        return repo_dir_by_platform[platform]
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


def _replace_url_host(raw_value: str, host_name: str) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return value
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    netloc = host_name
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _validated_components_for_host(host: dict[str, Any], *, label: str) -> list[str]:
    items = _validate_components(host.get("components"), label=label)
    host["components"] = list(items)
    return host["components"]


def _validated_env_for_scope(container: dict[str, Any], *, key: str, label: str) -> dict[str, Any]:
    current = container.get(key)
    if current is None:
        container[key] = {}
        return container[key]
    if not isinstance(current, dict):
        die(f"{label} must be an object")
    validated = _validate_env_map(current, label=label)
    container[key] = dict(validated)
    return container[key]


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
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(output_lines))
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


def cmd_families(_args: argparse.Namespace) -> int:
    for family_name in sorted(FAMILY_SPECS):
        print(family_name)
    return 0


def cmd_move_family(args: argparse.Namespace) -> int:
    topology_path = Path(args.topology_file)
    payload = _read_json(topology_path)
    defaults = payload.get("defaults")
    if defaults is None:
        defaults = {}
        payload["defaults"] = defaults
    if not isinstance(defaults, dict):
        die("topology.defaults must be an object")

    hosts = payload.get("hosts")
    if not isinstance(hosts, dict):
        die("topology.hosts must be an object")

    from_host_name = str(args.from_host).strip()
    to_host_name = str(args.to_host).strip()
    if not from_host_name or not to_host_name:
        die("--from-host and --to-host are required")
    if from_host_name == to_host_name:
        die("--from-host and --to-host must be different")

    from_host = hosts.get(from_host_name)
    if not isinstance(from_host, dict):
        die(f"unknown topology host: {from_host_name}")
    to_host = hosts.get(to_host_name)
    if not isinstance(to_host, dict):
        die(f"unknown topology host: {to_host_name}")

    family_name = str(args.family).strip()
    family_spec = FAMILY_SPECS.get(family_name)
    if family_spec is None:
        die(f"unknown family: {family_name}")

    family_components = list(family_spec.get("components") or [])
    family_default_env_keys = list(family_spec.get("default_env_keys") or [])
    family_host_env_keys = list(family_spec.get("host_env_keys") or [])

    defaults_env = _validated_env_for_scope(defaults, key="env", label="defaults.env")
    from_components = _validated_components_for_host(from_host, label=f"hosts.{from_host_name}.components")
    to_components = _validated_components_for_host(to_host, label=f"hosts.{to_host_name}.components")
    from_env = _validated_env_for_scope(from_host, key="env", label=f"hosts.{from_host_name}.env")
    to_env = _validated_env_for_scope(to_host, key="env", label=f"hosts.{to_host_name}.env")

    moved_components: list[str] = []
    added_destination_components: list[str] = []
    updated_default_env_keys: list[str] = []
    moved_host_env_keys: list[str] = []

    if args.components_mode == "move":
        next_from_components = [item for item in from_components if item not in family_components]
        next_to_components = list(to_components)
        for component in family_components:
            if component in from_components:
                moved_components.append(component)
            if component not in next_to_components:
                next_to_components.append(component)
                added_destination_components.append(component)
        from_host["components"] = next_from_components
        to_host["components"] = next_to_components

    for key in family_default_env_keys:
        if key not in defaults_env:
            continue
        new_value = _replace_url_host(str(defaults_env.get(key) or ""), to_host_name)
        if defaults_env.get(key) != new_value:
            defaults_env[key] = new_value
            updated_default_env_keys.append(key)

    if args.host_env_mode == "move":
        for key in family_host_env_keys:
            if key not in from_env:
                continue
            to_env[key] = from_env.pop(key)
            moved_host_env_keys.append(key)

    summary = {
        "family": family_name,
        "from_host": from_host_name,
        "to_host": to_host_name,
        "components_mode": args.components_mode,
        "host_env_mode": args.host_env_mode,
        "components": family_components,
        "moved_components": moved_components,
        "added_destination_components": added_destination_components,
        "updated_default_env_keys": updated_default_env_keys,
        "moved_host_env_keys": moved_host_env_keys,
        "topology_file": str(topology_path),
    }

    if args.write:
        with topology_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")

    print(json.dumps(summary, indent=2))
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

    parser_families = subparsers.add_parser("families", help="List supported topology backend families.")
    parser_families.set_defaults(func=cmd_families)

    parser_move = subparsers.add_parser("move-family", help="Move a backend family between topology hosts.")
    parser_move.add_argument("--topology-file", required=True)
    parser_move.add_argument("--family", required=True)
    parser_move.add_argument("--from-host", required=True)
    parser_move.add_argument("--to-host", required=True)
    parser_move.add_argument("--components-mode", choices=["move", "ignore"], default="move")
    parser_move.add_argument("--host-env-mode", choices=["move", "ignore"], default="move")
    parser_move.add_argument("--write", action="store_true")
    parser_move.set_defaults(func=cmd_move_family)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
