#!/usr/bin/env python3
"""Render and validate reusable vLLM tool-capability profiles."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = REPO_ROOT / "deploy" / "config" / "vllm-tool-profiles.json"
ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
PROFILE_SELECTOR_RE = re.compile(r"^(VLLM(?:_[A-Z0-9]+)*)_TOOL_PROFILE$")
ENV_SUFFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def die(message: str) -> "NoReturn":
    raise SystemExit(message)


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"vLLM tool profile catalog not found: {path}")
    except json.JSONDecodeError as exc:
        die(f"invalid vLLM tool profile catalog at {path}: {exc}")
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        die(f"invalid vLLM tool profile catalog at {path}: profiles must be an object")
    return payload


def get_profile(catalog: dict[str, Any], name: str) -> dict[str, Any]:
    profile = catalog["profiles"].get(name)
    if not isinstance(profile, dict):
        available = ", ".join(sorted(catalog["profiles"]))
        die(f"unknown vLLM tool profile {name!r}; available profiles: {available}")
    return profile


def normalize_prefix(raw: str) -> str:
    prefix = raw.strip().upper().rstrip("_")
    if not re.fullmatch(r"VLLM(?:_[A-Z0-9]+)*", prefix):
        die("--prefix must be VLLM or a lane prefix such as VLLM_FAST")
    return prefix


def rendered_profile_env(profile: dict[str, Any], prefix: str) -> dict[str, str]:
    raw = profile.get("vllm_env")
    if not isinstance(raw, dict):
        die("selected profile is missing a vllm_env object")
    rendered: dict[str, str] = {}
    for suffix, value in raw.items():
        if not isinstance(suffix, str) or not ENV_SUFFIX_RE.fullmatch(suffix):
            die(f"invalid vllm_env suffix in selected profile: {suffix!r}")
        if isinstance(value, bool):
            normalized = "true" if value else "false"
        elif value is None:
            normalized = ""
        elif isinstance(value, (str, int, float)):
            normalized = str(value)
        else:
            die(f"invalid vllm_env value for {suffix}: expected scalar")
        rendered[f"{prefix}_{suffix}"] = normalized
    return rendered


def parse_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        die(f"env file not found: {path}")
    values: dict[str, str] = {}
    for line in lines:
        match = ENV_LINE_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def validate_selected_profiles(catalog: dict[str, Any], env: dict[str, str]) -> list[str]:
    errors: list[str] = []
    selectors = sorted(
        (key, value.strip(), PROFILE_SELECTOR_RE.fullmatch(key))
        for key, value in env.items()
        if PROFILE_SELECTOR_RE.fullmatch(key)
    )
    for selector, profile_name, match in selectors:
        assert match is not None
        prefix = match.group(1)
        try:
            profile = get_profile(catalog, profile_name)
            expected = rendered_profile_env(profile, prefix)
        except SystemExit as exc:
            errors.append(f"{selector}: {exc}")
            continue
        for key, expected_value in expected.items():
            if key not in env:
                errors.append(f"{selector}={profile_name}: missing {key}")
            elif env[key] != expected_value:
                errors.append(
                    f"{selector}={profile_name}: {key}={env[key]!r}, expected {expected_value!r}"
                )
    return errors


def cmd_list(args: argparse.Namespace) -> int:
    catalog = load_catalog(Path(args.catalog))
    for name, profile in sorted(catalog["profiles"].items()):
        description = profile.get("description", "") if isinstance(profile, dict) else ""
        print(f"{name}\t{description}")
    return 0


def cmd_render_env(args: argparse.Namespace) -> int:
    catalog = load_catalog(Path(args.catalog))
    profile = get_profile(catalog, args.profile)
    prefix = normalize_prefix(args.prefix)
    print(f"{prefix}_TOOL_PROFILE={args.profile}")
    for key, value in rendered_profile_env(profile, prefix).items():
        if "\n" in value or "\r" in value:
            die(f"profile value for {key} cannot contain a newline")
        print(f"{key}={value}")
    return 0


def cmd_alias_json(args: argparse.Namespace) -> int:
    catalog = load_catalog(Path(args.catalog))
    profile = get_profile(catalog, args.profile)
    alias = profile.get("gateway_alias")
    if not isinstance(alias, dict):
        die("selected profile is missing a gateway_alias object")
    payload = {
        "backend": args.backend,
        "model": args.model,
        **alias,
    }
    if args.context_window is not None:
        payload["context_window"] = args.context_window
    print(json.dumps(payload, indent=2))
    return 0


def cmd_check_env(args: argparse.Namespace) -> int:
    catalog = load_catalog(Path(args.catalog))
    env = parse_env_file(Path(args.env_file))
    selectors = [key for key in env if PROFILE_SELECTOR_RE.fullmatch(key)]
    if not selectors:
        print("No VLLM*_TOOL_PROFILE selectors configured; nothing to validate.")
        return 0
    errors = validate_selected_profiles(catalog, env)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Validated vLLM tool profiles: " + ", ".join(f"{key}={env[key]}" for key in sorted(selectors)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render model-family tool settings and detect serving/Gateway profile drift."
    )
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List available profiles.")
    list_parser.set_defaults(func=cmd_list)

    render_parser = subparsers.add_parser("render-env", help="Render lane env settings.")
    render_parser.add_argument("--profile", required=True)
    render_parser.add_argument("--prefix", required=True, help="VLLM or a lane prefix such as VLLM_FAST")
    render_parser.set_defaults(func=cmd_render_env)

    alias_parser = subparsers.add_parser("alias-json", help="Render Gateway alias capability metadata.")
    alias_parser.add_argument("--profile", required=True)
    alias_parser.add_argument("--backend", required=True)
    alias_parser.add_argument("--model", required=True)
    alias_parser.add_argument("--context-window", type=int)
    alias_parser.set_defaults(func=cmd_alias_json)

    check_parser = subparsers.add_parser("check-env", help="Validate selected profiles in a rendered env file.")
    check_parser.add_argument("--env-file", required=True)
    check_parser.set_defaults(func=cmd_check_env)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
