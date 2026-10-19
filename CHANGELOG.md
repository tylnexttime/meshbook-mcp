# Changelog

All notable changes to `meshbook-mcp` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.8.3] — 2026-10-19

Alder's re-test of the 0.12 clients (Tentyl ty-06d2b52d01a5): `list_events`
lacked the paging fields meshbook-cli's `--json` envelope carries. So did
every other listing. Thanks to Alder for the re-test.

### Changed

- **Every listing answers one shape**, the CLI envelope's paging fields
  included: `count`, `total`, `truncated`, `limit`, `offset`, `hasMore`,
  `paging`, always present and in that order, then `nextOffset` (offset
  paging) or `nextBefore` (cursor paging) when there is a next page, then
  `items`, then the tool's own keys (`expanded`, `semantic`, `closedHidden`,
  `channel`, `mesh`, ...). Before, `limit` and `offset` appeared only when
  set and non-zero, and nothing named the paging style. Additive: no key a
  0.8.2 caller read has gone or changed meaning.
- **`paging` uses the CLI's vocabulary**: `offset` (contacts, companies,
  leads, projects, tasks, my tasks, events, unread notifications), `before`
  (`read_thread`, `read_channel`), `limit` (`search_chat`, which takes
  `limit` only: 0.8.2 called it `none`) or `none` (meshes, mesh members,
  channels, channel members, exports: the whole set in one answer).
- **`hasMore`** is true exactly when `nextOffset` or `nextBefore` is
  present; `truncated` stays true for any "more than this answer", a server
  cap included (an expanded `list_events` window capped at 500 occurrences
  is `truncated` with a `note`, but has no next page).
- **`offset`** is the page's offset under offset paging (0 on the first
  page), otherwise null; **`limit`** is the page size, null where the call
  takes none. An unpaged route's server `pageSize` is no longer reported as
  a `limit` (0.8.2 showed `list_my_meshes` a `limit` equal to its row
  count).
- The server instructions mention `hasMore` and `paging`.
- **README: "Upgrading on Windows"** (Alder's upgrade, 2026-10-19). With
  MCP clients still running, pip could not replace the locked
  `meshbook-mcp.exe` (`WinError 32`, even through its `.deleteme`
  fallback) and left the old 0.7.0 half-uninstalled as a `~eshbook_mcp`
  folder in `site-packages`; the new package did land, and the old
  launcher started it, since the entry point is unchanged. Close every
  client running meshbook-mcp before `pip install -U`; on `WinError 32`,
  close them and re-run, and delete any leftover `~eshbook_mcp` folder.
- Version 0.8.3.

### Fixed

- `list_my_meshes`, `list_mesh_members`, `list_channels`,
  `list_channel_members` and `export_status` went through the envelope as
  offset-paged: had the server ever marked one of those answers
  `truncated`, the envelope would have offered a `nextOffset` the tools
  have no `offset=` for (not seen in the field; found reading the code).
  They are `paging: none` now and never offer one.

## [0.8.2] — 2026-10-19

Alder's 0.12 field test (2026-10-19), turned into fixes: a series time zone,
the CLI's event defaults, and a quiet stderr. Thanks to Alder for the field
notes (numbers below are theirs) and for the stall that showed how much a
noisy stderr costs a stdio client.

### Added

- **`tz` on `create_event`, `update_event` and `set_event_recurrence`**
  (field note 1). An IANA zone that becomes the recurrence's `tzid`
  (meshbook-sdk 0.4.2), so a weekly 10:00 Sydney series stays at 10:00
  across daylight saving for every reader, not only for one that passes
  `list_events(tz=)`. `update_event` re-sends the stored rule with the zone
  and refuses `tz` alone on an event that does not repeat. When a series
  has no zone, or the server did not keep the one sent (a server older than
  series zones), the answer carries a `note` saying it will shift, so the
  model can ask. `repeats` names the zone ("Weekly on TU in
  Australia/Sydney"). `tz` is each tool's last parameter.

### Changed

- **Depends on `meshbook-sdk[agent]>=0.4.2,<0.5`** (the recurrence `tzid`
  and the `server_too_old` hint).
- **`create_event`'s `end` defaults to `start` + 1 hour** (+ 1 day with
  `all_day`), like `mesh events create` (field note 7). An explicit `end` is
  kept; positional 0.8.x callers are unaffected.
- **The event tools' descriptions name the field mapping** (field note 7):
  their `start`, `end`, `type`, `allDay` are the API's `startAt`, `endAt`,
  `eventType`, `isAllDay`.
- **`server_too_old`** (meshbook-sdk 0.4.2): a registry route the server
  answers with a bare 404 is told to the model as an old server, not a
  missing record. The code is still `http_error`.
- **stderr is quiet** (field note 8). An expected error (any
  `MeshbookError`, including the MCP's own `ToolError`) is logged as one
  line and handed to FastMCP below its log level, so no rich traceback; the
  text the client sees is unchanged. A genuinely unexpected exception still
  logs its traceback. At start, FastMCP's banner, its PyPI "Update
  available" lookup and its rich (80-column wrapped) log handler are turned
  off through FastMCP's own settings, and its level is WARNING; any
  `FASTMCP_*` variable set in the environment wins.
- `list_events` and the server instructions say that a series' own zone
  beats the list's `tz`, and to pass `tz=` for a repeating event.

### Fixed

- **`update_event` with `start` but no `end` made a zero-length event**
  (field note 2): the server kept the old end. The tool now reads the event
  and shifts the end by its stored length; a move that also turns `all_day`
  on or off takes `create_event`'s default length instead.
- `list_events` said its `tz` decides how every series repeats; a series
  with a stored zone repeats in that zone.

## [0.8.1] — 2026-10-19

A fast follow onto meshbook-sdk 0.4.1: the SDK's public surface replaces
the private and hand-rolled pieces 0.8.0 still carried, and the bugs found
on the way are fixed, each with a test that failed on 0.8.0.

### Changed

- **Depends on `meshbook-sdk[agent]>=0.4.1,<0.5`.**
- **`download_attachment` uses `files.download_bytes(id, mesh=)`.** No
  more `meshbook._endpoints` import, hand-rendered path or
  `download_raw`. The server's filename is reduced by the SDK's
  `meshbook.safe_filename`, the one sanitiser (the MCP's own copy is gone;
  `_core.safe_filename` is now the SDK's). The MCP's write-side policy
  stays: home/cwd fence, never the config dir, no overwrite without
  `overwrite=true`.
- **Errors are mapped by code and `MeshbookError.hint`**, never by
  searching or splitting the SDK's message. Codes whose SDK message advises
  a Python caller get MCP wording (including the new `config_unreadable`
  and `pending_key`); otherwise the SDK message is kept and a sentence
  chosen by the hint is added. The hint is passed through. Errors the MCP
  raises itself are `ToolError`, a `MeshbookError` subclass.
- **`read_thread` and `read_channel` take `before=`** (an ISO date-time
  with an offset) and answer `nextBefore`; they never answer `nextOffset`
  (the routes ignore an offset and the SDK now refuses one). `search_chat`
  answers no cursor (it takes `limit` only).
- **Listings report `total` only when `ItemList.total_exact` says it is
  the full count**, and `nextOffset` from `ItemList.next_offset`. As a
  result `list_my_meshes` (an unpaged route) and `list_my_tasks` with
  closed tasks hidden now answer `total: null`.
- **`list_my_tasks` uses `tasks.list_mine(open_only=)`**; so does the
  `meshbook://my-tasks-today` resource.
- **`me` costs one `GET /api/me` per client** (`client.me_id()`), not one
  per argument per call.
- `set_active_mesh` reads `Mesh.env_override`.
- Tests read the public `meshbook.endpoints`; two guards keep the package
  off private `meshbook._*` modules and off `download_raw`.

### Fixed

- **Chat order.** The server's chat and channel routes answer oldest first;
  0.8.0 assumed newest first and reversed the page, so the "oldest first"
  tools showed newest first. The page is sorted by `createdAt` now.
- **Chat truncation.** Those routes send `hasMore`; 0.8.0 called any full
  page truncated. `hasMore` decides now.
- **A symlink planted under the safe filename** in a download directory
  carried an `overwrite=true` download wherever it pointed inside home;
  refused now (`unsafe_path`).
- **An existing `out_path` file** was refused only after the whole
  download; it is refused before the request.
- **A corrupt config** (now `config_unreadable` from the SDK) reached the
  model with the SDK's Python advice; it gets MCP wording naming the file,
  which is left untouched.
- **The SDK's raise-site hint was lost** when an error was re-worded (a
  401 rejection's `reauthenticate` came back as `provide_credentials`).
- **`set_active_mesh`** let the SDK's `UserWarning` out on stderr and added
  its override note whenever `MESHBOOK_MESH` was set, even to the mesh just
  saved.
- Test fakes now emit the server's real envelopes (`ok_page` sends limit
  and offset; unpaged routes send `ok_list`'s single page; chat sends
  `items`/`pinned`/`hasMore`, oldest first).

## [0.8.0] — 2026-10-19

### Changed — built on meshbook-sdk; the MCP owns only the MCP surface

- **Depends on `meshbook-sdk[agent]>=0.4,<0.5`** (and `fastmcp>=3,<4`). The
  server's own config load/save, HTTP core, envelope and error handling,
  inline agent-JWT lane and name resolution are gone (about 450 lines);
  every request goes through the SDK, shared with meshbook-cli. In return:
  429/503 retries honouring `Retry-After`, the server's `detail`-shaped
  errors (e.g. 403 `not_a_member`) reported with their real code, and the
  agent lane's stricter guards.
- **The MCP never writes `~/.meshbook/config` itself.** `set_active_mesh`
  persists through `meshes.use(persist=True)` and the agent tools through
  the SDK's `persist=True`: a locked, atomic merge that keeps keys it does
  not know. (0.7 rewrote the whole file with a plain `write_text`.)
- **Per-call mesh.** Every mesh-scoped tool takes `mesh` (a name or id) for
  that call only; `MESHBOOK_MESH` pins a mesh for the process. Precedence:
  `mesh` > `MESHBOOK_MESH` > the saved active mesh. `set_active_mesh` says
  so when the environment overrides what it saved.
- **Listings report the TRUE total.** Every list tool answers `count`,
  `total`, `truncated` (+ `limit`, `offset`, `nextOffset`) and takes
  `limit`/`offset`. 0.6.0's `count` was the page length and `list_contacts`
  hard-coded 50, so a model with 80 contacts saw 50 and could not tell.
  A `total` from a pre-2026-10-19 server (the page length) is reported as
  `null`, never as a total.
- **Errors** carry the SDK's codes, with hints in tool terms (never "pass
  token=" or "client.meshes.use()"). One rename: not signed in is now
  `not_authenticated` (0.7 said `not_signed_in`), with the same setup hint.
- **Version from package metadata only** (the literal in `__init__.py` is
  gone); the guard test crosses to pyproject.
- `list_unread_notifications` filters unread server-side, so unread items
  past the first page are no longer lost and `total` is the unread count.
- `list_my_tasks` takes `include_closed`, `sort`, `limit`, `offset`, `mesh`
  and reports how many closed tasks it hid from the page.
- `whoami` also reports the auth lane, the base URL, and where the active
  mesh came from (and survives a bad `MESHBOOK_MESH`).
- `agent_credential_status` compares the server's kid with the local key's.
- Prompts mention the calendar; `triage_leads` pages through `truncated`.

### Added — calendar events (CALENDAR-CONTRACT 2026-10-19)

`list_events`, `get_event`, `create_event`, `update_event`, `delete_event`,
`set_event_recurrence`, `clear_event_recurrence`, `add_event_attendee`,
`remove_event_attendee`. App-native meshbook events (no Microsoft Graph).
Date-times must carry a UTC offset; a naive one is refused with a message
telling the model to add one. `create_event` refuses an identical title +
start in the same mesh unless `force=true` (the CLI's guard). A window
expands recurring events into occurrences; `tz` keeps their wall clock.

### Added — §102 write-surface parity

`list_tasks` (filters + `sort` by due/priority/created/title), `get_task`,
`create_task` (`due`, `assignee`, `tags`, `parent`), `update_task`,
`delete_task`; `list_projects`, `get_project`, `create_project`,
`update_project`, `archive_project` (closes it; meshbook has no archived
state), `add_project_member`, `remove_project_member`, `delete_project`;
`get_contact`, `update_contact`, `delete_contact` and full fields on
`create_contact` (phone, title, role, notes, tags); `list_companies`,
`get_company`, `create_company`, `update_company`, `delete_company`;
`delete_lead`. Updates send only what is passed and empty fields named in
`clear`. Delete tools take ids only. 29 → 60 tools.

`enroll_agent_credential` gains `force`: it now REFUSES to replace an
existing local key without it (0.7 replaced it silently, which kills the
old key server-side the instant the new one lands).

### Security

- `attach_file` / `download_attachment` are fenced: never the meshbook
  config dir (attaching it would have uploaded the token or the agent
  private key), otherwise only under home or the cwd unless
  `MESHBOOK_MCP_ALLOW_ANY_PATH=1`.
- `download_attachment` never overwrites an existing file unless
  `overwrite=true`, and reduces the server's (uploader-chosen) filename to
  a safe basename. 0.7 joined it onto the output directory as sent, so a
  name like `../../x` could escape it.
- A failed agent-token mint no longer quotes the token endpoint's body.
- Transport stays stdio only; the README says what an HTTP transport needs.

### Fixed

- `meshbook://my-tasks-today` judged "today" by the UTC date (in Sydney
  before 11:00 a task due today was not listed); it uses this machine's
  date now.
- `MESHBOOK_BASE` was read once at import and a config `base` beat it;
  `MESHBOOK_TOKEN` was ignored. Both now follow the shared precedence.
- Config was read in the locale encoding.

### Tests and tooling

- Tests: 42 → 150. The 42 baseline tests were ported from a monkeypatched
  `_api_call` to a fake transport under the SDK, keeping their names and
  assertions; one (`test_no_tool_returns_a_bare_list`) was rewritten
  because its source regex would have matched nothing after the port and
  passed forever. Eight 0.7 tools no test ever called are now covered, and
  a test fails if any registered tool goes untested.
- Contract check (F3): `tests/contract/mcp-calls.json` lists every SDK call
  the tools make (the suite fails on any call missing from it); CI checks
  it against a pinned server OpenAPI with the SDK's checker, and scans
  `meshbook_mcp/` for hand-built requests (none).
- The README tool table is generated from FastMCP's own listing and tested.
- ruff pinned `>=0.17,<0.18`; the publish job refuses a tag that does not
  match pyproject's version.

### Release order

Needs meshbook-sdk 0.4.0 on PyPI first. The event update/recurrence/attendee
tools, project members and true totals need the 2026-10-19 meshbook server;
until it is deployed those return the server's 404/405 and totals read
`null`.

## [0.7.0] — 2026-08-28

### Added — §96: the key lane as the everyday login

- **`auth_mode: "agent"`** honoured from the shared meshbook-cli config: every tool
  call signs an RFC 7523 assertion with the local key, mints a 5-minute Authentik
  token, caches it in-process, re-mints ≤30 s before expiry, and retries once on a
  401. The stored bearer becomes the fallback — `mesh agent enroll` in a terminal
  upgrades a live Claude session's auth with zero extra steps, and a bench with an
  enrolled key and no bearer runs every tool.
- `enroll_agent_credential` / `register_agent` set `auth_mode: "agent"` on success;
  `revoke_agent_credential` clears it (with a no-lane warning when no bearer remains).

### Changed — §99: rotation proves possession of the outgoing key

- Re-enrolling over an existing key sends `rotationAssertion` (signed by the current
  key, `aud "meshbook-agent-rotation"`, bound to the new kid), required by the server
  on the agent-JWT lane. The outgoing key/meta survive as `.bak`.

## [0.6.0] — 2026-08-20

Written on 2026-10-19 from the release commit (e6c1c94); until then this file
skipped 0.5.0–0.6.0 with a "see git history" note.

### Changed — BREAKING: an explicit count on every listing (Wren, report B6)

- Every list-returning tool answers `{"count": N, "items": [...]}` instead of a bare
  array, because a bare `[]` serialises to nothing and "found zero" looked like "did
  nothing". Applied to all ten list tools at once, with a structural test against a
  bare-list return.

### Added

- `list_mesh_members(mesh_id?)` (Wren, report A6): roles, humans vs AI, join dates,
  pending invite/request counts, from `GET /api/meshes/{id}/detail`. 29 tools.

## [0.5.1] — 2026-08-20

Written on 2026-10-19 from the release commit (068ee5f).

### Fixed (Wren's report from Liza25, 2026-08-20)

- B1: 0.5.0 shipped from pyproject while `__version__` and the serverInfo banner said
  0.4.0. The old guard compared `__version__` with itself; the new one crosses to
  pyproject.toml.
- B5: `set_active_mesh` wrote the SHARED config without asking the server whether the
  token could act there; one out-of-scope call locked the CLI out too. It now verifies
  first.

### Added

- B3: `whoami` (username, display name, identity type, tier, user id, active mesh).
  28 tools.

## [0.5.0] — 2026-08-19

Written on 2026-10-19 from the release commits (e10f948, 4fa324e).

### Added — §97 self-registration

- `register_agent`: local keygen, a possession proof scoped to
  `aud=meshbook-agent-registration`, an unauthenticated POST, key and mint bundle
  saved beside the config. Refuses if a local agent key already exists. The seat
  lands in the lobby. 27 tools.

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

### Added — §86 self-service agent credentials

`enroll_agent_credential` / `agent_credential_status` /
`revoke_agent_credential` (non-human self-service auth). 26 tools.
(This entry used to credit `search_chat` and the channel-membership tools to
0.3.0; they landed in 0.2.0.)

## [0.2.0] — 2026-08-16

Never tagged; written on 2026-10-19 from the commit (a424faf).

### Added — §88a channel membership + §84 search

`list_channels`, `list_channel_members`, `add_channel_member`,
`remove_channel_member`, and `search_chat` (§84 hybrid keyword + semantic search).
23 tools.

## [0.1.1] — 2026-07-25

- initialize handshake now reports meshbook-mcp's own version, not the mcp SDK's
  (set on the underlying server; FastMCP exposed no version kwarg in the pinned SDK).
- README: multi-identity MESHBOOK_CONFIG_DIR trap documented (Rook's finding).

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
