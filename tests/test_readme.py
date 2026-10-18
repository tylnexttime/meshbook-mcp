"""The README's tool table must match FastMCP's live tool listing.

0.7's README listed about 18 of its 29 tools and still called the
dependency "the official mcp SDK". The table is generated now
(scripts/gen_tool_table.py --write); this fails when it goes stale.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib

from meshbook_mcp import server

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _gen():
    spec = importlib.util.spec_from_file_location("gen_tool_table", ROOT / "scripts" /
                                                  "gen_tool_table.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_readme_tool_table_matches_the_live_listing():
    gen = _gen()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert gen.splice(readme, gen.render()) == readme, (
        "README tool table is stale: run python scripts/gen_tool_table.py --write"
    )


def test_readme_names_every_tool():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    missing = [n for n in names if f"| `{n}` |" not in readme]
    assert missing == []


def test_readme_is_honest_about_dependencies_and_transport():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "official [`mcp`]" not in readme and "official `mcp`" not in readme
    assert "fastmcp" in readme and "meshbook-sdk" in readme
    assert "stdio only" in readme
