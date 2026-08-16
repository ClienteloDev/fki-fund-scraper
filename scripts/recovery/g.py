"""Grep one document in a batch-06 workspace.

    uv run python g.py <workspace> <url> [pattern ...] [--ctx N] [--limit N]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "scripts" / "emergency"))

import b6  # noqa: E402


def main():
    argv = sys.argv[1:]
    ctx = 3
    limit = 6000
    if "--ctx" in argv:
        i = argv.index("--ctx")
        ctx = int(argv[i + 1])
        del argv[i:i + 2]
    if "--limit" in argv:
        i = argv.index("--limit")
        limit = int(argv[i + 1])
        del argv[i:i + 2]
    ws, url, patterns = argv[0], argv[1], argv[2:]
    b6.use(ws)
    where = b6.served_by(url, ws)
    meta, text, pages = b6.text(url)
    print(f"[{meta['status']} pages={pages} chars={len(text)} cache={where} "
          f"quality={b6.probe_quality(text, pages)}] {meta['final_url']}")
    if not patterns:
        print(text[:limit])
        return
    lines = text.split("\n")
    hits: set[int] = set()
    for i, line in enumerate(lines):
        for p in patterns:
            if re.search(p, line, re.I):
                hits.update(range(max(0, i - ctx), min(len(lines), i + ctx + 1)))
    prev = -2
    for i in sorted(hits):
        if i != prev + 1:
            print("  ...")
        print(f"{i:>5}| {lines[i]}")
        prev = i


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
