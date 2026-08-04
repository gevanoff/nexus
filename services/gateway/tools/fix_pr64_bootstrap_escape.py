#!/usr/bin/env python3
from pathlib import Path

path = Path(__file__).with_name("apply_pr64_review_fixes.py")
text = path.read_text(encoding="utf-8")
slash = chr(92)


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one {label} target, found {count}: {old!r}")
    text = text.replace(old, new, 1)


replace_once(
    f"Working branch: {{task.get('branch_name')}}.{slash}n{slash}n\"",
    f"Working branch: {{task.get('branch_name')}}.{slash * 2}n{slash * 2}n\"",
    "workspace prompt escape",
)
replace_once(
    f'+ "{slash}n{slash}n".join(request_bits)',
    f'+ "{slash * 2}n{slash * 2}n".join(forced_request_bits)',
    "forced request join",
)
forced_anchor = (
    '    if forced_context:\n'
    '        text_call_guidance = f"{_text_tool_call_guidance(task)} " if text_tool_mode else ""\n'
)
forced_replacement = (
    '    if forced_context:\n'
    '        forced_request_bits = [f"Original user request:'
    + slash * 2
    + 'n{original or \'(none recorded)\'}"]\n'
    '        if current and current != original:\n'
    '            forced_request_bits.append(f"Current run request:'
    + slash * 2
    + 'n{current}")\n'
    '        text_call_guidance = f"{_text_tool_call_guidance(task)} " if text_tool_mode else ""\n'
)
replace_once(forced_anchor, forced_replacement, "forced request context")

path.write_text(text, encoding="utf-8")
print("Repaired PR #64 bootstrap source.")
