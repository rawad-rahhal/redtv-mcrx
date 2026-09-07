# Security Policy

RED TV MCRX controls broadcast playout and must be treated as operational infrastructure.

## Deployment boundary

- Do **not** expose the API Gateway, Live Ingest, Automation AI, or Program Renderer directly to the public internet.
- Deploy on a trusted MCR/control network or behind an authenticated reverse proxy/VPN.
- Restrict ingress with host firewall/network ACL rules to approved operator and service hosts.
- Treat playlist paths, staged media paths, SRT inputs, graphics controls, and renderer restart controls as privileged operations.
- `/api/playlist/load_file` must remain confined to `paths.playlist_dir`; it accepts a filename, not an arbitrary host path.

## Reporting

Report security issues privately to the project owner. Do not include credentials, private media, internal network addresses, or customer data in public issues.

## Current scope

The built-in APIs do not yet provide a full identity/RBAC layer. Production deployments must supply that boundary externally until native authentication is implemented and tested.
