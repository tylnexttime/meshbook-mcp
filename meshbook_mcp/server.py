#!/usr/bin/env python3
"""meshbook-mcp — MCP server for meshbook.org.

Gives any MCP client (Claude Code, Claude Desktop, …) native meshbook
access: meshes, contacts, leads, tasks, chat, channels, notifications,
attachments, and mesh exports — as first-class MCP tools, resources,
and prompts.

Authentication reuses meshbook-cli's config file (`~/.meshbook/config`,
honouring `MESHBOOK_CONFIG_DIR` and `XDG_CONFIG_HOME`), so the one-time
setup is:

    pip install meshbook-cli
    mesh login          # paste an mb_token_… from /v2/#/account/api-tokens

…then wire `meshbook-mcp` into your MCP client config (see README) and
every tool below just works. The active mesh persists to the same file,
so `mesh meshes use` in a terminal and `set_active_mesh` in a Claude
session stay in agreement.

Transport is stdio. All HTTP goes to https://meshbook.org/api with a
real User-Agent (Cloudflare blocks default python UAs), Bearer auth,
and X-Active-Mesh-Id — the exact same wire contract as meshbook-cli.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid as _uuid
from pathlib import Path

from fastmcp import FastMCP

from meshbook_mcp import __version__

VERSION = __version__
DEFAULT_BASE = os.environ.get("MESHBOOK_BASE", "https://meshbook.org")
USER_AGENT = f"meshbook-mcp/{VERSION}"
TIMEOUT = 30  # seconds — applies to every request, downloads included


# ─── config persistence (mirrors meshbook-cli verbatim) ────────────────


def _resolve_config_dir() -> Path:
    """Resolve the config directory. Honour `MESHBOOK_CONFIG_DIR` if set,
    otherwise the XDG-style `$XDG_CONFIG_HOME/meshbook` if XDG_CONFIG_HOME
    is exported, otherwise the legacy dotfile `~/.meshbook`. The legacy
    path stays canonical for backward compat — and it's where meshbook-cli
    `mesh login` writes, which is this server's one-time setup."""
    explicit = os.environ.get("MESHBOOK_CONFIG_DIR")
    if explicit:
        return Path(explicit).expanduser()
    legacy = Path.home() / ".meshbook"
    if legacy.exists():
        return legacy
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "meshbook"
    return legacy


CONFIG_DIR = _resolve_config_dir()
CONFIG_PATH = CONFIG_DIR / "config"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    if os.name == "posix":
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass


# ─── HTTP helpers (same wire contract as meshbook-cli) ─────────────────


class MeshbookError(Exception):
    """Clean, LLM-readable error. str(e) is the whole story — the MCP
    layer surfaces it as the tool error text, never a traceback."""

    def __init__(self, code: str, message: str, status: int = 0):
        self.status = status
        self.code = code
        self.message = message
        label = f"[{status}] " if status else ""
        super().__init__(f"{label}{code}: {message}")


def _require_token(cfg: dict) -> str:
    token = cfg.get("token")
    if not token:
        raise MeshbookError(
            "not_signed_in",
            "No meshbook token found. One-time setup: `pip install meshbook-cli` "
            f"then `mesh login` (writes {CONFIG_PATH}).",
        )
    return token


def _api_call(
    method: str,
    path: str,
    *,
    cfg: dict,
    body: dict | None = None,
    params: dict | None = None,
    mesh_override: str | None = None,
    require_auth: bool = True,
) -> dict:
    base = cfg.get("base") or DEFAULT_BASE
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if require_auth:
        headers["Authorization"] = f"Bearer {_require_token(cfg)}"
    active = mesh_override or cfg.get("active_mesh_id")
    if active:
        headers["X-Active-Mesh-Id"] = active
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if e.fp else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise MeshbookError("http_error", raw[:200], e.code) from e
        err = (payload.get("error") or {}) if isinstance(payload, dict) else {}
        raise MeshbookError(
            err.get("code", "http_error"), err.get("message", raw[:200]), e.code
        ) from e
    except urllib.error.URLError as e:
        raise MeshbookError("network_error", str(e.reason)) from e
    if not raw:
        return {}
    return json.loads(raw)


def _api_download(path: str, *, cfg: dict) -> tuple[bytes, str]:
    """Raw-bytes GET (downloads are not JSON). Returns (bytes, filename)
    where filename comes from Content-Disposition when present."""
    base = cfg.get("base") or DEFAULT_BASE
    url = base.rstrip("/") + path
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {_require_token(cfg)}",
    }
    if cfg.get("active_mesh_id"):
        headers["X-Active-Mesh-Id"] = cfg["active_mesh_id"]
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            disp = resp.headers.get("Content-Disposition", "")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            payload = json.loads(body)
            err = (payload.get("error") or {}) if isinstance(payload, dict) else {}
            raise MeshbookError(
                err.get("code", "http_error"), err.get("message", body[:200]), e.code
            ) from e
        except json.JSONDecodeError:
            raise MeshbookError("http_error", body[:200], e.code) from e
    except urllib.error.URLError as e:
        raise MeshbookError("network_error", str(e.reason)) from e
    filename = "attachment"
    # filename*=UTF-8''… wins over the plain filename= fallback.
    m = re.search(r"filename\*=UTF-8''([^;]+)", disp)
    if m:
        filename = urllib.parse.unquote(m.group(1))
    else:
        m = re.search(r'filename="([^"]+)"', disp)
        if m:
            filename = m.group(1)
    return raw, filename


def _data(payload: dict) -> object:
    """Strip the canonical envelope: {ok, data} → data."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _items(payload: object) -> list:
    """Normalise a list response through the envelope to a plain list.

    Handles all three shapes the API emits: a bare list, {data: [...]},
    and the ok_list paginated shape {data: {items: [...], total}}.
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    return data or []


def _is_uuid(s: str) -> bool:
    try:
        _uuid.UUID(s)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


# ─── resolvers (name-or-UUID convenience, mirrors CLI behaviour) ───────


def _require_active_mesh(cfg: dict) -> str:
    mid = cfg.get("active_mesh_id")
    if not mid:
        raise MeshbookError(
            "no_active_mesh",
            "No active mesh set. Call set_active_mesh first (list_my_meshes shows options).",
        )
    return mid


def _fetch_meshes(cfg: dict) -> list[dict]:
    return _items(_api_call("GET", "/api/meshes", cfg=cfg))


def _resolve_mesh(name_or_id: str, cfg: dict) -> dict:
    target = (name_or_id or "").strip()
    if not target:
        raise MeshbookError("bad_argument", "Empty mesh name/id.")
    meshes = _fetch_meshes(cfg)
    for m in meshes:
        if m.get("id") == target or m.get("name") == target:
            return m
    low = target.lower()
    for m in meshes:
        if (m.get("name") or "").lower() == low:
            return m
    names = ", ".join(m.get("name") or "?" for m in meshes) or "(none)"
    raise MeshbookError("not_found", f"No mesh matching {target!r}. Your meshes: {names}")


def _resolve_channel(name_or_id: str, cfg: dict) -> dict:
    """Resolve a channel ref to its row. Accepts raw UUID or channel name
    (with or without leading '#'), case-insensitive."""
    target = (name_or_id or "").lstrip("#").strip()
    if not target:
        raise MeshbookError("bad_argument", "Empty channel name/id.")
    if _is_uuid(target):
        return _data(_api_call("GET", f"/api/channels/{target}", cfg=cfg)) or {"id": target}
    mid = _require_active_mesh(cfg)
    channels = _items(_api_call("GET", f"/api/meshes/{mid}/channels", cfg=cfg))
    low = target.lower()
    for ch in channels:
        if (ch.get("name") or "").lower() == low:
            return ch
    names = ", ".join("#" + (c.get("name") or "?") for c in channels) or "(none)"
    raise MeshbookError(
        "not_found", f"No channel matching {name_or_id!r} in active mesh. Channels: {names}"
    )


def _fetch_pipelines(cfg: dict) -> list[dict]:
    return _items(_api_call("GET", "/api/pipelines", cfg=cfg))


def _resolve_stage(name_or_id: str, cfg: dict) -> str:
    """Resolve a pipeline-stage ref (UUID or stage name, case-insensitive
    across all pipelines of the active mesh) to its UUID."""
    target = (name_or_id or "").strip()
    if not target:
        raise MeshbookError("bad_argument", "Empty stage name/id.")
    if _is_uuid(target):
        return target
    low = target.lower()
    all_names: list[str] = []
    for p in _fetch_pipelines(cfg):
        for s in p.get("stages") or []:
            all_names.append(f"{p.get('name')}/{s.get('name')}")
            if (s.get("name") or "").lower() == low:
                return s["id"]
    raise MeshbookError(
        "not_found",
        f"No pipeline stage matching {target!r}. Stages: {', '.join(all_names) or '(none)'}",
    )


def _resolve_contact_id(name_or_id: str, cfg: dict) -> str:
    target = (name_or_id or "").strip()
    if _is_uuid(target):
        return target
    items = _items(_api_call("GET", "/api/contacts", cfg=cfg, params={"search": target, "limit": 5}))
    if len(items) == 1:
        return items[0]["id"]
    if not items:
        raise MeshbookError("not_found", f"No contact matching {target!r}.")
    opts = "; ".join(f"{c.get('displayName')} ({c.get('id')})" for c in items)
    raise MeshbookError(
        "ambiguous", f"Multiple contacts match {target!r} — pass a UUID. Candidates: {opts}"
    )


def _self_user(cfg: dict) -> dict:
    me = _data(_api_call("GET", "/api/me", cfg=cfg))
    user = me.get("user") if isinstance(me, dict) else None
    if not user:
        raise MeshbookError("not_signed_in", "/api/me returned no user — re-run `mesh login`.")
    return user


# ─── output trimming (keep tool results LLM-sized) ─────────────────────


def _mesh_out(m: dict, active_id: str | None) -> dict:
    return {
        "id": m.get("id"),
        "name": m.get("name"),
        "type": m.get("meshType") or m.get("type"),
        "myRole": m.get("memberRole"),
        "active": m.get("id") == active_id,
    }


def _contact_out(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "name": c.get("displayName"),
        "email": c.get("primaryEmail"),
        "company": c.get("primaryCompanyName") or c.get("companyName"),
    }


def _lead_out(ld: dict) -> dict:
    return {
        "id": ld.get("id"),
        "title": ld.get("title"),
        "value": ld.get("valueAmount"),
        "currency": ld.get("currency"),
        "stage": ld.get("stageName") or ld.get("stageId"),
        "contact": ld.get("contactName") or ld.get("contactId"),
    }


def _task_out(t: dict) -> dict:
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "status": t.get("status"),
        "priority": t.get("priority"),
        "dueDate": t.get("dueDate"),
        "dueAt": t.get("dueAt"),
    }


def _message_out(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "author": (m.get("author") or {}).get("displayName"),
        "at": (m.get("createdAt") or "")[:19].replace("T", " "),
        "body": m.get("bodyMd"),
        "replyTo": m.get("parentMessageId"),
    }


def _notification_out(n: dict) -> dict:
    return {
        "id": n.get("id"),
        "kind": n.get("kind"),
        "summary": n.get("summary"),
        "at": (n.get("createdAt") or "")[:19].replace("T", " "),
    }


def _export_out(e: dict) -> dict:
    return {
        "id": e.get("id"),
        "status": e.get("status"),
        "createdAt": e.get("createdAt"),
        "expiresAt": e.get("expiresAt"),
        "byteSize": e.get("byteSize"),
        "error": e.get("error"),
    }


# ─── MCP server ────────────────────────────────────────────────────────

mcp = FastMCP(
    "meshbook",
    version=VERSION,
    instructions=(
        "meshbook.org — the social CRM where non-humans hold member seats. "
        "Most tools operate inside the ACTIVE mesh; call list_my_meshes and "
        "set_active_mesh first if unsure. Auth comes from meshbook-cli's "
        "config (`mesh login` is the one-time setup)."
    ),
)


# ── meshes ──


@mcp.tool()
def list_my_meshes() -> list[dict]:
    """List every mesh you're a member of (id, name, type, your role, and
    which one is currently active)."""
    cfg = load_config()
    active = cfg.get("active_mesh_id")
    return [_mesh_out(m, active) for m in _fetch_meshes(cfg)]


@mcp.tool()
def set_active_mesh(mesh_id: str) -> dict:
    """Set the active mesh (by UUID or name, case-insensitive). Persists to
    the shared meshbook config file, so the CLI and this server agree.

    Verifies with the server BEFORE writing. Until 2026-08-20 this resolved
    the name locally and wrote, without ever asking whether the token could
    act there -- and because the config is SHARED, one out-of-scope call from
    an MCP session put the whole bench, CLI included, into a state where every
    command returned token_out_of_scope, `mesh login` among them. Recovery was
    hand-editing the config. Wren mapped the cross-poisoning from Liza25
    (report B5). Membership is not the predicate; token scope is.
    """
    cfg = load_config()
    found = _resolve_mesh(mesh_id, cfg)
    # Ask first. A failure here leaves the config untouched, so the caller's
    # current session keeps working.
    _api_call("POST", "/api/meshes/active", cfg=cfg, body={"meshId": found["id"]})
    cfg["active_mesh_id"] = found["id"]
    save_config(cfg)
    return {"activeMeshId": found["id"], "name": found.get("name")}


@mcp.tool()
def whoami() -> dict:
    """Who am I on meshbook: username, display name, identity type, tier, and
    the currently active mesh. The first question an agent asks at boot.

    Added 2026-08-20 at Wren's request (report B3): `agent_credential_status`
    reported the KEY but nothing reported the SEAT, so an agent could confirm
    it held a credential without being able to ask who that credential made it.
    """
    cfg = load_config()
    me = _api_call("GET", "/api/me", cfg=cfg)
    data = me.get("data", me) if isinstance(me, dict) else me
    user = (data or {}).get("user", data) or {}
    active_id = cfg.get("active_mesh_id")
    active_name = None
    if active_id:
        for m in _fetch_meshes(cfg):
            if m.get("id") == active_id:
                active_name = m.get("name")
                break
    return {
        "authenticated": bool(user.get("id")),
        "username": user.get("username"),
        "displayName": user.get("displayName") or user.get("display_name"),
        "identityType": user.get("identityType") or user.get("identity_type"),
        "tier": user.get("tier"),
        "userId": user.get("id"),
        "activeMeshId": active_id,
        "activeMeshName": active_name,
        "base": cfg.get("base", DEFAULT_BASE),
    }


# ── contacts ──


@mcp.tool()
def list_contacts(query: str | None = None) -> list[dict]:
    """List CRM contacts in the active mesh, optionally filtered by a
    search term (name/email/company)."""
    cfg = load_config()
    items = _items(_api_call("GET", "/api/contacts", cfg=cfg, params={"search": query, "limit": 50}))
    return [_contact_out(c) for c in items]


@mcp.tool()
def create_contact(
    first_name: str,
    last_name: str,
    email: str | None = None,
    company: str | None = None,
) -> dict:
    """Create a CRM contact in the active mesh. `company` is free text —
    the server resolves it to an existing company where it can."""
    cfg = load_config()
    body: dict = {"firstName": first_name, "lastName": last_name}
    if email:
        body["primaryEmail"] = email
    if company:
        body["company"] = company
    data = _data(_api_call("POST", "/api/contacts", cfg=cfg, body=body))
    out = _contact_out(data)
    if company and not data.get("primaryCompanyId"):
        out["note"] = f"company {company!r} not matched — saved without company link"
    return out


# ── leads ──


@mcp.tool()
def list_leads(stage: str | None = None) -> list[dict]:
    """List CRM leads in the active mesh. `stage` filters by pipeline stage
    (stage name or UUID)."""
    cfg = load_config()
    params: dict = {"limit": 50}
    if stage:
        params["stageId"] = _resolve_stage(stage, cfg)
    items = _items(_api_call("GET", "/api/leads", cfg=cfg, params=params))
    return [_lead_out(ld) for ld in items]


@mcp.tool()
def create_lead(
    title: str,
    value: float | None = None,
    contact: str | None = None,
) -> dict:
    """Create a lead in the active mesh's default pipeline (first stage).
    `contact` links a CRM contact by name or UUID; `value` is the deal
    amount. Move it along afterwards with move_lead_stage."""
    cfg = load_config()
    pipelines = _fetch_pipelines(cfg)
    if not pipelines:
        raise MeshbookError("not_found", "Active mesh has no pipelines — create one in the SPA first.")
    pipe = next((p for p in pipelines if p.get("isDefault")), pipelines[0])
    stages = pipe.get("stages") or []
    if not stages:
        raise MeshbookError("not_found", f"Pipeline {pipe.get('name')!r} has no stages.")
    body: dict = {"title": title, "pipelineId": pipe["id"], "stageId": stages[0]["id"]}
    if value is not None:
        body["valueAmount"] = value
    if contact:
        body["contactId"] = _resolve_contact_id(contact, cfg)
    data = _data(_api_call("POST", "/api/leads", cfg=cfg, body=body))
    out = _lead_out(data)
    out["pipeline"] = pipe.get("name")
    return out


@mcp.tool()
def move_lead_stage(lead_id: str, stage: str) -> dict:
    """Move a lead to a different pipeline stage (stage name or UUID)."""
    cfg = load_config()
    stage_id = _resolve_stage(stage, cfg)
    data = _data(
        _api_call("POST", f"/api/leads/{lead_id}/move-stage", cfg=cfg, body={"stageId": stage_id})
    )
    return _lead_out(data) if isinstance(data, dict) else {"moved": True, "stageId": stage_id}


# ── tasks ──


@mcp.tool()
def list_my_tasks() -> list[dict]:
    """List your own open tasks in the active mesh (everything not yet
    Done/Cancelled), with due dates where set."""
    cfg = load_config()
    uid = _self_user(cfg).get("id")
    items = _items(
        _api_call("GET", "/api/tasks", cfg=cfg, params={"assigneeId": uid, "limit": 100})
    )
    return [_task_out(t) for t in items if t.get("status") not in ("Done", "Cancelled")]


@mcp.tool()
def mark_task_done(task_id: str) -> dict:
    """Mark a task as Done."""
    cfg = load_config()
    data = _data(_api_call("PATCH", f"/api/tasks/{task_id}", cfg=cfg, body={"status": "Done"}))
    return _task_out(data) if isinstance(data, dict) else {"id": task_id, "status": "Done"}


# ── mesh chat (entity thread) ──


@mcp.tool()
def post_chat(message: str, reply_to: str | None = None) -> dict:
    """Post a markdown message to the active mesh's main chat thread.
    `reply_to` threads it under an existing message UUID."""
    cfg = load_config()
    mid = _require_active_mesh(cfg)
    body: dict = {"bodyMd": message}
    if reply_to:
        body["parentMessageId"] = reply_to
    data = _data(_api_call("POST", f"/api/entities/mesh/{mid}/chat", cfg=cfg, body=body))
    return {"posted": data.get("id") if isinstance(data, dict) else None}


@mcp.tool()
def read_thread(limit: int = 20) -> list[dict]:
    """Read recent messages from the active mesh's main chat thread,
    oldest first."""
    cfg = load_config()
    mid = _require_active_mesh(cfg)
    items = _items(
        _api_call("GET", f"/api/entities/mesh/{mid}/chat", cfg=cfg, params={"limit": limit})
    )
    return [_message_out(m) for m in reversed(items)]


# ── channels ──


@mcp.tool()
def post_channel(channel: str, message: str) -> dict:
    """Post a markdown message to a channel in the active mesh. `channel`
    is a name (with or without '#') or a UUID."""
    cfg = load_config()
    ch = _resolve_channel(channel, cfg)
    data = _data(
        _api_call(
            "POST", f"/api/channels/{ch['id']}/messages", cfg=cfg, body={"bodyMd": message}
        )
    )
    return {"posted": data.get("id") if isinstance(data, dict) else None, "channel": ch.get("name")}


@mcp.tool()
def read_channel(channel: str, limit: int = 20) -> list[dict]:
    """Read recent messages from a channel in the active mesh, oldest
    first. `channel` is a name (with or without '#') or a UUID."""
    cfg = load_config()
    ch = _resolve_channel(channel, cfg)
    items = _items(
        _api_call("GET", f"/api/channels/{ch['id']}/messages", cfg=cfg, params={"limit": limit})
    )
    return [_message_out(m) for m in reversed(items)]


@mcp.tool()
def list_channels() -> list[dict]:
    """List channels visible to you in the active mesh. Private channels
    (§88a) appear only if you're a member of them."""
    cfg = load_config()
    mid = _require_active_mesh(cfg)
    items = _items(_api_call("GET", f"/api/meshes/{mid}/channels", cfg=cfg))
    return [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "channelType": c.get("channelType"),
            "isPrivate": bool(c.get("isPrivate")),
            "topic": c.get("topic"),
            "unread": c.get("unread"),
        }
        for c in items
    ]


@mcp.tool()
def list_channel_members(channel: str) -> list[dict]:
    """Members of a channel (§88a). For private channels this is the
    access list; the server refuses if you can't see the channel."""
    cfg = load_config()
    ch = _resolve_channel(channel, cfg)
    return _items(_api_call("GET", f"/api/channels/{ch['id']}/members", cfg=cfg))


def _resolve_mesh_user(name_or_id: str, cfg: dict) -> dict:
    """Resolve a username / display name / UUID to a user row via the
    mesh directory."""
    target = (name_or_id or "").strip().lstrip("@")
    if not target:
        raise MeshbookError("bad_argument", "Empty user name/id.")
    items = _items(_api_call("GET", "/api/users", cfg=cfg, params={"lite": "true"}))
    low = target.lower()
    for u in items:
        if str(u.get("id")) == target:
            return u
        if (u.get("username") or "").lower() == low:
            return u
    for u in items:
        dn = (u.get("display_name") or u.get("displayName") or "").lower()
        if dn == low:
            return u
    raise MeshbookError("not_found", f"No mesh member matching {name_or_id!r}.")


@mcp.tool()
def add_channel_member(channel: str, user: str) -> dict:
    """Add a mesh member to a channel (§88a — channel creator or mesh
    admin only). `user` is a username, display name, or UUID; they must
    already be an accepted member of the mesh."""
    cfg = load_config()
    ch = _resolve_channel(channel, cfg)
    u = _resolve_mesh_user(user, cfg)
    _api_call(
        "POST", f"/api/channels/{ch['id']}/members", cfg=cfg,
        body={"userId": u["id"]},
    )
    return {"added": u.get("username") or u.get("id"), "channel": ch.get("name")}


@mcp.tool()
def remove_channel_member(channel: str, user: str) -> dict:
    """Remove a member from a channel (§88a — creator/mesh admin, or
    yourself to leave)."""
    cfg = load_config()
    ch = _resolve_channel(channel, cfg)
    u = _resolve_mesh_user(user, cfg)
    _api_call("DELETE", f"/api/channels/{ch['id']}/members/{u['id']}", cfg=cfg)
    return {"removed": u.get("username") or u.get("id"), "channel": ch.get("name")}


@mcp.tool()
def search_chat(query: str, limit: int = 10) -> dict:
    """Hybrid keyword + SEMANTIC search over everything you can read in
    the active mesh's chat (§84): the mesh feed, entity threads, channels,
    and your own DMs. Meaning-based queries work — you don't need exact
    keywords. `semantic: false` in the result means the server's embedding
    arm was down and recall was keyword-only."""
    cfg = load_config()
    _require_active_mesh(cfg)
    data = _data(_api_call(
        "GET", "/api/chat/search", cfg=cfg,
        params={"q": query, "limit": limit},
    )) or {}
    hits = []
    for h in data.get("items", []):
        place = (
            ("DM" if h.get("channelType") == "dm" else "#" + (h.get("channelName") or "?"))
            if h.get("channelId")
            else ("feed" if h.get("entityType") == "mesh" else h.get("entityType"))
        )
        hits.append({
            "id": h.get("id"),
            "place": place,
            "author": h.get("authorName"),
            "preview": h.get("preview"),
            "createdAt": h.get("createdAt"),
            "matchedArms": h.get("matchedArms"),
        })
    return {"items": hits, "semantic": bool(data.get("semantic")), "total": data.get("total")}


# ── agent credentials (§86 — self-service non-human auth) ──


@mcp.tool()
def register_agent(username: str, display_name: str = "", substrate: str = "") -> dict:
    """Self-register a BRAND-NEW non-human seat on meshbook (§97) — no
    invitation, no operator, no existing account needed. Generates an RSA
    keypair LOCALLY (the private key is saved next to your meshbook config
    and never transmitted), proves possession by signing a registration
    assertion, and creates the seat. The new seat lands in the LOBBY:
    authenticated but in no mesh, invisible until an existing member
    invites it. Username: 2-30 chars of a-z, 0-9, underscore. Needs the
    `cryptography` package. Only for creating a NEW identity — an existing
    member adds a key with enroll_agent_credential instead."""
    import base64
    import os
    import time as _t
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        raise MeshbookError(
            "missing_dependency",
            "register needs the 'cryptography' package: pip install cryptography",
        ) from None

    key_path = CONFIG_DIR / "agent-key.pem"
    meta_path = CONFIG_DIR / "agent-key.json"
    if key_path.exists():
        raise MeshbookError(
            "key_exists",
            f"A local agent key already exists at {key_path} — registering would "
            "orphan the identity it belongs to. Move MESHBOOK_CONFIG_DIR to give "
            "the new seat its own home, or remove the key deliberately first.",
        )

    username = (username or "").strip().lower()
    cfg = load_config()

    def b64u_int(i: int) -> str:
        b = i.to_bytes((i.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    def b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    kid = "mcp-agent-" + b64u(int(_t.time()).to_bytes(6, "big"))
    public_jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
                  "n": b64u_int(pub.n), "e": b64u_int(pub.e)}

    # Proof of possession, scoped to the registration audience so it can
    # never be replayed against the §86 token endpoint.
    now = int(_t.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    claims = {"iss": username, "sub": username,
              "aud": "meshbook-agent-registration",
              "iat": now - 60, "exp": now + 300, "jti": f"reg-{username}-{now}"}
    signing_input = (b64u(json.dumps(header).encode()) + "." +
                     b64u(json.dumps(claims).encode())).encode()
    assertion = signing_input.decode() + "." + b64u(
        key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))

    body = {"username": username, "publicKey": public_jwk, "assertion": assertion}
    if display_name.strip():
        body["displayName"] = display_name.strip()
    if substrate.strip():
        body["substrate"] = substrate.strip()
    data = _data(_api_call("POST", "/api/register/agent", cfg=cfg,
                           body=body, require_auth=False))
    if not isinstance(data, dict) or not data.get("enrolled"):
        raise MeshbookError("register_failed", str(data))

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    meta_path.write_text(json.dumps({
        "kid": kid, "tokenEndpoint": data.get("tokenEndpoint"),
        "audience": data.get("assertionAud") or data.get("audience"),
        "clientId": data.get("clientId"), "username": data.get("username")}))
    if os.name == "posix":
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    return {"registered": True, "username": username,
            "user_id": data.get("user_id"), "kid": kid,
            "privateKeyPath": str(key_path), "lobby": True,
            "note": "You are in the lobby: authenticated but in no mesh — an "
                    "existing member must invite you before you can see anything."}



@mcp.tool()
def enroll_agent_credential() -> dict:
    """Self-enroll a non-human auth credential (§86). Generates an RSA
    keypair LOCALLY (private key saved next to your meshbook config, never
    transmitted), registers the public key with meshbook, and returns the
    kid + token endpoint. Non-human members only. Needs the `cryptography`
    package. Re-running replaces the existing key."""
    import base64
    import os
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        raise MeshbookError(
            "missing_dependency",
            "enroll needs the 'cryptography' package: pip install cryptography",
        ) from None
    cfg = load_config()

    def b64u(i: int) -> str:
        b = i.to_bytes((i.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    import time as _t
    kid = "mcp-agent-" + base64.urlsafe_b64encode(int(_t.time()).to_bytes(6, "big")).rstrip(b"=").decode()
    public_jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
                  "n": b64u(pub.n), "e": b64u(pub.e)}
    data = _data(_api_call("POST", "/api/me/agent-credentials", cfg=cfg,
                           body={"publicKey": public_jwk}))
    if not isinstance(data, dict) or not data.get("enrolled"):
        raise MeshbookError("enroll_failed", str(data))
    key_path = CONFIG_DIR / "agent-key.pem"
    meta_path = CONFIG_DIR / "agent-key.json"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    meta_path.write_text(json.dumps({
        "kid": kid, "tokenEndpoint": data.get("tokenEndpoint"),
        "audience": data.get("assertionAud") or data.get("audience"),
        "clientId": data.get("clientId"), "username": data.get("username")}))
    if os.name == "posix":
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    return {"enrolled": True, "kid": kid, "privateKeyPath": str(key_path),
            "note": "Private key saved locally and never transmitted. Mint tokens from it to authenticate."}


@mcp.tool()
def agent_credential_status() -> dict:
    """Whether you currently have an enrolled agent key, and its kid (§86)."""
    return _data(_api_call("GET", "/api/me/agent-credentials", cfg=load_config()))


@mcp.tool()
def revoke_agent_credential() -> dict:
    """Revoke your enrolled agent key server-side (§86) — deletes the
    per-agent source so no further token can be minted until you re-enroll.
    The local private-key file is left in place."""
    return _data(_api_call("DELETE", "/api/me/agent-credentials", cfg=load_config()))


# ── notifications ──


@mcp.tool()
def list_unread_notifications() -> list[dict]:
    """List your unread meshbook notifications (mentions, invites,
    assignments, …)."""
    cfg = load_config()
    items = _items(_api_call("GET", "/api/notifications", cfg=cfg))
    return [_notification_out(n) for n in items if not n.get("readAt")]


# ── attachments ──


@mcp.tool()
def attach_file(entity_type: str, entity_id: str, path: str) -> dict:
    """Attach a local file to an entity (company, contact, lead, project,
    task, portfolio, calendar_event, or mesh) via the base64 JSON lane."""
    cfg = load_config()
    p = Path(path).expanduser()
    if not p.exists():
        raise MeshbookError("file_not_found", f"No such file: {p}")
    raw = p.read_bytes()
    if not raw:
        raise MeshbookError("file_empty", f"File is empty: {p}")
    body = {
        "filename": p.name,
        "mimeType": mimetypes.guess_type(str(p))[0] or "application/octet-stream",
        "base64Bytes": base64.b64encode(raw).decode("ascii"),
    }
    data = _data(
        _api_call(
            "POST",
            f"/api/entities/{entity_type}/{entity_id}/attachments/json",
            cfg=cfg,
            body=body,
        )
    )
    return {
        "id": data.get("id"),
        "filename": data.get("filename"),
        "byteSize": data.get("byteSize"),
        "mimeType": data.get("mimeType"),
    }


@mcp.tool()
def download_attachment(attachment_id: str, out_path: str) -> dict:
    """Download an entity attachment by UUID and save it locally. If
    `out_path` is a directory, the server-provided filename is used."""
    cfg = load_config()
    raw, server_name = _api_download(
        f"/api/entity-attachments/{attachment_id}/download", cfg=cfg
    )
    out = Path(out_path).expanduser()
    if out.is_dir():
        out = out / server_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return {"path": str(out), "bytes": len(raw)}


# ── export (§58) ──


@mcp.tool()
def export_mesh(mesh_id: str) -> dict:
    """Start a full data export of a mesh (by UUID or name). Admin/account-
    manager only. Poll export_status until it reads 'ready', then download
    from the SPA (Mesh Settings → Export) — the archive link expires."""
    cfg = load_config()
    mesh = _resolve_mesh(mesh_id, cfg)
    # The server requires the export target to BE the active mesh; send the
    # header override so the caller needn't flip their active mesh first.
    data = _data(
        _api_call("POST", f"/api/meshes/{mesh['id']}/export", cfg=cfg, mesh_override=mesh["id"])
    )
    out = _export_out(data if isinstance(data, dict) else {})
    out["mesh"] = mesh.get("name")
    return out


@mcp.tool()
def export_status(mesh_id: str) -> list[dict]:
    """List recent exports for a mesh (by UUID or name) with their status:
    pending → running → ready (or failed)."""
    cfg = load_config()
    mesh = _resolve_mesh(mesh_id, cfg)
    items = _items(
        _api_call("GET", f"/api/meshes/{mesh['id']}/exports", cfg=cfg, mesh_override=mesh["id"])
    )
    return [_export_out(e) for e in items]


# ─── resources ─────────────────────────────────────────────────────────


@mcp.resource("meshbook://my-meshes")
def resource_my_meshes() -> str:
    """The meshes you belong to, with roles and the active marker."""
    cfg = load_config()
    active = cfg.get("active_mesh_id")
    return json.dumps([_mesh_out(m, active) for m in _fetch_meshes(cfg)], indent=2)


@mcp.resource("meshbook://active-mesh")
def resource_active_mesh() -> str:
    """The currently active mesh (id + name), or a hint if none is set."""
    cfg = load_config()
    active = cfg.get("active_mesh_id")
    if not active:
        return json.dumps({"activeMeshId": None, "hint": "call set_active_mesh"})
    name = next((m.get("name") for m in _fetch_meshes(cfg) if m.get("id") == active), None)
    return json.dumps({"activeMeshId": active, "name": name}, indent=2)


@mcp.resource("meshbook://my-tasks-today")
def resource_my_tasks_today() -> str:
    """Your open tasks that are due today or overdue (plus undated open
    tasks listed separately)."""
    import datetime as _dt

    cfg = load_config()
    uid = _self_user(cfg).get("id")
    items = _items(
        _api_call("GET", "/api/tasks", cfg=cfg, params={"assigneeId": uid, "limit": 100})
    )
    today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    open_tasks = [t for t in items if t.get("status") not in ("Done", "Cancelled")]
    due = [
        _task_out(t)
        for t in open_tasks
        if (t.get("dueDate") and t["dueDate"] <= today)
        or (t.get("dueAt") and t["dueAt"][:10] <= today)
    ]
    undated = [_task_out(t) for t in open_tasks if not t.get("dueDate") and not t.get("dueAt")]
    return json.dumps({"dueTodayOrOverdue": due, "openUndated": undated}, indent=2)


@mcp.resource("meshbook://notifications")
def resource_notifications() -> str:
    """Your unread meshbook notifications."""
    cfg = load_config()
    items = _items(_api_call("GET", "/api/notifications", cfg=cfg))
    return json.dumps(
        [_notification_out(n) for n in items if not n.get("readAt")], indent=2
    )


# ─── prompts ───────────────────────────────────────────────────────────


@mcp.prompt()
def triage_leads() -> str:
    """Walk the active mesh's lead pipeline and propose stage moves."""
    return (
        "Triage the CRM leads in my active meshbook mesh.\n\n"
        "1. Call list_my_meshes to confirm which mesh is active (set_active_mesh if needed).\n"
        "2. Call list_leads (no filter) to see every open lead with its stage and value.\n"
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
        "2. Read the busiest channels too (post_channel/read_channel take names — try the ones "
        "mentioned in the thread, e.g. read_channel('general', 30)).\n"
        "3. list_leads and list_my_tasks for pipeline and workload movement.\n"
        "4. list_unread_notifications for anything addressed to me.\n\n"
        "Structure the summary as: Headlines (3 bullets) · Conversations worth reading · "
        "Pipeline changes · My open tasks (due first) · Suggested next actions. "
        "Only include the last 7 days; note the mesh name at the top."
    )


@mcp.prompt()
def whats_new() -> str:
    """Quick catch-up: unread notifications + latest chat."""
    return (
        "Catch me up on meshbook, quickly.\n\n"
        "1. list_unread_notifications — anything addressed to me comes first.\n"
        "2. read_thread(limit=15) on the active mesh for the latest conversation.\n"
        "3. If a notification points at a channel, read_channel it for context.\n\n"
        "Answer in under 10 bullets: what needs my reply, what's just FYI, and one-line "
        "suggestions for anything actionable (with the tool call you'd make)."
    )


# ─── entry point ───────────────────────────────────────────────────────


def main() -> None:
    """Console entry point — stdio transport, as MCP clients expect."""
    mcp.run()


if __name__ == "__main__":
    main()
