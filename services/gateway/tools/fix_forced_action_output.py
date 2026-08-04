from pathlib import Path

root = Path(__file__).resolve().parents[3]

resilience = root / "services/gateway/app/coding_stagnation_resilience.py"
text = resilience.read_text(encoding="utf-8")
start = text.index("_COMMITMENT_PATTERN = re.compile(")
end = text.index("_GENERIC_ACTION_PREFIXES =", start)
block = text[start:end]
fixed = block.replace("\\\\", "\\")
if fixed == block:
    raise RuntimeError("commitment regex did not contain doubled escapes")
resilience.write_text(text[:start] + fixed + text[end:], encoding="utf-8")

runtime_test = root / "services/gateway/tests/test_coding_runtime_guardrails.py"
text = runtime_test.read_text(encoding="utf-8")
old = '''        for spec in req.tools or []:
            fn = spec.get("function") if isinstance(spec, dict) else None
            if isinstance(fn, dict):
                names.append(str(fn.get("name") or ""))
'''
new = '''        for spec in req.tools or []:
            if isinstance(spec, dict):
                fn = spec.get("function")
                if isinstance(fn, dict):
                    names.append(str(fn.get("name") or ""))
            elif getattr(spec, "function", None) is not None:
                names.append(str(getattr(spec.function, "name", "") or ""))
'''
if text.count(old) != 1:
    raise RuntimeError("ToolSpec test extraction anchor changed")
runtime_test.write_text(text.replace(old, new, 1), encoding="utf-8")
