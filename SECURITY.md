# Security policy

The latest published release is supported. Report vulnerabilities through
GitHub's private vulnerability reporting when available; do not disclose a
working exploit or private backup in a public issue. If private reporting is
unavailable, open an issue requesting a private contact without sensitive details.

## Deployment model

This is a trusted-LAN service without authentication. Every person who can reach
the UI can access the library and backups and request changes. Bind to localhost
or a trusted LAN address. Do not expose it directly through WAN port forwarding.
Use an authenticated reverse proxy if remote access is required.

The container runs without root, capabilities or a Docker socket. Host access is
limited to explicitly mounted data directories. Mutations use same-origin checks
and a per-process CSRF token. Uploaded content is never executed. Archive imports
are verified and restore data only; they do not install service code.

Metadata requests use a fixed HTTPS provider allowlist, checked redirects,
timeouts and response-size limits. ROM checksums are computed locally. Game-title
searches and artwork requests reveal those titles to the respective providers.

Backups are unencrypted and can contain private ROMs, BIOS and saves. Protect the
data directories and exported copies. Never include `.env`, backups, provider
caches or real deployment credentials in issues or pull requests.
