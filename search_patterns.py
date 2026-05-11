#!/usr/bin/env python3
import os, re
root = '/var/lib/gateway/data/coding/workspaces/code_6bd2370b2c3e/repo'
pats = {
    'exec(': r'\bexec\s*\(',
    'eval(': r'\beval\s*\(',
    'asyncio.run(': r'asyncio\.run\s*\(',
    'subprocess': r'subprocess',
    'os.system(': r'os\.system\s*\(',
    'bare except:': r'except\s*:',
    'except Exception:': r'except\s+Exception\s*:',
    'TODO/FIXME/HACK/BUG': r'\bTODO\b|\bFIXME\b|\bHACK\b|\bBUG\b',
}
for name, regex in pats.items():
    pat = re.compile(regex)
    hits = []
    for dp, dns, fns in os.walk(root):
        if '.git' in dp:
            continue
        for fn in fns:
            if not fn.endswith('.py'):
                continue
            fp = os.path.join(dp, fn)
            rel = os.path.relpath(fp, root)
            try:
                with open(fp, 'r', errors='replace') as f:
                    for i, line in enumerate(f, 1):
                        if pat.search(line):
                            hits.append(f"{rel}:{i}: {line.strip()}")
            except Exception:
                pass
    if hits:
        print(f"\n=== {name} ({len(hits)}) ===")
        for h in hits[:30]:
            print(h)
        if len(hits) > 30:
            print(f"... and {len(hits)-30} more")