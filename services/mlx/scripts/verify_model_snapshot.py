#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


SHARD_RE = re.compile(r"^(?P<prefix>.+)-(?P<index>\d+)-of-(?P<total>\d+)\.(?:safetensors|bin)$")


def verify_snapshot(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_dir():
        return [f"snapshot directory is missing: {path}"]
    groups: dict[tuple[str, int], set[int]] = {}
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        match = SHARD_RE.match(item.name)
        if not match:
            continue
        total = int(match.group("total"))
        relative_prefix = (item.relative_to(path).parent / match.group("prefix")).as_posix()
        groups.setdefault((relative_prefix, total), set()).add(int(match.group("index")))
    for (prefix, total), present in sorted(groups.items()):
        expected = set(range(1, total + 1))
        missing = sorted(expected - present)
        if missing:
            preview = ",".join(str(index) for index in missing[:12])
            suffix = ",..." if len(missing) > 12 else ""
            errors.append(f"{prefix} is missing {len(missing)} of {total} shards: {preview}{suffix}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify that every numbered model shard exists in a Hugging Face snapshot")
    parser.add_argument("snapshot")
    args = parser.parse_args()
    errors = verify_snapshot(Path(args.snapshot))
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
