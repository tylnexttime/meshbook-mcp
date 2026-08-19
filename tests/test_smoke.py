"""Smoke tests for meshbook-mcp — registry wiring + helpers + wire shapes.

No live HTTP: network-touching paths are exercised through monkeypatched
`_api_call`. For end-to-end coverage, `mesh login` a token and drive the
server from a real MCP client.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from meshbook_mcp import __version__, server

# ─── registry ──────────────────────────────────────────────────────────


EXPECTED_TOOLS = {
    "list_my_meshes", "set_active_mesh",
    "list_contacts", "create_contact",
    "list_leads", "create_lead", "move_lead_stage",
    "list_my_tasks", "mark_task_done",
    "post_chat", "read_thread",
    "post_channel", "read_channel", "list_channels",
    "list_channel_members", "add_channel_member", "remove_channel_member",
    "search_chat",
    "enroll_agent_credential", "agent_credential_status", "revoke_agent_credential",
    "list_unread_notifications",
    "attach_file", "download_attachment",
    "export_mesh", "export_status",
}

EXPECTED_RESOURCES = {
    "meshbook://my-meshes",
    "meshbook://active-mesh",
    "meshbook://my-tasks-today",
    "meshbook://notifications",
}

EXPECTED_PROMPTS = {"triage_leads", "summarise_mesh_week", "whats_new"}


def test_version_constant():
    assert __version__ == server.VERSION
    parts = __version__.split(".")
    assert len(parts) >= 2 and parts[0].isdigit()


def test_all_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names, EXPECTED_TOOLS - names


def test_every_tool_has_description():
    for t in asyncio.run(server.mcp.list_tools()):
        assert t.description and t.description.strip(), f"tool {t.name} lacks a docstring"


def test_all_resources_registered():
    resources = asyncio.run(server.mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    assert EXPECTED_RESOURCES <= uris, EXPECTED_RESOURCES - uris


def test_all_prompts_registered():
    prompts = asyncio.run(server.mcp.list_prompts())
    names = {p.name for p in prompts}
    assert EXPECTED_PROMPTS <= names, EXPECTED_PROMPTS - names


def test_user_agent_is_real():
    """Cloudflare blocks default python UAs — ours must be branded."""
    assert server.USER_AGENT == f"meshbook-mcp/{__version__}"
    assert "python" not in server.USER_AGENT.lower()


# ─── envelope helpers (same contract as meshbook-cli) ──────────────────


def test_items_envelope_shapes():
    assert server._items([1, 2, 3]) == [1, 2, 3]
    assert server._items({"data": [1, 2]}) == [1, 2]
    assert server._items({"data": {"items": [9], "total": 1}}) == [9]
    assert server._items({"data": None}) == []
    assert server._items(None) == []


def test_data_envelope():
    assert server._data({"ok": True, "data": {"id": "x"}}) == {"id": "x"}
    assert server._data({"id": "y"}) == {"id": "y"}


# ─── config round trip ─────────────────────────────────────────────────


def test_config_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path / ".meshbook")
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / ".meshbook" / "config")
    assert server.load_config() == {}
    server.save_config({"token": "mb_token_test", "active_mesh_id": "m-1"})
    assert server.load_config()["active_mesh_id"] == "m-1"


def test_corrupt_config_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path / ".meshbook")
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / ".meshbook" / "config")
    server.CONFIG_DIR.mkdir()
    server.CONFIG_PATH.write_text("{ not valid json")
    assert server.load_config() == {}


# ─── wire shapes (monkeypatched — no live HTTP) ────────────────────────


MESH_ID = "11111111-1111-1111-1111-111111111111"


def _patch_cfg(monkeypatch, cfg):
    monkeypatch.setattr(server, "load_config", lambda: dict(cfg))


def test_set_active_mesh_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path / ".meshbook")
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / ".meshbook" / "config")
    monkeypatch.setattr(
        server, "_api_call",
        lambda m, p, **kw: {"data": [{"id": MESH_ID, "name": "Tyl Mesh"}]},
    )
    out = server.set_active_mesh("tyl mesh")  # case-insensitive name match
    assert out == {"activeMeshId": MESH_ID, "name": "Tyl Mesh"}
    assert server.load_config()["active_mesh_id"] == MESH_ID


def test_post_chat_wire_shape(monkeypatch):
    captured = {}

    def fake(method, path, *, cfg, body=None, **kw):
        captured.update(method=method, path=path, body=body)
        return {"data": {"id": "msg-1"}}

    monkeypatch.setattr(server, "_api_call", fake)
    _patch_cfg(monkeypatch, {"token": "t", "active_mesh_id": MESH_ID})
    out = server.post_chat("hello", reply_to="msg-0")
    assert out == {"posted": "msg-1"}
    assert captured["method"] == "POST"
    assert captured["path"] == f"/api/entities/mesh/{MESH_ID}/chat"
    assert captured["body"] == {"bodyMd": "hello", "parentMessageId": "msg-0"}


def test_post_chat_requires_active_mesh(monkeypatch):
    _patch_cfg(monkeypatch, {"token": "t"})
    with pytest.raises(server.MeshbookError, match="no_active_mesh"):
        server.post_chat("hello")


def test_mark_task_done_wire_shape(monkeypatch):
    captured = {}

    def fake(method, path, *, cfg, body=None, **kw):
        captured.update(method=method, path=path, body=body)
        return {"data": {"id": "t-1", "title": "x", "status": "Done"}}

    monkeypatch.setattr(server, "_api_call", fake)
    _patch_cfg(monkeypatch, {"token": "t", "active_mesh_id": MESH_ID})
    out = server.mark_task_done("t-1")
    assert out["status"] == "Done"
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/api/tasks/t-1"
    assert captured["body"] == {"status": "Done"}


def test_create_lead_defaults_pipeline_and_stage(monkeypatch):
    """create_lead must pick the default pipeline's first stage — the
    server requires pipelineId + stageId (LeadIn)."""
    captured = {}

    def fake(method, path, *, cfg, body=None, params=None, **kw):
        if path == "/api/pipelines":
            return {"data": [
                {"id": "p-2", "name": "Side", "isDefault": False,
                 "stages": [{"id": "s-9", "name": "Weird"}]},
                {"id": "p-1", "name": "Sales", "isDefault": True,
                 "stages": [{"id": "s-1", "name": "New"}, {"id": "s-2", "name": "Won"}]},
            ]}
        captured.update(method=method, path=path, body=body)
        return {"data": {"id": "lead-1", "title": "Big deal", "stageName": "New"}}

    monkeypatch.setattr(server, "_api_call", fake)
    _patch_cfg(monkeypatch, {"token": "t", "active_mesh_id": MESH_ID})
    out = server.create_lead("Big deal", value=1000.0)
    assert out["id"] == "lead-1"
    assert out["pipeline"] == "Sales"
    assert captured["path"] == "/api/leads"
    assert captured["body"] == {
        "title": "Big deal", "pipelineId": "p-1", "stageId": "s-1", "valueAmount": 1000.0,
    }


def test_move_lead_stage_resolves_name(monkeypatch):
    captured = {}

    def fake(method, path, *, cfg, body=None, params=None, **kw):
        if path == "/api/pipelines":
            return {"data": [{"id": "p-1", "name": "Sales", "isDefault": True,
                              "stages": [{"id": "s-1", "name": "New"},
                                         {"id": "s-2", "name": "Won"}]}]}
        captured.update(path=path, body=body)
        return {"data": {"id": "lead-1", "stageName": "Won"}}

    monkeypatch.setattr(server, "_api_call", fake)
    _patch_cfg(monkeypatch, {"token": "t", "active_mesh_id": MESH_ID})
    out = server.move_lead_stage("lead-1", "won")
    assert out["stage"] == "Won"
    assert captured["path"] == "/api/leads/lead-1/move-stage"
    assert captured["body"] == {"stageId": "s-2"}


def test_read_channel_resolves_hash_name(monkeypatch):
    calls = []

    def fake(method, path, *, cfg, body=None, params=None, **kw):
        calls.append(path)
        if path.endswith("/channels"):
            return {"data": [{"id": "ch-1", "name": "bugs"}]}
        return {"data": {"items": [
            {"id": "m-2", "bodyMd": "second", "createdAt": "2026-07-12T01:00:00Z",
             "author": {"displayName": "Rook"}},
            {"id": "m-1", "bodyMd": "first", "createdAt": "2026-07-12T00:00:00Z",
             "author": {"displayName": "Chris"}},
        ], "total": 2}}

    monkeypatch.setattr(server, "_api_call", fake)
    _patch_cfg(monkeypatch, {"token": "t", "active_mesh_id": MESH_ID})
    out = server.read_channel("#BUGS", limit=2)
    assert "/api/channels/ch-1/messages" in calls
    # Oldest first
    assert [m["body"] for m in out] == ["first", "second"]


def test_list_unread_notifications_filters_read(monkeypatch):
    def fake(method, path, *, cfg, **kw):
        return {"data": [
            {"id": "n-1", "kind": "mention", "summary": "hi", "readAt": None,
             "createdAt": "2026-07-12T00:00:00Z"},
            {"id": "n-2", "kind": "invite", "summary": "old", "readAt": "2026-07-11T00:00:00Z",
             "createdAt": "2026-07-11T00:00:00Z"},
        ]}

    monkeypatch.setattr(server, "_api_call", fake)
    _patch_cfg(monkeypatch, {"token": "t"})
    out = server.list_unread_notifications()
    assert [n["id"] for n in out] == ["n-1"]


def test_attach_file_posts_base64_json(monkeypatch, tmp_path):
    import base64 as b64

    f = tmp_path / "pic.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    captured = {}

    def fake(method, path, *, cfg, body=None, **kw):
        captured.update(method=method, path=path, body=body)
        return {"data": {"id": "att-1", "filename": "pic.png",
                         "byteSize": 12, "mimeType": "image/png"}}

    monkeypatch.setattr(server, "_api_call", fake)
    _patch_cfg(monkeypatch, {"token": "t", "active_mesh_id": MESH_ID})
    out = server.attach_file("lead", "e-1", str(f))
    assert out["id"] == "att-1"
    assert captured["path"] == "/api/entities/lead/e-1/attachments/json"
    assert captured["body"]["mimeType"] == "image/png"
    assert b64.b64decode(captured["body"]["base64Bytes"]).startswith(b"\x89PNG")


def test_download_attachment_writes_bytes(monkeypatch, tmp_path):
    def fake_download(path, *, cfg):
        assert path == "/api/entity-attachments/att-9/download"
        return b"hello-bytes", "served-name.bin"

    monkeypatch.setattr(server, "_api_download", fake_download)
    _patch_cfg(monkeypatch, {"token": "t"})
    outdir = tmp_path / "dl"
    outdir.mkdir()
    out = server.download_attachment("att-9", str(outdir))
    assert out["bytes"] == 11
    assert (outdir / "served-name.bin").read_bytes() == b"hello-bytes"


def test_export_mesh_overrides_active_mesh_header(monkeypatch):
    """POST /api/meshes/{id}/export 400s unless X-Active-Mesh-Id matches
    the target — export_mesh must send the override so callers don't have
    to flip their active mesh first."""
    captured = {}

    def fake(method, path, *, cfg, body=None, params=None, mesh_override=None, **kw):
        if path == "/api/meshes":
            return {"data": [{"id": MESH_ID, "name": "Tyl Mesh"}]}
        captured.update(method=method, path=path, mesh_override=mesh_override)
        return {"data": {"id": "exp-1", "status": "pending"}}

    monkeypatch.setattr(server, "_api_call", fake)
    _patch_cfg(monkeypatch, {"token": "t", "active_mesh_id": "some-other-mesh"})
    out = server.export_mesh("Tyl Mesh")
    assert out["status"] == "pending"
    assert out["mesh"] == "Tyl Mesh"
    assert captured["path"] == f"/api/meshes/{MESH_ID}/export"
    assert captured["mesh_override"] == MESH_ID


def test_not_signed_in_is_clean_error(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, {})
    with pytest.raises(server.MeshbookError) as exc:
        server.list_contacts()
    msg = str(exc.value)
    assert "not_signed_in" in msg and "mesh login" in msg
    assert "Traceback" not in msg


def test_prompts_render_text():
    for fn in (server.triage_leads, server.summarise_mesh_week, server.whats_new):
        text = fn()
        assert isinstance(text, str) and len(text) > 50


def test_tool_outputs_are_json_serialisable(monkeypatch):
    monkeypatch.setattr(
        server, "_api_call",
        lambda m, p, **kw: {"data": [{"id": MESH_ID, "name": "Tyl Mesh", "memberRole": "admin"}]},
    )
    _patch_cfg(monkeypatch, {"token": "t", "active_mesh_id": MESH_ID})
    json.dumps(server.list_my_meshes())


def test_version_matches_pyproject():
    """__version__ must equal the version pyproject actually ships.

    Added 2026-08-20, after Wren reported meshbook-mcp 0.5.0 self-reporting
    0.4.0 in both __version__ and the MCP serverInfo banner. pyproject said
    0.5.0; __init__ said 0.4.0; the package published as 0.5.0 and told every
    client it was 0.4.0.

    The existing assertion `__version__ == server.VERSION` did NOT catch it and
    never could: server.VERSION is ASSIGNED from __version__ (server.py:42), so
    the test compares a value with itself. It reads like a version guard and is
    a tautology. This one crosses the boundary to the file that decides what
    actually gets shipped, which is the only place the truth lives.
    """
    import pathlib
    import re
    py = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = py.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "no version in pyproject.toml"
    assert __version__ == m.group(1), (
        f"__version__ is {__version__} but pyproject ships {m.group(1)} - "
        "clients will be told the wrong version"
    )
