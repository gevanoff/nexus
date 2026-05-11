#!/usr/bin/env python3
p = "services/gateway/app/tools_bus.py"
with open(p) as f:
    lines = f.readlines()
s = next(i for i, l in enumerate(lines) if "def _run_coroutine_sync" in l)
e = next(i for i in range(s+1, len(lines)) if lines[i].startswith("def "))
r = ["def _run_coroutine_sync(coro: Any) -> Any:\n",
"    try:\n",
"        loop = asyncio.get_running_loop()\n",
"    except RuntimeError:\n",
"        return asyncio.run(coro)\n",
"\n",
"    # run_coroutine_threadsafe + result() deadlocks if called from the\n",
"    # event loop thread.  Always use a separate thread with a fresh\n",
"    # event loop to avoid this.\n",
"    result: Any = None\n",
"    error: Exception | None = None\n",
"\n",
"    def runner() -> None:\n",
"        nonlocal result, error\n",
"        try:\n",
"            result = asyncio.run(coro)\n",
"        except Exception as exc:\n",
"            error = exc\n",
"\n",
"    thread = threading.Thread(target=runner, daemon=True)\n",
"    thread.start()\n",
"    thread.join()\n",
"    if error is not None:\n",
"        raise error\n",
"    return result\n",
"\n",
]
lines[s:e] = r
with open(p, "w") as f:
    f.writelines(lines)
print(f"Replaced lines {s+1}-{e}")