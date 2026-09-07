# RED TV MCRX — Master Control v4.16.2

Deterministic software-defined broadcast playout and master-control foundation for IP-first MCR environments, with an explicit real-time execution guard, fail-safe state machine and playlist preflight.

> **Naming note:** Formerly developed internally as **RED TV ASTRA**. Renamed to **MCRX** at the first public release to avoid confusion with Aveco's long-established ASTRA MCR product. Version numbering continues from the internal development history rather than resetting.

> **Validation scope:** deterministic core logic, API lifespan, guard/preflight behavior and packaging are covered by automated tests. The `program_renderer` and `live_ingest` runtime services do not yet have representative end-to-end media/network coverage. Native API authentication is not yet implemented; deploy only on an isolated trusted VLAN or behind an authenticated reverse proxy/VPN.

---

## What's New in v4.16.2

- ✅ Fixed SPSC telemetry ring overflow race; producer now drops new events without touching consumer state
- ✅ Restricted playlist file loading to configured `playlist_dir` and blocked host-path/traversal/symlink escapes
- ✅ Genericized infrastructure examples and added explicit source-review rights notice
- ✅ Preserved v4.16.1 startup, preflight, guard API, packaging and lifecycle hardening
- ✅ 121 automated tests passing with a 45% CI coverage floor (47.44% measured in this validation)

## Key Features

| Feature | Description |
|---------|-------------|
| **MultiView** | 4-pane operator dashboard (Program, Live Preview, Next Item, Graphics/Guard) |
| **Graphics CG** | Full Bug + Lower Third control plane with API + UI |
| **Source Slots** | Named live inputs (cam_1, cam_2, whatsapp_1) with connect/disconnect/on-air |
| **Live Preview** | Real JPEG preview from FFmpeg via live_ingest, shown in Pane B |
| **Config Validator** | Startup check for missing keys, unsafe settings, UNC sanity |
| **Structured Logging** | JSON logs across all services |
| **Full .bat scripts** | `run_all.bat`, `run_api.bat`, `run_live_ingest.bat`, `run_operator_ui.bat` |

All v3.x hardening preserved: RT guard, FSM, atomic writes, test suite.

---

## Security & deployment status

This repository is a product foundation, not a turnkey third-party broadcast appliance. Before deployment outside a trusted MCR network, add authentication/RBAC and an operator audit trail. Mutating control endpoints must not be exposed directly to untrusted networks.

`/api/playlist/load_file` accepts only a filename resolved inside `paths.playlist_dir`; caller-supplied host filesystem paths are rejected.

See `SECURITY.md` and `HARDENING_REPORT.md` for the current threat model and known work.

---

## Quick Start

### Windows
```bat
scripts\run_all.bat
```
Opens 4 service windows. Dashboard at http://localhost:8000/dashboard

### Linux / Mac
```bash
bash scripts/run_all.sh
```

### Manual (one by one)
```bat
REM Terminal 1 — Live Ingest (must start first)
python -m live_ingest.main

REM Terminal 2 — API Gateway + Playout Core
python -m uvicorn api_gateway.app:app --host 0.0.0.0 --port 8000

REM Terminal 3 — Automation AI
python -m automation_ai.main

REM Terminal 4 — Desktop Client (optional)
python -m operator_ui.desktop_client --api http://localhost:8000
```

---

## Requirements

```bash
# Runtime dependencies
pip install -e .

# Development + tests
pip install -e ".[dev]"
```

FFmpeg must be on PATH for live preview to work:
- Download: https://ffmpeg.org/download.html
- Windows: add ffmpeg\bin to System PATH

---

## MultiView — How to Use

Open http://localhost:8000/dashboard and click the **MultiView** tab.

| Pane | Content |
|------|---------|
| **A — Program** | Automation state: current clip, frames, drift, violations |
| **B — Live Preview** | JPEG from live_ingest (~2fps). Updates when a live source is connected and ready |
| **C — Next Item** | Next scheduled clip + health counters |
| **D — Graphics/Guard** | Current bug state, lower third preview, guard violation count |

**Pane B live preview** activates automatically when:
1. A slot is connected via Live Sources tab (or SRT Connect on Control tab)
2. The live source reaches READY state
3. FFmpeg is writing preview JPEGs to the temp folder

---

## How to TAKE a Lower Third

### Via Web Dashboard
1. Click **Graphics / CG** tab
2. Type **Headline** (required) and **Subline** (optional)
3. Choose **Style**: Standard / Breaking News / Sport / Minimal
4. Set **Duration** in seconds
5. Click **◉ TAKE**
6. Preview appears immediately in Pane D (MultiView) and the CG panel
7. Click **✗ CLEAR** to take it off air early

### Via API
```bash
curl -X POST http://localhost:8000/api/graphics/l3/set \
  -H "Content-Type: application/json" \
  -d @samples/take_lower_third.json
```

### Sample payload (`samples/take_lower_third.json`)
```json
{
  "headline": "Breaking News",
  "subline": "Our correspondent reports from the field",
  "style": "breaking",
  "duration_s": 8.0,
  "animate_in": "left",
  "animate_out": "left"
}
```

---

## Channel Bug

### Via Web Dashboard
1. Click **Graphics / CG** tab
2. Enter optional **Bug Text** (e.g. "LIVE")
3. Choose **Position** and **Opacity**
4. Click **✓ ENABLE BUG**

### Via API
```bash
curl -X POST http://localhost:8000/api/graphics/bug/set \
  -H "Content-Type: application/json" \
  -d @samples/set_bug.json
```

---

## Live Sources — Source Slots

### Connect a Camera Slot
1. Click **Live Sources** tab
2. Select slot name (cam_1, cam_2, whatsapp_1)
3. Enter SRT URL or file URL
4. Click **📡 CONNECT SLOT**
5. Wait for status → CONNECTED
6. Click **▶ ON AIR** to switch PROGRAM to this slot

### Via API
```bash
# Connect slot
curl -X POST http://localhost:8000/api/live/slots/connect \
  -H "Content-Type: application/json" \
  -d @samples/connect_slot.json

# Put on air
curl -X POST http://localhost:8000/api/live/slots/cam_1/on_air

# Disconnect
curl -X POST http://localhost:8000/api/live/slots/cam_1/disconnect
```

### Stage a WhatsApp File
```bash
curl -X POST http://localhost:8000/api/live/file \
  -H "Content-Type: application/json" \
  -d '{"path": "\\\\NAS-SERVER\\CHANNEL\\live\\clip.mp4"}'
```

---

## Graphics API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/graphics/bug/set` | Enable channel bug |
| POST | `/api/graphics/bug/clear` | Disable channel bug |
| POST | `/api/graphics/l3/set` | TAKE lower third |
| POST | `/api/graphics/l3/clear` | CLEAR lower third |
| GET | `/api/graphics/state` | Full graphics state |

---

## RT Safety Guarantee

The playout_core RT loop has **zero I/O, zero locks, zero blocking calls**.

- `ProgramRouter.get_program_frame()` → GIL-atomic int read + `deque.popleft()`
- All network, subprocess, and file work lives in `live_ingest` (port 8020) or control threads
- `RealTimeGuard` patches `open`, `subprocess`, `logging` in the RT thread — violations are logged and surfaced in the dashboard

---

## Architecture

```
operator_ui (web/desktop)
      │  WebSocket + REST
api_gateway (port 8000)
      │                    │
playout_core          live_ingest (port 8020)
  ├ engine                  └ FFmpeg preview writer
  ├ program_router          └ SRT probe + JPEG loop
  ├ state_machine
  └ decoder (DoubleBuffer)

automation_ai (port 8001)  ←→  api_gateway
media_factory              ←→  NAS / shared storage
```

---

## Tests

```bash
python -m pytest tests/ -v
```

Run the suite to get the authoritative current count; CI must pass before release.

---

## Config (`config/redtv.yaml`)

Key v4 additions:
```yaml
live:
  srt_ingest_service_url: "http://127.0.0.1:8020"
  refuse_switch_if_not_ready: true   # NEVER change to false in production

logging:
  level: INFO
  log_dir: logs
```

---

## Current Limitations

- SRT in `playout_core` still uses a synthetic-frame adapter; real SRT preview/decode lives in `live_ingest`.
- Direct DeckLink/SDI output remains a stub. The separate Program Renderer supports UDP MPEG-TS and optional NDI when the installed FFmpeg build provides NDI support.
- End-to-end audio passthrough is not yet implemented.
- Native API identity/RBAC is not yet implemented; production deployments must use a trusted MCR network or authenticated reverse proxy/VPN (see `SECURITY.md`).

---

## Changelog

### v4.16.2 — public baseline hardening
- Fixed the SPSC telemetry ring overflow race; producer drops new events instead of advancing consumer state
- Added sustained-overflow regression coverage for duplicate/torn-event detection
- Restricted `/api/playlist/load_file` to simple filenames inside configured `playlist_dir`
- Rejected absolute, drive-qualified, nested, traversal, and symlink-escape playlist inputs
- Removed raw schema-validation reflection from playlist-file responses
- Genericized NAS/share examples for public source review
- Added an explicit all-rights-reserved source-review notice
- Reframed validation claims to disclose current auth and renderer/ingest test gaps
- 121 tests passing; 47.44% measured production-code coverage

### v4.16.1 — hardening
- Fixed production lifespan startup by restoring `_preload_emergency_slate()` as an engine method
- Fixed `/api/guard/snapshot` and `/api/guard/ack` property usage
- Fixed Broadcast Guard false rejection of BREAK/LIVE/FILLER slots without a media path
- Restricted Nashra playlist output to the configured playlist directory
- Removed global Pydantic test-module contamination
- Included `live_ingest`, `program_renderer`, config assets, and dashboard HTML in built wheels
- Made preview-adapter lifecycle idempotent and removed duplicate startup ownership
- Replaced deprecated naive UTC timestamp creation
- Added real FastAPI lifespan, guard endpoint, path-containment, preflight, and lifecycle regression tests
- Added CI, a coverage floor, `.gitignore`, and deployment security guidance

### v4.6.0
- MultiView 4-pane dashboard
- Graphics/CG control plane (BugGraphic, LowerThird, GraphicsState)
- `/api/graphics/*` endpoints (bug/set, bug/clear, l3/set, l3/clear, state)
- Source Slots (`/api/live/slots/*`)
- Live preview JPEG proxy (`/api/live/preview/{name}.jpg`)
- Config validator with startup warnings
- Structured JSON logging
- `run_all.bat`, `run_api.bat`, `run_operator_ui.bat` scripts
- `/api/status` extended with graphics + live_slots
- `samples/` directory with example payloads
- 25 new tests

### v3.5
- live_ingest: real FFmpeg JPEG preview writer
- `/preview.jpg` and `/preview.mjpeg` endpoints on port 8020
- `run_live_ingest.bat` script

### v3.3
- Dual-source program bus (ProgramRouter)
- LiveSourceAdapter (file_live + srt_live stub)
- 10 dual-source tests
- `/api/live/*` endpoints

### v3.2
- 8 critical RT hardening fixes
- 51 tests passing


## Program Renderer (v4.5)

- Starts an FFmpeg-based program pipeline that overlays BUG + L3 and produces:
  - UDP MPEG-TS output (default) (config: `program_renderer.output_udp`)
  - **NDI output** (optional) if your FFmpeg build includes the `libndi_newtek` muxer
    (config: `program_renderer.output_mode: ndi`, `program_renderer.ndi_name`)
  - Program preview JPEG proxied via api_gateway: `GET /api/renderer/preview/program.jpg`

NDI note:
- Many Windows FFmpeg builds do **not** ship with NDI enabled. If `output_mode: ndi` is set
  but the muxer is missing, the renderer will **log a warning and fall back to UDP**.

Run:

```bat
scripts\run_program_renderer.bat
```

## Broadcast Guard (Preflight)

Before any playlist is accepted, the API runs a preflight that checks media existence and basic sanity.
- If guard fails, `/api/playlist/load` returns HTTP 422 with `guard_report`.
- Last report: `GET /api/guard/report`
