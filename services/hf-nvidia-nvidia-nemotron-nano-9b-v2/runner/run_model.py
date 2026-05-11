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
    if hasattr(tok,"apply_chat_template") and tok.chat_template:
        text=tok.apply_chat_template(msgs,tokenize=False,add_generation_prompt=True)
    else:
        parts=[f"<|{m['role']}|>\n{m['content']}" for m in msgs]
        text="".join(parts)+"<|assistant|>\n"
    inputs=tok(text,return_tensors="pt").to(dv)
    import torch
    with torch.no_grad():
        out_ids=model.generate(**inputs,max_new_tokens=body.get("max_tokens",2048),temperature=body.get("temperature",0.7),top_p=body.get("top_p",0.9),top_k=body.get("top_k",50),do_sample=True,repetition_penalty=body.get("repeat_penalty",1.0),pad_token_id=tok.eos_token_id)
        new=out_ids[0][inputs["input_ids"].shape[1]:]
        content=tok.decode(new,skip_special_tokens=True)
    resp={"model":mid,"choices":[{"message":{"role":"assistant","content":content},"finish_reason":"stop"}]}
    out.write_text(json.dumps(resp,indent=2))
    return 0
if __name__=="__main__":
    raise SystemExit(main())
