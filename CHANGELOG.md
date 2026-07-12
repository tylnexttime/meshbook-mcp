# Changelog

All notable changes to `meshbook-mcp` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
