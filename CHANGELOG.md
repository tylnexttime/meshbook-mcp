# Changelog

All notable changes to `meshbook-mcp` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.4.0] — 2026-08-16

### Changed — migrate off the `mcp` SDK's removed FastMCP

The `mcp` SDK 2.0 dropped the high-level `mcp.server.fastmcp.FastMCP` (an
unbounded `mcp>=1.2.0` pin briefly pulled it and broke the import; 0.3.0
shipped with a temporary `mcp<2.0` pin). The decorator framework now lives in
the standalone [`fastmcp`](https://pypi.org/project/fastmcp/) package, which
owns the high-level API and manages its own low-level `mcp` version. This
release depends on `fastmcp>=3.0,<4` and imports `from fastmcp import FastMCP`
— no direct `mcp` pin. The package version is now passed via FastMCP's
`version=` kwarg (the previous `_mcp_server.version` internals hack is gone).
All 26 tools, 4 resources, and 3 prompts register unchanged; smoke tests green.

## [0.3.0] — 2026-08-16

### Added — §86 self-service agent credentials + §84 search

`enroll_agent_credential` / `agent_credential_status` /
`revoke_agent_credential` (non-human self-service auth), plus `search_chat`
(§84 hybrid search) and the §88a channel-membership tools.

## [0.1.0] — 2026-07-12

### Added — §33: meshbook as MCP tools

First release. An MCP server (stdio transport, FastMCP) that gives Claude Code / Claude Desktop / any MCP client native meshbook.org access, sharing auth and the active-mesh pointer with [meshbook-cli](https://github.com/tylnexttime/meshbook-cli)'s `~/.meshbook/config` — `mesh login` is the entire setup.

- **18 tools**: `list_my_meshes`, `set_active_mesh`, `list_contacts`, `create_contact`, `list_leads`, `create_lead`, `move_lead_stage`, `list_my_tasks`, `mark_task_done`, `post_chat`, `read_thread`, `post_channel`, `read_channel`, `list_unread_notifications`, `attach_file`, `download_attachment`, `export_mesh`, `export_status`.
- **4 resources**: `meshbook://my-meshes`, `meshbook://active-mesh`, `meshbook://my-tasks-today`, `meshbook://notifications`.
- **3 prompts**: `triage_leads`, `summarise_mesh_week`, `whats_new`.
- Name-or-UUID resolution for meshes, channels (`#name`), pipeline stages, and contacts — models don't need to juggle UUIDs.
- `create_lead` auto-targets the default pipeline's first stage (the API requires `pipelineId` + `stageId`); `export_mesh` sends the `X-Active-Mesh-Id` override the export endpoint demands, so callers needn't flip their active mesh.
- Clean tool errors from the API's `{error: {code, message}}` envelope — never a traceback. 30 s timeouts. Branded `meshbook-mcp/0.1.0` User-Agent (Cloudflare blocks default python UAs).
- Single runtime dependency: the official `mcp` Python SDK; HTTP stays stdlib `urllib`, mirroring the CLI's discipline.

## 0.1.1 — 2026-07-25
- initialize handshake now reports meshbook-mcp's own version, not the mcp SDK's
  (set on the underlying server; FastMCP exposes no version kwarg in our pinned SDK).
- README: multi-identity MESHBOOK_CONFIG_DIR trap documented (Rook's finding).
