# Security

Harmonist holds a real credential, the Bandcamp login cookies, and exposes
actions that change a whole library: syncing, tagging, forgetting albums,
erasing sidecars. It is designed for one person on a private network, usually
behind a reverse proxy that handles login.

The attacks worth defending against follow from that:

- **Accidental exposure.** Harmonist listens only on the local machine unless
  told otherwise. (The Docker image has to listen on all interfaces, which is why
  exposure through Docker is the case the user docs warn about.)
- **Cross-site requests** from a malicious page open in a browser that can also
  reach Harmonist. Every request that changes anything must carry the header
  HTMX sends. A browser can't add that header to a cross-origin request without
  a CORS preflight, which Harmonist doesn't answer. The request's origin is also
  checked, as a second layer.
- **DNS rebinding**, where a hostile hostname is pointed at Harmonist's address.
  Only listed hostnames are answered, once a list is set. The list is open by
  default so a fresh install works, and a startup warning says so when Harmonist
  is listening on the network.

The built-in password is **defence in depth for users without a proxy**, not a
replacement for one. It's off by default, and meaningless without HTTPS.

Multi-user access is out of scope: there is one library and one owner.
