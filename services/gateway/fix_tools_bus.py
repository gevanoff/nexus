#!/usr/bin/env python3
p = "services/gateway/app/tools_bus.py"
with open(p) as f:
    content = f.read()
old = content[content.find("def _run_coroutine_sync"):content.find("\ndef _embed_text_sync")]
new = """def _run_coroutine_sync(coro: Any) -> Any:
    # If no event loop is running, use asyncio.run() directly.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # run_coroutine_threadsafe + result() deadlocks if called from the
    # event loop thread.  Always use a separate thread with a fresh
    # event loop to avoid this.
    result: Any = None
    error: Exception | None = None

    def runner() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(coro)
        except Exception as exc:
            error = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error is not None:
        raise error
    return result

"""
content = content.replace(old, new)
with open(p, "w") as f:
    f.write(content)
print("OK")