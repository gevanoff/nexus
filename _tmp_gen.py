#!/usr/bin/env python3
P="services/hf-nvidia-nvidia-nemotron-nano-9b-v2/app/nexus_model_service.py"
t=open(P).read()
t+="""
@app.post(\"/v1/chat/completions\")
async def chat_completions(req: ChatCompletionRequest):
    model,tok=get_model()
    conv=[m.model_dump() for m in req.messages]
    text=format_prompt(conv)
    inputs=tok(text,return_tensors=\"pt\").to(DEVICE)
    rid=str(uuid.uuid4())
    if req.stream:
        return StreamingResponse(stream_resp(req),media_type=\"text/event-stream\")
    import torch
    with torch.no_grad():
        out=model.generate(**inputs,max_new_tokens=req.max_tokens or MAX_NEW_TOKENS,temperature=req.temperature or TEMPERATURE,top_p=req.top_p or TOP_P,top_k=req.top_k or TOP_K,do_sample=True,repetition_penalty=req.repeat_penalty or REPEAT_PENALTY,pad_token_id=tok.eos_token_id)
        new=out[0][inputs[\"input_ids\"].shape[1]:]
        content=tok.decode(new,skip_special_tokens=True)
    return ChatCompletionResponse(id=rid,created=int(time.time()),model=MODEL_ID,choices=[ChatCompletionChoice(message=ChoiceMessage(content=content),finish_reason=\"stop\")])
"""
open(P,"w").write(t)
print("done")