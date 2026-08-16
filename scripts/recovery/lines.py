"""Print a line range of a document in a batch-06 workspace.

    uv run python lines.py <workspace> <url> <from> <to>
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "scripts" / "emergency"))

import b6  # noqa: E402


def main():
    ws, url, a, b = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    b6.use(ws)
    meta, text, pages = b6.text(url)
    lines = text.split("\n")
    print(f"[{meta['status']} pages={pages} chars={len(text)} lines={len(lines)}] {meta['final_url']}")
    for i in range(max(0, a), min(len(lines), b)):
        print(f"{i:>5}| {lines[i]}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
