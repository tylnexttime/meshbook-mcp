"""SDK errors reach a model mapped by code and hint, never by message text.

0.8.0 searched and split the SDK's message (``startswith("No API token")``,
``"registering" in message``, ``split(" Pass dedupe=False")``), so any
rewording in the SDK changed what a model was told, sometimes into advice
for a Python caller. meshbook-sdk 0.4.1 added ``MeshbookError.hint``, a
stable machine channel; the MCP maps on code, hint and status only.
"""
from __future__ import annotations

import pytest
from meshbook import MeshbookError

from meshbook_mcp import _core, server
from tests._fakes import Err


def test_a_reworded_local_auth_failure_still_reads_as_setup():
    """Red on 0.8.0: any wording but "No API token..." went through verbatim."""
    e = MeshbookError("not_authenticated", "Completely new wording. Pass token=..., or set it.")
    out = _core.hinted(e)
    assert out.code == "not_authenticated" and out.hint == e.hint
    assert "token=" not in out.message and "mesh login" in out.message


def test_a_server_rejection_keeps_the_servers_words():
    e = MeshbookError("not_authenticated", "Token revoked by an admin.", 401,
                      hint="reauthenticate")
    out = _core.hinted(e)
    assert out.message.startswith("Token revoked by an admin.") and "mesh login" in out.message
    assert out.status == 401 and out.hint == "reauthenticate"


def test_key_exists_default_wording_ignores_the_sdk_message():
    """0.8.0 picked the register wording when the SDK message said
    'registering'. The tool now decides (register_agent has an override)."""
    out = _core.hinted(MeshbookError("key_exists", "registering would overwrite it. force=True"))
    assert "enrolling would replace it" in out.message and "force=True" not in out.message
    assert out.hint == "confirm_replace_key"


def test_mapping_by_hint_appends_to_the_sdk_message():
    e = MeshbookError("network_error", "Lost the answer to POST /api/tasks.",
                      hint="check_before_retry")
    out = _core.hinted(e)
    assert out.message.startswith("Lost the answer") and "before calling it again" in out.message
    cursor = _core.hinted(MeshbookError("bad_argument", "no offset here.",
                                        hint="use_before_cursor"))
    assert "before=" in cursor.message and "nextBefore" in cursor.message


def test_an_unmapped_error_passes_through_unchanged():
    e = MeshbookError("not_a_member", "Not a member.", 403)
    assert _core.hinted(e) is e


def test_errors_the_mcp_raised_itself_are_not_rewritten():
    with pytest.raises(_core.ToolError) as exc:
        _core.aware_iso("2026-10-20T09:00:00", "start")
    assert _core.hinted(exc.value) is exc.value
    assert "start=" in exc.value.message  # its field name survives (the SDK mapping has none)


def test_sdk_datetime_errors_drop_the_python_examples():
    e = MeshbookError("naive_datetime", "before has no time zone. Pass datetime(..., tzinfo=...)")
    out = _core.hinted(e)
    assert "tzinfo" not in out.message and "UTC offset" in out.message


def test_the_sdk_hint_survives_the_mcp_wording(api):
    api.add("GET", "/api/contacts", Err(401, {"error": {"code": "token_revoked",
                                                        "message": "revoked"}}))
    with pytest.raises(server.MeshbookError) as exc:
        server.list_contacts()
    assert exc.value.code == "token_revoked" and exc.value.hint == "reauthenticate"
    assert "sign in again" in exc.value.message


def test_no_message_text_is_parsed_in_the_mapping():
    """Structural: the mapping never inspects e.message beyond passing it on."""
    import inspect

    src = inspect.getsource(_core.hinted) + "".join(
        inspect.getsource(f) for f in (_core._not_authenticated,)
    )
    for needle in (".startswith(", ".split(", " in e.message", "e.message.find"):
        assert needle not in src


def test_server_too_old_tells_the_model_the_server_lacks_the_route(api):
    """meshbook-sdk 0.4.2 marks a registry route answered with FastAPI's bare
    404 as server_too_old (the code stays http_error). 0.8.1 passed the bare
    "Not Found" on, which reads like a missing record."""
    api.add("GET", "/api/contacts", Err(404, {"detail": "Not Found"}))
    with pytest.raises(server.MeshbookError) as exc:
        server.list_contacts()
    e = exc.value
    assert e.code == "http_error" and e.hint == "server_too_old" and e.status == 404
    assert e.message.startswith("Not Found")
    assert "older than this client" in e.message and "not a missing record" in e.message


def test_a_routes_own_404_is_not_server_too_old(api):
    from meshbook_mcp import _core as core

    api.add("GET", "/api/contacts/" + "d" * 8 + "-0000-0000-0000-000000000001",
            Err(404, {"error": {"code": "not_found", "message": "Contact not found."}}))
    with pytest.raises(server.MeshbookError) as exc:
        server.get_contact("dddddddd-0000-0000-0000-000000000001")
    assert exc.value.code == "not_found" and exc.value.hint != "server_too_old"
    assert core._APPEND_BY_HINT["server_too_old"] not in exc.value.message
