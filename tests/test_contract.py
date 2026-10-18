"""The contract check (PLAN 2026-10-19, F3), MCP half.

* No direct HTTP outside the SDK: nothing under meshbook_mcp/ imports an
  HTTP or socket library, opens a URL, or calls the SDK's raw escape hatch.
* The SDK calls this server makes (tests/contract/mcp-calls.json, kept
  honest by the conftest guard) all exist in the pinned OpenAPI snapshot,
  checked with the SDK's own checker (vendored as scripts/contract_check.py).
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "meshbook_mcp"
SPEC = ROOT / "tests" / "contract" / "openapi.json"
CALLS = ROOT / "tests" / "contract" / "mcp-calls.json"

FORBIDDEN_MODULES = {"urllib", "http", "requests", "httpx", "aiohttp", "socket", "ssl",
                     "urllib3", "pycurl"}
FORBIDDEN_CALLS = {"urlopen", "request", "_send", "_call", "_list", "_download"}


def _checker():
    spec = importlib.util.spec_from_file_location("contract_check", ROOT / "scripts" /
                                                  "contract_check.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_no_http_library_is_imported_by_the_package():
    offenders = []
    for py in PKG.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in FORBIDDEN_MODULES:
                    offenders.append(f"{py.name}:{node.lineno} imports {n}")
    assert offenders == []


def test_no_raw_or_private_transport_call():
    """Every request goes through a public SDK namespace method. The one raw
    download (download_raw) is public SDK API on a registry-rendered path."""
    offenders = []
    for py in PKG.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_CALLS:
                    offenders.append(f"{py.name}:{node.lineno} .{node.func.attr}(")
    assert offenders == []


def test_scan_finds_no_hand_built_api_call():
    mod = _checker()
    assert mod.from_scan(str(PKG), "/api") == []


def test_every_mcp_call_exists_in_the_pinned_spec(capsys):
    mod = _checker()
    rc = mod.main(["--spec", str(SPEC), "--calls", str(CALLS)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "missing: 0" in out


def test_mcp_calls_are_sdk_registry_entries():
    from meshbook._endpoints import ENDPOINTS

    registry = {(e.method, e.path) for e in ENDPOINTS.values()}
    listed = {(e["method"], e["path"]) for e in json.loads(CALLS.read_text(encoding="utf-8"))}
    assert listed and listed <= registry, listed - registry


def test_pinned_spec_is_byte_exact():
    source = json.loads((SPEC.parent / "openapi.source.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(SPEC.read_bytes()).hexdigest() == source["sha256"]


def test_checker_fails_on_a_missing_route(tmp_path, capsys):
    """The check can fail (a contract check that cannot go red proves nothing)."""
    bogus = tmp_path / "calls.json"
    bogus.write_text(json.dumps([{"method": "GET", "path": "/api/no-such-route"}]),
                     encoding="utf-8")
    assert _checker().main(["--spec", str(SPEC), "--calls", str(bogus)]) == 1
    capsys.readouterr()
