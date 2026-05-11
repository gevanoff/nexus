#!/usr/bin/env python3
import pathlib
T = pathlib.Path("services/hf-nvidia-nvidia-nemotron-nano-9b-v2/app/nexus_model_service.py")
T.write_text(pathlib.Path("_tmp_content.py").read_text())
print("wrote", T)