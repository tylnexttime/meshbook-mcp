"""Smoke tests for meshbook-mcp: registry wiring, envelopes and wire shapes.

The 29 tests here are the 0.7 baseline, ported in 0.8.0 from a
monkeypatched ``_api_call`` (gone: every request now goes through the
meshbook SDK) to a fake transport under the SDK. Each keeps its original
name and asserts the same wire contract. Where 0.8.0 changed a behaviour on
purpose, the test says so.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import pathlib
import re

import pytest

from meshbook_mcp import __version__, server
from tests._fakes import CH1, MESH, MESH2, Err, Resp, ok, ok_list
from tests.conftest import write_config

# ─── registry ──────────────────────────────────────────────────────────

#: The 0.7 tool set; every one must survive (renames break existing prompts).
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


def test_user_agent_is_real(api):
    """Cloudflare blocks default python UAs — ours must be branded, and it
    must actually ride on the wire (0.8.0: through the SDK's User-Agent)."""
    assert server.USER_AGENT == f"meshbook-mcp/{__version__}"
    assert "python" not in server.USER_AGENT.lower()
    server.list_my_meshes()
    ua = api.last.get_header("User-agent")
    assert ua.startswith(server.USER_AGENT + " meshbook-sdk/"), ua


# ─── envelope handling (the SDK's now; asserted end to end) ────────────


def test_items_envelope_shapes(api):
    """All three list shapes the API emits reach the model as one envelope."""
    api.add("GET", f"/api/meshes/{MESH}/channels",
            [{"id": "c-1", "name": "a"}],                      # bare list
            {"data": [{"id": "c-1", "name": "a"}]},            # {data: [...]}
            ok_list([{"id": "c-1", "name": "a"}]),             # ok_list
            {"data": None})                                    # nothing
    for _ in range(3):
        out = server.list_channels()
        assert out["count"] == 1 and out["items"][0]["name"] == "a"
    assert server.list_channels()["count"] == 0


def test_data_envelope(api):
    api.add("POST", "/api/contacts", ok({"id": "x", "displayName": "Ada L"}))
    assert server.create_contact("Ada", "L")["id"] == "x"


# ─── config (the SDK owns it since 0.8.0) ──────────────────────────────


def test_config_round_trip(api):
    """set_active_mesh is the one write, and it goes through the SDK's
    merge: the token and keys this server does not know survive it."""
    path = write_config(token="mb_token_test", active_mesh_id=MESH, favourite="tea")
    api.add("POST", "/api/meshes/active", ok({}))
    server.set_active_mesh("Second Mesh")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["active_mesh_id"] == MESH2
    assert cfg["token"] == "mb_token_test"
    assert cfg["favourite"] == "tea"
    assert cfg["schema_version"] == 1


def test_corrupt_config_returns_empty(router):
    """A corrupt config reads as empty: a clean not-signed-in error, never a
    JSONDecodeError traceback."""
    from meshbook import config as mb_config

    p = mb_config.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(server.MeshbookError) as exc:
        server.list_contacts()
    assert exc.value.code == "not_authenticated"


# ─── wire shapes ───────────────────────────────────────────────────────


def test_set_active_mesh_persists(api):
    api.add("POST", "/api/meshes/active", ok({}))
    out = server.set_active_mesh("tyl mesh")  # case-insensitive name match
    assert out == {"activeMeshId": MESH, "name": "Tyl Mesh"}
    assert api.body(api.calls("POST", "/api/meshes/active")[0]) == {"meshId": MESH}
    from meshbook import config as mb_config

    assert mb_config.load_config()["active_mesh_id"] == MESH


def test_post_chat_wire_shape(api):
    api.add("POST", f"/api/entities/mesh/{MESH}/chat", ok({"id": "msg-1"}))
    out = server.post_chat("hello", reply_to="msg-0")
    assert out == {"posted": "msg-1"}
    assert api.last.get_method() == "POST"
    assert api.body() == {"bodyMd": "hello", "parentMessageId": "msg-0"}


def test_post_chat_requires_active_mesh(router):
    write_config(token="t")
    with pytest.raises(server.MeshbookError) as exc:
        server.post_chat("hello")
    assert exc.value.code == "no_active_mesh"
    assert "mesh=" in exc.value.message  # the hint names the per-call override


def test_mark_task_done_wire_shape(api):
    api.add("PATCH", "/api/tasks/t-1", ok({"id": "t-1", "title": "x", "status": "Done"}))
    out = server.mark_task_done("t-1")
    assert out["status"] == "Done"
    assert api.last.get_method() == "PATCH"
    assert api.body() == {"status": "Done"}


PIPELINES = ok_list([
    {"id": "p-2", "name": "Side", "isDefault": False,
     "stages": [{"id": "s-9", "name": "Weird"}]},
    {"id": "p-1", "name": "Sales", "isDefault": True,
     "stages": [{"id": "s-1", "name": "New"}, {"id": "s-2", "name": "Won"}]},
])


def test_create_lead_defaults_pipeline_and_stage(api):
    """create_lead must pick the default pipeline's first stage — the server
    requires pipelineId + stageId (LeadIn)."""
    api.add("GET", "/api/pipelines", PIPELINES)
    api.add("POST", "/api/leads", ok({"id": "lead-1", "title": "Big deal", "stageName": "New"}))
    out = server.create_lead("Big deal", value=1000.0)
    assert out["id"] == "lead-1"
    assert out["pipeline"] == "Sales"
    assert api.body() == {
        "title": "Big deal", "pipelineId": "p-1", "stageId": "s-1", "valueAmount": 1000.0,
    }


def test_move_lead_stage_resolves_name(api):
    api.add("GET", "/api/pipelines", PIPELINES)
    api.add("POST", "/api/leads/lead-1/move-stage", ok({"id": "lead-1", "stageName": "Won"}))
    out = server.move_lead_stage("lead-1", "won")
    assert out["stage"] == "Won"
    assert api.body() == {"stageId": "s-2"}


def test_read_channel_resolves_hash_name(api):
    api.add("GET", f"/api/meshes/{MESH}/channels", [{"id": CH1, "name": "bugs"}])
    api.add("GET", f"/api/channels/{CH1}", ok({"id": CH1, "name": "bugs"}))
    api.add("GET", f"/api/channels/{CH1}/messages", ok_list([
        {"id": "m-2", "bodyMd": "second", "createdAt": "2026-07-12T01:00:00Z",
         "author": {"displayName": "Rook"}},
        {"id": "m-1", "bodyMd": "first", "createdAt": "2026-07-12T00:00:00Z",
         "author": {"displayName": "Chris"}},
    ]))
    out = server.read_channel("#BUGS", limit=2)
    assert api.query(api.calls("GET", f"/api/channels/{CH1}/messages")[0])["limit"] == "2"
    # Oldest first
    assert out["count"] == 2
    assert [m["body"] for m in out["items"]] == ["first", "second"]
    assert out["channel"] == "bugs"


def test_list_unread_notifications_filters_read(api):
    """0.8.0: unread is filtered SERVER-side (unreadOnly=true), so `total`
    is the true unread count. 0.7 filtered one page client-side and lost
    any unread notification past it."""
    api.add("GET", "/api/notifications", ok_list([
        {"id": "n-1", "kind": "mention", "summary": "hi", "readAt": None,
         "createdAt": "2026-07-12T00:00:00Z"},
    ], total=73, limit=50))
    out = server.list_unread_notifications()
    assert api.query()["unreadOnly"] == "true"
    assert out["count"] == 1
    assert out["total"] == 73
    assert [n["id"] for n in out["items"]] == ["n-1"]


def test_attach_file_posts_base64_json(api, home_and_cwd):
    f = home_and_cwd / "pic.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    api.add("POST", "/api/entities/lead/e-1/attachments/json",
            ok({"id": "att-1", "filename": "pic.png", "byteSize": 12, "mimeType": "image/png"}))
    out = server.attach_file("lead", "e-1", str(f))
    assert out["id"] == "att-1"
    body = api.body()
    assert body["mimeType"] == "image/png"
    assert base64.b64decode(body["base64Bytes"]).startswith(b"\x89PNG")


def test_download_attachment_writes_bytes(api, home_and_cwd):
    api.add("GET", "/api/entity-attachments/att-9/download",
            Resp(b"hello-bytes", {"Content-Disposition": 'attachment; filename="served-name.bin"'}))
    outdir = home_and_cwd / "dl"
    outdir.mkdir()
    out = server.download_attachment("att-9", str(outdir))
    assert out["bytes"] == 11
    assert (outdir / "served-name.bin").read_bytes() == b"hello-bytes"


def test_export_mesh_overrides_active_mesh_header(api):
    """POST /api/meshes/{id}/export 400s unless X-Active-Mesh-Id matches the
    target — export_mesh must send it so callers needn't flip their mesh."""
    write_config(token="mb_token_test", active_mesh_id=MESH2)
    api.add("POST", f"/api/meshes/{MESH}/export", ok({"id": "exp-1", "status": "pending"}))
    out = server.export_mesh("Tyl Mesh")
    assert out["status"] == "pending"
    assert out["mesh"] == "Tyl Mesh"
    assert api.last.get_header("X-active-mesh-id") == MESH


def test_not_signed_in_is_clean_error(router):
    """0.8.0: the code is the SDK's `not_authenticated` (0.7 said
    `not_signed_in`); the setup hint is the MCP's own."""
    with pytest.raises(server.MeshbookError) as exc:
        server.list_contacts()
    msg = str(exc.value)
    assert "not_authenticated" in msg and "mesh login" in msg
    assert "Traceback" not in msg


def test_prompts_render_text():
    for fn in (server.triage_leads, server.summarise_mesh_week, server.whats_new):
        text = fn()
        assert isinstance(text, str) and len(text) > 50


def test_tool_outputs_are_json_serialisable(api):
    json.dumps(server.list_my_meshes())


def test_version_matches_pyproject():
    """__version__ must equal the version pyproject actually ships.

    Added 2026-08-20, after Wren reported meshbook-mcp 0.5.0 self-reporting
    0.4.0. The earlier assertion `__version__ == server.VERSION` compared a
    value with itself. This one crosses to the file that decides what gets
    shipped. Since 0.8.0 __version__ is read from package metadata (no
    literal left to go stale), so this also catches stale install metadata.
    """
    py = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = py.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "no version in pyproject.toml"
    assert __version__ == m.group(1), (
        f"__version__ is {__version__} but pyproject ships {m.group(1)} - "
        "clients will be told the wrong version (reinstall: pip install -e .)"
    )


def test_listing_makes_zero_visible():
    """A zero result must be something you can SEE (Wren, report B6)."""
    empty = server._listing([])
    assert empty["count"] == 0
    assert empty["items"] == []
    assert str(empty).strip() not in ("", "[]"), "zero must render as something"

    full = server._listing([{"a": 1}, {"a": 2}], mesh="X")
    assert full["count"] == 2
    assert full["mesh"] == "X", "extra context must survive the envelope"


def test_no_tool_returns_a_bare_list():
    """Structural guard: the B6 fix holds for the whole CLASS of tools.

    0.7's version regex-scanned the source for `@mcp.tool()\\ndef ...`; since
    0.8.0 tools register through a wrapper, so that regex would match nothing
    and pass forever. This inspects every REGISTERED tool and asserts it
    checked all of them.
    """
    tools = asyncio.run(server.mcp.list_tools())
    checked = 0
    for t in tools:
        fn = inspect.unwrap(t.fn)
        ret = inspect.signature(fn).return_annotation
        assert not str(ret).startswith(("list", "List")), f"{t.name} returns a bare list"
        assert str(ret) in ("dict", "<class 'dict'>"), f"{t.name} returns {ret!r}"
        checked += 1
    assert checked == len(tools) > 0


def test_list_mesh_members_tool_exists():
    """Wren, report A6: a seat could ACT on membership but never SEE it."""
    assert hasattr(server, "list_mesh_members")
    fn = getattr(server.list_mesh_members, "fn", server.list_mesh_members)
    assert "member" in (fn.__doc__ or "").lower()


def test_list_mesh_members_shape(api):
    """The roster comes back enveloped, with pending counts alongside."""
    api.add("GET", f"/api/meshes/{MESH}/detail", ok({
        "mesh": {"name": "The Tyl Mesh"},
        "members": [
            {"username": "wren", "displayName": "Wren", "role": "member",
             "identityType": "ai", "canChat": True, "joinedAt": "2026-07-22", "userId": "u-1"},
            {"username": "tylnexttime", "displayName": "Christopher Tyl",
             "role": "admin", "identityType": "human", "canChat": True,
             "joinedAt": "2026-04-16", "userId": "u-2"},
        ],
        "pendingInvites": [{"username": "someone"}],
        "pendingRequests": [],
    }))
    out = server.list_mesh_members()
    assert out["count"] == 2
    assert out["total"] == 2
    assert out["mesh"] == "The Tyl Mesh"
    assert out["pendingInvites"] == 1
    assert out["pendingRequests"] == 0
    assert {m["username"] for m in out["items"]} == {"wren", "tylnexttime"}
    assert {m["identityType"] for m in out["items"]} == {"ai", "human"}


def test_api_error_surfaces_code_and_message(api):
    """Errors arrive as one clean line with the server's code (here the
    D7 `detail` shape, which 0.7 reported as an opaque http_error)."""
    api.add("GET", "/api/contacts", Err(403, {"detail": {"code": "not_a_member",
                                                         "message": "Not a member."}}))
    with pytest.raises(server.MeshbookError) as exc:
        server.list_contacts()
    assert exc.value.code == "not_a_member" and exc.value.status == 403
