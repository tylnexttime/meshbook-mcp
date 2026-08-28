"""§96 — `auth_mode: agent` in the MCP server: the enrolled key as the
everyday login, sharing meshbook-cli's config contract.

Offline coverage: token resolution and caching, bearer fallback, the §99
rotation proof, and the enroll/register/revoke auth_mode flips. Network
is always monkeypatched.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from meshbook_mcp import server  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_cache():
    server._invalidate_agent_token()
    yield
    server._invalidate_agent_token()


@pytest.fixture()
def bench(tmp_path, monkeypatch):
    """A temp CONFIG_DIR with an enrolled key + meta, the way
    MESHBOOK_CONFIG_DIR would set it up."""
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "agent-key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (tmp_path / "agent-key.json").write_text(json.dumps({
        "kid": "kid-old", "tokenEndpoint": "https://auth.example/token/",
        "audience": "https://auth.example/issuer/", "clientId": "meshbook-agents",
        "username": "wanderer"}))
    return {"dir": tmp_path, "key": key}


def _fake_jwt(exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


# ─── _auth_token: lane resolution ────────────────────────────────────

def test_bearer_mode_returns_stored_token(bench):
    assert server._auth_token({"token": "mb_token_x"}) == "mb_token_x"


def test_bearer_mode_without_token_raises(bench):
    with pytest.raises(server.MeshbookError) as e:
        server._auth_token({})
    assert e.value.code == "not_signed_in"


def test_agent_mode_mints_and_caches(bench, monkeypatch):
    mints = []
    monkeypatch.setattr(server, "_mint_agent_token",
                        lambda cfg: mints.append(1) or _fake_jwt(int(time.time()) + 300))
    cfg = {"auth_mode": "agent"}
    t1 = server._auth_token(cfg)
    t2 = server._auth_token(cfg)
    assert t1 == t2
    assert len(mints) == 1


def test_agent_mode_remints_when_near_expiry(bench, monkeypatch):
    mints = []
    monkeypatch.setattr(server, "_mint_agent_token",
                        lambda cfg: mints.append(1) or _fake_jwt(int(time.time()) + 300))
    cfg = {"auth_mode": "agent"}
    server._auth_token(cfg)
    server._AGENT_TOKEN_CACHE["exp"] = time.time() + 10   # inside the 30 s margin
    server._auth_token(cfg)
    assert len(mints) == 2


def test_agent_mode_falls_back_to_bearer_on_mint_failure(bench, monkeypatch):
    def boom(cfg):
        raise server.MeshbookError("mint_failed", "503")
    monkeypatch.setattr(server, "_mint_agent_token", boom)
    assert server._auth_token(
        {"auth_mode": "agent", "token": "mb_token_fallback"}) == "mb_token_fallback"


def test_agent_mode_without_bearer_propagates_failure(bench, monkeypatch):
    def boom(cfg):
        raise server.MeshbookError("mint_failed", "503")
    monkeypatch.setattr(server, "_mint_agent_token", boom)
    with pytest.raises(server.MeshbookError):
        server._auth_token({"auth_mode": "agent"})


def test_agent_mode_unready_falls_through_to_bearer(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)   # no key on disk
    assert server._auth_token(
        {"auth_mode": "agent", "token": "mb_token_x"}) == "mb_token_x"


# ─── §99: the rotation proof ─────────────────────────────────────────

def test_rotation_assertion_signed_by_current_key(bench):
    assertion = server._sign_rotation_assertion("kid-new")
    head, payload, sig = assertion.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    claims = json.loads(base64.urlsafe_b64decode(pad(payload)))
    assert claims["sub"] == "wanderer"
    assert claims["aud"] == "meshbook-agent-rotation"
    assert claims["new_kid"] == "kid-new"
    assert claims["exp"] - claims["iat"] <= 600
    bench["key"].public_key().verify(
        base64.urlsafe_b64decode(pad(sig)),
        f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_rotation_assertion_none_without_current_key(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    assert server._sign_rotation_assertion("kid-new") is None


# ─── enroll / register / revoke auth_mode flips ──────────────────────

def test_enroll_rotation_sends_assertion_and_flips_mode(bench, monkeypatch):
    captured, saved = {}, {}

    def fake_api(method, path, *, cfg, body=None, **kw):
        captured.update(path=path, body=body)
        return {"data": {"enrolled": True, "username": "wanderer",
                         "kid": body["publicKey"]["kid"],
                         "tokenEndpoint": "https://auth.example/token/",
                         "audience": "https://auth.example/issuer/",
                         "clientId": "meshbook-agents"}}
    monkeypatch.setattr(server, "_api_call", fake_api)
    monkeypatch.setattr(server, "save_config", lambda c: saved.update(c))
    monkeypatch.setattr(server, "load_config", lambda: {"token": "mb_token_x"})
    result = server.enroll_agent_credential()
    assert result["enrolled"] is True
    assert captured["path"] == "/api/me/agent-credentials"
    assert "rotationAssertion" in captured["body"]          # §99 proof rode along
    assert saved.get("auth_mode") == "agent"                # §96 flip persisted
    assert (bench["dir"] / "agent-key.pem.bak").exists()    # outgoing key kept
    new_meta = json.loads((bench["dir"] / "agent-key.json").read_text())
    assert new_meta["kid"] != "kid-old"


def test_first_enroll_sends_no_assertion(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config")
    captured = {}

    def fake_api(method, path, *, cfg, body=None, **kw):
        captured.update(body=body)
        return {"data": {"enrolled": True, "username": "wanderer",
                         "kid": body["publicKey"]["kid"]}}
    monkeypatch.setattr(server, "_api_call", fake_api)
    monkeypatch.setattr(server, "save_config", lambda c: None)
    monkeypatch.setattr(server, "load_config", lambda: {"token": "mb_token_x"})
    result = server.enroll_agent_credential()
    assert result["enrolled"] is True
    assert "rotationAssertion" not in captured["body"]


def test_register_sets_agent_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config")
    saved = {}

    def fake_api(method, path, *, cfg, body=None, **kw):
        return {"data": {"enrolled": True, "username": "newborn",
                         "user_id": "u-1", "kid": body["publicKey"]["kid"]}}
    monkeypatch.setattr(server, "_api_call", fake_api)
    monkeypatch.setattr(server, "save_config", lambda c: saved.update(c))
    monkeypatch.setattr(server, "load_config", lambda: {})
    result = server.register_agent("newborn")
    assert result["registered"] is True
    assert result["authMode"] == "agent"
    assert saved.get("auth_mode") == "agent"


def test_revoke_clears_agent_mode(bench, monkeypatch):
    saved = {}
    monkeypatch.setattr(server, "_api_call",
                        lambda *a, **k: {"data": {"revoked": True, "username": "wanderer"}})
    monkeypatch.setattr(server, "save_config", lambda c: saved.update(c))
    monkeypatch.setattr(server, "load_config",
                        lambda: {"auth_mode": "agent", "token": "mb_token_x"})
    result = server.revoke_agent_credential()
    assert result["revoked"] is True
    assert result["authMode"] == "bearer"
    assert "auth_mode" not in saved
