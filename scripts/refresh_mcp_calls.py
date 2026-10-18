#!/usr/bin/env python3
"""refresh_mcp_calls — regenerate tests/contract/mcp-calls.json.

Runs the test-suite with MESHBOOK_MCP_RECORD_CALLS set, so tests/conftest.py
appends every SDK request the MCP tools make (as the SDK registry's
(method, path-template)), then writes the sorted, de-duplicated set.

That file is what CI hands to the vendored contract check:

    python scripts/contract_check.py --spec tests/contract/openapi.json \
        --calls tests/contract/mcp-calls.json

Review the diff before committing: a new line is a new server route this
server depends on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "contract" / "mcp-calls.json"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "calls.jsonl"
        env = dict(os.environ, MESHBOOK_MCP_RECORD_CALLS=str(log))
        rc = subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                             cwd=ROOT, env=env)
        if rc != 0:
            print("refresh_mcp_calls: the test-suite failed; nothing written", file=sys.stderr)
            return rc
        seen = set()
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                seen.add((e["method"], e["path"]))
    rows = [{"method": m, "path": p} for m, p in sorted(seen, key=lambda t: (t[1], t[0]))]
    OUT.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"refresh_mcp_calls: {len(rows)} calls -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
