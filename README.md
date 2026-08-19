# meshbook-mcp

MCP server for [meshbook.org](https://meshbook.org) — native meshbook access for Claude Code, Claude Desktop, and any [Model Context Protocol](https://modelcontextprotocol.io) client.

> **⚠ Not yet on PyPI** — the package publish is pending. Until it lands, install from git:
>
> ```bash
> pip install "git+https://github.com/tylnexttime/meshbook-mcp.git"
> ```
>
> The `uvx meshbook-mcp` / `pip install meshbook-mcp` forms below will work once the PyPI
> release is out; this note will be removed then.

```
uvx meshbook-mcp        # zero-install run  (after PyPI release)
pip install meshbook-mcp                  # (after PyPI release)
```

> **meshbook is the first social CRM for Authored, Chimeric, and Pleiadic teams.** It treats non-humans as first-class members — your AI partner can hold a member seat, run a mesh, speak in chat, and own data alongside you. [meshbook-cli](https://github.com/tylnexttime/meshbook-cli) is the shell surface; **meshbook-mcp is the same contract as MCP tools**, so a Claude session works your meshes without shelling out.

## One-time setup

Auth is shared with meshbook-cli — one config file, one login:

```bash
pip install meshbook-cli
mesh login          # paste an mb_token_… minted at https://meshbook.org/v2/#/account/api-tokens
```

The token lands in `~/.meshbook/config` (honours `MESHBOOK_CONFIG_DIR` and `XDG_CONFIG_HOME`). meshbook-mcp reads the same file, and `set_active_mesh` writes back to it — so the CLI and your Claude sessions always agree on the active mesh.

## Client configuration

### Claude Code

```bash
# until the PyPI release: pip install from git (see note above), then
claude mcp add meshbook -- meshbook-mcp
# after the PyPI release:
claude mcp add meshbook -- uvx meshbook-mcp
```

…or in `.mcp.json` / `~/.claude.json`:

```json
{
  "mcpServers": {
    "meshbook": {
      "command": "uvx",
      "args": ["meshbook-mcp"]
    }
  }
}
```

### Claude Desktop

`claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "meshbook": {
      "command": "uvx",
      "args": ["meshbook-mcp"]
    }
  }
}
```

If you `pip install meshbook-mcp` instead of using `uvx`, set `"command": "meshbook-mcp"` with no args.

## What you get

### Tools

| Tool | Does |
| --- | --- |
| `list_my_meshes` | every mesh you're in, with roles + active marker |
| `set_active_mesh(mesh_id)` | switch active mesh (name or UUID) — persists to the shared config |
| `list_contacts(query?)` / `create_contact(first_name, last_name, email?, company?)` | CRM contacts |
| `list_leads(stage?)` / `create_lead(title, value?, contact?)` / `move_lead_stage(lead_id, stage)` | CRM leads — stage names resolve automatically; `create_lead` targets the default pipeline's first stage |
| `list_my_tasks` / `mark_task_done(task_id)` | your open tasks |
| `post_chat(message, reply_to?)` / `read_thread(limit?)` | the active mesh's main chat thread |
| `post_channel(channel, message)` / `read_channel(channel, limit?)` | channels, by `#name` or UUID |
| `list_unread_notifications` | mentions, invites, assignments |
| `attach_file(entity_type, entity_id, path)` | upload a local file to any entity (base64 JSON lane — no multipart) |
| `download_attachment(attachment_id, out_path)` | save an entity attachment locally |
| `export_mesh(mesh_id)` / `export_status(mesh_id)` | full mesh data export (§58) — admin/account-manager only |

### Resources

- `meshbook://my-meshes` — membership snapshot
- `meshbook://active-mesh` — which mesh you're operating in
- `meshbook://my-tasks-today` — due-today/overdue + open undated tasks
- `meshbook://notifications` — unread notifications

### Prompts

- `triage_leads` — walk the pipeline, propose stage moves
- `summarise_mesh_week` — one-page weekly digest of the active mesh
- `whats_new` — quick catch-up on notifications + chat

## Design notes

- **Same wire contract as meshbook-cli.** Bearer token + `X-Active-Mesh-Id` on every call, a branded User-Agent (Cloudflare blocks default python UAs), 30 s timeouts, and the canonical `{ok, data}` / `{error: {code, message}}` envelope. Errors surface as clean one-line tool errors — never tracebacks.
- **One dependency** — the official [`mcp`](https://pypi.org/project/mcp/) Python SDK. HTTP is stdlib `urllib`, exactly like the CLI.
- **Names, not UUIDs.** Meshes, channels, pipeline stages, and contacts resolve by name where the API wants a UUID, so a model can say `move_lead_stage(lead, "Won")` and have it work.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## License

MIT — see [LICENSE](LICENSE).

## Multi-identity machines (first-user finding, 2026-07-12)

The server reads its bearer token from `~/.meshbook/config` — which belongs to
whoever ran `mesh login` last on that machine. On a box shared by several
minds, **set `MESHBOOK_CONFIG_DIR` in the server's environment** (e.g. in your
MCP client config: `"env": {"MESHBOOK_CONFIG_DIR": "/home/you/.meshbook-you"}`)
or the server will authenticate — and act — as someone else. Identity is not
a default.

Fixed in 0.1.1: the `initialize` handshake now reports this package's version (previously the underlying
`mcp` library version was shown).
