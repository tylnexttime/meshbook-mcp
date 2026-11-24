"""0.9.0: the notification tools (meshbook DEV-DEBT §106, critique CR-5).

Run against meshbook-sdk's STATEFUL fake (``meshbook.testing``), not the
canned Router: it models the production server (``mode="old"``: a PATCH
answers ``updated: true`` whether or not a row matched) and the s106 fix
(``mode="fixed"``: a coded 404 for a miss, the row echoed for a hit), so a
tool that trusts the wrong signal goes red. Assertions read the fake's own
rows (``fake.row(id)``), not only the tool's answer.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from meshbook import MeshbookError
from meshbook.testing import FOREIGN_MESH, MESH_A, FakeNotificationServer

from meshbook_mcp import _shapes as S
from meshbook_mcp import server
from tests.conftest import write_config

MODES = ("old", "fixed")

#: 0.8's keys: a caller that read them must still find them, unchanged.
OLD_KEYS = ("id", "kind", "summary", "at")
NEW_KEYS = ("title", "isRead", "readAt", "meshId", "meshName", "isCrossMesh", "severity", "link")


def _fake(monkeypatch, mode: str = "old", **kw) -> FakeNotificationServer:
    """A signed-in bench whose active mesh is MESH_A ("Alpha")."""
    write_config(token="mb_token_test", active_mesh_id=MESH_A)
    return FakeNotificationServer(mode=mode, **kw).install(monkeypatch)


@pytest.fixture(params=MODES)
def fake(request, monkeypatch) -> FakeNotificationServer:
    return _fake(monkeypatch, request.param)


@pytest.fixture()
def old(monkeypatch) -> FakeNotificationServer:
    return _fake(monkeypatch, "old")


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


# ─── the read side: list_unread_notifications, the resource, the shape ──


def test_shape_is_additive():
    row = {
        "id": "n-1", "kind": "Mention", "summary": "hi", "createdAt": "2026-11-24T09:00:00Z",
        "title": "hi", "isRead": False, "readAt": None, "meshId": MESH_A, "isCrossMesh": False,
        "severity": "info", "linkEntityType": "task", "linkEntityId": "t-1",
        "linkSecondaryId": "c-9",
    }
    out = S.notification_out(row, is_read=False, mesh_name="Alpha")
    assert list(out)[:4] == list(OLD_KEYS)
    assert out["at"] == "2026-11-24 09:00:00"  # 0.8's format, unchanged
    assert set(out) == set(OLD_KEYS) | set(NEW_KEYS)
    assert out["link"] == {"entityType": "task", "entityId": "t-1", "secondaryId": "c-9"}
    assert out["meshName"] == "Alpha" and out["isRead"] is False
    # A row that links nowhere says so with null, not an empty dict.
    assert S.notification_out({"id": "n-2"})["link"] is None


def test_list_keeps_a_row_marked_unread_again(fake):
    """C-02: the server keeps readAt when a row is marked unread. 0.8 dropped
    every row with a readAt, so a re-unread row vanished from the list."""
    stale = fake.add(mesh=MESH_A, is_read=False, read_at=fake.clock)
    fresh = fake.add(mesh=MESH_A)
    out = server.list_unread_notifications()
    ids = [n["id"] for n in out["items"]]
    assert ids == [fresh, stale]
    item = out["items"][1]
    assert item["isRead"] is False and item["readAt"] is not None
    assert all(k in item for k in OLD_KEYS + NEW_KEYS)


def test_list_names_your_meshes_and_flags_cross_mesh_rows(fake):
    mine = fake.add(mesh=MESH_A)
    invite = fake.add(cross_mesh=True, mesh=FOREIGN_MESH, kind="MeshInvitation")
    items = {n["id"]: n for n in server.list_unread_notifications()["items"]}
    assert items[mine]["meshName"] == "Alpha" and items[mine]["isCrossMesh"] is False
    # An invitation is ABOUT a mesh you are not in yet: no name, but flagged.
    assert items[invite]["meshName"] is None and items[invite]["isCrossMesh"] is True
    # Naming costs one GET /api/meshes for the page, not one per row.
    assert len(fake.requests("GET", "meshes.list")) == 1


def test_list_total_is_not_claimed_as_the_unread_count(fake):
    """C-18: the route's total is the page length. 0.8 said it was the true
    unread count; the tool now reports null and the description points at
    notification_summary."""
    fake.add_many(60, mesh=MESH_A)
    out = server.list_unread_notifications()
    assert out["count"] == 50 and out["total"] is None
    assert out["hasMore"] is True and out["nextOffset"] == 50
    desc = _tools()["list_unread_notifications"].description
    assert "notification_summary" in desc and "NOT your unread count" in desc


def test_list_limit_is_capped_at_the_servers_200(fake):
    """C-10: limit=300 passed 0.8's local check (500) and came back a 422."""
    fake.add_many(3, mesh=MESH_A)
    with pytest.raises(MeshbookError) as e:
        server.list_unread_notifications(limit=201)
    assert e.value.code == "bad_argument" and "200" in e.value.message
    assert fake.log == []  # refused before the wire
    assert server.list_unread_notifications(limit=200)["count"] == 3


def test_resource_uses_the_same_shape_and_read_state(fake):
    stale = fake.add(mesh=MESH_A, is_read=False, read_at=fake.clock)
    rows = json.loads(server.resource_notifications())
    assert [r["id"] for r in rows] == [stale]
    assert rows[0]["isRead"] is False and rows[0]["meshName"] == "Alpha"
