# Deployment & security

Read this before Harmonist is reachable by anything other than your own machine.
For getting it running in the first place, see
**[installation.md](installation.md)**.

Harmonist stores a Bandcamp session cookie (a real credential — it's how the
sync logs in to your account) and exposes destructive actions: bulk tagging,
"Forget", and "erase all sidecars". It is **not** built to face the public
internet directly. The expected deployment is single-user, on a private network
or behind a reverse proxy that handles authentication.

## What ships in the box

Three layers of defense apply automatically:

1. **Loopback by default.** `server.host = 127.0.0.1` unless you change it.
   (The Docker image overrides this to `0.0.0.0` because container networking
   requires it — see below.)
2. **CSRF protection.** All state-changing requests require an `HX-Request:
   true` header (sent by HTMX, not by a malicious cross-origin form) plus a
   matching `Origin`/`Referer`. This blocks drive-by CSRF even if you're
   already logged in.
3. **Hostname allow-listing** via `server.allowed_hosts` (DNS-rebinding
   protection). Default is `["*"]` (permissive — see below).

## Recommended: a reverse proxy

Put Harmonist behind a reverse proxy on its own hostname, with TLS (e.g. Let's
Encrypt) and authentication handled by the proxy. Caddy, nginx, Traefik,
Authelia, Authentik, and Tailscale Serve all work; pick what you already run.
Then lock down the hostname allow-list to match:

```toml
[server]
host = "0.0.0.0"                              # for Docker / LAN bind
allowed_hosts = ["harmonist.example.com",     # your real hostname
                 "localhost", "127.0.0.1"]    # keep healthchecks working
```

## Fallback: built-in Basic auth

**If you can't put a proxy in front**, enable the built-in HTTP Basic auth as
a fallback:

```bash
python -m harmonist.web.security
# Password: ********
# Confirm:  ********
#
# password_hash = "pbkdf2_sha256$600000$...$..."
```

```toml
[auth]
enabled = true
username = "alice"
password_hash = "pbkdf2_sha256$600000$...$..."   # paste from the CLI above
```

Restart, and every request except `/healthz` is gated by Basic auth.
Basic auth without TLS sends the password in plaintext on every request —
**always pair it with HTTPS** (i.e. with a reverse proxy or a Tailscale tunnel).

**Do not expose Harmonist's raw port to the internet.** The combination of a
credential-holding tagger and destructive endpoints is not something you want
behind nothing but luck.
