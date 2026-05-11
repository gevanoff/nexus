import pathlib
P="services/hf-nvidia-nvidia-nemotron-nano-9b-v2/app/nexus_model_service.py"
C=[]
def w(s):C.append(s)
w("class ChatMessage(BaseModel):")
w("    role:str")
w("    content:str")
