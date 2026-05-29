"""Web-layer security: CSRF + Basic auth middlewares, password hashing.

Threat model in two lines: Harmonist holds Bandcamp cookies (a real
credential) and exposes destructive endpoints (Sync, tag, Forget, erase
sidecars). The plausible attacks are (a) accidental internet exposure of
a non-loopback bind, (b) drive-by CSRF from a malicious site visited in a
browser that's also reachable to Harmonist, and (c) DNS rebinding against
a `0.0.0.0`-bound instance.

The canonical deployment is "single user, behind a reverse proxy that
handles auth (Authelia / Authentik / Tailscale-only / Basic auth in the
proxy)". This module provides in-app defense in depth for that pattern,
*not* a replacement for it:

- ``CSRFMiddleware`` blocks cross-site state-changing requests regardless
  of auth state. Required header (``HX-Request: true``) cannot be set
  cross-origin without a CORS preflight the app doesn't honour, and an
  ``Origin``/``Referer`` host check is the belt to that suspenders.
- ``BasicAuthMiddleware`` is an opt-in knob for users who don't put a
  proxy in front. Off by default.

Hostname allow-listing (DNS rebinding protection) is handled by
``starlette.middleware.trustedhost.TrustedHostMiddleware`` directly, wired
in ``main.create_app``.

``python -m harmonist.web.security`` prompts for a password and prints a
``password_hash = "..."`` line ready to paste into ``harmonist.toml``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp

# Methods that mutate state. GETs / HEADs / OPTIONS are read-only and
# safe; we don't gate them.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Routes exempt from auth — Docker's HEALTHCHECK is unauthenticated and
# we want it to keep working. The healthcheck only reports liveness; it
# does not leak album data or accept actions.
_AUTH_EXEMPT_PATHS = frozenset({"/healthz"})

# Password hash format: pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>.
# Stdlib only — no bcrypt/argon2 dependency. PBKDF2-SHA256 is appropriate
# for a single-user, low-value secret (gatekeeping a self-hosted tagger);
# choose argon2 if Harmonist ever gains real multi-user auth.
_PBKDF2_ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-SHA256
_HASH_SCHEME = "pbkdf2_sha256"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """Derive a self-contained hash string from a plaintext password.

    Format: ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``. The
    iteration count and salt travel with the hash so a future bump in
    cost doesn't invalidate existing config.
    """
    if not password:
        raise ValueError("password must be non-empty")
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_HASH_SCHEME}${iterations}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, hash_str: str) -> bool:
    """Constant-time check of ``password`` against a stored hash. Returns
    False for malformed hashes — never raises, so a typo in config can't
    crash the auth middleware mid-request.
    """
    try:
        scheme, iter_s, salt_b64, hash_b64 = hash_str.split("$")
    except ValueError:
        return False
    if scheme != _HASH_SCHEME:
        return False
    try:
        iterations = int(iter_s)
        salt = _b64decode(salt_b64)
        expected = _b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64decode(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# ---------------------------------------------------------------------------
# CSRF middleware
# ---------------------------------------------------------------------------


class CSRFMiddleware(BaseHTTPMiddleware):
    """Block cross-site state-changing requests.

    For POST / PUT / PATCH / DELETE the request must satisfy *both*:

    1. ``HX-Request: true`` header is present. HTMX sends this on every
       AJAX request; a cross-origin attacker cannot set custom headers
       on a simple form POST without a CORS preflight, which Harmonist
       does not honour. This alone is sufficient CSRF protection for the
       app's actual usage, but the Origin/Referer check is cheap belt.
    2. If ``Origin`` is present, its host must match the request's Host.
       Otherwise, if ``Referer`` is present, its host must match. If
       neither header is present (curl, tooling), the HX-Request check
       carries the request.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method not in _STATE_CHANGING_METHODS:
            return await call_next(request)

        if request.headers.get("hx-request", "").lower() != "true":
            return PlainTextResponse("CSRF: missing HX-Request header", status_code=403)

        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if origin is not None and _host_of(origin) != host:
            return PlainTextResponse("CSRF: Origin mismatch", status_code=403)
        if origin is None and referer is not None and _host_of(referer) != host:
            return PlainTextResponse("CSRF: Referer mismatch", status_code=403)
        # No Origin and no Referer: the HX-Request gate above is enough.
        return await call_next(request)


def _host_of(url: str) -> str:
    """Extract the ``host[:port]`` of a URL for comparison with the Host
    header. Returns the raw input on parse failure so the comparison
    fails closed.
    """
    try:
        return urlsplit(url).netloc
    except ValueError:
        return url


# ---------------------------------------------------------------------------
# Basic auth middleware
# ---------------------------------------------------------------------------


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic auth, single user. Off unless ``cfg.auth.enabled``.

    ``/healthz`` is exempt so Docker's container HEALTHCHECK keeps
    working without baked-in credentials. The healthcheck reveals only
    "is the process up", which is acceptable to leak.
    """

    def __init__(self, app: ASGIApp, *, username: str, password_hash: str) -> None:
        super().__init__(app)
        self._username = username
        self._password_hash = password_hash

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:].encode("ascii")).decode("utf-8")
                username, _, password = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                return self._challenge()
            if hmac.compare_digest(username, self._username) and verify_password(
                password, self._password_hash
            ):
                return await call_next(request)
        return self._challenge()

    @staticmethod
    def _challenge() -> Response:
        return PlainTextResponse(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Harmonist"'},
        )


# ---------------------------------------------------------------------------
# CLI: `python -m harmonist.web.security` — generate a password hash.
# ---------------------------------------------------------------------------


def _main() -> None:
    import getpass

    print("Generate a Harmonist password hash for harmonist.toml.")
    print("Paste the resulting line under [auth] alongside `enabled = true`.")
    print()
    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw1 != pw2:
        raise SystemExit("Passwords don't match.")
    if not pw1:
        raise SystemExit("Empty password.")
    print()
    print(f'password_hash = "{hash_password(pw1)}"')


if __name__ == "__main__":
    _main()
