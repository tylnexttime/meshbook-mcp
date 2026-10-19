"""0.8.3: every listing answers ONE shape, the CLI's paging fields included.

Alder's re-test (Tentyl ty-06d2b52d01a5): ``list_events`` lacked the paging
fields meshbook-cli's ``--json`` envelope carries (``offset``, ``hasMore``,
``paging``), and so did every other MCP listing. Now each listing has

    count, total, truncated, limit, offset, hasMore, paging, items

always (``limit``/``offset`` null where the call has none), plus
``nextOffset`` (offset paging) or ``nextBefore`` (cursor paging) when there
is a next page, plus the tool's own extras (``expanded``, ``semantic``, ...).
``paging`` is the CLI vocabulary: ``offset`` (pass offset=nextOffset),
``before`` (pass before=nextBefore), ``limit`` (no next page: only a larger
limit) or ``none`` (the whole set in one answer).
"""
from __future__ import annotations

import asyncio

import pytest
from meshbook import ItemList

from meshbook_mcp import server
from tests._fakes import CH1, MESH, U1, ok, ok_list

COMMON = ("count", "total", "truncated", "limit", "offset", "hasMore", "paging", "items")

CHANNELS = [{"id": CH1, "name": "general", "channelType": "group", "isPrivate": False}]
CAL = "/api/calendar-events"
EVENT = {"id": "e-1", "title": "Standup", "startAt": "2026-10-20T22:00:00+00:00",
         "endAt": "2026-10-20T22:15:00+00:00", "eventType": "Meeting"}


def _route_all(api):
    """One answer for every route a listing tool reads."""
    api.add("GET", "/api/me", ok({"user": {"id": U1}}))
    api.add("GET", f"/api/meshes/{MESH}/detail", ok({
        "mesh": {"name": "Tyl Mesh"}, "members": [{"username": "wren"}],
        "pendingInvites": [], "pendingRequests": []}))
    for path in ("/api/contacts", "/api/companies", "/api/leads", "/api/projects",
                 "/api/tasks", CAL, "/api/notifications"):
        api.add("GET", path, ok_list([{"id": "x"}], total=1, limit=50))
    api.add("GET", f"/api/meshes/{MESH}/channels", CHANNELS)
    api.add("GET", f"/api/channels/{CH1}", ok(CHANNELS[0]))
    api.add("GET", f"/api/channels/{CH1}/members", [{"userId": U1}])
    page = ok({"items": [{"id": "m", "createdAt": "2026-10-19T01:00:00Z"}],
               "pinned": [], "hasMore": False})
    api.add("GET", f"/api/entities/mesh/{MESH}/chat", page)
    api.add("GET", f"/api/channels/{CH1}/messages", page)
    api.add("GET", "/api/chat/search", ok_list([{"id": "h"}], total=1, limit=10))
    api.add("GET", f"/api/meshes/{MESH}/exports", ok_list([{"id": "x", "status": "ready"}]))


# tool -> (call, expected paging)
LISTINGS = {
    "list_my_meshes": (lambda: server.list_my_meshes(), "none"),
    "list_mesh_members": (lambda: server.list_mesh_members(), "none"),
    "list_contacts": (lambda: server.list_contacts(), "offset"),
    "list_companies": (lambda: server.list_companies(), "offset"),
    "list_leads": (lambda: server.list_leads(), "offset"),
    "list_projects": (lambda: server.list_projects(), "offset"),
    "list_tasks": (lambda: server.list_tasks(), "offset"),
    "list_my_tasks": (lambda: server.list_my_tasks(), "offset"),
    "list_events": (lambda: server.list_events(), "offset"),
    "read_thread": (lambda: server.read_thread(), "before"),
    "read_channel": (lambda: server.read_channel("general"), "before"),
    "list_channels": (lambda: server.list_channels(), "none"),
    "list_channel_members": (lambda: server.list_channel_members("general"), "none"),
    "search_chat": (lambda: server.search_chat("q"), "limit"),
    "list_unread_notifications": (lambda: server.list_unread_notifications(), "offset"),
    "export_status": (lambda: server.export_status("Tyl Mesh"), "none"),
}


@pytest.mark.parametrize("name", sorted(LISTINGS))
def test_every_listing_has_the_one_shape(api, name):
    _route_all(api)
    call, paging = LISTINGS[name]
    out = call()
    missing = [k for k in COMMON if k not in out]
    assert not missing, f"{name} lacks {missing}"
    assert out["paging"] == paging
    assert isinstance(out["hasMore"], bool) and isinstance(out["truncated"], bool)
    if paging == "offset":
        assert isinstance(out["offset"], int)
    else:
        assert out["offset"] is None, f"{name}: no offset argument, so offset is null"
    # the common keys lead, in one order, so two answers read alike
    assert list(out)[: len(COMMON) - 1] == list(COMMON[:-1])


def test_the_table_names_every_listing_tool():
    """A new list tool must join LISTINGS (and so get the shape checked)."""
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    listing_like = {n for n in tools
                    if n.startswith(("list_", "read_", "search_")) or n == "export_status"}
    assert listing_like == set(LISTINGS)


def test_list_events_pages_like_the_cli(api):
    rows = [dict(EVENT, id=f"e-{i}") for i in range(3)]
    api.add("GET", CAL, ok_list(rows, total=7, limit=3, offset=0))
    out = server.list_events(limit=3)
    assert out["offset"] == 0 and out["limit"] == 3 and out["total"] == 7
    assert out["hasMore"] is True and out["truncated"] is True
    assert out["paging"] == "offset" and out["nextOffset"] == 3
    assert out["expanded"] is False
    api.add("GET", CAL, ok_list(rows[:1], total=7, limit=3, offset=6))
    out = server.list_events(limit=3, offset=6)
    assert out["offset"] == 6 and out["hasMore"] is False and out["truncated"] is False
    assert "nextOffset" not in out


def test_list_events_server_cap_is_truncated_but_not_more(api):
    """An expanded window the server capped at 500: every row read, so no
    next page (hasMore false) but truncated true, and the note says why."""
    rows = [dict(EVENT, id="e-1", occurrenceStart="2026-10-20T22:00:00+00:00")]
    api.add("GET", CAL, ok_list(rows, total=1, limit=50, offset=0, truncated=True))
    out = server.list_events("2026-10-01T00:00:00+11:00", "2026-11-01T00:00:00+11:00")
    assert out["expanded"] is True and out["truncated"] is True
    assert out["hasMore"] is False and "nextOffset" not in out and "note" in out


def test_has_more_matches_the_next_cursor():
    rows = ItemList([1, 2], total=5, limit=2, offset=0, total_exact=True)
    out = server._listing(rows, rows=rows)
    assert out["hasMore"] is True and out["nextOffset"] == 2
    out = server._listing(rows, rows=rows, paging="before", next_before="2026-10-19T00:00:00Z")
    assert out["hasMore"] is True and out["nextBefore"] == "2026-10-19T00:00:00Z"
    assert out["offset"] is None and "nextOffset" not in out
    out = server._listing(rows, rows=rows, paging="limit")
    assert out["hasMore"] is True and "nextOffset" not in out and "nextBefore" not in out


def test_plain_list_is_the_whole_set():
    out = server._listing([1, 2, 3], paging="none")
    assert out == {"count": 3, "total": 3, "truncated": False, "limit": None,
                   "offset": None, "hasMore": False, "paging": "none", "items": [1, 2, 3]}


def test_unknown_paging_is_refused():
    with pytest.raises(ValueError):
        server._listing([], paging="page")
