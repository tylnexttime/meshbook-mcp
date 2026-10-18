"""The file fence, safe downloads, resources (incl. the local-date fix) and
errors as a real MCP client sees them."""
from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from meshbook import config as mb_config

from meshbook_mcp import _core, server
from tests._fakes import MESH, U1, Resp, ok, ok_list

ATT = "/api/entity-attachments/att-9/download"


def _served(api, body=b"payload", name='attachment; filename="report.pdf"'):
    api.add("GET", ATT, Resp(body, {"Content-Disposition": name}))


# ─── attach_file ───────────────────────────────────────────────────────


def test_attach_refuses_paths_outside_home_and_cwd(api, home_and_cwd, tmp_path):
    outside = tmp_path / "elsewhere" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(server.MeshbookError) as exc:
        server.attach_file("lead", "e-1", str(outside))
    assert exc.value.code == "path_not_allowed"
    assert _core.ALLOW_ANY_PATH_ENV in exc.value.message
    assert not api.requests


def test_attach_opt_in_allows_any_path(api, home_and_cwd, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    monkeypatch.setenv(_core.ALLOW_ANY_PATH_ENV, "1")
    api.add("POST", "/api/entities/lead/e-1/attachments/json", ok({"id": "a"}))
    assert server.attach_file("lead", "e-1", str(outside))["id"] == "a"


def test_the_config_dir_is_never_attachable(api, home_and_cwd, monkeypatch):
    """attach_file would upload the bearer token or the agent private key."""
    monkeypatch.setenv(_core.ALLOW_ANY_PATH_ENV, "1")      # not even with the opt-in
    cfg = mb_config.config_path()
    for target in (cfg, cfg.parent / "agent-key.pem"):
        target.write_text("secret", encoding="utf-8")
        with pytest.raises(server.MeshbookError) as exc:
            server.attach_file("lead", "e-1", str(target))
        assert exc.value.code == "path_not_allowed" and "config dir" in exc.value.message
    assert not api.requests


def test_attach_relative_path_and_missing_file(api, home_and_cwd):
    (home_and_cwd / "notes.txt").write_text("hello", encoding="utf-8")
    api.add("POST", "/api/entities/task/t-1/attachments/json", ok({"id": "a"}))
    server.attach_file("task", "t-1", "notes.txt")
    assert api.body()["filename"] == "notes.txt"
    with pytest.raises(server.MeshbookError) as exc:
        server.attach_file("task", "t-1", "nope.txt")
    assert exc.value.code == "file_not_found"


# ─── download_attachment ───────────────────────────────────────────────


def test_download_never_overwrites_silently(api, home_and_cwd):
    _served(api, b"new")
    target = home_and_cwd / "report.pdf"
    target.write_bytes(b"old")
    with pytest.raises(server.MeshbookError) as exc:
        server.download_attachment("att-9", str(home_and_cwd))
    assert exc.value.code == "file_exists" and "overwrite=true" in exc.value.message
    assert target.read_bytes() == b"old"
    assert [p.name for p in home_and_cwd.iterdir()] == ["report.pdf"]  # no temp file left
    out = server.download_attachment("att-9", str(target), overwrite=True)
    assert target.read_bytes() == b"new" and out["bytes"] == 3


@pytest.mark.parametrize("disposition,expected", [
    ("attachment; filename*=UTF-8''..%2F..%2Fevil.sh", "evil.sh"),
    ('attachment; filename="..\\\\..\\\\evil.bat"', "evil.bat"),
    ('attachment; filename="C:evil.txt"', "C_evil.txt"),
    ('attachment; filename=".."', "attachment"),
    ('attachment; filename="nul.txt"', "_nul.txt"),
])
def test_download_ignores_a_hostile_server_filename(api, home_and_cwd, disposition, expected):
    _served(api, b"x", disposition)
    dl = home_and_cwd / "dl"
    dl.mkdir()
    out = server.download_attachment("att-9", str(dl))
    assert [p.name for p in dl.iterdir()] == [expected]
    assert out["path"].endswith(expected)
    assert sorted(p.name for p in home_and_cwd.iterdir()) == ["dl"]


def test_download_refuses_the_config_dir(api, home_and_cwd, monkeypatch):
    monkeypatch.setenv(_core.ALLOW_ANY_PATH_ENV, "1")
    with pytest.raises(server.MeshbookError) as exc:
        server.download_attachment("att-9", str(mb_config.config_path()), overwrite=True)
    assert exc.value.code == "path_not_allowed"
    assert not api.requests


def test_download_with_per_call_mesh(api, home_and_cwd):
    _served(api)
    server.download_attachment("att-9", str(home_and_cwd / "f.pdf"), mesh="Second Mesh")
    from tests._fakes import MESH2

    assert api.last.get_header("X-active-mesh-id") == MESH2


# ─── resources ─────────────────────────────────────────────────────────


def test_my_tasks_today_uses_the_local_calendar_day(api, monkeypatch):
    """0.7 judged 'today' by the UTC date: at 08:00 in Sydney (21:00 UTC the
    day before) a task due today was not listed. Fixed: the machine's day."""
    sydney = dt.timezone(dt.timedelta(hours=11))
    monkeypatch.setattr(server, "_now", lambda: dt.datetime(2026, 10, 21, 8, 0, tzinfo=sydney))
    api.add("GET", "/api/me", ok({"user": {"id": U1}}))
    api.add("GET", "/api/tasks", ok_list([
        {"id": "today", "status": "NotStarted", "dueDate": "2026-10-21"},
        {"id": "tonight", "status": "NotStarted", "dueAt": "2026-10-21T19:00:00+11:00"},
        {"id": "tonight-utc", "status": "NotStarted", "dueAt": "2026-10-21T10:00:00Z"},
        {"id": "tomorrow", "status": "InProgress", "dueAt": "2026-10-22T09:00:00+11:00"},
        {"id": "overdue", "status": "Blocked", "dueDate": "2026-10-01"},
        {"id": "done", "status": "Done", "dueDate": "2026-10-21"},
        {"id": "undated", "status": "NotStarted"},
    ], total=7, limit=200))
    out = json.loads(server.resource_my_tasks_today())
    assert out["today"] == "2026-10-21"
    assert [t["id"] for t in out["dueTodayOrOverdue"]] == ["today", "tonight", "tonight-utc",
                                                           "overdue"]
    assert [t["id"] for t in out["openUndated"]] == ["undated"]
    assert out["truncated"] is False


def test_active_mesh_resource(api):
    out = json.loads(server.resource_active_mesh())
    assert out["activeMeshId"] == MESH and out["activeMeshName"] == "Tyl Mesh"


def test_notifications_resource_is_unread_only(api):
    api.add("GET", "/api/notifications", ok_list([{"id": "n", "kind": "mention"}]))
    assert json.loads(server.resource_notifications())[0]["id"] == "n"
    assert api.query()["unreadOnly"] == "true"


# ─── through a real MCP client (FastMCP in-memory transport) ───────────


def _call(name, args):
    from fastmcp import Client

    async def go():
        async with Client(server.mcp) as c:
            return await c.call_tool(name, args, raise_on_error=False)

    return asyncio.run(go())


def test_model_sees_the_offset_hint(api):
    res = _call("list_events", {"start": "2026-10-20T09:00:00", "end": "2026-10-21T09:00:00Z"})
    assert res.is_error
    text = res.content[0].text
    assert "naive_datetime" in text and "UTC offset" in text and "Traceback" not in text


def test_model_gets_structured_results(api):
    api.add("GET", "/api/contacts", ok_list([{"id": "c", "displayName": "Ada"}], total=1,
                                            limit=50))
    res = _call("list_contacts", {"query": "ada"})
    assert not res.is_error
    assert res.structured_content["total"] == 1
    assert res.structured_content["items"][0]["name"] == "Ada"


def test_unknown_arguments_are_rejected(api):
    res = _call("list_contacts", {"serach": "typo"})
    assert res.is_error
