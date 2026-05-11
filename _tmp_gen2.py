#!/usr/bin/env python3
P="services/hf-nvidia-nvidia-nemotron-nano-9b-v2/app/nexus_model_service.py"
t=open(P).read()
t+="""
async def stream_resp(req: ChatCompletionRequest):
    model,tok=get_model()
    conv=[m.model_dump() for m in req.messages]
    text=format_prompt(conv)
    inputs=tok(text,return_tensors="pt").to(DEVICE)
    rid=str(uuid.uuid4())
    import torch
    with torch.no_grad():
        for i,tid in enumerate(model.generate(**inputs,max_new_tokens=req.max_tokens or MAX_NEW_TOKENS,temperature=req.temperature or TEMPERATURE,top_p=req.top_p or TOP_P,top_k=req.top_k or TOP_K,do_sample=True,repetition_penalty=req.repeat_penalty or REPEAT_PENALTY,pad_token_id=tok.eos_token_id,streamer=None)[0][inputs["input_ids"].shape[1]:]):
            w=tok.decode(tid)
            yield f"data: {json.dumps({'id':rid,'object':'chat.completion.chunk','created':int(time.time()),'model':MODEL_ID,'choices':[{'index':0,'delta':{'content':w}}]})}\\n\\n"
        yield f"data: {json.dumps({'id':rid,'object':'chat.completion.chunk','created':int(time.time()),'model':MODEL_ID,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})}\\n\\n"
"""
open(P,"w").write(t)
print("done")