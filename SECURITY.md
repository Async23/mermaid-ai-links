# Security Policy

## Supported versions

Security fixes are provided for the latest `0.1.x` release and the current
`main` branch.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use the
repository's **Security → Report a vulnerability** form so the report and any
proof of concept remain private.

Include the affected version, impact, reproduction steps, and any suggested
mitigation. You should receive an acknowledgement within seven days. A fix,
coordinated disclosure date, or status update will follow after triage.

## Security boundaries

`mermaid-ai-links` is a local automation bridge, not a hosted security
boundary:

- The HTTP bridge binds only to `127.0.0.1` and signed links are accepted only
  for local Markdown files. Do not expose the bridge or Chrome DevTools port to
  a network.
- Chrome DevTools Protocol grants powerful control over its browser profile.
  Use a dedicated Chrome profile and never reuse the remote-debugging port on
  an untrusted network.
- CLI and MCP processes inherit the invoking user's filesystem permissions.
  Configure the MCP adapter only in trusted AI hosts.
- A generated link contains an encoded absolute file path and a stable block
  identifier. The signature prevents tampering; it does **not** encrypt the
  path. Do not commit generated local links to a public repository.
- Mermaid source is written into the configured Mermaid Chart scratch diagram.
  Users are responsible for deciding whether that content may be sent to the
  service.
- Keep the real configuration and HMAC secret outside the repository with
  permission mode `0600`.

