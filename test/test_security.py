"""Web-layer security: CSRF middleware, TrustedHost, Basic auth, password hashing.

These tests construct a minimal FastAPI app rather than going through
`create_app()` for most cases, so we exercise *only* the middleware
behaviour without dragging in the full route surface (the route fixtures
already cover end-to-end happy paths). The two integration tests that do
go through `create_app()` verify the middleware is wired in correctly
and that `/healthz` is exempt from Basic auth.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harmonist.config import (
    AuthConfig,
    BandcampConfig,
    Config,
    PathsConfig,
    ServerConfig,
    TestConfig,
)
from harmonist.web.main import create_app
from harmonist.web.security import (
    BasicAuthMiddleware,
    CSRFMiddleware,
    hash_password,
    verify_password,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _csrf_app() -> FastAPI:
    """Tiny app with one GET and one POST, behind CSRFMiddleware only."""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "1"}

    @app.post("/act")
    def act() -> dict[str, str]:
        return {"ok": "1"}

    return app


def _auth_app(*, username: str = "alice", password: str = "hunter2") -> FastAPI:
    """Tiny app behind BasicAuthMiddleware. Includes /healthz to verify the
    exemption."""
    app = FastAPI()
    app.add_middleware(
        BasicAuthMiddleware,
        username=username,
        password_hash=hash_password(password),
    )

    @app.get("/")
    def root() -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _full_cfg(tmp_path, **overrides) -> Config:
    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "cfg", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(**overrides.pop("server", {})),
        auth=AuthConfig(**overrides.pop("auth", {})),
        test=TestConfig(mode="fixture"),
        **overrides,
    )
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    return cfg


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_hash_password_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong password", h)


def test_hash_password_each_call_different_salt():
    # Two hashes of the same password must differ — salt is random.
    h1 = hash_password("pw")
    h2 = hash_password("pw")
    assert h1 != h2
    # But both verify.
    assert verify_password("pw", h1)
    assert verify_password("pw", h2)


def test_hash_password_format():
    h = hash_password("x", iterations=1000)
    parts = h.split("$")
    assert parts[0] == "pbkdf2_sha256"
    assert parts[1] == "1000"
    assert len(parts) == 4


def test_hash_password_empty_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        hash_password("")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-hash",
        "pbkdf2_sha256$abc",
        "wrongscheme$1000$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$notanint$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$1000$!!!notbase64$aGFzaA==",
    ],
)
def test_verify_password_malformed_hash_returns_false(bad: str):
    # Must not raise — config typos should fail-closed at the middleware,
    # not crash mid-request.
    assert verify_password("anything", bad) is False


# ---------------------------------------------------------------------------
# CSRF middleware
# ---------------------------------------------------------------------------


def test_csrf_get_request_passes_without_headers():
    client = TestClient(_csrf_app())
    r = client.get("/ping")
    assert r.status_code == 200


def test_csrf_post_without_hx_request_rejected():
    client = TestClient(_csrf_app())
    r = client.post("/act")
    assert r.status_code == 403
    assert "HX-Request" in r.text


def test_csrf_post_with_hx_request_accepted():
    client = TestClient(_csrf_app(), headers={"HX-Request": "true"})
    r = client.post("/act")
    assert r.status_code == 200


def test_csrf_post_with_matching_origin_accepted():
    client = TestClient(
        _csrf_app(),
        headers={"HX-Request": "true", "Origin": "http://testserver"},
    )
    r = client.post("/act")
    assert r.status_code == 200


def test_csrf_post_with_mismatched_origin_rejected():
    client = TestClient(
        _csrf_app(),
        headers={"HX-Request": "true", "Origin": "http://evil.example.com"},
    )
    r = client.post("/act")
    assert r.status_code == 403
    assert "Origin" in r.text


def test_csrf_post_with_matching_referer_accepted():
    # Browser form submits can omit Origin but always send Referer; the
    # middleware should accept that path too.
    client = TestClient(
        _csrf_app(),
        headers={"HX-Request": "true", "Referer": "http://testserver/some/page"},
    )
    r = client.post("/act")
    assert r.status_code == 200


def test_csrf_post_with_mismatched_referer_rejected():
    client = TestClient(
        _csrf_app(),
        headers={"HX-Request": "true", "Referer": "http://evil.example.com/page"},
    )
    r = client.post("/act")
    assert r.status_code == 403
    assert "Referer" in r.text


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_csrf_other_mutating_methods_also_gated(method: str):
    """The middleware gates every mutating method, not just POST."""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.api_route("/x", methods=[method])
    def x() -> dict[str, str]:
        return {"ok": "1"}

    client = TestClient(app)
    # No HX-Request: should be rejected regardless of method.
    r = client.request(method, "/x")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Basic auth middleware
# ---------------------------------------------------------------------------


def test_basic_auth_no_credentials_returns_401_with_challenge():
    client = TestClient(_auth_app())
    r = client.get("/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic ")


def test_basic_auth_correct_credentials_accepted():
    client = TestClient(_auth_app())
    r = client.get("/", auth=("alice", "hunter2"))
    assert r.status_code == 200


def test_basic_auth_wrong_password_rejected():
    client = TestClient(_auth_app())
    r = client.get("/", auth=("alice", "wrong"))
    assert r.status_code == 401


def test_basic_auth_wrong_username_rejected():
    client = TestClient(_auth_app())
    r = client.get("/", auth=("bob", "hunter2"))
    assert r.status_code == 401


def test_basic_auth_malformed_header_rejected():
    client = TestClient(_auth_app())
    r = client.get("/", headers={"Authorization": "Basic !!notbase64!!"})
    assert r.status_code == 401


def test_basic_auth_healthz_exempt():
    """Docker HEALTHCHECK has no credentials — /healthz must be reachable."""
    client = TestClient(_auth_app())
    r = client.get("/healthz")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Integration: create_app() wiring
# ---------------------------------------------------------------------------


def test_create_app_enforces_csrf_on_post(tmp_path):
    cfg = _full_cfg(tmp_path)
    client = TestClient(create_app(cfg))  # NO HX-Request default
    r = client.post("/forget/some-id")
    assert r.status_code == 403


def test_create_app_trusted_host_rejects_unknown(tmp_path):
    cfg = _full_cfg(tmp_path, server={"allowed_hosts": ["harmonist.example.com"]})
    client = TestClient(create_app(cfg), headers={"HX-Request": "true"})
    # TestClient's default Host is "testserver", which isn't in the
    # allow-list — TrustedHostMiddleware should 400 it.
    r = client.get("/healthz")
    assert r.status_code == 400


def test_create_app_trusted_host_accepts_allowed(tmp_path):
    cfg = _full_cfg(tmp_path, server={"allowed_hosts": ["harmonist.example.com"]})
    client = TestClient(
        create_app(cfg),
        headers={"HX-Request": "true", "Host": "harmonist.example.com"},
        base_url="http://harmonist.example.com",
    )
    r = client.get("/healthz")
    assert r.status_code == 200


def test_create_app_basic_auth_when_enabled(tmp_path):
    cfg = _full_cfg(
        tmp_path,
        auth={
            "enabled": True,
            "username": "alice",
            "password_hash": hash_password("hunter2"),
        },
    )
    client = TestClient(create_app(cfg), headers={"HX-Request": "true"})
    # No credentials → 401.
    r = client.get("/")
    assert r.status_code == 401
    # With credentials → 200.
    r = client.get("/", auth=("alice", "hunter2"))
    assert r.status_code == 200
    # /healthz exempt.
    r = client.get("/healthz")
    assert r.status_code == 200


def test_create_app_refuses_to_start_with_broken_auth(tmp_path):
    """auth.enabled=true but no username/password_hash is misconfiguration
    that would silently lock users out (every request 401). Better to
    refuse to construct the app than to ship a brick."""
    cfg = _full_cfg(tmp_path, auth={"enabled": True})  # no username/hash
    with pytest.raises(RuntimeError, match=r"auth\.enabled"):
        create_app(cfg)
