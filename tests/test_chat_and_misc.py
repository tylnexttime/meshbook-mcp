"""Tools the 0.7 suite never exercised: channels, search, thread, export status,
credential status. Plus a guard that no tool can land untested again."""
from __future__ import annotations

import asyncio
import pathlib
import re

from meshbook_mcp import server
from tests._fakes import CH1, MESH, MESH2, U2, ok, ok_list

CHANNELS = [{"id": CH1, "name": "general", "channelType": "group", "isPrivate": False}]
USERS = ok_list([{"id": U2, "username": "wren", "displayName": "Wren"}])


def _channels(api):
    api.add("GET", f"/api/meshes/{MESH}/channels", CHANNELS)
    api.add("GET", f"/api/channels/{CH1}", ok(CHANNELS[0]))


def test_post_channel(api):
    _channels(api)
    api.add("POST", f"/api/channels/{CH1}/messages", ok({"id": "m-1"}))
    out = server.post_channel("#General", "hi all")
    assert out == {"posted": "m-1", "channel": "general"}
    assert api.body() == {"bodyMd": "hi all"}


def test_channel_membership(api):
    _channels(api)
    api.add("GET", "/api/users", USERS)
    api.add("POST", f"/api/channels/{CH1}/members", ok_list([{"userId": U2}]))
    assert server.add_channel_member("general", "@wren") == {"added": "wren", "channel": "general"}
    assert api.body() == {"userId": U2}
    api.add("DELETE", f"/api/channels/{CH1}/members/{U2}", ok({"removed": True}))
    assert server.remove_channel_member("general", "Wren")["removed"] == "wren"
    api.add("GET", f"/api/channels/{CH1}/members", ok_list([{"userId": U2, "username": "wren"}]))
    out = server.list_channel_members("general")
    assert out["count"] == 1 and out["channel"] == "general"


def test_read_thread_oldest_first(api):
    api.add("GET", f"/api/entities/mesh/{MESH}/chat", ok_list([
        {"id": "2", "bodyMd": "b", "createdAt": "2026-10-19T01:00:00Z"},
        {"id": "1", "bodyMd": "a", "createdAt": "2026-10-19T00:00:00Z"},
    ]))
    out = server.read_thread(limit=2)
    assert [m["body"] for m in out["items"]] == ["a", "b"]
    assert api.query()["limit"] == "2" and "offset" not in api.query()


def test_read_thread_in_another_mesh(api):
    api.add("GET", f"/api/entities/mesh/{MESH2}/chat", ok_list([]))
    server.read_thread(mesh="Second Mesh")
    assert api.last.get_header("X-active-mesh-id") == MESH2


def test_search_chat(api):
    api.add("GET", "/api/chat/search", ok_list([
        {"id": "h", "channelId": CH1, "channelName": "general", "authorName": "Wren",
         "preview": "the calendar", "matchedArms": ["semantic"]},
    ], total=12, limit=10, semantic=True))
    out = server.search_chat("calendar plans")
    assert api.query() == {"q": "calendar plans", "limit": "10"}
    assert out["semantic"] is True and out["total"] == 12 and out["truncated"] is True
    assert out["items"][0]["place"] == "#general"


def test_export_status(api):
    api.add("GET", f"/api/meshes/{MESH}/exports", ok_list([{"id": "x", "status": "ready"}]))
    out = server.export_status("tyl mesh")
    assert out["items"][0]["status"] == "ready" and out["mesh"] == "Tyl Mesh"
    assert api.last.get_header("X-active-mesh-id") == MESH


def test_agent_credential_status_compares_kids(api):
    from meshbook import config as mb_config

    (mb_config.config_dir() / "agent-key.json").write_text('{"kid": "k1"}', encoding="utf-8")
    (mb_config.config_dir() / "agent-key.pem").write_text("pem", encoding="utf-8")
    api.add("GET", "/api/me/agent-credentials", ok({"enrolled": True, "username": "w", "kid": "k1"}))
    out = server.agent_credential_status()
    assert out["matchesLocal"] is True and out["localKid"] == "k1"
    assert api.last.get_header("X-active-mesh-id") is None   # account-level: never a mesh
    api.add("GET", "/api/me/agent-credentials", ok({"enrolled": True, "username": "w", "kid": "k9"}))
    assert server.agent_credential_status()["matchesLocal"] is False


def test_every_tool_is_exercised_by_some_test():
    """0.7 shipped eight tools no test ever called. Not again."""
    src = "\n".join(p.read_text(encoding="utf-8")
                    for p in pathlib.Path(__file__).parent.glob("test_*.py"))
    names = sorted(t.name for t in asyncio.run(server.mcp.list_tools()))
    untested = [n for n in names if not re.search(rf'server\.{n}\(|_call\("{n}"', src)]
    assert untested == []
