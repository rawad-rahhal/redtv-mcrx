# RED TV MCRX v4.16.2 — Public Baseline Hardening Report

## Validation snapshot

- Test suite: **121 passed**
- Production-code coverage: **47.44%** (45% CI floor)
- Real FastAPI lifespan: **PASS** (startup, health, guard, shutdown)
- RTRingBuffer sustained-overflow integrity regression: **PASS**
- Playlist host-path / traversal / symlink-escape regressions: **PASS**
- Built wheel: **PASS**
- Installed-wheel runtime smoke: **PASS**
- Critical packaged assets: config YAML/schemas/presets + dashboard HTML included
- Secret-pattern scan: no obvious hard-coded API keys/passwords/tokens found
- Repository examples use generic `NAS-SERVER/CHANNEL` paths rather than a real NAS hostname/share layout

## v4.16.2 public-baseline fixes

1. **Fixed SPSC ring ownership violation.** `RTRingBuffer.push()` no longer advances the consumer-owned `_read` index or overwrites unread slots on overflow. A full ring drops the new event. The regression test drives sustained concurrent overflow and verifies exact accounting, uniqueness, and field coherence (`frame_number`, `drift_ms`, `detail`).
2. **Closed arbitrary host-file reads in `/api/playlist/load_file`.** The endpoint accepts only a simple filename resolved beneath configured `paths.playlist_dir`; absolute, drive-qualified, nested, traversal, and symlink-escape paths are rejected before any open.
3. **Stopped schema-error reflection from file parsing.** Invalid playlist files receive generic validation errors rather than raw Pydantic exception text that can echo file values.
4. Updated web/desktop playlist controls so a pasted path is reduced client-side to a filename; the server still enforces containment independently.
5. Genericized NAS/share examples from project-specific infrastructure to `NAS-SERVER/CHANNEL`.
6. Added an explicit all-rights-reserved source-review notice; no permissive open-source license is granted.
7. Reworded the README to describe MCRX as a software-defined playout/master-control foundation and explicitly disclose the current authentication and runtime-test limitations.
8. Removed `__pycache__`, `.pyc`, pytest/coverage/build artifacts from the public baseline export.

## v4.16.1 fixes retained

1. Restored `PlayoutEngine._preload_emergency_slate()` as a real method so production startup no longer raises `AttributeError`.
2. Fixed `/api/guard/snapshot` and `/api/guard/ack` to use `RealTimeGuard` properties correctly.
3. Fixed Broadcast Guard preflight so non-PROGRAM slots are not falsely rejected solely because they lack `media_path`.
4. Restricted Automation AI output filenames to the configured playlist directory.
5. Removed global Pydantic replacement from `test_dual_source.py`, eliminating cross-test module contamination.
6. Added production-path regression tests for FastAPI lifespan, guard endpoints, preflight behavior, output path resolution, and output-adapter lifecycle.
7. Fixed wheel packaging to include `live_ingest`, `program_renderer`, config assets, and operator dashboard HTML.
8. Made `PreviewWindowAdapter` lifecycle idempotent.
9. Replaced deprecated `datetime.utcnow()` timestamp construction with timezone-aware UTC.
10. Added GitHub Actions CI, a coverage floor, `.gitignore`, and `SECURITY.md` deployment guidance.

## Known remaining work

- Native API authentication/RBAC is not yet implemented. Deploy only on a trusted isolated MCR network or behind an authenticated reverse proxy/VPN.
- `program_renderer/service.py` and `live_ingest/service.py` still lack representative end-to-end media/network tests.
- `/api/dropin` should pass through the same deterministic preflight policy as normal playlist loading.
- Lifespan startup failure cleanup and explicit control-thread shutdown verification should be hardened.
- Import-time config/logging/application construction should be reduced.
- Preflight should add file-type/readability/non-zero-size checks and warn on unknown PROGRAM duration.
- CI should evolve to run static analysis and a built-wheel smoke test.
- Direct DeckLink/SDI output, end-to-end audio, as-run compliance logging, redundancy, and long-duration soak evidence remain product-roadmap items.
- Product identity was renamed from the internal **RED TV ASTRA** name to **RED TV MCRX** before the first public release to avoid confusion with Aveco's long-established ASTRA MCR product.

## Release rule

Do not describe this repository as fully production-validated solely because the automated suite passes. Hardware, FFmpeg/NDI, SRT, NAS/SMB, long-duration soak, failover and broadcaster-specific integration tests remain environment-dependent.
