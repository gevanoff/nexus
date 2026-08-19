from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping


_ACCEPTANCE_PARTS = {"tests", "test", "fixtures", "fixture", "examples", "example"}
_CONTEXT_SUFFIXES = {".md", ".rst", ".txt"}
_EXPLICIT_BARE_RE = re.compile(
    r"(?:`([A-Za-z0-9_.-]+)`|'([A-Za-z0-9_.-]+)'|\"([A-Za-z0-9_.-]+)\")"
)
_LEADING_CONVENTIONAL_BARE_RE = re.compile(r"^\s*([A-Z][A-Z0-9_-]{1,})\b")
_ROOT_SUFFIXLESS_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,}$")


def _has_standalone_occurrence(text: str, name: str) -> bool:
    """Return true when a filename is cited independently, not inside a path/URL.

    Full paths are handled separately by coding_contract_hardening._repository_paths.
    Basename recovery exists only for genuinely bare citations such as
    ``config.py``. Without this boundary check, ``/etc/config.py`` and
    ``https://example.test/...`` could accidentally manufacture corrective
    basename targets from path/domain fragments.
    """
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])",
        re.IGNORECASE,
    )
    raw = str(text or "")
    for match in pattern.finditer(raw):
        before = raw[match.start() - 1] if match.start() > 0 else ""
        after = raw[match.end()] if match.end() < len(raw) else ""
        if before in {"/", "\\"} or after in {"/", "\\"}:
            continue
        return True
    return False


def _excluded_causal_target(target: str) -> bool:
    normalized = str(target or "").strip().replace("\\", "/").strip("/")
    if not normalized:
        return True
    path = PurePosixPath(normalized)
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    stem = path.stem.casefold()
    conventional_test = (
        name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or stem.endswith("_test")
        or stem.endswith("_spec")
    )
    if parts & _ACCEPTANCE_PARTS or conventional_test:
        return True
    if path.suffix.casefold() in _CONTEXT_SUFFIXES or "docs" in parts:
        return True
    return False


def _explicit_suffixless_basenames(text: str) -> list[str]:
    """Extract explicit root-file citations without treating ordinary prose as paths.

    Arbitrary suffixless repository files are valid causal evidence, but a bare
    lowercase word in prose is too ambiguous to authorize a path-locked read.
    Accept quoted/backticked tokens, plus conventional leading all-caps root
    filenames such as BUILD or WORKSPACE. Slash-containing paths are handled by
    the repository-path parser instead.
    """
    raw = str(text or "")
    out: list[str] = []

    def append(candidate: str) -> None:
        name = str(candidate or "").strip()
        if (
            not name
            or "/" in name
            or "\\" in name
            or "." in name
            or _excluded_causal_target(name)
            or not _has_standalone_occurrence(raw, name)
        ):
            return
        if name not in out:
            out.append(name)

    for match in _EXPLICIT_BARE_RE.finditer(raw):
        append(next((group for group in match.groups() if group), ""))

    leading = _LEADING_CONVENTIONAL_BARE_RE.match(raw)
    if leading:
        append(str(leading.group(1) or ""))
    return out


def _explicit_root_suffixless_basenames(text: str) -> list[str]:
    """Return conventional suffixless names that explicitly denote repo-root files."""
    return [
        name
        for name in _explicit_suffixless_basenames(text)
        if _ROOT_SUFFIXLESS_RE.fullmatch(name)
    ]


def install(hardening: Any) -> None:
    """Install repository-path safety and suffixless-file recovery refinements."""
    if bool(getattr(hardening, "_coding_contract_path_safety_installed", False)):
        return

    original_target_is_causal = hardening._target_is_causal

    def target_is_causal_with_suffixless_paths(target: str) -> bool:
        if original_target_is_causal(target):
            return True
        raw = str(target or "").strip()
        normalized = hardening._normalized_path(raw) if "/" in raw or "\\" in raw else raw
        if not normalized or "/" not in normalized:
            return False
        if _excluded_causal_target(normalized):
            return False
        # Match coding_evidence_policy._path_class: after acceptance/context
        # exclusions, any concrete repository-relative file may be causal,
        # including arbitrary suffixless paths such as bin/server.
        return True

    hardening._target_is_causal = target_is_causal_with_suffixless_paths

    original_basenames = hardening._repository_basenames

    def repository_basenames_with_boundaries(text: str) -> list[str]:
        out = [
            name
            for name in original_basenames(text)
            if _has_standalone_occurrence(str(text or ""), str(name or ""))
        ]
        for name in _explicit_suffixless_basenames(text):
            if name not in out:
                out.append(name)
        return out

    hardening._repository_basenames = repository_basenames_with_boundaries

    original_resolve = hardening._resolve_asserted_targets

    def resolve_asserted_targets_with_root_lock(
        repository_evidence: str,
        state: Mapping[str, Any],
    ) -> list[str]:
        targets = list(original_resolve(repository_evidence, state))
        explicit_roots = _explicit_root_suffixless_basenames(repository_evidence)
        if not explicit_roots:
            return targets
        # A conventional all-caps suffixless citation such as BUILD or WORKSPACE
        # denotes the repository-root file. Do not let a same-basename candidate
        # elsewhere in the tree reinterpret that explicit root citation.
        for root in explicit_roots:
            targets = [
                target
                for target in targets
                if PurePosixPath(str(target or "")).name != root
            ]
            targets.append(root)
        return targets

    hardening._resolve_asserted_targets = resolve_asserted_targets_with_root_lock

    original_read_matches_target = hardening._read_matches_target

    def read_matches_target_with_root_lock(requested: str, target: str) -> bool:
        if "/" not in str(target or "") and _ROOT_SUFFIXLESS_RE.fullmatch(str(target or "")):
            return str(requested or "") == str(target or "")
        return original_read_matches_target(requested, target)

    hardening._read_matches_target = read_matches_target_with_root_lock
    hardening._coding_contract_path_safety_installed = True
