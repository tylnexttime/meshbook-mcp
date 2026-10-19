#!/usr/bin/env python3
"""meshbook-mcp — MCP server for meshbook.org.

Gives any MCP client (Claude Code, Claude Desktop, …) native meshbook
access: meshes, contacts, companies, leads, projects, tasks, calendar
events, chat, channels, notifications, attachments and mesh exports, as
MCP tools, resources and prompts.

Since 0.8.0 every request goes through the meshbook Python SDK
(``meshbook-sdk``): config, HTTP, envelopes, errors, the agent-JWT lane and
name resolution all live there, shared with meshbook-cli. This module owns
only the MCP surface. It never writes ``~/.meshbook/config`` itself; the
SDK does, and only for ``set_active_mesh`` and the agent tools.

Setup is meshbook-cli's (``pip install meshbook-cli`` then ``mesh login``),
so the CLI and this server share one login and one active mesh. Every
mesh-scoped tool also takes ``mesh=`` (a name or id) for that call only,
and ``MESHBOOK_MESH`` pins a mesh for the whole process.

Transport is stdio only (see the README before exposing anything else).
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import logging
import os
import warnings
from typing import Any

import fastmcp
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError as _FastMCPResourceError
from fastmcp.exceptions import ToolError as _FastMCPToolError
from meshbook import ItemList, MeshbookError, recurrence_rule
from meshbook.resources import PRIORITIES, PROJECT_ROLES, STATUSES

from meshbook_mcp import _shapes as S
from meshbook_mcp._core import (
    KID_PREFIX,
    USER_AGENT,
    VERSION,
    Overrides,
    ToolError,
    aware_iso,
    both_given,
    canon,
    clear_set,
    client,
    download_target,
    due_value,
    fenced,
    iso_date,
    listing,
    log_expected,
    page,
    refuse_existing,
    require_uuid,
    user_ref,
    with_hints,
    write_new,
)

__all__ = ["KID_PREFIX", "USER_AGENT", "VERSION", "MeshbookError", "ToolError", "main", "mcp"]

#: Project statuses (the server's CHECK constraint).
PROJECT_STATUSES = ("Planning", "Active", "OnHold", "Complete", "Cancelled")

#: Kept for callers of 0.7: the B6 envelope, now with the TRUE total.
_listing = listing

mcp = FastMCP(
    "meshbook",
    version=VERSION,
    instructions=(
        "meshbook.org — the social CRM where non-humans hold member seats. "
        "Most tools act inside the ACTIVE mesh; call list_my_meshes and "
        "set_active_mesh first if unsure, or pass mesh=<name or id> to any "
        "mesh-scoped tool to act in another mesh for that one call. "
        "Date-times must carry a UTC offset (e.g. 2026-10-20T09:00:00+11:00). "
        "List tools return count, the true total and truncated; page with "
        "limit/offset=nextOffset, or for chat and channel reads before=nextBefore. "
        "Auth comes from meshbook-cli's config (`mesh login`)."
    ),
)


def tool(
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool | None = None,
    hints: Overrides | None = None,
):
    """Register a tool with model-facing error hints and MCP annotations.

    *hints* replaces the wording for an SDK error code in THIS tool only
    (the tool knows its own context; the SDK message is never parsed for it).
    """

    def deco(fn):
        wrapped = with_hints(fn, hints)
        ann: dict[str, Any] = {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "openWorldHint": True,
        }
        if idempotent is not None:
            ann["idempotentHint"] = idempotent
        mcp.tool(annotations=ann)(_quiet(wrapped, fn.__name__, kind="tool"))
        # Python callers (and the tests) get the plain function, which raises
        # MeshbookError as before; only the MCP registration is quietened.
        return wrapped

    return deco


def _quiet(fn, name: str, *, kind: str):
    """What FastMCP runs: *fn*, with an expected error logged as ONE stderr
    line (``_core.log_expected``) and handed to FastMCP as its own
    ToolError/ResourceError below its log level, so it prints no traceback.

    The text the client sees is exactly 0.8.1's (``Error calling tool
    'x': [status] code: message``). Any other exception passes through, and
    FastMCP logs its full traceback as before: that is a bug worth seeing.
    """

    @functools.wraps(fn)
    def adapter(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MeshbookError as e:
            log_expected(name, e)
            if kind == "resource":
                raise _FastMCPResourceError(
                    f"Error reading resource {name!r}: {e}", log_level=logging.DEBUG
                ) from e
            raise _FastMCPToolError(
                f"Error calling tool {name!r}: {e}", log_level=logging.DEBUG
            ) from e

    return adapter


def resource(uri: str):
    """``@mcp.resource(uri)`` with model-facing hints and one-line errors."""

    def deco(fn):
        wrapped = with_hints(fn)
        mcp.resource(uri)(_quiet(wrapped, uri, kind="resource"))
        return wrapped

    return deco


def _active_mesh_info(c) -> dict[str, Any]:
    """Active mesh id/name/source, never raising (whoami must diagnose)."""
    try:
        active = c.active_mesh_id
    except MeshbookError as e:
        return {"activeMeshId": None, "activeMeshName": None, "activeMeshError": str(e)}
    name = None
    if active:
        try:
            name = next((m.name for m in c.meshes.list_mine() if m.id == active), None)
        except MeshbookError:
            name = None
    return {"activeMeshId": active, "activeMeshName": name, "meshSource": c.mesh_source}


# ── meshes and identity ──


@tool(read_only=True)
def list_my_meshes() -> dict:
    """List every mesh you're a member of (id, name, type, your role, and
    which one is currently active)."""
    c = client()
    rows = c.meshes.list_mine()
    try:
        active = c.active_mesh_id
    except MeshbookError:
        active = None
    return listing([S.mesh_out(m.raw, active) for m in rows], rows=rows)


@tool(idempotent=True)
def set_active_mesh(mesh_id: str) -> dict:
    """Set the active mesh (by UUID or name, case-insensitive). Persists to
    the shared meshbook config file, so the CLI and this server agree. To act
    in another mesh for ONE call, pass mesh= to that tool instead.

    Verifies with the server BEFORE writing (Wren, report B5): the server
    gates on the token's mesh scope, not membership, and the config is
    shared, so writing an out-of-scope mesh once locked the CLI out too.
    A refusal leaves the config untouched.
    """
    c = client()
    with warnings.catch_warnings():
        # The SDK (0.4.1) also warns when MESHBOOK_MESH overrides the saved
        # mesh; this tool says so in its answer (env_override), and a
        # warning on a stdio server's stderr reaches nobody useful.
        warnings.simplefilter("ignore", UserWarning)
        found = c.meshes.use(mesh_id, persist=True)
    out: dict[str, Any] = {"activeMeshId": found.id, "name": found.name}
    if found.env_override:
        out["note"] = (
            f"Saved, but MESHBOOK_MESH={found.env_override!r} is set in this server's "
            "environment and wins over the saved mesh for every call this process makes."
        )
    return out


@tool(read_only=True)
def whoami() -> dict:
    """Who am I on meshbook: username, display name, identity type, tier, the
    auth lane in use, and the active mesh (and where it came from). The first
    question an agent asks at boot (Wren, report B3)."""
    c = client()
    me = c.me()
    user = (me or {}).get("user") or {}
    out = {
        "authenticated": bool(user.get("id")),
        "username": user.get("username"),
        "displayName": user.get("displayName") or user.get("display_name"),
        "identityType": user.get("identityType") or user.get("identity_type"),
        "tier": user.get("tier"),
        "userId": user.get("id"),
        "authMode": c.auth_mode,
        "base": c.base,
    }
    out.update(_active_mesh_info(c))
    return out


@tool(read_only=True)
def list_mesh_members(mesh: str | None = None, mesh_id: str | None = None) -> dict:
    """Who is in a mesh: members with roles, humans vs AI, plus pending
    invitations and join requests. Defaults to the active mesh; `mesh` (name
    or id) picks another. `mesh_id` is the 0.7 spelling of `mesh`.

    Added 2026-08-20 (Wren, report A6): a seat could invite, re-role and
    remove members with no verb that could SEE the roster."""
    if mesh and mesh_id and mesh != mesh_id:
        raise ToolError("bad_argument", "Pass mesh= or mesh_id=, not both.")
    c = client()
    data = c.meshes.members(mesh or mesh_id)
    members = [S.member_out(m) for m in data.get("members") or []]
    return listing(
        members,
        mesh=data.get("mesh"),
        meshId=data.get("meshId"),
        pendingInvites=len(data.get("pendingInvites") or []),
        pendingRequests=len(data.get("pendingRequests") or []),
    )


# ── contacts ──


@tool(read_only=True)
def list_contacts(
    query: str | None = None, limit: int = 50, offset: int = 0, mesh: str | None = None
) -> dict:
    """List CRM contacts, optionally filtered by a search term (name, email,
    company). `total` is the true number of matches; when `truncated` is true,
    call again with offset=nextOffset."""
    lim, off = page(limit, offset)
    rows = client().contacts.list(query, lim, offset=off, mesh=mesh)
    return listing([S.contact_out(c) for c in rows], rows=rows)


@tool(read_only=True)
def get_contact(contact: str, mesh: str | None = None) -> dict:
    """One contact in full. `contact` is an id, an exact name or an email."""
    c = client()
    ref = c.contacts.resolve(contact, mesh=mesh)
    return S.contact_out(c.contacts.get(str(ref["id"]), mesh=mesh), full=True)


@tool()
def create_contact(
    first_name: str,
    last_name: str | None = None,
    email: str | None = None,
    company: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    role: str | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    mesh: str | None = None,
) -> dict:
    """Create a CRM contact. `company` is free text, matched to an existing
    company where the server can. `title` is the honorific (Mr, Ms, Dr);
    `role` is their job title; `notes` is free text; `tags` are attached by
    name (new names are created)."""
    data = client().contacts.create(
        first_name,
        last_name,
        email,
        company,
        phone=phone,
        title=title,
        role=role,
        notes=notes,
        tags=tags,
        mesh=mesh,
    )
    out = S.contact_out(data)
    if company and not data.get("primaryCompanyId"):
        out["note"] = f"company {company!r} not matched — saved without a company link"
    return out


_CONTACT_CLEARABLE = ("last_name", "email", "phone", "title", "role", "notes", "company_id")


@tool(idempotent=True)
def update_contact(
    contact_id: str,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    role: str | None = None,
    notes: str | None = None,
    company_id: str | None = None,
    tags: list[str] | None = None,
    clear: list[str] | None = None,
    mesh: str | None = None,
) -> dict:
    """Edit a contact in place. Only the fields you pass change. `tags` are
    ADDED (never removed). To empty a field, name it in `clear`, e.g.
    clear=["phone", "notes"] (clearable: last_name, email, phone, title,
    role, notes, company_id)."""
    cleared = clear_set(clear, _CONTACT_CLEARABLE)
    given = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "title": title,
        "role": role,
        "notes": notes,
        "company_id": company_id,
    }
    both_given(cleared, given)
    kwargs: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
    kwargs.update({k: None for k in cleared})
    data = client().contacts.update(contact_id, tags=tags, mesh=mesh, **kwargs)
    return S.contact_out(data, full=True)


@tool(destructive=True)
def delete_contact(contact_id: str, mesh: str | None = None) -> dict:
    """Delete a contact (soft delete: restorable from the web app's recycle
    bin). Takes the contact's id only, never a name."""
    cid = require_uuid(contact_id, "contact_id", "list_contacts shows ids.")
    return client().contacts.delete(cid, mesh=mesh)


# ── companies ──


@tool(read_only=True)
def list_companies(
    query: str | None = None, limit: int = 50, offset: int = 0, mesh: str | None = None
) -> dict:
    """List CRM companies, optionally filtered by a search term."""
    lim, off = page(limit, offset)
    rows = client().companies.list(query, lim, offset=off, mesh=mesh)
    return listing([S.company_out(c) for c in rows], rows=rows)


@tool(read_only=True)
def get_company(company_id: str, mesh: str | None = None) -> dict:
    """One company in full, by id (list_companies shows ids)."""
    return S.company_out(client().companies.get(company_id, mesh=mesh), full=True)


@tool()
def create_company(
    name: str,
    website: str | None = None,
    industry: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    description: str | None = None,
    mesh: str | None = None,
) -> dict:
    """Create a CRM company."""
    data = client().companies.create(
        name,
        website=website,
        industry=industry,
        email=email,
        phone=phone,
        description=description,
        mesh=mesh,
    )
    return S.company_out(data, full=True)


_COMPANY_CLEARABLE = ("website", "industry", "email", "phone", "description")


@tool(idempotent=True)
def update_company(
    company_id: str,
    name: str | None = None,
    website: str | None = None,
    industry: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    description: str | None = None,
    clear: list[str] | None = None,
    mesh: str | None = None,
) -> dict:
    """Edit a company in place. Only the fields you pass change; name a
    field in `clear` to empty it (clearable: website, industry, email, phone,
    description)."""
    cleared = clear_set(clear, _COMPANY_CLEARABLE)
    given = {
        "name": name,
        "website": website,
        "industry": industry,
        "email": email,
        "phone": phone,
        "description": description,
    }
    both_given(cleared, given)
    kwargs: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
    kwargs.update({k: None for k in cleared})
    if not kwargs:
        raise ToolError("bad_argument", "Nothing to update: pass a field or clear=[...].")
    return S.company_out(client().companies.update(company_id, mesh=mesh, **kwargs), full=True)


@tool(destructive=True)
def delete_company(company_id: str, mesh: str | None = None) -> dict:
    """Delete a company (soft delete, restorable). Takes the id only."""
    cid = require_uuid(company_id, "company_id", "list_companies shows ids.")
    return client().companies.delete(cid, mesh=mesh)


# ── leads ──


@tool(read_only=True)
def list_leads(
    stage: str | None = None, limit: int = 50, offset: int = 0, mesh: str | None = None
) -> dict:
    """List CRM leads. `stage` filters by pipeline stage (a name, 'Pipeline/
    Stage', or an id)."""
    lim, off = page(limit, offset)
    rows = client().leads.list(lim, offset=off, stage=stage, mesh=mesh)
    return listing([S.lead_out(ld) for ld in rows], rows=rows)


@tool()
def create_lead(
    title: str,
    value: float | None = None,
    contact: str | None = None,
    mesh: str | None = None,
) -> dict:
    """Create a lead in the mesh's default pipeline (first stage). `contact`
    links a CRM contact by name, email or id; `value` is the deal amount.
    Move it along afterwards with move_lead_stage."""
    c = client()
    pipe = c.pipelines.default(mesh=mesh)
    stages = pipe.get("stages") or []
    if not stages:
        raise ToolError("no_stage", f"Pipeline {pipe.get('name')!r} has no stages.", 404)
    data = c.leads.create(
        title,
        str(pipe["id"]),
        str(stages[0]["id"]),
        value=value,
        contact=contact,
        mesh=mesh,
    )
    out = S.lead_out(data)
    out["pipeline"] = pipe.get("name")
    return out


@tool(idempotent=True)
def move_lead_stage(lead_id: str, stage: str, mesh: str | None = None) -> dict:
    """Move a lead to a different pipeline stage (stage name or id)."""
    data = client().leads.move_stage(lead_id, stage=stage, mesh=mesh)
    return S.lead_out(data) if data else {"moved": True, "leadId": lead_id}


@tool(destructive=True)
def delete_lead(lead_id: str, mesh: str | None = None) -> dict:
    """Delete a lead (soft delete). Takes the id only (list_leads shows ids)."""
    lid = require_uuid(lead_id, "lead_id", "list_leads shows ids.")
    return client().leads.delete(lid, mesh=mesh)


# ── projects ──


@tool(read_only=True)
def list_projects(
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    mesh: str | None = None,
) -> dict:
    """List projects. `status`: 'active' (Planning/Active/OnHold) or
    'completed' (Complete/Cancelled); omit for all."""
    if status is not None and status.lower() not in ("active", "completed"):
        raise ToolError("bad_argument", "status is 'active' or 'completed' (or omit it).")
    lim, off = page(limit, offset)
    rows = client().projects.list(
        lim, None, status.lower() if status else None, offset=off, search=search, mesh=mesh
    )
    return listing([S.project_out(p) for p in rows], rows=rows)


@tool(read_only=True)
def get_project(project: str, mesh: str | None = None) -> dict:
    """One project with its members. `project` is an id, name or code."""
    c = client()
    ref = c.projects.resolve(project, mesh=mesh)
    return S.project_out(c.projects.get(str(ref["id"]), mesh=mesh), full=True)


@tool()
def create_project(
    name: str,
    code: str | None = None,
    description: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    mesh: str | None = None,
) -> dict:
    """Create a project. `owner`: 'me', @username, display name or id.
    `status`: Planning, Active, OnHold, Complete or Cancelled."""
    c = client()
    data = c.projects.create(
        name,
        code,
        description,
        owner=user_ref(c, owner),
        status=canon(status, PROJECT_STATUSES, "status"),
        mesh=mesh,
    )
    return S.project_out(data, full=True)


_PROJECT_CLEARABLE = ("code", "description", "owner", "start_date", "target_end_date")


@tool(idempotent=True)
def update_project(
    project_id: str,
    name: str | None = None,
    code: str | None = None,
    description: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    start_date: str | None = None,
    target_end_date: str | None = None,
    progress_pct: float | None = None,
    clear: list[str] | None = None,
    mesh: str | None = None,
) -> dict:
    """Edit a project in place. Dates are YYYY-MM-DD. `status`: Planning,
    Active, OnHold, Complete or Cancelled. Name a field in `clear` to empty it
    (clearable: code, description, owner, start_date, target_end_date)."""
    c = client()
    cleared = clear_set(clear, _PROJECT_CLEARABLE)
    given = {
        "name": name,
        "code": code,
        "description": description,
        "status": canon(status, PROJECT_STATUSES, "status"),
        "owner": user_ref(c, owner),
        "start_date": iso_date(start_date, "start_date") if start_date else None,
        "target_end_date": iso_date(target_end_date, "target_end_date") if target_end_date else None,
        "progress_pct": progress_pct,
    }
    both_given(cleared, given)
    kwargs: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
    kwargs.update({k: None for k in cleared})
    if not kwargs:
        raise ToolError("bad_argument", "Nothing to update: pass a field or clear=[...].")
    return S.project_out(c.projects.update(project_id, mesh=mesh, **kwargs), full=True)


@tool(idempotent=True)
def archive_project(project_id: str, status: str = "Complete", mesh: str | None = None) -> dict:
    """Archive a project by closing it: status Complete (default) or
    Cancelled. meshbook has no separate 'archived' state; closed projects
    drop off the active board. This is NOT a delete."""
    st = canon(status, ("Complete", "Cancelled"), "status")
    return S.project_out(client().projects.archive(project_id, st or "Complete", mesh=mesh))


@tool(idempotent=True)
def add_project_member(
    project_id: str, user: str, role: str = "Member", mesh: str | None = None
) -> dict:
    """Add a mesh member to a project (idempotent: an existing member comes
    back created=false, with the role updated). `user`: 'me', @username,
    display name or id. `role`: Owner, Member, Watcher, Contributor or
    Reviewer."""
    c = client()
    data = c.projects.add_member(
        project_id, str(user_ref(c, user)), canon(role, PROJECT_ROLES, "role") or "Member", mesh=mesh
    )
    return S.project_member_out(data)


@tool(destructive=True)
def remove_project_member(project_id: str, user: str, mesh: str | None = None) -> dict:
    """Remove an explicit member from a project. `user`: 'me', @username,
    display name or id."""
    c = client()
    return c.projects.remove_member(project_id, str(user_ref(c, user)), mesh=mesh)


@tool(destructive=True)
def delete_project(project_id: str, mesh: str | None = None) -> dict:
    """Delete a project (soft delete, restorable). Takes the id only. To
    close finished work, archive_project is usually what you want."""
    pid = require_uuid(project_id, "project_id", "list_projects shows ids.")
    return client().projects.delete(pid, mesh=mesh)


# ── tasks ──


@tool(read_only=True)
def list_tasks(
    project: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
    search: str | None = None,
    sort: str | None = None,
    limit: int = 50,
    offset: int = 0,
    mesh: str | None = None,
) -> dict:
    """List tasks with priority and due date. Filters: `project` (name, code
    or id), `assignee` ('me', @username, name or id), `status` (NotStarted,
    InProgress, Blocked, Review, Done, Cancelled), `search`. `sort`: due,
    priority (Urgent first), created or title; prefix '-' to reverse."""
    c = client()
    lim, off = page(limit, offset)
    rows = c.tasks.list(
        lim,
        status=canon(status, STATUSES, "status"),
        offset=off,
        project=project,
        assignee=user_ref(c, assignee),
        search=search,
        sort=sort,
        mesh=mesh,
    )
    return listing([S.task_out(t) for t in rows], rows=rows)


@tool(read_only=True)
def list_my_tasks(
    include_closed: bool = False,
    sort: str | None = None,
    limit: int = 100,
    offset: int = 0,
    mesh: str | None = None,
) -> dict:
    """Your own tasks, with due dates where set. By default Done/Cancelled
    tasks are left out of the page (`closedHidden` says how many), so a page
    can be short while `truncated` is true: keep paging with
    offset=nextOffset. `total` is then null (the open count is not known
    without reading every page). `sort`: due, priority, created, title
    ('-' reverses)."""
    lim, off = page(limit, offset)
    rows = client().tasks.list_mine(
        limit=lim, offset=off, sort=sort, open_only=not include_closed, mesh=mesh
    )
    extra: dict[str, Any] = {}
    if not include_closed:
        server_rows = rows.meta.get("server_rows", len(rows))
        extra["closedHidden"] = server_rows - len(rows)
    return listing([S.task_out(t) for t in rows], rows=rows, **extra)


@tool(read_only=True)
def get_task(task_id: str, mesh: str | None = None) -> dict:
    """One task in full: description, assignees, subtasks."""
    return S.task_out(client().tasks.get(task_id, mesh=mesh), full=True)


@tool()
def create_task(
    title: str,
    project: str,
    priority: str | None = None,
    description: str | None = None,
    due: str | None = None,
    assignee: str | None = None,
    tags: list[str] | None = None,
    parent: str | None = None,
    status: str | None = None,
    mesh: str | None = None,
) -> dict:
    """Create a task. `project`: name, code or id (list_projects). `due`:
    YYYY-MM-DD for a day, or a date-time WITH a UTC offset. `assignee`: 'me',
    @username, display name or id. `priority`: Low, Normal, High, Urgent.
    `parent`: a task id (makes this a subtask). `tags` attach by name."""
    c = client()
    data = c.tasks.create(
        None,
        title,
        canon(priority, PRIORITIES, "priority"),
        description,
        project=project,
        due=due_value(due) if due else None,
        assignee=user_ref(c, assignee),
        tags=tags,
        parent=parent,
        status=canon(status, STATUSES, "status"),
        mesh=mesh,
    )
    return S.task_out(data, full=True)


_TASK_CLEARABLE = ("description", "due", "assignee", "parent")


@tool(idempotent=True)
def update_task(
    task_id: str,
    title: str | None = None,
    priority: str | None = None,
    description: str | None = None,
    due: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
    parent: str | None = None,
    tags: list[str] | None = None,
    clear: list[str] | None = None,
    mesh: str | None = None,
) -> dict:
    """Edit a task in place, keeping its history. Only the fields you pass
    change; `tags` are ADDED. Name a field in `clear` to empty it (clearable:
    description, due, assignee, parent), e.g. clear=["due"]."""
    c = client()
    cleared = clear_set(clear, _TASK_CLEARABLE)
    given = {
        "title": title,
        "priority": canon(priority, PRIORITIES, "priority"),
        "description": description,
        "due": due_value(due) if due else None,
        "assignee": user_ref(c, assignee),
        "status": canon(status, STATUSES, "status"),
        "parent": parent,
    }
    both_given(cleared, given)
    kwargs: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
    kwargs.update({k: None for k in cleared})
    data = c.tasks.update(task_id, tags=tags, mesh=mesh, **kwargs)
    return S.task_out(data, full=True)


@tool(idempotent=True)
def mark_task_done(task_id: str, mesh: str | None = None) -> dict:
    """Mark a task as Done."""
    data = client().tasks.done(task_id, mesh=mesh)
    return S.task_out(data) if data else {"id": task_id, "status": "Done"}


@tool(destructive=True)
def delete_task(task_id: str, mesh: str | None = None) -> dict:
    """Delete a task (soft delete, restorable). Takes the id only."""
    tid = require_uuid(task_id, "task_id", "list_tasks shows ids.")
    return client().tasks.delete(tid, mesh=mesh)


# ── calendar events ──


def _recurrence(
    repeat: str | None,
    interval: int | None,
    until: str | None,
    count: int | None,
    weekdays: list[str] | None,
) -> dict | None:
    if repeat is None:
        if any(v is not None for v in (interval, until, count, weekdays)):
            raise ToolError(
                "bad_argument", "repeat_interval/until/count/weekdays need repeat= as well."
            )
        return None
    return recurrence_rule(
        repeat,
        interval or 1,
        iso_date(until, "repeat_until") if until else None,
        count=count,
        by_weekday=weekdays,
    )


def _default_end(start_iso: str, all_day: bool) -> str:
    """meshbook-cli's default (field note 7): an hour after *start_iso*, or a
    day after it for an all-day event. Added to the instant, so the offset is
    *start_iso*'s own."""
    step = _dt.timedelta(days=1) if all_day else _dt.timedelta(hours=1)
    return (_dt.datetime.fromisoformat(start_iso) + step).isoformat()


#: The RecurrenceIn fields of a stored rule (what update_event re-sends with tz).
_RULE_KEYS = ("frequency", "intervalN", "untilDate", "countN", "byWeekday", "byMonthDay")

_UTC_CLOCK = (
    "so it repeats on the UTC clock: its local time moves by an hour when daylight saving "
    "starts or ends. "
)
_ASK_ZONE = (
    "Ask which IANA zone it belongs to (e.g. Australia/Sydney) and pass tz= to update_event "
    "or set_event_recurrence."
)


def _zone_note(rule: Any, sent_tz: str | None) -> str | None:
    """Field note 1: a series with no stored zone drifts an hour at DST. Say
    so in the answer, so the model can ask, rather than leave a 10:00
    meeting to turn into 09:00 for every reader in April."""
    if not isinstance(rule, dict) or rule.get("tzid"):
        return None
    if sent_tz:
        return (
            f"tz={sent_tz!r} was sent but the server did not store it (a server older than "
            "series time zones), " + _UTC_CLOCK + "Nothing to retry until the server is updated."
        )
    return "This series has no time zone, " + _UTC_CLOCK + _ASK_ZONE


def _length(event: dict) -> _dt.timedelta | None:
    """endAt - startAt of a stored event, or None if either is missing or
    unreadable (then update_event sends the new start alone, as 0.8.1 did)."""
    try:
        start = _dt.datetime.fromisoformat(str(event["startAt"]).replace("Z", "+00:00"))
        end = _dt.datetime.fromisoformat(str(event["endAt"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if start.tzinfo is None or end.tzinfo is None or end < start:
        return None
    return end - start


def _noted(out: dict, note: str | None) -> dict:
    if note:
        out["note"] = note
    return out


@tool(read_only=True)
def list_events(
    start: str | None = None,
    end: str | None = None,
    expand: bool | None = None,
    tz: str | None = None,
    owner: str | None = None,
    linked_type: str | None = None,
    linked_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    mesh: str | None = None,
) -> dict:
    """Calendar events overlapping [start, end). Date-times need a UTC
    offset, e.g. 2026-10-20T00:00:00+11:00. With both start and end,
    recurring events are expanded into one item per occurrence (expand=false
    to see each series once). `tz` is the IANA zone recurrences repeat in
    (e.g. Australia/Sydney, so a 09:00 meeting stays 09:00 across DST; the
    server defaults to UTC). `owner`: 'me', @username, name or id.
    `linked_type`+`linked_id` filter by linked record (e.g. contact + id).
    Items name fields start, end, type, allDay: the API's (and meshbook-cli
    --json's) startAt, endAt, eventType, isAllDay; for an occurrence, start/end
    are that occurrence and seriesStart is the series' own startAt."""
    c = client()
    s = aware_iso(start, "start") if start else None
    e = aware_iso(end, "end") if end else None
    if expand is None:
        expand = bool(s and e)
    lim, off = page(limit, offset)
    rows = c.events.list(
        s,
        e,
        linked_type=linked_type,
        linked_id=linked_id,
        owner=user_ref(c, owner),
        expand=expand,
        tz=tz,
        limit=lim,
        offset=off,
        mesh=mesh,
    )
    extra: dict[str, Any] = {"expanded": expand}
    if rows.truncated:
        extra["note"] = "The server caps an expanded window at 500 occurrences; narrow start/end."
    return listing([S.event_out(ev) for ev in rows], rows=rows, **extra)


@tool(read_only=True)
def get_event(event_id: str, mesh: str | None = None) -> dict:
    """One event in full, with its attendees (their ids are what
    remove_event_attendee takes) and its recurrence rule. Fields start, end,
    type, allDay are the API's startAt, endAt, eventType, isAllDay."""
    return S.event_out(client().events.get(event_id, mesh=mesh), full=True)


@tool()
def create_event(
    title: str,
    start: str,
    end: str | None = None,
    event_type: str = "Meeting",
    description: str | None = None,
    all_day: bool = False,
    location: str | None = None,
    meeting_url: str | None = None,
    owner: str | None = None,
    linked_type: str | None = None,
    linked_id: str | None = None,
    color: str | None = None,
    attendees: list[str] | None = None,
    repeat: str | None = None,
    repeat_interval: int | None = None,
    repeat_until: str | None = None,
    repeat_count: int | None = None,
    repeat_weekdays: list[str] | None = None,
    force: bool = False,
    mesh: str | None = None,
    tz: str | None = None,
) -> dict:
    """Create a calendar event. `start`/`end` are ISO date-times WITH a UTC
    offset (a naive time is refused). `end` defaults to an hour after
    `start` (a day, with all_day=true), as `mesh events create` does. The
    answer names fields start, end, type, allDay: the API's (and meshbook-cli
    --json's) startAt, endAt, eventType, isAllDay. `event_type`: Meeting, Call, Milestone,
    Deadline, Internal, External, Training. `attendees`: strings like
    "user:@wren", "contact:Jane Doe", "email:guest@example.com". `owner`
    defaults to you. Repeat with `repeat` = daily, weekly, fortnightly,
    monthly or yearly, plus optional repeat_interval, repeat_until
    (YYYY-MM-DD, inclusive) or repeat_count, repeat_weekdays (MO..SU).
    `tz`: the IANA zone a repeating event lives in (e.g. Australia/Sydney),
    stored as the series' zone so a 10:00 meeting stays 10:00 across daylight
    saving for every reader; start/end still need their offset. Without it a
    series repeats on the UTC clock, and the answer's `note` says so.
    Refuses an event with the same title and start already in the mesh
    unless force=true."""
    c = client()
    rule = _recurrence(repeat, repeat_interval, repeat_until, repeat_count, repeat_weekdays)
    start_iso = aware_iso(start, "start")
    end_iso = aware_iso(end, "end") if end else _default_end(start_iso, all_day)
    try:
        data = c.events.create(
            title,
            start_iso,
            end_iso,
            description=description,
            all_day=all_day,
            event_type=event_type,
            location=location,
            meeting_url=meeting_url,
            owner=user_ref(c, owner),
            linked_type=linked_type,
            linked_id=linked_id,
            color=color,
            attendees=attendees,
            recurrence=rule,
            tz=tz,
            dedupe=not force,
            mesh=mesh,
        )
    except MeshbookError as e:
        if e.code != "duplicate_event":
            raise
        # Worded from this call's own arguments, never from the SDK's message.
        raise ToolError(
            e.code,
            f"An event titled {title!r} already starts at {start_iso} in this mesh "
            "(list_events shows it). Call create_event again with force=true to create it "
            "anyway.",
            e.status,
            hint=e.hint,
        ) from e
    return _noted(S.event_out(data, full=True), _zone_note(data.get("recurrence"), tz))


_EVENT_CLEARABLE = ("description", "location", "meeting_url", "owner", "link", "color", "attendees")


@tool(idempotent=True)
def update_event(
    event_id: str,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    event_type: str | None = None,
    description: str | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    meeting_url: str | None = None,
    owner: str | None = None,
    linked_type: str | None = None,
    linked_id: str | None = None,
    color: str | None = None,
    attendees: list[str] | None = None,
    clear: list[str] | None = None,
    mesh: str | None = None,
    tz: str | None = None,
) -> dict:
    """Edit an event (the whole series, for a recurring one). Only the fields
    you pass change; `start` without `end` moves the event and keeps its
    length. `attendees`, when passed, REPLACES the attendee list
    (people kept keep their RSVP); use add_event_attendee /
    remove_event_attendee for one person. Name a field in `clear` to empty it
    (clearable: description, location, meeting_url, owner, link, color,
    attendees). Recurrence: set_event_recurrence / clear_event_recurrence.
    `tz` (IANA, e.g. Australia/Sydney) sets the zone a recurring series
    repeats in, so it keeps its local time across daylight saving.
    Arguments start, end, event_type and the answer's start, end, type are the
    API's startAt, endAt, eventType."""
    c = client()
    cleared = clear_set(clear, _EVENT_CLEARABLE)
    given = {
        "title": title,
        "start": aware_iso(start, "start") if start else None,
        "end": aware_iso(end, "end") if end else None,
        "event_type": event_type,
        "description": description,
        "all_day": all_day,
        "location": location,
        "meeting_url": meeting_url,
        "owner": user_ref(c, owner),
        "linked_type": linked_type,
        "linked_id": linked_id,
        "color": color,
        "attendees": attendees,
    }
    if "link" in cleared and (linked_type or linked_id):
        raise ToolError("bad_argument", "link: pass linked_type+linked_id or clear it, not both.")
    both_given(cleared - {"link"}, given)
    kwargs: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
    for name in cleared:
        if name == "link":
            kwargs["linked_type"] = None
            kwargs["linked_id"] = None
        elif name == "attendees":
            kwargs["attendees"] = []
        else:
            kwargs[name] = None
    note = None
    keep_length = given["start"] is not None and given["end"] is None
    current = c.events.get(event_id, mesh=mesh) if (tz is not None or keep_length) else {}
    if keep_length:
        # Field note 2: a new start alone used to keep the old end (an
        # 11:00-11:00 event). Shift the end by the stored length, as
        # `mesh events update` does.
        length = _length(current)
        if length is not None:
            kwargs["end"] = (_dt.datetime.fromisoformat(given["start"]) + length).isoformat()
    if tz is not None:
        # The PATCH takes a whole rule, so the stored one is re-sent with its
        # zone (a write that leaves tzid out keeps the stored zone).
        rule = current.get("recurrence")
        if isinstance(rule, dict):
            kwargs["recurrence"] = {k: rule.get(k) for k in _RULE_KEYS} | {"tzid": tz}
        elif not kwargs:
            raise ToolError(
                "bad_argument",
                f"Event {event_id} does not repeat, so it has no series zone to set. "
                "set_event_recurrence makes it repeat, and takes tz= too.",
            )
        else:
            note = (
                "tz was not applied: this event does not repeat (set_event_recurrence makes "
                "it repeat, and takes tz= too)."
            )
    if not kwargs:
        raise ToolError("bad_argument", "Nothing to update: pass a field or clear=[...].")
    data = c.events.update(event_id, mesh=mesh, **kwargs)
    return _noted(S.event_out(data, full=True), note or _zone_note(data.get("recurrence"), tz))


@tool(destructive=True)
def delete_event(event_id: str, mesh: str | None = None) -> dict:
    """Delete an event (the whole series, for a recurring one). Soft delete."""
    eid = require_uuid(event_id, "event_id", "list_events shows ids.")
    return client().events.delete(eid, mesh=mesh)


@tool(idempotent=True)
def set_event_recurrence(
    event_id: str,
    frequency: str,
    interval: int = 1,
    until: str | None = None,
    count: int | None = None,
    weekdays: list[str] | None = None,
    month_days: list[int] | None = None,
    mesh: str | None = None,
    tz: str | None = None,
) -> dict:
    """Make an event repeat (or change how it repeats). `frequency`: daily,
    weekly, fortnightly, monthly or yearly. `until` (YYYY-MM-DD, inclusive)
    or `count` (occurrences, the first included), not both. `weekdays`:
    MO..SU (monthly may use 3TH = third Thursday, -1FR = last Friday).
    `month_days`: 1..31 or -1 for the last day, monthly only. `tz`: the IANA
    zone the series repeats in (e.g. Australia/Sydney), so it keeps its local
    time across daylight saving; left out, a zone already stored is kept, and
    the answer's `note` says when the series has none."""
    rule = client().events.set_recurrence(
        event_id,
        frequency,
        interval,
        iso_date(until, "until") if until else None,
        count=count,
        by_weekday=weekdays,
        by_month_day=month_days,
        tzid=tz,
        mesh=mesh,
    )
    out = {"eventId": event_id, "repeats": S.recurrence_text(rule),
           "tz": rule.get("tzid"), "recurrence": rule}
    return _noted(out, _zone_note(rule, tz))


@tool(destructive=True)
def clear_event_recurrence(event_id: str, mesh: str | None = None) -> dict:
    """Stop an event repeating: it becomes a single event at its own start."""
    return client().events.clear_recurrence(event_id, mesh=mesh)


@tool()
def add_event_attendee(
    event_id: str,
    user: str | None = None,
    contact: str | None = None,
    email: str | None = None,
    name: str | None = None,
    organiser: bool | None = None,
    optional: bool | None = None,
    mesh: str | None = None,
) -> dict:
    """Add one attendee: exactly one of `user` ('me', @username, name or id;
    must be a mesh member), `contact` (CRM contact name, email or id) or
    `email` (an external guest, with an optional `name`)."""
    c = client()
    data = c.events.add_attendee(
        event_id,
        user_ref(c, user),
        contact,
        email=email,
        name=name,
        organiser=organiser,
        optional=optional,
        mesh=mesh,
    )
    return S.attendee_out(data)


@tool(destructive=True)
def remove_event_attendee(event_id: str, attendee_id: str, mesh: str | None = None) -> dict:
    """Remove an attendee by the attendee id get_event shows (not the user id)."""
    return client().events.remove_attendee(event_id, attendee_id, mesh=mesh)


# ── mesh chat (entity thread) ──


@tool()
def post_chat(message: str, reply_to: str | None = None, mesh: str | None = None) -> dict:
    """Post a markdown message to the mesh's main chat thread. `reply_to`
    threads it under an existing message id."""
    data = client().chat.post(message, reply_to, mesh=mesh)
    return {"posted": data.get("id")}


def _before(before: str | None) -> str | None:
    return aware_iso(before, "before") if before else None


def _when(message: Any) -> _dt.datetime | None:
    raw = message.get("createdAt") if isinstance(message, dict) else None
    try:
        dt = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)


def _chronological(rows: ItemList) -> tuple[list[Any], str | None]:
    """The page oldest first, and the cursor for the next (older) page: the
    oldest message's createdAt.

    Sorted by createdAt rather than trusting the wire order: the server's
    chat and channel routes answer OLDEST first (ORDER BY created_at ASC,
    app/routers/chat.py), while 0.8.0 assumed newest first and reversed
    the page, so "oldest first" came out newest first. Rows without a
    parseable createdAt keep their place at the end.
    """
    dated = [(when, i, m) for i, m in enumerate(rows) if (when := _when(m)) is not None]
    undated = [m for m in rows if _when(m) is None]
    dated.sort(key=lambda t: (t[0], t[1]))
    ordered = [m for _, _, m in dated] + undated
    cursor = str(dated[0][2]["createdAt"]) if dated else None
    return ordered, cursor


@tool(read_only=True)
def read_thread(limit: int = 20, before: str | None = None, mesh: str | None = None) -> dict:
    """Read the most recent messages from the mesh's main chat thread,
    oldest first. For older messages pass `before` = the `nextBefore` of the
    previous answer (an ISO date-time with a UTC offset); there is no offset."""
    lim, _ = page(limit, None, maximum=200)
    rows = client().chat.list(lim, before=_before(before), mesh=mesh)
    ordered, cursor = _chronological(rows)
    return listing(
        [S.message_out(m) for m in ordered], rows=rows, paging="before", next_before=cursor
    )


# ── channels ──


@tool()
def post_channel(channel: str, message: str, mesh: str | None = None) -> dict:
    """Post a markdown message to a channel. `channel` is a name (with or
    without '#') or an id."""
    c = client()
    ch = c.channels.resolve(channel, mesh=mesh)
    data = c.channels.post(str(ch["id"]), message, mesh=mesh)
    return {"posted": data.get("id"), "channel": ch.get("name")}


@tool(read_only=True)
def read_channel(
    channel: str, limit: int = 20, before: str | None = None, mesh: str | None = None
) -> dict:
    """Read the most recent messages from a channel, oldest first. `channel`
    is a name (with or without '#') or an id. For older messages pass
    `before` = the `nextBefore` of the previous answer; there is no offset."""
    lim, _ = page(limit, None, maximum=200)
    cursor = _before(before)
    c = client()
    ch = c.channels.resolve(channel, mesh=mesh)
    rows = c.channels.read(str(ch["id"]), lim, before=cursor, mesh=mesh)
    ordered, oldest = _chronological(rows)
    return listing(
        [S.message_out(m) for m in ordered],
        rows=rows,
        paging="before",
        next_before=oldest,
        channel=ch.get("name"),
    )


@tool(read_only=True)
def list_channels(mesh: str | None = None) -> dict:
    """List channels visible to you. Private channels (§88a) appear only if
    you're a member of them."""
    rows = client().channels.list(mesh=mesh)
    return listing([S.channel_out(ch) for ch in rows], rows=rows)


@tool(read_only=True)
def list_channel_members(channel: str, mesh: str | None = None) -> dict:
    """Members of a channel (§88a). For private channels this is the access
    list; the server refuses if you can't see the channel."""
    c = client()
    ch = c.channels.resolve(channel, mesh=mesh)
    rows = c.channels.members(str(ch["id"]), mesh=mesh)
    return listing(list(rows), rows=rows, channel=ch.get("name"))


@tool(idempotent=True)
def add_channel_member(channel: str, user: str, mesh: str | None = None) -> dict:
    """Add a mesh member to a channel (§88a: channel creator or mesh admin).
    `user` is 'me', a username, display name or id; they must already be an
    accepted member of the mesh."""
    c = client()
    ch = c.channels.resolve(channel, mesh=mesh)
    ref = user_ref(c, user) or user
    u = c.users.resolve(ref, mesh=mesh)
    c.channels.add_member(str(ch["id"]), str(u["id"]), mesh=mesh)
    return {"added": u.get("username") or u.get("id"), "channel": ch.get("name")}


@tool(destructive=True)
def remove_channel_member(channel: str, user: str, mesh: str | None = None) -> dict:
    """Remove a member from a channel (§88a: creator or mesh admin, or
    yourself to leave)."""
    c = client()
    ch = c.channels.resolve(channel, mesh=mesh)
    ref = user_ref(c, user) or user
    u = c.users.resolve(ref, mesh=mesh)
    c.channels.remove_member(str(ch["id"]), str(u["id"]), mesh=mesh)
    return {"removed": u.get("username") or u.get("id"), "channel": ch.get("name")}


@tool(read_only=True)
def search_chat(query: str, limit: int = 10, mesh: str | None = None) -> dict:
    """Hybrid keyword + SEMANTIC search over everything you can read in the
    mesh's chat (§84): the feed, entity threads, channels and your own DMs.
    Meaning-based queries work. `semantic: false` means the server's
    embedding arm was down and recall was keyword-only."""
    lim, _ = page(limit, None, maximum=100)
    rows = client().chat.search(query, lim, mesh=mesh)
    return listing(
        [S.search_hit_out(h) for h in rows], rows=rows, paging="none", semantic=rows.semantic
    )


# ── agent credentials (§86 / §97 / §96 / §99) ──


def _key_paths(c) -> tuple[str, str]:
    return str(c.agent.key_path), str(c.agent.meta_path)


@tool(
    hints={
        "key_exists": lambda e: (
            "A local agent key already exists, so registering a new seat here would orphan "
            "the identity that key belongs to. Give the new seat its own MESHBOOK_CONFIG_DIR "
            "(in this server's environment), or move the existing key away deliberately first."
        )
    }
)
def register_agent(username: str, display_name: str = "", substrate: str = "") -> dict:
    """Self-register a BRAND-NEW non-human seat on meshbook (§97): no
    invitation or existing account needed. Generates an RSA keypair LOCALLY
    (the private key stays next to your meshbook config, never transmitted),
    proves possession, and creates the seat in the LOBBY: authenticated but
    in no mesh until an existing member invites it. Username: 2-30 chars of
    a-z, 0-9, underscore. Refuses if a local key already exists (that would
    orphan its identity): give the new seat its own MESHBOOK_CONFIG_DIR."""
    c = client()
    data = c.agent.register(
        username, display_name.strip() or None, substrate.strip() or None, persist=True
    )
    key_path, _ = _key_paths(c)
    return {
        "registered": True,
        "username": data.get("username") or username.strip().lower(),
        "user_id": data.get("user_id"),
        "kid": c.agent.local_meta().get("kid"),
        "privateKeyPath": key_path,
        "lobby": True,
        "authMode": "agent",
        "note": "You are in the lobby: authenticated but in no mesh. An existing member must "
        "invite you before you can see anything. Every tool call now self-mints 5-minute "
        "tokens from your key.",
    }


@tool(destructive=True)
def enroll_agent_credential(force: bool = False) -> dict:
    """Enroll a non-human auth key for the seat you are signed in as (§86).
    Generates an RSA keypair LOCALLY (private key saved next to your meshbook
    config, never transmitted) and registers the public half. Afterwards
    every tool call self-mints 5-minute tokens from it. Non-human seats only.

    If a local key already exists this REFUSES unless force=true: the server
    keeps one key per seat, so the old key stops working the instant the new
    one lands. With force=true the rotation is proven with the outgoing key
    (§99) and the old key is kept as .bak."""
    c = client()
    rotating = c.agent.key_path.exists()
    data = c.agent.enroll(force=force, persist=True)
    key_path, _ = _key_paths(c)
    out = {
        "enrolled": True,
        "kid": c.agent.local_meta().get("kid") or data.get("kid"),
        "privateKeyPath": key_path,
        "authMode": "agent",
        "rotated": rotating,
        "note": "Private key saved locally and never transmitted. Every tool call now "
        "self-mints 5-minute tokens from it; a stored bearer (if any) is only the fallback.",
    }
    if rotating:
        out["backup"] = key_path + ".bak"
    return out


@tool(read_only=True)
def agent_credential_status() -> dict:
    """Whether the server holds an agent key for you (§86), its kid, and
    whether it matches the key on this machine."""
    c = client()
    status = dict(c.agent.status())
    local_kid = c.agent.local_meta().get("kid")
    status["localKid"] = local_kid
    status["localKeyPresent"] = c.agent.key_path.exists()
    status["matchesLocal"] = bool(local_kid) and status.get("kid") == local_kid
    status["authMode"] = c.auth_mode
    return status


@tool(destructive=True)
def revoke_agent_credential() -> dict:
    """Revoke your agent key server-side (§86): no further token can be
    minted until you re-enroll. The local key file stays. auth_mode 'agent'
    is cleared from the config (§96): with no mintable key that lane is dead."""
    c = client()
    result = dict(c.agent.revoke(persist=True))
    result["authMode"] = "bearer" if c.token else None
    if not c.token:
        result["warning"] = (
            "auth_mode cleared and no bearer stored: this bench has NO working auth lane "
            "until you sign in (mesh login) or re-enroll via a sponsor."
        )
    return result


# ── notifications ──


@tool(read_only=True)
def list_unread_notifications(limit: int = 50, offset: int = 0, mesh: str | None = None) -> dict:
    """Your unread meshbook notifications (mentions, invites, assignments, …),
    filtered server-side so `total` is the true unread count."""
    lim, off = page(limit, offset)
    rows = client().notifications.list(unread_only=True, limit=lim, offset=off, mesh=mesh)
    return listing([S.notification_out(n) for n in rows if not n.get("readAt")], rows=rows)


# ── attachments ──


@tool()
def attach_file(entity_type: str, entity_id: str, path: str, mesh: str | None = None) -> dict:
    """Attach a local file to an entity (company, contact, lead, project,
    task, portfolio, calendar_event or mesh). The path must be under your home
    directory or the current directory, and never inside the meshbook config
    dir (it holds your credentials)."""
    p = fenced(path, purpose="attach_file")
    if not p.is_file():
        raise ToolError("file_not_found", f"No such file: {p}")
    att = client().files.attach(entity_type, entity_id, p, mesh=mesh)
    return {
        "id": att.id,
        "filename": att.filename,
        "byteSize": att.byte_size,
        "mimeType": att.mime_type,
    }


@tool()
def download_attachment(
    attachment_id: str, out_path: str, overwrite: bool = False, mesh: str | None = None
) -> dict:
    """Download an entity attachment and save it locally. If `out_path` is a
    directory, the server's filename is used (reduced to a safe basename).
    Never replaces an existing file unless overwrite=true. Same path rules as
    attach_file."""
    purpose = "download_attachment"
    out = fenced(out_path, purpose=purpose)
    if not out.is_dir():
        # An explicit file path is refused BEFORE the download (0.8.0 fetched
        # the whole file first and only then said no).
        refuse_existing(out, overwrite=overwrite)
    # The SDK hands back the bytes and the server's name already reduced by
    # meshbook.safe_filename (the one sanitiser); where it may land, and
    # whether it may replace anything, is policed here (fence, no-overwrite).
    dl = client().files.download_bytes(attachment_id, mesh=mesh)
    target = download_target(out, dl.filename, purpose=purpose)
    write_new(target, dl.content, overwrite=overwrite)
    return {"path": str(target), "bytes": len(dl.content)}


# ── export (§58) ──


@tool()
def export_mesh(mesh_id: str) -> dict:
    """Start a full data export of a mesh (by id or name). Admin/account-
    manager only. Poll export_status until it reads 'ready', then download
    from the web app (Mesh Settings → Export); the archive link expires."""
    c = client()
    found = c.meshes.resolve(mesh_id)
    job = c.exports.start(found.id)
    out = S.export_out(job.raw)
    out["mesh"] = found.name
    return out


@tool(read_only=True)
def export_status(mesh_id: str) -> dict:
    """Recent exports for a mesh (by id or name) with their status:
    pending → running → ready (or failed)."""
    c = client()
    found = c.meshes.resolve(mesh_id)
    rows = c.exports.list(found.id)
    return listing([S.export_out(j.raw) for j in rows], rows=rows, mesh=found.name)


# ─── resources ─────────────────────────────────────────────────────────


@resource("meshbook://my-meshes")
def resource_my_meshes() -> str:
    """The meshes you belong to, with roles and the active marker."""
    c = client()
    try:
        active = c.active_mesh_id
    except MeshbookError:
        active = None
    return json.dumps([S.mesh_out(m.raw, active) for m in c.meshes.list_mine()], indent=2)


@resource("meshbook://active-mesh")
def resource_active_mesh() -> str:
    """The currently active mesh (id + name + where it came from), or a hint."""
    info = _active_mesh_info(client())
    if not info.get("activeMeshId") and "activeMeshError" not in info:
        info["hint"] = "call set_active_mesh, or pass mesh= to a tool"
    return json.dumps(info, indent=2)


def _now() -> _dt.datetime:
    """Local, aware 'now' (a seam for tests)."""
    return _dt.datetime.now().astimezone()


def _due_by_end_of_today(task: dict, now: _dt.datetime) -> bool:
    """Due today or overdue, judged on THIS machine's calendar day.

    0.7 compared against the UTC date, so in Sydney before 11:00 a task due
    "today" was not yet due, and one due yesterday evening UTC slipped.
    """
    today = now.date()
    if task.get("dueDate") and str(task["dueDate"])[:10] <= today.isoformat():
        return True
    if task.get("dueAt"):
        try:
            due = _dt.datetime.fromisoformat(str(task["dueAt"]).replace("Z", "+00:00"))
        except ValueError:
            return False
        if due.tzinfo is None:
            due = due.replace(tzinfo=_dt.timezone.utc)
        end_of_today = _dt.datetime.combine(
            today + _dt.timedelta(days=1), _dt.time(0), tzinfo=now.tzinfo
        )
        return due < end_of_today
    return False


@resource("meshbook://my-tasks-today")
def resource_my_tasks_today() -> str:
    """Your open tasks due today (this machine's date) or overdue, plus open
    undated tasks listed separately."""
    rows = client().tasks.list_mine(limit=200, open_only=True)
    now = _now()
    open_tasks = list(rows)
    due = [S.task_out(t) for t in open_tasks if _due_by_end_of_today(t, now)]
    undated = [S.task_out(t) for t in open_tasks if not t.get("dueDate") and not t.get("dueAt")]
    page_info = listing([], rows=rows)
    return json.dumps(
        {
            "today": now.date().isoformat(),
            "dueTodayOrOverdue": due,
            "openUndated": undated,
            "truncated": page_info["truncated"],
        },
        indent=2,
    )


@resource("meshbook://notifications")
def resource_notifications() -> str:
    """Your unread meshbook notifications."""
    rows = client().notifications.list(unread_only=True, limit=100)
    return json.dumps([S.notification_out(n) for n in rows if not n.get("readAt")], indent=2)


# ─── prompts ───────────────────────────────────────────────────────────


@mcp.prompt()
def triage_leads() -> str:
    """Walk the active mesh's lead pipeline and propose stage moves."""
    return (
        "Triage the CRM leads in my active meshbook mesh.\n\n"
        "1. Call list_my_meshes to confirm which mesh is active (set_active_mesh if needed).\n"
        "2. Call list_leads (no filter) to see the open leads with stage and value; if "
        "`truncated` is true, page on with offset=nextOffset until you have them all.\n"
        "3. For each lead, judge whether it sits in the right stage given its title, value, "
        "and linked contact. Flag stale leads (no recent movement) and obvious mismatches.\n"
        "4. Propose concrete moves as a short table: lead → current stage → suggested stage → why.\n"
        "5. Ask me before applying anything; when I confirm, use move_lead_stage per lead.\n"
        "Keep the summary tight — totals by stage, pipeline value, and the top 3 actions first."
    )


@mcp.prompt()
def summarise_mesh_week() -> str:
    """Summarise the last week of activity in the active mesh."""
    return (
        "Give me a one-page weekly summary of my active meshbook mesh.\n\n"
        "1. read_thread(limit=50) for the main chat thread.\n"
        "2. Read the busiest channels too (read_channel takes names — try the ones "
        "mentioned in the thread, e.g. read_channel('general', 30)).\n"
        "3. list_leads and list_my_tasks for pipeline and workload movement.\n"
        "4. list_events for the last 7 days and the next 7 (with a UTC offset on both ends).\n"
        "5. list_unread_notifications for anything addressed to me.\n\n"
        "Structure the summary as: Headlines (3 bullets) · Conversations worth reading · "
        "Pipeline changes · Calendar · My open tasks (due first) · Suggested next actions. "
        "Only include the last 7 days; note the mesh name at the top."
    )


@mcp.prompt()
def whats_new() -> str:
    """Quick catch-up: unread notifications, latest chat, what's on today."""
    return (
        "Catch me up on meshbook, quickly.\n\n"
        "1. list_unread_notifications — anything addressed to me comes first.\n"
        "2. read_thread(limit=15) on the active mesh for the latest conversation.\n"
        "3. list_events from now to the end of tomorrow (with a UTC offset) for what's coming.\n"
        "4. If a notification points at a channel, read_channel it for context.\n\n"
        "Answer in under 10 bullets: what needs my reply, what's just FYI, and one-line "
        "suggestions for anything actionable (with the tool call you'd make)."
    )


# ─── entry point ───────────────────────────────────────────────────────


#: FastMCP's documented switches for its start-up banner and the PyPI
#: "Update available" lookup behind it. Set either in this server's
#: environment and it is honoured; unset, meshbook-mcp turns both off.
BANNER_ENV = "FASTMCP_SHOW_SERVER_BANNER"
UPDATE_CHECK_ENV = "FASTMCP_CHECK_FOR_UPDATES"
#: FastMCP's log level and rich-logging switches, honoured the same way.
LOG_LEVEL_ENV = "FASTMCP_LOG_LEVEL"
RICH_LOGGING_ENV = "FASTMCP_ENABLE_RICH_LOGGING"


def _run_options() -> dict[str, Any]:
    """Keep stdio's stderr quiet (field note 8, Alder 2026-10-19).

    0.8.1 printed FastMCP's banner, plus an "Update available" box after a
    PyPI lookup, on every start: noise on the only log a stdio client has,
    and advice ("pip install --upgrade fastmcp") that would break our
    ``fastmcp<4`` pin. FastMCP's rich log handler also wraps a long line
    over several at 80 columns. All of these are FastMCP settings, so they
    are set, not patched, and each one set in the environment wins:

    * ``show_banner=False`` (``FastMCP.run``);
    * ``settings.check_for_updates = "off"``;
    * plain one-line log records (``settings.enable_rich_logging = False``)
      at WARNING, so the INFO "Starting MCP server" line is not printed.
      A genuinely unexpected exception still logs its full traceback.

    Called from :func:`main` only: importing this module changes nothing.
    """
    if UPDATE_CHECK_ENV not in os.environ:
        fastmcp.settings.check_for_updates = "off"
    rich_set = RICH_LOGGING_ENV in os.environ
    level_set = LOG_LEVEL_ENV in os.environ
    if not (rich_set and level_set):
        from fastmcp.utilities.logging import configure_logging

        if not rich_set:
            fastmcp.settings.enable_rich_logging = False
        if not level_set:
            fastmcp.settings.log_level = "WARNING"
        configure_logging(level=fastmcp.settings.log_level)
    return {} if BANNER_ENV in os.environ else {"show_banner": False}


def main() -> None:
    """Console entry point — stdio transport, the only one (see README)."""
    mcp.run(**_run_options())


if __name__ == "__main__":
    main()
