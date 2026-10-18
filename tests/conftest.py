"""Suite-wide guards for meshbook-mcp.

1. **Isolation.** No test may read or write a real ``~/.meshbook``:
   MESHBOOK_CONFIG_DIR points at a fresh temp dir, the other meshbook env
   vars are cleared, and the server's cached SDK client is dropped around
   every test.

2. **No network, ever.** The ``api`` fixture replaces the SDK's single
   transport seam (``meshbook.client._urlopen``) with a router; an
   unrouted request fails the test.

3. **Every request is a known SDK call.** ``meshbook.client`` reports every
   request it sends to ``_on_request``. Here that hook asserts the (method,
   path) matches an entry in the SDK's endpoint registry AND is listed in
   ``tests/contract/mcp-calls.json``, the file CI hands to the contract
   check. A new SDK call the MCP starts making therefore fails here until
   it is added to that file (and so checked against the pinned OpenAPI).
   Set ``MESHBOOK_MCP_RECORD_CALLS=<file>`` to append every call seen to a
   file instead (how mcp-calls.json is refreshed).
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from pathlib import Path

import meshbook.client as mc
import pytest
from meshbook._endpoints import ENDPOINTS

from meshbook_mcp import _core
from tests import _fakes

CALLS_FILE = Path(__file__).parent / "contract" / "mcp-calls.json"

_TEMPLATES = [
    (ep.method, ep.path, re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(ep.path)) + "$"))
    for ep in ENDPOINTS.values()
]


def _template(method: str, url: str) -> str | None:
    path = urllib.parse.urlsplit(url).path
    for m, tpl, rx in _TEMPLATES:
        if m == method and rx.match(path):
            return tpl
    return None


def _listed() -> set[tuple[str, str]]:
    data = json.loads(CALLS_FILE.read_text(encoding="utf-8"))
    return {(e["method"], e["path"]) for e in data}


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("MESHBOOK_CONFIG_DIR", str(tmp_path_factory.mktemp("meshbook-config")))
    for var in ("MESHBOOK_TOKEN", "MESHBOOK_BASE", "MESHBOOK_MESH", _core.ALLOW_ANY_PATH_ENV):
        monkeypatch.delenv(var, raising=False)
    _core.reset_client()
    yield
    _core.reset_client()


@pytest.fixture(autouse=True)
def _call_guard(request, monkeypatch):
    record = os.environ.get("MESHBOOK_MCP_RECORD_CALLS")
    listed = _listed()

    def hook(method: str, url: str) -> None:
        tpl = _template(method, url)
        if tpl is None:
            # The SDK's token mint goes to the issuer's own host, outside /api.
            if not urllib.parse.urlsplit(url).path.startswith("/api/"):
                return
            raise AssertionError(f"{method} {url} is not an SDK registry call")
        if record:
            with open(record, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"method": method, "path": tpl}) + "\n")
            return
        assert (method, tpl) in listed, (
            f"{method} {tpl} is not in tests/contract/mcp-calls.json; add it so the "
            "contract check covers it"
        )

    monkeypatch.setattr(mc, "_on_request", hook)
    yield


def write_config(**cfg) -> Path:
    from meshbook import config as mb_config

    path = mb_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


@pytest.fixture()
def router(monkeypatch) -> _fakes.Router:
    """A bare router (no config written)."""
    r = _fakes.Router()
    monkeypatch.setattr(mc, "_urlopen", r)
    sleeps: list[float] = []
    monkeypatch.setattr(mc, "_sleep", sleeps.append)
    r.sleeps = sleeps  # type: ignore[attr-defined]
    return r


@pytest.fixture()
def api(router) -> _fakes.Router:
    """A signed-in bench: a bearer token and an active mesh in the config,
    plus the mesh list routed (name resolution needs it)."""
    write_config(token="mb_token_test", active_mesh_id=_fakes.MESH)
    router.add(
        "GET", "/api/meshes",
        _fakes.meshes_payload((_fakes.MESH, "Tyl Mesh"), (_fakes.MESH2, "Second Mesh")),
    )
    return router


@pytest.fixture()
def home_and_cwd(tmp_path, monkeypatch):
    """Make tmp_path both the home directory and the cwd (the file fence's roots)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(home)
    return home
