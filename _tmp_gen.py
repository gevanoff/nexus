#!/usr/bin/env python3
P="services/hf-nvidia-nvidia-nemotron-nano-9b-v2/app/nexus_model_service.py"
t=open(P).read()
i1=t.find("@app.get(\"/v1/metadata\")")
i2=t.rfind("@app.get(\"/v1/metadata\")")
if i1!=i2:
    t=t[:i2]
open(P,"w").write(t)
print("done")