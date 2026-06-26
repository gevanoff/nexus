#!/usr/bin/env python3
import argparse
import datetime
import difflib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

KEY_RE = re.compile(r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\r?\n)?$")

SECRET_HINTS = (
    "TOKEN", "SECRET", "PASSWORD", "PASS", "KEY", "PRIVATE", "CREDENTIAL",
    "AUTH", "BEARER", "API", "COOKIE", "SESSION",
)

@dataclass
class EnvEntry:
    key: str
    index: int
    line: str
    value_raw: str
    value_norm: str

def normalize_value(raw: str) -> str:
    """
    Normalize only for empty/difference detection.
    Preserves raw value for output/replacement.
    """
    value = raw.strip()

    # Treat unquoted inline comments as comments for empty detection.
    bare = value.split("#", 1)[0].strip()

    if bare in ("", '""', "''"):
        return ""

    # For comparison, remove simple surrounding quotes.
    if (bare.startswith('"') and bare.endswith('"')) or (bare.startswith("'") and bare.endswith("'")):
        return bare[1:-1]

    return bare

def parse_env_lines(lines):
    """
    Returns first occurrence of each KEY -> EnvEntry.
    Duplicate keys are ignored after the first occurrence.
    """
    entries = {}

    for i, line in enumerate(lines):
        m = KEY_RE.match(line)
        if not m:
            continue

        key = m.group(3)
        if key in entries:
            continue

        raw_value = m.group(5)
        entries[key] = EnvEntry(
            key=key,
            index=i,
            line=line if line.endswith("\n") else line + "\n",
            value_raw=raw_value,
            value_norm=normalize_value(raw_value),
        )

    return entries

def mask_value(key: str, value: str, reveal: bool) -> str:
    if reveal:
        return value

    if value == "":
        return ""

    key_upper = key.upper()
    looks_secret = any(hint in key_upper for hint in SECRET_HINTS)

    if looks_secret:
        if len(value) <= 6:
            return "***"
        return value[:3] + "…" + value[-3:]

    if len(value) > 80:
        return value[:40] + "…" + value[-20:]

    return value

def print_compare_table(rows, reveal=False):
    if not rows:
        print("No differing non-empty values.")
        return

    rendered = []
    for key, env_val, ex_val in rows:
        env_out = mask_value(key, env_val, reveal)
        ex_out = mask_value(key, ex_val, reveal)
        rendered.append((key, env_out, ex_out))

    key_w = min(max(len(r[0]) for r in rendered), 48)
    env_w = min(max(len(r[1]) for r in rendered), 64)
    ex_w = min(max(len(r[2]) for r in rendered), 64)

    print(f"{'KEY':<{key_w}}  {'ENV VALUE':<{env_w}}  {'EXAMPLE VALUE':<{ex_w}}")
    print(f"{'-' * key_w}  {'-' * env_w}  {'-' * ex_w}")

    for key, env_val, ex_val in rendered:
        print(f"{key:<{key_w}}  {env_val:<{env_w}}  {ex_val:<{ex_w}}")

def main():
    ap = argparse.ArgumentParser(
        description=(
            "Fill missing/empty .env vars from an example file. "
            "Can also compare non-empty differing values side by side."
        )
    )
    ap.add_argument("--env", default=".env", help="Target .env file")
    ap.add_argument("--example", default=".env.example", help="Example env file")
    ap.add_argument("--write", action="store_true", help="Modify .env in place")
    ap.add_argument("--no-backup", action="store_true", help="Do not create a timestamped backup")

    ap.add_argument(
        "--compare-values",
        action="store_true",
        help="Show non-empty .env values that differ from .env.example side by side",
    )
    ap.add_argument(
        "--reveal-values",
        action="store_true",
        help="Show full raw values in comparison output. Default masks likely secrets.",
    )
    ap.add_argument(
        "--diff",
        action="store_true",
        help="Print unified diff of proposed changes",
    )
    ap.add_argument(
        "--apply-different",
        action="store_true",
        help=(
            "Also replace non-empty .env values with differing .env.example values. "
            "Requires --write to persist."
        ),
    )

    args = ap.parse_args()

    env_path = Path(args.env)
    example_path = Path(args.example)

    if not example_path.exists():
        raise SystemExit(f"example file not found: {example_path}")

    env_lines = env_path.read_text().splitlines(keepends=True) if env_path.exists() else []
    example_lines = example_path.read_text().splitlines(keepends=True)

    env_entries = parse_env_lines(env_lines)
    ex_entries = parse_env_lines(example_lines)

    new_lines = list(env_lines)
    additions = []
    fill_empty = []
    differing_non_empty = []

    # Preserve example order.
    for line in example_lines:
        m = KEY_RE.match(line)
        if not m:
            continue

        key = m.group(3)
        ex = ex_entries[key]
        env = env_entries.get(key)

        if env is None:
            additions.append(ex.line)
            continue

        if env.value_norm == "":
            fill_empty.append((key, env.index, ex.line))
            continue

        if ex.value_norm != "" and env.value_norm != ex.value_norm:
            differing_non_empty.append((key, env.value_norm, ex.value_norm, env.index, ex.line))

    if args.compare_values:
        compare_rows = [(key, env_val, ex_val) for key, env_val, ex_val, _, _ in differing_non_empty]
        print_compare_table(compare_rows, reveal=args.reveal_values)
        print()

    for key, idx, replacement in fill_empty:
        print(f"fill empty: {key}")
        new_lines[idx] = replacement

    if args.apply_different:
        for key, env_val, ex_val, idx, replacement in differing_non_empty:
            print(f"replace different: {key}")
            new_lines[idx] = replacement
    elif differing_non_empty and not args.compare_values:
        print(f"{len(differing_non_empty)} non-empty differing value(s) found. Use --compare-values to view them.")

    if additions:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        if new_lines and new_lines[-1].strip():
            new_lines.append("\n")
        new_lines.append("# Added from example env\n")

        for line in additions:
            key = KEY_RE.match(line).group(3)
            print(f"add missing: {key}")
            new_lines.append(line)

    changed = new_lines != env_lines

    if args.diff and changed:
        diff = difflib.unified_diff(
            env_lines,
            new_lines,
            fromfile=str(env_path),
            tofile=f"{env_path} proposed",
            lineterm="",
        )
        print()
        print("\n".join(diff))

    if not changed:
        print("No changes needed.")
        return

    if not args.write:
        print("\nDry run only. Re-run with --write to modify the file.")
        if differing_non_empty and not args.apply_different:
            print("Non-empty differing values were reported only, not changed. Add --apply-different to include them.")
        return

    if env_path.exists() and not args.no_backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = env_path.with_name(env_path.name + f".bak.{stamp}")
        shutil.copy2(env_path, backup)
        print(f"backup: {backup}")

    env_path.write_text("".join(new_lines))
    print(f"updated: {env_path}")

if __name__ == "__main__":
    main()
