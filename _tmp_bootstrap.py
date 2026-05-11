import base64, pathlib
pathlib.Path("_tmp_gen.py").write_text(base64.b64decode(open("_tmp_b64.txt").read()).decode())