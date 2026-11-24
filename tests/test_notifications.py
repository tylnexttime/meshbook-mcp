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
import datetime as dt
import json

import pytest
from meshbook import MeshbookError
from meshbook.resources import Notifications
from meshbook.testing import FOREIGN_MESH, MESH_A, MESH_B, FakeNotificationServer

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


# ─── annotations and descriptions (C-11) ──────────────────────────────

WRITE_TOOLS = (
    "mark_notifications_read",
    "mark_notifications_unread",
    "dismiss_notifications",
    "mark_all_notifications_read",
)


@pytest.mark.parametrize(
    "name, read_only, destructive, idempotent",
    [
        ("list_unread_notifications", True, False, None),
        ("notification_summary", True, False, None),
        ("mark_notifications_read", False, False, True),
        ("mark_notifications_unread", False, False, True),
        ("dismiss_notifications", False, True, None),
        ("mark_all_notifications_read", False, True, None),
    ],
)
def test_annotations_are_what_the_tool_does(name, read_only, destructive, idempotent):
    ann = _tools()[name].annotations
    assert ann.readOnlyHint is read_only
    assert ann.destructiveHint is destructive
    assert ann.idempotentHint is idempotent


@pytest.mark.parametrize("name", WRITE_TOOLS)
def test_first_sentence_states_the_reach(name):
    """The README table shows only the first sentence: the reach must be in it."""
    from tests.test_readme import _gen

    first = _gen()._first_sentence(_tools()[name].description)
    assert first.startswith("Changes only your own account's ")
    assert "everywhere you are signed in" in first
    assert "mesh" in first
    if name == "dismiss_notifications":
        assert "PERMANENTLY" in first
    if name == "mark_all_notifications_read":
        assert "DRY RUN" in first and "apply=true" in first


# ─── mark read / unread ───────────────────────────────────────────────


def test_mark_read_applies_and_says_so(fake):
    nid = fake.add(mesh=MESH_A)
    out = server.mark_notifications_read([nid])
    assert fake.row(nid).is_read
    assert out["allApplied"] is True and out["sent"] == 1
    assert out["counts"] == {"applied": 1}
    rec = out["results"][0]
    assert rec["id"] == nid and rec["status"] == "applied" and rec["mesh"] == MESH_A
    # The SDK's own keys are all still there.
    assert {"action", "ok", "counts", "results", "server", "searched"} <= set(out)
    assert out["server"] == fake.mode


def test_one_id_as_a_string_is_one_id(fake):
    nid = fake.add(mesh=MESH_A)
    out = server.mark_notifications_read(nid)
    assert [r["id"] for r in out["results"]] == [nid] and fake.row(nid).is_read


def test_a_row_in_another_mesh_is_found_and_patched_there(fake):
    nid = fake.add(mesh=MESH_B)
    out = server.mark_notifications_read([nid])
    assert fake.row(nid).is_read and out["allApplied"] is True
    assert out["results"][0]["mesh"] == MESH_B
    assert all(p.mesh == MESH_B for p in fake.patches)


def test_a_silent_miss_is_never_reported_applied(fake):
    """The §106 trap: the PATCH is answered like a hit and changes nothing."""
    nid = fake.add(mesh=MESH_A)
    fake.drop(nid)
    out = server.mark_notifications_read([nid])
    assert not fake.row(nid).is_read
    assert out["allApplied"] is False and out["sent"] == 1
    assert out["results"][0]["status"] == "unconfirmed"
    assert "unconfirmed" in out["statusMeanings"]
    assert "check it" in out["note"]


def test_a_wrong_mesh_is_not_in_mesh_and_nothing_is_sent(fake):
    nid = fake.add(mesh=MESH_A)
    with pytest.raises(MeshbookError) as e:
        server.mark_notifications_read([nid], mesh=MESH_B)
    assert e.value.code == "nothing_sent"
    assert "not_in_mesh" in e.value.message and nid in e.value.message
    assert "Leave mesh= out" in e.value.message
    assert fake.patches == [] and not fake.row(nid).is_read


def test_a_partial_batch_answers_per_id_not_an_error(fake):
    here = fake.add(mesh=MESH_B)
    elsewhere = fake.add(mesh=MESH_A)
    out = server.mark_notifications_read([here, elsewhere], mesh=MESH_B)
    assert out["allApplied"] is False
    assert out["counts"] == {"applied": 1, "not_in_mesh": 1}
    assert [r["status"] for r in out["results"]] == ["applied", "not_in_mesh"]
    assert fake.row(here).is_read and not fake.row(elsewhere).is_read


def test_an_unknown_id_is_not_found_and_nothing_is_sent(fake):
    with pytest.raises(MeshbookError) as e:
        server.mark_notifications_read(["0badc0de-0000-4000-8000-000000000000"])
    assert e.value.code == "nothing_sent" and "not_found" in e.value.message
    assert fake.patches == []


def test_already_read_is_settled_without_a_patch(fake):
    nid = fake.add(mesh=MESH_A, is_read=True)
    out = server.mark_notifications_read([nid])
    assert out["allApplied"] is True and out["sent"] == 0
    assert out["counts"] == {"already": 1} and fake.patches == []


def test_a_refused_patch_is_answered_per_id_not_raised(monkeypatch):
    """Something was sent, so the records are the answer (C-11)."""
    fake = _fake(monkeypatch, "old", readonly=True)
    a, b = fake.add(mesh=MESH_A), fake.add(mesh=MESH_A)
    out = server.mark_notifications_read([a, b])
    assert out["allApplied"] is False and out["sent"] == 1
    assert [r["status"] for r in out["results"]] == ["error", "not_attempted"]
    assert out["results"][0]["code"] == "token_readonly"
    assert not fake.row(a).is_read and not fake.row(b).is_read


def test_mark_unread_brings_a_row_back_into_the_list(fake):
    """C-02 end to end: after mark-unread the row is unread (isRead false)
    with a stale readAt on the old server, and the list shows it."""
    nid = fake.add(mesh=MESH_A, is_read=True)
    out = server.mark_notifications_unread([nid])
    assert out["allApplied"] is True and not fake.row(nid).is_read
    assert [n["id"] for n in server.list_unread_notifications()["items"]] == [nid]


def test_bad_ids_and_oversized_batches_are_refused_before_the_wire(fake):
    with pytest.raises(MeshbookError) as e:
        server.mark_notifications_read(["not-a-uuid"])
    assert e.value.code == "bad_argument"
    too_many = [f"{i:08x}-0000-4000-8000-000000000000" for i in range(Notifications.MAX_IDS + 1)]
    with pytest.raises(MeshbookError) as e:
        server.mark_notifications_read(too_many)
    assert e.value.code == "bad_argument" and str(Notifications.MAX_IDS) in e.value.message
    assert fake.log == []


# ─── dismiss (C-13) ────────────────────────────────────────────────────


def test_dismiss_without_confirm_is_refused_and_sends_nothing(fake):
    nid = fake.add(mesh=MESH_A)
    with pytest.raises(MeshbookError) as e:
        server.dismiss_notifications([nid])
    assert e.value.code == "bad_argument" and e.value.hint == "confirm_irreversible"
    assert "confirm=true" in e.value.message and "mark_notifications_read" in e.value.message
    assert fake.log == [] and fake.row(nid).dismissed_at is None
    with pytest.raises(MeshbookError):
        server.dismiss_notifications([nid], confirm="true")  # only a real true
    assert fake.log == []


def test_dismiss_with_confirm_dismisses(fake):
    nid = fake.add(mesh=MESH_A)
    out = server.dismiss_notifications([nid], confirm=True)
    assert fake.row(nid).dismissed_at is not None
    assert out["allApplied"] is True and out["action"] == "dismiss"


def test_a_dismiss_that_did_not_land_is_not_applied(fake):
    nid = fake.add(mesh=MESH_A)
    fake.drop(nid)
    out = server.dismiss_notifications([nid], confirm=True)
    assert fake.row(nid).dismissed_at is None
    assert out["allApplied"] is False and out["results"][0]["status"] == "unconfirmed"


# ─── mark all (C-04, C-03) ─────────────────────────────────────────────


def test_mark_all_is_a_dry_run_by_default(fake):
    ids = fake.add_many(3, mesh=MESH_A)
    out = server.mark_all_notifications_read()
    assert out["dry_run"] is True and out["allApplied"] is None and out["counts"] is None
    assert sorted(out["ids"]) == sorted(ids) and out["count"] == 3
    assert "Dry run" in out["note"]
    assert fake.patches == [] and not any(fake.row(i).is_read for i in ids)
    assert fake.requests("POST", "notifications.read_all") == []


def test_mark_all_apply_marks_exactly_what_matched(fake):
    mine = fake.add_many(2, mesh=MESH_A)
    other = fake.add(mesh=MESH_B)
    out = server.mark_all_notifications_read(apply=True)
    assert out["dry_run"] is False and out["allApplied"] is True
    assert out["counts"] == {"applied": 2}
    assert all(fake.row(i).is_read for i in mine) and not fake.row(other).is_read
    assert out["outcome"]["allApplied"] is True
    assert fake.requests("POST", "notifications.read_all") == []  # never the blanket route


def test_mark_all_apply_reports_a_silent_miss(fake):
    a, b = fake.add_many(2, mesh=MESH_A)
    fake.drop(b)
    out = server.mark_all_notifications_read(apply=True)
    assert out["allApplied"] is False
    assert out["counts"] == {"applied": 1, "unconfirmed": 1}
    assert fake.row(a).is_read and not fake.row(b).is_read


def test_mark_all_filters_by_age_and_kind(fake):
    t0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    old_mention = fake.add(mesh=MESH_A, created_at=t0)
    fake.add(mesh=MESH_A, created_at=t0 + dt.timedelta(days=10))
    fake.add(mesh=MESH_A, created_at=t0, kind="TaskAssigned")
    out = server.mark_all_notifications_read(
        kinds=["Mention"], older_than="2026-11-05T00:00:00+11:00"
    )
    assert out["ids"] == [old_mention]
    assert server._older_than("7d") == dt.timedelta(days=7)
    assert server._older_than(" 12H ") == dt.timedelta(hours=12)


def test_mark_all_refuses_bad_filters_before_the_wire(fake):
    with pytest.raises(MeshbookError) as e:
        server.mark_all_notifications_read(group="invitations")  # the C-03 trap
    assert e.value.code == "bad_argument"
    with pytest.raises(MeshbookError) as e:
        server.mark_all_notifications_read(older_than="2026-11-05T00:00:00")
    assert "'7d'" in e.value.message and "offset" in e.value.message
    assert fake.log == []


# ─── summary (C-16) ────────────────────────────────────────────────────


def test_summary_one_mesh_and_all_meshes(fake):
    fake.add_many(2, mesh=MESH_A)
    fake.add(mesh=MESH_B)
    fake.add(cross_mesh=True, mesh=FOREIGN_MESH, kind="MeshInvitation")
    one = server.notification_summary()
    assert one["meshId"] == MESH_A and one["meshName"] == "Alpha"
    assert one["unread"] == 3 and one["unreadInMesh"] == 2 and one["unreadCrossMesh"] == 1
    every = server.notification_summary(all_meshes=True)
    # The invitation is counted ONCE, not once per mesh.
    assert every["unread"] == 4 and every["unreadCrossMesh"] == 1 and every["complete"] is True
    assert fake.patches == []


# ─── prompts (C-18): the wake rituals clear what they handled ─────────


@pytest.mark.parametrize("prompt", [server.whats_new, server.summarise_mesh_week])
def test_prompts_mark_what_was_handled_and_never_tidy_up(prompt):
    text = prompt()
    assert "mark_notifications_read" in text and "notification_summary" in text
    assert "Never use mark_all_notifications_read or dismiss_notifications" in text
    # Every tool a prompt names exists.
    names = set(_tools())
    for tool_name in ("list_unread_notifications", "mark_notifications_read",
                      "notification_summary", "mark_all_notifications_read",
                      "dismiss_notifications"):
        assert tool_name in names


# ─── through a real MCP client ────────────────────────────────────────


def _call(name, args):
    from fastmcp import Client

    async def go():
        async with Client(server.mcp) as c:
            return await c.call_tool(name, args, raise_on_error=False)

    return asyncio.run(go())


def test_the_model_sees_records_or_one_clean_error(old):
    nid = old.add(mesh=MESH_A)
    res = _call("mark_notifications_read", {"ids": nid})
    assert not res.is_error and res.structured_content["allApplied"] is True
    res = _call("dismiss_notifications", {"ids": [nid]})
    assert res.is_error
    text = res.content[0].text
    assert "confirm=true" in text and "Traceback" not in text
