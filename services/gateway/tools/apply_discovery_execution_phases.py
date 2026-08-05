from pathlib import Path
import base64
import hashlib
import zlib

root = Path(__file__).resolve().parent
payload = "".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("discovery_execution_payload.part*")))
if hashlib.sha256(payload.encode("utf-8")).hexdigest() != "a70d366d2c8db3067cccce38020f125a51911a3de0bcefc64ecad222d73142a8":
    raise RuntimeError("phase-controller payload checksum mismatch")
source = zlib.decompress(base64.b64decode(payload))
if hashlib.sha256(source).hexdigest() != "eda036741e2d19e0c101407bfa5f9c0ce516a75a3de73ba1e51180c651923b28":
    raise RuntimeError("phase-controller source checksum mismatch")
exec(compile(source, __file__, "exec"))

# Direct callers of build_working_memory may not yet have a persisted phase.
# Derive it from the current mission and intervention stage in that case.
path = root.parent / "app" / "coding_stagnation_resilience.py"
text = path.read_text(encoding="utf-8")
old = '''    phase = str(controller.get("work_phase") or work_phases.current_phase(task) or work_phases.DISCOVERY)
    phase_decision = str(controller.get("phase_decision") or "")
'''
new = '''    derived_phase = work_phases.advance_phase(task, stage=stage, events=events)
    phase = str(controller.get("work_phase") or derived_phase.get("phase") or work_phases.DISCOVERY)
    phase_decision = str(controller.get("phase_decision") or derived_phase.get("decision") or "")
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one phase derivation target, found {text.count(old)}")
text = text.replace(old, new, 1)
text = text.replace(
    "report confirmed defects separately from environment/configuration blockers",
    "report confirmed defects separately from environment or configuration blockers",
)
path.write_text(text, encoding="utf-8")
