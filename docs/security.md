# Security

Harmonist holds your Bandcamp login cookies and can rewrite tags across your
whole library. It's built for one person on a private network, and **must not
be exposed directly to the internet**.

## What's built in

- **Cross-site request protection.** Another website can't make your browser
  change anything in Harmonist.
- **Hostname checking**, once you set `allowed_hosts` (below).
- **An optional password**, for when you have nothing better (below).

## Put it behind a reverse proxy

To reach Harmonist from other machines, put it behind a reverse proxy with its
own hostname, HTTPS, and a login. Caddy, nginx, Traefik, Authelia, Authentik and
Tailscale Serve all work; use whatever you already run.

## Limit the hostnames it answers to

By default Harmonist answers to any hostname. Once it's reachable from other
machines, list the names you use to reach it. This blocks
[DNS rebinding](https://en.wikipedia.org/wiki/DNS_rebinding), where a malicious
website tricks your browser into talking to Harmonist.

```toml
[server]
allowed_hosts = ["harmonist.example.com", "nas.local"]
```

Or, in Docker, `HARMONIST_ALLOWED_HOSTS=harmonist.example.com,nas.local`.
`localhost` and `127.0.0.1` always work, so health checks keep running. Harmonist
logs a warning at startup while this is left open on a network-facing address.

## Built-in password

If you can't use a reverse proxy, Harmonist can ask for a username and password
itself. Generate a password hash:

```bash
docker exec -it harmonist python -m harmonist.web.security
```

Then add it to `harmonist.toml` and restart:

```toml
[auth]
enabled = true
username = "alice"
password_hash = "pbkdf2_sha256$600000$...$..."
```

Without HTTPS the password crosses the network in plain text, so pair this with
HTTPS, from a proxy or a Tailscale tunnel.
