from pathlib import Path

path = Path(__file__).with_name("apply_forced_action_enforcement.py")
text = path.read_text(encoding="utf-8")
old = '''def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
'''
new = '''def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 1:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return
    wanted = old.strip("\\n").splitlines()
    source = text.splitlines(keepends=True)
    matches = []
    for start in range(0, len(source) - len(wanted) + 1):
        if all(source[start + offset].strip() == wanted[offset].strip() for offset in range(len(wanted))):
            matches.append(start)
    if len(matches) != 1:
        raise RuntimeError(f"{label}: expected one anchor, found exact={count} normalized={len(matches)}")
    start = matches[0]
    end = start + len(wanted)
    source_first = source[start].rstrip("\\r\\n")
    source_indent_width = len(source_first) - len(source_first.lstrip())
    wanted_first = wanted[0]
    wanted_indent_width = len(wanted_first) - len(wanted_first.lstrip())
    base_indent = " " * max(0, source_indent_width - wanted_indent_width)
    replacement_lines = new.strip("\\n").splitlines()
    replacement = "\\n".join((base_indent + line if line else line) for line in replacement_lines)
    if source[end - 1].endswith("\\n"):
        replacement += "\\n"
    source[start:end] = [replacement]
    path.write_text("".join(source), encoding="utf-8")
'''
if text.count(old) != 1:
    raise RuntimeError("replace_once helper anchor changed")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
