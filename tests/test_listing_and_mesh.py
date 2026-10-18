"""Listings (true total + truncated), per-call mesh, MESHBOOK_MESH, client rebuild."""
from __future__ import annotations

import pytest
from meshbook import ItemList
from meshbook import config as mb_config

from meshbook_mcp import server
from tests._fakes import MESH, MESH2, U1, ok, ok_list, old_ok_list
from tests.conftest import write_config

# ─── the listing envelope ──────────────────────────────────────────────


def test_true_total_and_truncated_from_the_new_server(api):
    """The B6 count was the page length; with 80 contacts the model saw 50
    and could not tell. Now: count 50, total 80, truncated, nextOffset 50."""
    rows = [{"id": f"c-{i}", "displayName": f"C{i}"} for i in range(50)]
    api.add("GET", "/api/contacts", ok_list(rows, total=80, limit=50, offset=0))
    out = server.list_contacts()
    assert api.query()["limit"] == "50" and "offset" not in api.query()
    assert out["count"] == 50 and out["total"] == 80
    assert out["truncated"] is True and out["nextOffset"] == 50


def test_last_page_is_not_truncated(api):
    rows = [{"id": f"c-{i}"} for i in range(30)]
    api.add("GET", "/api/contacts", ok_list(rows, total=80, limit=50, offset=50))
    out = server.list_contacts(offset=50)
    assert api.query()["offset"] == "50"
    assert out["truncated"] is False and "nextOffset" not in out and out["offset"] == 50


def test_exactly_one_full_page_is_not_truncated(api):
    rows = [{"id": f"c-{i}"} for i in range(50)]
    api.add("GET", "/api/contacts", ok_list(rows, total=50, limit=50, offset=0))
    out = server.list_contacts()
    assert out["total"] == 50 and out["truncated"] is False


def test_old_server_total_is_not_trusted(api):
    """Before 2026-10-19 the server put the PAGE LENGTH in `total`. That
    number must not be reported as a total, and a full page may have more."""
    rows = [{"id": f"c-{i}"} for i in range(50)]
    api.add("GET", "/api/contacts", old_ok_list(rows))
    out = server.list_contacts()
    assert out["total"] is None
    assert out["truncated"] is True and out["nextOffset"] == 50
    api.add("GET", "/api/contacts", old_ok_list(rows[:7]))
    out = server.list_contacts()
    assert out["total"] is None and out["truncated"] is False


def test_unpaged_bare_list_total_is_its_length(api):
    api.add("GET", f"/api/meshes/{MESH}/channels", [{"id": "a"}, {"id": "b"}])
    out = server.list_channels()
    assert out["total"] == 2 and out["truncated"] is False


def test_listing_helper_on_item_lists():
    rows = ItemList([1, 2], total=None, limit=2, offset=None)
    out = server._listing(rows, rows=rows)
    assert out["total"] is None and out["truncated"] is True     # full page, total unknown
    rows = ItemList([1], total=None, limit=5)
    assert server._listing(rows, rows=rows)["truncated"] is False
    rows = ItemList([1, 2, 3], total=9, limit=3, offset=0, meta={"client_paginated": True},
                    total_exact=True)       # how the SDK builds a page it cut itself
    out = server._listing(rows, rows=rows)
    assert out["total"] == 9 and out["nextOffset"] == 3


def test_exactness_is_the_sdks_call_not_a_guess_from_meta_keys():
    """0.8.0 trusted a total whenever 'offset' (or 'limit', or
    'client_paginated') was in the meta. The SDK (0.4.1) says whether a
    total is exact; the listing reports it only then."""
    guessy = ItemList([1, 2, 3], total=3, limit=3, offset=0, meta={"offset": 0})
    assert guessy.total_exact is False
    out = server._listing(guessy, rows=guessy)
    assert out["total"] is None and out["truncated"] is True and out["nextOffset"] == 3
    exact = ItemList([1, 2, 3], total=3, limit=3, offset=0, meta={"offset": 0, "limit": 3})
    out = server._listing(exact, rows=exact)
    assert out["total"] == 3 and out["truncated"] is False and "nextOffset" not in out


def test_next_offset_follows_the_servers_rows_after_a_client_side_filter():
    """A page the SDK filtered (tasks.list_mine(open_only=True)) is shorter
    than what the server sent; the next page starts after the SERVER's rows."""
    rows = ItemList([{"id": "a"}], total=None, limit=3, offset=6,
                    meta={"server_rows": 3, "server_total": 20})
    out = server._listing(list(rows), rows=rows)
    assert out["count"] == 1 and out["truncated"] is True and out["nextOffset"] == 9


@pytest.mark.parametrize("limit,offset", [(0, 0), (-1, 0), (501, 0), (10, -5)])
def test_bad_paging_arguments(api, limit, offset):
    with pytest.raises(server.MeshbookError) as exc:
        server.list_contacts(limit=limit, offset=offset)
    assert exc.value.code == "bad_argument"


# ─── mesh selection ────────────────────────────────────────────────────


def test_meshbook_mesh_env_by_name(api, monkeypatch):
    monkeypatch.setenv("MESHBOOK_MESH", "Second Mesh")
    api.add("GET", "/api/contacts", ok_list([]))
    server.list_contacts()
    assert api.last.get_header("X-active-mesh-id") == MESH2


def test_per_call_mesh_beats_env(api, monkeypatch):
    monkeypatch.setenv("MESHBOOK_MESH", "Second Mesh")
    api.add("GET", "/api/contacts", ok_list([]))
    server.list_contacts(mesh=MESH)
    assert api.last.get_header("X-active-mesh-id") == MESH


def test_unknown_mesh_name_is_clean(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.list_contacts(mesh="Nope")
    assert exc.value.code == "mesh_not_found"
    assert "list_my_meshes" in exc.value.message


def test_set_active_mesh_warns_when_env_pins_the_mesh(api, monkeypatch):
    monkeypatch.setenv("MESHBOOK_MESH", "Tyl Mesh")
    api.add("POST", "/api/meshes/active", ok({}))
    out = server.set_active_mesh("Second Mesh")
    assert "MESHBOOK_MESH" in out["note"]
    assert mb_config.load_config()["active_mesh_id"] == MESH2


def test_set_active_mesh_refusal_changes_nothing(api):
    from tests._fakes import Err

    api.add("POST", "/api/meshes/active",
            Err(403, {"detail": {"code": "token_out_of_scope", "message": "scoped"}}))
    with pytest.raises(server.MeshbookError) as exc:
        server.set_active_mesh("Second Mesh")
    assert exc.value.code == "token_out_of_scope"
    assert mb_config.load_config()["active_mesh_id"] == MESH


def test_whoami_reports_source_and_survives_a_bad_env_mesh(api, monkeypatch):
    api.add("GET", "/api/me", ok({"user": {"id": U1, "username": "chris",
                                           "displayName": "Chris", "identityType": "HUMAN"}}))
    out = server.whoami()
    assert out["activeMeshName"] == "Tyl Mesh" and out["meshSource"] == "config"
    assert out["authMode"] == "bearer" and out["base"] == "https://meshbook.org"
    monkeypatch.setenv("MESHBOOK_MESH", "Ghost Mesh")
    out = server.whoami()
    assert out["username"] == "chris" and out["activeMeshId"] is None
    assert "mesh_not_found" in out["activeMeshError"]


def test_meshbook_base_env_is_honoured(api, monkeypatch):
    monkeypatch.setenv("MESHBOOK_BASE", "https://staging.example")
    api.add("GET", "/api/contacts", ok_list([]))
    server.list_contacts()
    assert api.last.get_full_url().startswith("https://staging.example/api/contacts")


def test_a_terminal_login_reaches_a_running_session(api):
    """The client is cached per process but rebuilt when the config changes."""
    api.add("GET", "/api/contacts", ok_list([]))
    server.list_contacts()
    assert api.last.get_header("Authorization") == "Bearer mb_token_test"
    write_config(token="mb_token_new", active_mesh_id=MESH)
    server.list_contacts()
    assert api.last.get_header("Authorization") == "Bearer mb_token_new"


def test_the_client_is_reused_between_calls(api):
    from meshbook_mcp import _core

    api.add("GET", "/api/contacts", ok_list([]))
    server.list_contacts(mesh="Second Mesh")
    first = _core.client()
    server.list_contacts(mesh="Second Mesh")
    assert _core.client() is first
    # the mesh-name cache lives on the client: one /api/meshes for both calls
    assert len(api.calls("GET", "/api/meshes")) == 1


def test_list_my_meshes_marks_active(api):
    """/api/meshes answers the server's unpaged ok_list() (total = pageSize =
    the row count), which the SDK cannot tell from a page-length total, so
    total_exact is False and total is null; nothing says there is more."""
    out = server.list_my_meshes()
    assert out["count"] == 2 and out["total"] is None and out["truncated"] is False
    assert [m["active"] for m in out["items"]] == [True, False]


def test_meshbook_token_env_beats_the_config(api, monkeypatch):
    """0.7 ignored MESHBOOK_TOKEN entirely (SURVEY §4)."""
    monkeypatch.setenv("MESHBOOK_TOKEN", "mb_token_env")
    api.add("GET", "/api/contacts", ok_list([]))
    server.list_contacts()
    assert api.last.get_header("Authorization") == "Bearer mb_token_env"


def test_a_503_on_a_read_is_retried_honouring_retry_after(api):
    """0.7 had no retry at all; reads now ride the SDK's 429/503 policy."""
    from tests._fakes import Err

    api.add("GET", "/api/contacts",
            Err(503, {"error": {"code": "busy", "message": "busy"}}, {"Retry-After": "2"}),
            ok_list([{"id": "c"}]))
    assert server.list_contacts()["count"] == 1
    assert api.sleeps == [2.0]


def test_a_write_is_never_retried(api):
    from tests._fakes import Err

    api.add("POST", "/api/contacts",
            Err(503, {"error": {"code": "busy", "message": "busy"}}, {"Retry-After": "1"}))
    with pytest.raises(server.MeshbookError) as exc:
        server.create_contact("Ada")
    assert exc.value.status == 503 and len(api.calls("POST", "/api/contacts")) == 1
