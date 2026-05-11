from __future__ import annotations
import json,os
from pathlib import Path
def main() -> int:
    req=Path(os.environ["NEXUS_REQUEST_JSON"])
    out=Path(os.environ["NEXUS_OUTPUT_JSON"])
    body=json.loads(req.read_text())
    mid=os.environ.get("HF_MODEL_ID","nvidia/NVIDIA-Nemotron-Nano-9B-v2")
    dv=os.environ.get("DEVICE","cuda")
    from transformers import AutoModelForCausalLM,AutoTokenizer
    tok=AutoTokenizer.from_pretrained(mid,trust_remote_code=True)
    model=AutoModelForCausalLM.from_pretrained(mid,torch_dtype="auto",device_map=dv,trust_remote_code=True)
    model.eval()
    msgs=body.get("messages",[])
