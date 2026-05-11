#!/usr/bin/env python3
p = "services/gateway/app/tools_bus.py"
with open(p) as f:
    lines = f.readlines()
s = next(i for i, l in enumerate(lines) if "def _run_coroutine_sync" in l)
e = next(i for i in range(s+1, len(lines)) if lines[i].startswith("def "))
r = lines[s:e]
print(f"Replacing lines {s+1}-{e}")
for i, l in enumerate(r):
    print(f"  {s+i+1}: {l.rstrip()}")
print("---")
print("Need to replace with simpler version")