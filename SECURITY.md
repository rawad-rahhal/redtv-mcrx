# Security Policy

RED TV MCRX controls broadcast playout and must be treated as operational infrastructure.

## Deployment boundary

- Do **not** expose the API Gateway, Live Ingest, Automation AI, or Program Renderer directly to the public internet.
- API Gateway mutating HTTP operations require `X-API-Key` matching the `REDTV_API_KEY` environment variable and fail closed when the server key is absent.
- Deploy on a trusted MCR/control network or behind an authenticated reverse proxy/VPN.
- Restrict ingress with host firewall/network ACL rules to approved operator and service hosts.
- Treat playlist paths, staged media paths, SRT inputs, graphics controls, and renderer restart controls as privileged operations.
- `/api/playlist/load_file` must remain confined to `paths.playlist_dir`; it accepts a filename, not an arbitrary host path.

## Reporting

Report security issues privately to the project owner. Do not include credentials, private media, internal network addresses, or customer data in public issues.

## Current scope

The API Gateway now provides a single shared-key authentication boundary for mutating HTTP operations. It is **not** full identity/RBAC: all holders of the key have the same control authority, WebSocket/read-only status remains unauthenticated, and the directly reachable Live Ingest / Program Renderer services do not yet have service identity. Production deployments must therefore retain the trusted-network or authenticated reverse-proxy/VPN boundary until RBAC and service-to-service authentication are implemented and tested.

Never commit `REDTV_API_KEY` to the repository, YAML, screenshots, logs, or issue reports. Rotate it if disclosure is suspected.
