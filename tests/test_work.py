"""§102 write-surface parity: tasks, projects, contacts, companies, leads."""
from __future__ import annotations

import pytest

from meshbook_mcp import server
from tests._fakes import C1, CO1, L1, P1, T1, U1, U2, Err, ok, ok_list

USERS = ok_list([
    {"id": U1, "username": "chris", "displayName": "Chris"},
    {"id": U2, "username": "wren", "displayName": "Wren"},
])
PROJECTS = ok_list([{"id": P1, "name": "Books", "code": "BK", "status": "Active"}])
TASK = {"id": T1, "title": "Draft ch. 3", "status": "NotStarted", "priority": "High",
        "dueDate": "2026-10-24", "dueAt": None, "projectId": P1, "projectName": "Books"}


# ─── tasks ─────────────────────────────────────────────────────────────


def test_create_task_with_due_assignee_tags_parent(api):
    api.add("GET", "/api/projects", PROJECTS)
    api.add("GET", "/api/users", USERS)
    api.add("POST", "/api/tasks", ok(TASK))
    api.add("POST", f"/api/entities/task/{T1}/tags", ok({}))
    api.add("GET", f"/api/entities/task/{T1}/tags", ok_list([{"id": "tg", "name": "draft"}]))
    out = server.create_task("Draft ch. 3", "books", priority="high", due="2026-10-24",
                             assignee="@wren", tags=["draft"], parent=T1)
    body = api.body(api.calls("POST", "/api/tasks")[0])
    assert body == {"projectId": P1, "title": "Draft ch. 3", "priority": "High",
                    "dueDate": "2026-10-24", "assigneeUserId": U2, "parentTaskId": T1}
    assert api.body(api.calls("POST", f"/api/entities/task/{T1}/tags")[0]) == {"name": "draft"}
    assert out["tags"] == ["draft"] and out["project"] == "Books"


def test_create_task_due_datetime_needs_offset(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.create_task("x", "Books", due="2026-10-24T17:00:00")
    assert exc.value.code == "naive_datetime"
    api.add("GET", "/api/projects", PROJECTS)
    api.add("POST", "/api/tasks", ok(TASK))
    server.create_task("x", "Books", due="2026-10-24T17:00:00+11:00")
    assert api.body()["dueAt"] == "2026-10-24T17:00:00+11:00"


def test_create_task_rejects_unknown_priority(api):
    with pytest.raises(server.MeshbookError, match="Low, Normal, High, Urgent"):
        server.create_task("x", "Books", priority="whenever")


def test_update_task_partial_and_clear(api):
    api.add("PATCH", f"/api/tasks/{T1}", ok(TASK))
    server.update_task(T1, status="in progress", clear=["due", "assignee"])
    assert api.body() == {"status": "InProgress", "dueDate": None, "dueAt": None,
                          "assigneeUserId": None}
    server.update_task(T1, due="2026-11-01T09:00:00+11:00")
    assert api.body() == {"dueAt": "2026-11-01T09:00:00+11:00", "dueDate": None}


def test_update_task_clear_and_value_clash(api):
    with pytest.raises(server.MeshbookError, match="not both"):
        server.update_task(T1, due="2026-11-01", clear=["due"])
    with pytest.raises(server.MeshbookError, match="Clearable"):
        server.update_task(T1, clear=["title"])


def test_list_tasks_filters_sort_and_paging(api):
    api.add("GET", "/api/projects", PROJECTS)
    api.add("GET", "/api/me", ok({"user": {"id": U1}}))
    rows = [dict(TASK, id=f"t-{i}", priority=p) for i, p in enumerate(["Low", "Urgent", None])]
    api.add("GET", "/api/tasks", ok_list(rows, total=40, limit=3, offset=6))
    out = server.list_tasks(project="BK", assignee="me", status="blocked", sort="priority",
                            limit=3, offset=6)
    q = api.query(api.calls("GET", "/api/tasks")[0])
    assert q == {"limit": "3", "offset": "6", "projectId": P1, "assigneeId": U1,
                 "status": "Blocked", "sort": "priority"}
    assert [t["priority"] for t in out["items"]] == ["Urgent", "Low", None]
    assert out["total"] == 40 and out["truncated"] is True and out["nextOffset"] == 9
    assert {"priority", "dueDate", "dueAt"} <= set(out["items"][0])


def test_me_costs_one_api_me_per_client(api):
    """'me' resolves through client.me_id() (SDK 0.4.1), cached on the
    process's client: 0.8.0 called whoami() and so GET /api/me on every
    'me', and list_my_tasks fetched it again."""
    api.add("GET", "/api/me", ok({"user": {"id": U1}}))
    api.add("GET", "/api/tasks", ok_list([], total=0, limit=50))
    api.add("GET", "/api/users", USERS)
    server.list_tasks(assignee="me")
    server.list_tasks(assignee="@me")
    server.list_my_tasks()
    assert len(api.calls("GET", "/api/me")) == 1
    assert all(api.query(r)["assigneeId"] == U1 for r in api.calls("GET", "/api/tasks"))


def test_list_tasks_rejects_unknown_sort(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.list_tasks(sort="status")
    assert exc.value.code == "bad_argument"


def test_list_my_tasks_hides_closed_but_says_so(api):
    api.add("GET", "/api/me", ok({"user": {"id": U1}}))
    rows = [dict(TASK, id="a"), dict(TASK, id="b", status="Done"),
            dict(TASK, id="c", status="Cancelled")]
    api.add("GET", "/api/tasks", ok_list(rows, total=3, limit=100))
    out = server.list_my_tasks()
    assert [t["id"] for t in out["items"]] == ["a"]
    assert out["count"] == 1 and out["total"] == 3 and out["closedHidden"] == 2
    assert out["truncated"] is False
    assert server.list_my_tasks(include_closed=True)["count"] == 3


def test_get_and_delete_task(api):
    api.add("GET", f"/api/tasks/{T1}", ok(dict(TASK, description="d", subtasks=[
        {"id": "s", "title": "sub", "status": "Done"}])))
    out = server.get_task(T1)
    assert out["description"] == "d" and out["subtasks"][0]["title"] == "sub"
    with pytest.raises(server.MeshbookError, match="must be an id"):
        server.delete_task("Draft ch. 3")
    api.add("DELETE", f"/api/tasks/{T1}", ok({"id": T1, "deleted": True}))
    assert server.delete_task(T1)["deleted"] is True


# ─── projects ──────────────────────────────────────────────────────────


def test_project_lifecycle(api):
    api.add("GET", "/api/users", USERS)
    api.add("POST", "/api/projects", ok({"id": P1, "name": "Books", "status": "Planning"}))
    out = server.create_project("Books", code="BK", owner="@chris", status="planning")
    assert api.body() == {"name": "Books", "code": "BK", "status": "Planning",
                          "ownerUserId": U1}
    assert out["id"] == P1

    api.add("PATCH", f"/api/projects/{P1}", ok({"id": P1, "name": "Books", "status": "OnHold"}))
    server.update_project(P1, status="on hold", target_end_date="2026-12-01", clear=["code"])
    assert api.body() == {"status": "OnHold", "targetEndDate": "2026-12-01", "code": None}

    server.archive_project(P1)
    assert api.body() == {"status": "Complete"}
    server.archive_project(P1, status="cancelled")
    assert api.body() == {"status": "Cancelled"}
    with pytest.raises(server.MeshbookError):
        server.archive_project(P1, status="Active")


def test_project_dates_must_be_dates(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.update_project(P1, start_date="1 Nov")
    assert exc.value.code == "bad_date"


def test_project_members(api):
    api.add("GET", "/api/users", USERS)
    api.add("POST", f"/api/projects/{P1}/members", ok({
        "projectId": P1, "userId": U2, "displayName": "Wren", "roleOnProject": "Reviewer",
        "joinedAt": "2026-10-19T00:00:00+00:00", "created": True}))
    out = server.add_project_member(P1, "Wren", role="reviewer")
    assert api.body() == {"userId": U2, "roleOnProject": "Reviewer"}
    assert out["name"] == "Wren" and out["created"] is True
    api.add("DELETE", f"/api/projects/{P1}/members/{U2}",
            ok({"projectId": P1, "userId": U2, "removed": True}))
    assert server.remove_project_member(P1, "@wren")["removed"] is True


def test_list_get_delete_project(api):
    api.add("GET", "/api/projects", PROJECTS)
    out = server.list_projects(status="Active", search="bo")
    assert api.query()["statusFilter"] == "active" and out["items"][0]["code"] == "BK"
    api.add("GET", f"/api/projects/{P1}", ok({"id": P1, "name": "Books", "members": [
        {"userId": U1, "displayName": "Chris", "roleOnProject": "Owner"}]}))
    assert server.get_project("Books")["members"][0]["role"] == "Owner"
    with pytest.raises(server.MeshbookError):
        server.list_projects(status="archived")
    api.add("DELETE", f"/api/projects/{P1}", ok({"id": P1, "deleted": True}))
    assert server.delete_project(P1)["deleted"] is True


# ─── contacts ──────────────────────────────────────────────────────────


def test_create_contact_full_fields(api):
    api.add("POST", "/api/contacts", ok({"id": C1, "displayName": "Dr Jane Doe",
                                         "primaryCompanyId": None}))
    api.add("POST", f"/api/entities/contact/{C1}/tags", ok({}))
    api.add("GET", f"/api/entities/contact/{C1}/tags", ok_list([{"name": "vip"}]))
    out = server.create_contact("Jane", "Doe", email="jane@example.com", company="Acme",
                                phone="+61 400 000 000", title="Dr", role="CTO",
                                notes="Met at PyCon", tags=["vip"])
    assert api.body(api.calls("POST", "/api/contacts")[0]) == {
        "firstName": "Jane", "lastName": "Doe", "primaryEmail": "jane@example.com",
        "company": "Acme", "primaryPhone": "+61 400 000 000", "title": "Dr",
        "jobTitle": "CTO", "description": "Met at PyCon"}
    assert out["tags"] == ["vip"]
    assert "not matched" in out["note"]


def test_update_contact(api):
    api.add("PATCH", f"/api/contacts/{C1}", ok({"id": C1, "displayName": "Jane Doe"}))
    server.update_contact(C1, role="CEO", clear=["phone", "notes"])
    assert api.body() == {"primaryPhone": None, "jobTitle": "CEO", "description": None}


def test_get_contact_by_email_and_delete(api):
    api.add("GET", "/api/contacts", ok_list([{"id": C1, "displayName": "Jane Doe",
                                              "primaryEmail": "jane@example.com"}]))
    api.add("GET", f"/api/contacts/{C1}", ok({"id": C1, "displayName": "Jane Doe",
                                              "description": "notes"}))
    assert server.get_contact("jane@example.com")["notes"] == "notes"
    with pytest.raises(server.MeshbookError, match="must be an id"):
        server.delete_contact("Jane Doe")
    api.add("DELETE", f"/api/contacts/{C1}", ok({"id": C1, "deleted": True}))
    assert server.delete_contact(C1)["deleted"] is True


# ─── companies ─────────────────────────────────────────────────────────


def test_company_tools(api):
    api.add("GET", "/api/companies", ok_list([{"id": CO1, "name": "Acme"}], total=1, limit=50))
    assert server.list_companies("ac")["items"][0]["name"] == "Acme"
    assert api.query()["search"] == "ac"
    api.add("GET", f"/api/companies/{CO1}", ok({"id": CO1, "name": "Acme", "abn": "123"}))
    assert server.get_company(CO1)["abn"] == "123"
    api.add("POST", "/api/companies", ok({"id": CO1, "name": "Acme"}))
    server.create_company("Acme", website="https://acme.example")
    assert api.body() == {"name": "Acme", "website": "https://acme.example"}
    api.add("PATCH", f"/api/companies/{CO1}", ok({"id": CO1, "name": "Acme Pty"}))
    server.update_company(CO1, name="Acme Pty", clear=["phone"])
    assert api.body() == {"name": "Acme Pty", "phone": None}
    with pytest.raises(server.MeshbookError, match="Nothing to update"):
        server.update_company(CO1)
    api.add("DELETE", f"/api/companies/{CO1}", ok({"id": CO1, "deleted": True}))
    assert server.delete_company(CO1)["deleted"] is True


# ─── leads ─────────────────────────────────────────────────────────────


def test_delete_lead_and_list_paging(api):
    api.add("GET", "/api/leads", ok_list([{"id": L1, "title": "Deal"}], total=120, limit=1,
                                         offset=50))
    out = server.list_leads(limit=1, offset=50)
    assert out["total"] == 120 and out["nextOffset"] == 51
    with pytest.raises(server.MeshbookError, match="must be an id"):
        server.delete_lead("Deal")
    api.add("DELETE", f"/api/leads/{L1}", ok({"id": L1, "deleted": True}))
    assert server.delete_lead(L1)["deleted"] is True


def test_project_member_not_in_mesh_is_clean(api):
    api.add("POST", f"/api/projects/{P1}/members",
            Err(422, {"ok": False, "error": {"code": "not_a_mesh_member", "message": "nope"}}))
    with pytest.raises(server.MeshbookError) as exc:
        server.add_project_member(P1, U2)
    assert exc.value.code == "not_a_mesh_member" and exc.value.status == 422
