"""stderr stays quiet on a stdio server (field note 8, Alder 2026-10-19).

0.8.1 let FastMCP log every MeshbookError as a multi-line rich traceback,
and printed FastMCP's banner (with an "Update available" box after a PyPI
lookup) at every start. A stdio client that does not drain stderr can stall
on that: Alder's first probe did, and read it as a server crash. An
ordinary, expected error now costs one stderr line; a genuinely unexpected
exception still gets its full traceback.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading

import pytest

from meshbook_mcp import server


def _call(name, args):
    from fastmcp import Client

    async def go():
        async with Client(server.mcp) as c:
            return await c.call_tool(name, args, raise_on_error=False)

    return asyncio.run(go())


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


NAIVE = {"title": "x", "start": "2026-10-20T09:00:00", "end": "2026-10-20T10:00:00+11:00"}


def test_an_expected_error_is_one_stderr_line(api, capfd):
    capfd.readouterr()
    res = _call("create_event", NAIVE)
    err = capfd.readouterr().err
    assert res.is_error
    lines = _lines(err)
    assert len(lines) <= 1, err
    assert "Traceback" not in err
    assert lines and "create_event" in lines[0] and "naive_datetime" in lines[0]


def test_the_model_still_sees_the_same_text(api):
    """The client-visible text is unchanged from 0.8.1."""
    res = _call("create_event", NAIVE)
    text = res.content[0].text
    assert text.startswith("Error calling tool 'create_event': [0] naive_datetime: start=")
    assert "UTC offset" in text


def test_an_sdk_error_from_the_server_is_one_line(api, capfd):
    from tests._fakes import Err

    api.add("GET", "/api/contacts", Err(403, {"error": {"code": "not_a_member",
                                                        "message": "Not a\nmember."}}))
    capfd.readouterr()
    res = _call("list_contacts", {})
    err = capfd.readouterr().err
    assert res.is_error and "not_a_member" in res.content[0].text
    assert len(_lines(err)) == 1 and "not_a_member" in err, err


def test_an_expected_resource_error_is_one_line(router, capfd):
    """No credentials at all: the my-meshes resource refuses, quietly."""
    from fastmcp import Client
    from mcp.shared.exceptions import McpError

    async def go():
        async with Client(server.mcp) as c:
            with pytest.raises(McpError, match="not_authenticated"):
                await c.read_resource("meshbook://my-meshes")

    capfd.readouterr()
    asyncio.run(go())
    err = capfd.readouterr().err
    assert len(_lines(err)) <= 1 and "Traceback" not in err, err


def test_an_unexpected_exception_keeps_its_traceback(api, capfd, monkeypatch):
    def boom():
        raise RuntimeError("a genuine bug")

    monkeypatch.setattr(server, "client", boom)
    capfd.readouterr()
    res = _call("list_contacts", {})
    err = capfd.readouterr().err
    assert res.is_error
    assert "RuntimeError" in err and len(_lines(err)) > 1, err


# ─── the real thing: a stdio subprocess, as Alder ran it ───────────────


def _rpc(proc, msg) -> None:
    proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    proc.stdin.flush()


def _stdio_session(tmp_path, calls: list[tuple[str, dict]], extra_env=None):
    """Start ``python -m meshbook_mcp.server`` over stdio, make *calls*, and
    return (answers by id, everything the server wrote to stderr)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MESHBOOK_", "FASTMCP_"))}
    env.update(MESHBOOK_CONFIG_DIR=str(tmp_path), PYTHONIOENCODING="utf-8")
    env.update(extra_env or {})
    (tmp_path / "config").write_text(
        json.dumps({"token": "mb_token_test",
                    "active_mesh_id": "11111111-1111-1111-1111-111111111111"}),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "meshbook_mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=str(tmp_path),
    )
    err_chunks: list[bytes] = []
    drain = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    drain.start()
    answers: dict[int, dict] = {}
    try:
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "stderr-test", "version": "0"}}})
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        for n, (name, args) in enumerate(calls, start=2):
            _rpc(proc, {"jsonrpc": "2.0", "id": n, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
            while n not in answers:
                line = proc.stdout.readline()
                assert line, "the server closed stdout before answering"
                msg = json.loads(line)
                if "id" in msg:
                    answers[msg["id"]] = msg
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        drain.join(timeout=5)
    return answers, b"".join(err_chunks).decode("utf-8", "replace")


def test_stdio_server_validation_error_writes_at_most_one_stderr_line(tmp_path):
    answers, err = _stdio_session(tmp_path, [("create_event", NAIVE)])
    result = answers[2]["result"]
    assert result["isError"] is True and "naive_datetime" in result["content"][0]["text"]
    lines = _lines(err)
    assert "Update available" not in err and "FastMCP" not in err, err  # no banner
    assert "Traceback" not in err, err
    assert len(lines) <= 1, err
    assert lines and "naive_datetime" in lines[0], err


def test_stdio_server_bad_arguments_write_one_line_each(tmp_path):
    """FastMCP's own argument check (a missing title, an unknown argument)
    logs one plain line per call, not a rich record wrapped at 80 columns."""
    answers, err = _stdio_session(tmp_path, [
        ("create_event", {"start": "2026-10-20T09:00:00+11:00"}),
        ("list_contacts", {"serach": "a typo, and a long one " * 4}),
    ])
    assert answers[2]["result"]["isError"] and answers[3]["result"]["isError"]
    lines = _lines(err)
    assert len(lines) <= 2 and "Traceback" not in err, err


def test_fastmcp_settings_in_the_environment_win(monkeypatch):
    import fastmcp
    import fastmcp.utilities.logging as fl

    seen: list = []
    monkeypatch.setattr(fl, "configure_logging", lambda **kw: seen.append(kw))
    for attr in ("check_for_updates", "enable_rich_logging", "log_level"):
        monkeypatch.setattr(fastmcp.settings, attr, getattr(fastmcp.settings, attr))
    for var in (server.BANNER_ENV, server.UPDATE_CHECK_ENV, server.LOG_LEVEL_ENV,
                server.RICH_LOGGING_ENV):
        monkeypatch.delenv(var, raising=False)

    assert server._run_options() == {"show_banner": False}
    assert fastmcp.settings.check_for_updates == "off"
    assert fastmcp.settings.enable_rich_logging is False
    assert fastmcp.settings.log_level == "WARNING" and seen == [{"level": "WARNING"}]

    monkeypatch.setattr(fastmcp.settings, "check_for_updates", "stable")
    monkeypatch.setattr(fastmcp.settings, "enable_rich_logging", True)
    monkeypatch.setattr(fastmcp.settings, "log_level", "INFO")
    for var, value in ((server.BANNER_ENV, "true"), (server.UPDATE_CHECK_ENV, "stable"),
                       (server.LOG_LEVEL_ENV, "INFO"), (server.RICH_LOGGING_ENV, "true")):
        monkeypatch.setenv(var, value)
    seen.clear()
    assert server._run_options() == {}
    assert fastmcp.settings.check_for_updates == "stable"
    assert fastmcp.settings.enable_rich_logging is True and seen == []
