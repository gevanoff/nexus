#!/usr/bin/env python3
P="services/hf-nvidia-nvidia-nemotron-nano-9b-v2/runner/run_model.py"
t=open(P).read()
# Remove duplicate block
i1=t.find("    if hasattr(tok")
i2=t.rfind("    if hasattr(tok")
if i1!=i2:
    t=t[:i2]
t+="    inputs=tok(text,return_tensors=\"pt\").to(dv)\n"
t+="    import torch\n"
t+="    with torch.no_grad():\n"
t+="        out_ids=model.generate(**inputs,max_new_tokens=body.get(\"max_tokens\",2048),temperature=body.get(\"temperature\",0.7),top_p=body.get(\"top_p\",0.9),top_k=body.get(\"top_k\",50),do_sample=True,repetition_penalty=body.get(\"repeat_penalty\",1.0),pad_token_id=tok.eos_token_id)\n"
t+="        new=out_ids[0][inputs[\"input_ids\"].shape[1]:]\n"
t+="        content=tok.decode(new,skip_special_tokens=True)\n"
t+="    resp={\"model\":mid,\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":content},\"finish_reason\":\"stop\"}]}\n"
t+="    out.write_text(json.dumps(resp,indent=2))\n"
t+="    return 0\n"
t+="if __name__==\"__main__\":\n"
t+="    raise SystemExit(main())\n"
open(P,"w").write(t)
print("done")