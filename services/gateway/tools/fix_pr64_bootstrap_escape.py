#!/usr/bin/env python3
from pathlib import Path

path = Path(__file__).with_name("apply_pr64_review_fixes.py")
text = path.read_text(encoding="utf-8")
slash = chr(92)
replacements = [
    (
        f"Working branch: {{task.get('branch_name')}}.{slash}n{slash}n\"",
        f"Working branch: {{task.get('branch_name')}}.{slash * 2}n{slash * 2}n\"",
    ),
    (
        f'+ "{slash}n{slash}n".join(request_bits)',
        f'+ "{slash * 2}n{slash * 2}n".join(request_bits)',
    ),
]
for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one bootstrap escape target, found {count}: {old!r}")
    text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
print("Repaired PR #64 bootstrap escaping.")
