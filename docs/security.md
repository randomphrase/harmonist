# Security

Harmonist holds your Bandcamp login cookies and can rewrite tags across your
whole library. It's built for one person on a private network, and is **not
safe to expose directly to the internet**.

## What's built in

- **Cross-site request protection.** Another website can't make your browser
  change anything in Harmonist.
- **Hostname checking**, once `allowed_hosts` is set (below).
- **An optional password**, for setups with nothing better (below).

## Behind a reverse proxy

The recommended way to reach Harmonist from other machines is through a reverse
proxy with its own hostname, HTTPS, and a login. Caddy, nginx, Traefik,
Authelia, Authentik and Tailscale Serve all work, so whichever one is already
running is usually the easiest choice.

## Limiting the hostnames it answers to

By default Harmonist answers to any hostname. Once it's reachable from other
machines, `allowed_hosts` should list the names used to reach it. This blocks
[DNS rebinding](https://en.wikipedia.org/wiki/DNS_rebinding), where a malicious
website tricks your browser into talking to Harmonist.

```toml
[server]
allowed_hosts = ["harmonist.example.com", "nas.local"]
```

In Docker, the same list can go in `HARMONIST_ALLOWED_HOSTS=harmonist.example.com,nas.local`.
`localhost` and `127.0.0.1` always work, so health checks keep running. Harmonist
logs a warning at startup while this is left open on a network-facing address.

## Built-in password

Without a reverse proxy, Harmonist can ask for a username and password itself.
The password is stored as a hash, which this command generates:

```bash
docker exec -it harmonist python -m harmonist.web.security
```

The hash then goes in `harmonist.toml`, and takes effect after a restart:

```toml
[auth]
enabled = true
username = "alice"
password_hash = "pbkdf2_sha256$600000$...$..."
```

Without HTTPS the password crosses the network in plain text, so this is only
safe alongside HTTPS, from a proxy or a Tailscale tunnel.
