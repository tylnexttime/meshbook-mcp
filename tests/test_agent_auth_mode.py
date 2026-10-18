"""§96 — `auth_mode: agent` in the MCP server: the enrolled key as the
everyday login, sharing meshbook-cli's config contract.

The 13 tests here are the 0.7 baseline, ported in 0.8.0: the lane now lives
in the SDK (``client.agent``), so each test drives a TOOL over the fake
transport and asserts what goes on the wire, instead of calling the deleted
``_auth_token`` / ``_mint_agent_token`` helpers. Names and intent are kept.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from meshbook import config as mb_config  # noqa: E402

from meshbook_mcp import _core, server  # noqa: E402
from tests._fakes import MESH, Err, ok, ok_list  # noqa: E402
from tests.conftest import write_config  # noqa: E402

TOKEN_PATH = "/application/o/token/"


def _pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def _fake_jwt(exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


def _claims(jwt: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(_pad(jwt.split(".")[1])))


@pytest.fixture()
def bench(router):
    """A config dir with an enrolled key + mint bundle, the way
    MESHBOOK_CONFIG_DIR would set it up."""
    cfg_dir = mb_config.config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (cfg_dir / "agent-key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (cfg_dir / "agent-key.json").write_text(json.dumps({
        "kid": "kid-old", "tokenEndpoint": "https://auth.example" + TOKEN_PATH,
        "audience": "https://auth.example/issuer/", "clientId": "meshbook-agents",
        "username": "wanderer"}), encoding="utf-8")
    router.add("GET", "/api/contacts", ok_list([]))
    return {"dir": cfg_dir, "key": key, "router": router}


def _bearer(router) -> str:
    return router.calls("GET", "/api/contacts")[-1].get_header("Authorization")


# ─── lane resolution ─────────────────────────────────────────────────

def test_bearer_mode_returns_stored_token(bench):
    write_config(token="mb_token_x", active_mesh_id=MESH)
    server.list_contacts()
    assert _bearer(bench["router"]) == "Bearer mb_token_x"


def test_bearer_mode_without_token_raises(bench):
    write_config(active_mesh_id=MESH)
    with pytest.raises(server.MeshbookError) as e:
        server.list_contacts()
    assert e.value.code == "not_authenticated"


def test_agent_mode_mints_and_caches(bench):
    write_config(auth_mode="agent", active_mesh_id=MESH)
    bench["router"].add("POST", TOKEN_PATH,
                        {"access_token": _fake_jwt(int(time.time()) + 300), "expires_in": 300})
    server.list_contacts()
    server.list_contacts()
    assert len(bench["router"].calls("POST", TOKEN_PATH)) == 1   # cached across tool calls
    assert _bearer(bench["router"]).startswith("Bearer eyJ")
    form = dict(urllib.parse.parse_qsl(bench["router"].calls("POST", TOKEN_PATH)[0].data.decode()))
    assert _claims(form["client_assertion"])["aud"] == "https://auth.example/issuer/"


def test_agent_mode_remints_when_near_expiry(bench):
    write_config(auth_mode="agent", active_mesh_id=MESH)
    bench["router"].add("POST", TOKEN_PATH,
                        {"access_token": _fake_jwt(int(time.time()) + 300), "expires_in": 300})
    server.list_contacts()
    _core.client()._agent_token_expires_at = time.time() + 10   # inside the 30 s margin
    server.list_contacts()
    assert len(bench["router"].calls("POST", TOKEN_PATH)) == 2


def test_agent_mode_falls_back_to_bearer_on_mint_failure(bench):
    write_config(auth_mode="agent", token="mb_token_fallback", active_mesh_id=MESH)
    bench["router"].add("POST", TOKEN_PATH, Err(503, {"error": "unavailable"}))
    server.list_contacts()
    assert _bearer(bench["router"]) == "Bearer mb_token_fallback"


def test_agent_mode_without_bearer_propagates_failure(bench):
    write_config(auth_mode="agent", active_mesh_id=MESH)
    bench["router"].add("POST", TOKEN_PATH, Err(503, {"error": "unavailable"}))
    with pytest.raises(server.MeshbookError):
        server.list_contacts()
    assert not bench["router"].calls("GET", "/api/contacts")


def test_agent_mode_unready_falls_through_to_bearer(router):
    write_config(auth_mode="agent", token="mb_token_x", active_mesh_id=MESH)  # no key on disk
    router.add("GET", "/api/contacts", ok_list([]))
    server.list_contacts()
    assert router.last.get_header("Authorization") == "Bearer mb_token_x"


# ─── §99: the rotation proof ─────────────────────────────────────────

def _enroll_route(router):
    def handler_payload():
        return ok({"enrolled": True, "username": "wanderer", "kid": None,
                   "tokenEndpoint": "https://auth.example" + TOKEN_PATH,
                   "assertionAud": "https://auth.example/issuer/",
                   "clientId": "meshbook-agents"})
    router.add("POST", "/api/me/agent-credentials", handler_payload())


def test_rotation_assertion_signed_by_current_key(bench):
    write_config(token="mb_token_x")
    _enroll_route(bench["router"])
    server.enroll_agent_credential(force=True)
    body = bench["router"].body(bench["router"].calls("POST", "/api/me/agent-credentials")[0])
    head, payload, sig = body["rotationAssertion"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(_pad(payload)))
    assert claims["sub"] == "wanderer"
    assert claims["aud"] == "meshbook-agent-rotation"
    assert claims["new_kid"] == body["publicKey"]["kid"]
    assert claims["exp"] - claims["iat"] <= 600
    assert json.loads(base64.urlsafe_b64decode(_pad(head)))["kid"] == "kid-old"
    bench["key"].public_key().verify(
        base64.urlsafe_b64decode(_pad(sig)),
        f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_rotation_assertion_none_without_current_key(router):
    write_config(token="mb_token_x")
    _enroll_route(router)
    server.enroll_agent_credential()
    assert "rotationAssertion" not in router.body()


# ─── enroll / register / revoke auth_mode flips ──────────────────────

def test_enroll_rotation_sends_assertion_and_flips_mode(bench):
    write_config(token="mb_token_x")
    _enroll_route(bench["router"])
    result = server.enroll_agent_credential(force=True)
    assert result["enrolled"] is True and result["rotated"] is True
    body = bench["router"].body(bench["router"].calls("POST", "/api/me/agent-credentials")[0])
    assert "rotationAssertion" in body                               # §99 proof rode along
    assert body["publicKey"]["kid"].startswith("mcp-agent-")         # forensic label kept
    assert mb_config.load_config().get("auth_mode") == "agent"       # §96 flip persisted
    assert mb_config.load_config().get("token") == "mb_token_x"      # nothing else touched
    assert (bench["dir"] / "agent-key.pem.bak").exists()             # outgoing key kept
    new_meta = json.loads((bench["dir"] / "agent-key.json").read_text(encoding="utf-8"))
    assert new_meta["kid"] != "kid-old"


def test_first_enroll_sends_no_assertion(router):
    write_config(token="mb_token_x")
    _enroll_route(router)
    result = server.enroll_agent_credential()
    assert result["enrolled"] is True and result["rotated"] is False
    assert "rotationAssertion" not in router.body()


def test_register_sets_agent_mode(router):
    router.add("POST", "/api/register/agent", ok({
        "enrolled": True, "username": "newborn", "user_id": "u-1", "kid": None,
        "tokenEndpoint": "https://auth.example" + TOKEN_PATH,
        "assertionAud": "https://auth.example/issuer/", "clientId": "meshbook-agents"}))
    result = server.register_agent("Newborn")
    assert result["registered"] is True
    assert result["authMode"] == "agent"
    assert result["kid"].startswith("mcp-agent-")
    assert router.body()["username"] == "newborn"
    assert router.last.get_header("Authorization") is None          # unauthenticated route
    assert mb_config.load_config().get("auth_mode") == "agent"


def test_revoke_clears_agent_mode(bench):
    write_config(auth_mode="agent", token="mb_token_x")
    bench["router"].add("DELETE", "/api/me/agent-credentials",
                        ok({"revoked": True, "username": "wanderer"}))
    bench["router"].add("POST", TOKEN_PATH,
                        {"access_token": _fake_jwt(int(time.time()) + 300), "expires_in": 300})
    result = server.revoke_agent_credential()
    assert result["revoked"] is True
    assert result["authMode"] == "bearer"
    assert "auth_mode" not in mb_config.load_config()
    assert (bench["dir"] / "agent-key.pem").exists()                # local key left in place


# ─── 0.8.0: the SDK refuses a silent key replacement ─────────────────

def test_enroll_without_force_refuses_to_replace_a_key(bench):
    write_config(token="mb_token_x")
    with pytest.raises(server.MeshbookError) as exc:
        server.enroll_agent_credential()
    assert exc.value.code == "key_exists"
    assert "force=true" in exc.value.message
    assert not bench["router"].calls("POST", "/api/me/agent-credentials")
    meta = json.loads((bench["dir"] / "agent-key.json").read_text(encoding="utf-8"))
    assert meta["kid"] == "kid-old"                                   # untouched


def test_register_over_an_existing_key_points_at_config_dir_not_force(bench):
    with pytest.raises(server.MeshbookError) as exc:
        server.register_agent("second_seat")
    assert exc.value.code == "key_exists"
    assert "MESHBOOK_CONFIG_DIR" in exc.value.message and "force" not in exc.value.message
    assert not bench["router"].calls("POST", "/api/register/agent")


def test_a_failed_mint_never_quotes_the_token_endpoint_body(bench):
    """0.7 put the first 200 chars of the token endpoint's error body into
    the tool error. That endpoint's bodies can carry credentials; the SDK
    reports only their shape."""
    write_config(auth_mode="agent", active_mesh_id=MESH)
    bench["router"].add("POST", TOKEN_PATH,
                        Err(400, {"error": "invalid_client", "id_token": "eyJSECRETSECRET"}))
    with pytest.raises(server.MeshbookError) as exc:
        server.list_contacts()
    assert "SECRET" not in str(exc.value)
