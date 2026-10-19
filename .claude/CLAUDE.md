# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

QobuzProxy is a headless Qobuz music player that appears as a Qobuz Connect device, controllable from the official Qobuz app. It supports two audio backends: DLNA renderers (Sonos, Denon HEOS, etc.) and local audio output via PortAudio.

## Commands

This project uses **[uv](https://docs.astral.sh/uv/)** to manage the local environment (`.venv` + `uv.lock`). Do not use `pip`/`venv` directly.

```bash
# Setup
uv sync                                     # Create .venv, install runtime deps + dev group (default), write uv.lock
uv sync --all-extras                        # Also install the local audio backend extra (sounddevice, numpy, soundfile)

# Run (uv run executes inside .venv — no manual activation needed)
uv run python -m qobuz_proxy
uv run qobuz-proxy --config config.yaml    # Then visit http://localhost:8689 to log in
uv run qobuz-proxy --discover              # Find DLNA renderers

# Test
uv run pytest                              # All tests
uv run pytest tests/connect/test_protocol.py      # Single file
uv run pytest tests/connect/test_protocol.py::TestProtocol::test_method  # Single test

# Code quality
uv run ruff format qobuz_proxy/ tests/     # Format (100 char line length)
uv run ruff check qobuz_proxy/ tests/      # Lint
uv run mypy qobuz_proxy/                    # Type check (strict)
```

Run Python via `uv run` (or `uv run python`), never bare `python`/`python3` and never `pip install`. Add/remove dependencies with `uv add` / `uv remove` so `pyproject.toml` and `uv.lock` stay in sync.

## Protocol Buffer Compilation

Must be run before first use. Re-run if `.proto` files change:

```bash
protoc --python_out=qobuz_proxy/proto -I protos protos/*.proto

# Fix relative imports in generated files (macOS uses sed -i '', Linux uses sed -i)
sed -i'' -e 's/^import qconnect_common_pb2/from . import qconnect_common_pb2/g' qobuz_proxy/proto/qconnect_payload_pb2.py qobuz_proxy/proto/qconnect_queue_pb2.py
sed -i'' -e 's/^import qconnect_queue_pb2/from . import qconnect_queue_pb2/g' qobuz_proxy/proto/qconnect_payload_pb2.py
```

Proto files in `protos/`: `qconnect_common.proto`, `qconnect_envelope.proto`, `qconnect_payload.proto`, `qconnect_queue.proto`, `ws.proto`

## Architecture

### Component Wiring (app.py)

`QobuzProxy` in `app.py` is the main orchestrator. It wires together:

1. **Auth** (`auth/`): OAuth login via Qobuz desktop app credentials (`oauth.py`), MD5-signed API requests (`api_client.py`), token persistence (`credentials.py`), session/JWT tokens (`tokens.py`). See `docs/authentication.md` for details.
2. **Connect** (`connect/`): Registers as mDNS device + HTTP discovery endpoints (`discovery.py`), manages WebSocket connection to Qobuz servers (`ws_manager.py`), encodes/decodes protobuf messages (`protocol.py`)
3. **Playback** (`playback/`): State machine player (`player.py`), queue management (`queue.py`), track metadata from Qobuz API (`metadata.py`), command handlers (`command_handler.py`, `queue_handler.py`, `volume_handler.py`), periodic state reporting to Qobuz app (`state_reporter.py`)
4. **Backend** (`backends/`): Abstract `AudioBackend` interface (`base.py`), factory/registry pattern (`factory.py`). Two implementations:
   - **DLNA** (`dlna/`): SOAP/UPnP client (`client.py`), device capability detection (`capabilities.py`), audio proxy server (`proxy_server.py`)
   - **Local** (`local/`): Downloads FLAC, decodes to float32, plays via PortAudio (`backend.py`), ring buffer for streaming (`ring_buffer.py`), sounddevice output stream (`stream.py`). Optional deps: `sounddevice`, `numpy`, `soundfile`

### Key Data Flows

**Qobuz app command → audio playback**: WebSocket message → `protocol.py` decodes → `ws_manager.py` dispatches to handler → `command_handler.py`/`queue_handler.py` → `player.py` state machine → `DLNABackend` → DLNA SOAP commands to device

**Audio streaming (DLNA)**: Qobuz CDN → `proxy_server.py` (aiohttp server on port 7120) → DLNA device. The proxy is needed because DLNA devices fetch audio via HTTP GET, and Qobuz streaming URLs require specific headers.

**Audio streaming (Local)**: Qobuz CDN → aiohttp download → soundfile FLAC decode → float32 samples → `RingBuffer` → `AudioOutputStream` (PortAudio callback) → speakers

**State reporting**: `state_reporter.py` periodically builds state (playback state, position, queue) → `protocol.py` encodes to protobuf → WebSocket → Qobuz servers → Qobuz app UI

### Quality Auto-Detection

When `max_quality: auto`: DLNA `GetProtocolInfo` → `capabilities.py` parses Sink string → maps to Qobuz quality (27=Hi-Res 192k, 7=Hi-Res 96k, 6=CD, 5=MP3). Falls back to CD quality (6) on failure.

## Configuration Priority

1. CLI arguments (highest) → 2. Environment variables → 3. YAML config file → 4. Code defaults

Key env vars: `QOBUZ_AUTH_TOKEN`, `QOBUZ_USER_ID`, `QOBUZ_MAX_QUALITY`, `QOBUZPROXY_BACKEND`, `QOBUZPROXY_DEVICE_NAME`, `QOBUZPROXY_DLNA_IP`, `QOBUZPROXY_AUDIO_DEVICE`, `QOBUZPROXY_LOG_LEVEL`

## Code Style

- **Ruff** for both formatting (`ruff format`) and linting, 100 char line length, **mypy** strict
- Type hints required on all public functions, Google-style docstrings only for non-obvious APIs
- All I/O is async. No blocking calls in main event loop
- Never log passwords or auth tokens

## Testing

- `asyncio_mode = "auto"` in pyproject.toml — no `@pytest.mark.asyncio` decorators needed
- Tests mirror source structure in `tests/`

## Commit Convention

`feat(module):`, `fix(module):`, `refactor(module):`, `test(module):`, `docs:`

## Reference Materials

- **Protocol reference**: [StreamCore32](https://github.com/tobiasguyer/StreamCore32) (C++ ESP32 Qobuz Connect implementation)

## Debugging

### Reference Implementation

For Qobuz Connect protocol issues, the key files in [StreamCore32](https://github.com/tobiasguyer/StreamCore32) are:
- `stream/qobuz/src/QobuzPlayer.cpp` — Position tracking, state management
- `stream/qobuz/src/QobuzStream.cpp` — WebSocket message handling

### Position Tracking Data Flow (DLNA)

```
DLNA Device (GetPositionInfo SOAP → RelTime string)
  → DLNAClient.get_position_info() (parses to ms)
  → DLNABackend.get_position() (updates _position_ms, notifies callback)
  → Player._on_position_update() (sets _position_value_ms + _position_timestamp_ms)
  → StateReporter._build_state_report() (reads player._position_value_ms)
  → Protocol.encode_state_update() (encodes Position{timestamp, value})
  → WebSocket → Qobuz app
```

The protocol uses `Position { timestamp: fixed64, value: uint32 }`. The app interpolates: `value + (now - timestamp)`.

### Common Issues

1. **Position always 0**: DLNA device may not support `GetPositionInfo` for the current source
2. **State not updating**: `_playback_monitor_loop` only polls when `_state == PlaybackState.PLAYING`
3. **Protocol encoding**: Log values passed to `encode_state_update()` — binary issues are invisible otherwise

### Reproducing issues before fixing

Reproduce a reported bug live before you change code. Then confirm the fix with the same reproduction. Many reports have no logs, and the plausible cause is often the wrong one. For example, #33 looked like a PAUSED SET_STATE, but the cause was a heartbeat race. After the live repro, add a unit test that fails without the fix.

**Setup on a Mac (Claude can drive the macOS Qobuz app):**

- **Proxy**: run it on the host with a scratch config at `logging.level: debug`. Keep a stable speaker `name`/`uuid` so the app keeps its device entry across restarts. Grep the log for `SET_STATE received`: the full decoded message is logged there.
- **mDNS**: the macOS app does not see the host proxy's python-zeroconf announcement. Announce it again with `dns-sd -R <name> _qobuz-connect._tcp local <http_port> path=/streamcore type=SPEAKER sdk_version=py-1.0.0 "Name=<name>" device_uuid=<uuid>`.
- **Real renderer (upmpdcli + mpd)**: use `alpine:3.22` (upmpdcli 1.9.x, the version most users run) and `apk add upmpdcli mpd mpc`, with mpd on a `null` audio output (Debian has no upmpdcli). Run it with `-p 49152:49152` in OrbStack. Find the description URL with an SSDP M-SEARCH from inside the container. Check what the renderer really does with `docker exec <c> mpc status`. OrbStack sometimes drops host→container routes for a few seconds; a burst of "No route to host" SOAP errors is that, not the proxy.
- **Fake renderer**: `contrib/fake_renderer.py --on-fetch-error stop|stay-playing|transitioning` fetches the stream on Play and copies a renderer's reaction to a failed fetch. KEF-like renderers stay in PLAYING/TRANSITIONING with no audio.
- **Other controllers**: to simulate one (Lyrion, BubbleUPnP), send SOAP `Stop` / `SetAVTransportURI` / `Play` straight to the renderer's AVTransport control URL. For fault injection (CDN failures, slow loads, no gapless), add temporary env-gated hooks and delete them before you commit. Run `grep -rn REPRO`.
- **Device selection**: the picker also lists the user's real speakers, and its rows move when a device appears or disappears. Before you press play, screenshot the picker, and then confirm the proxy log shows `Received connection` or `Renderer set active: True`. The app can still be pointed at a real speaker from an earlier session. If the test device is missing, change the test speaker's name and UUID (the app caches its list per UUID) or ask the user to relaunch the app.
- **Driving the app**: take screenshots with `screencapture -R<window bounds>`, and click with a small Swift `CGEvent` binary (cliclick isn't installed). In the output picker the first click often only hovers, so click twice. After a proxy restart the app falls back to local output and may start playing on the Mac's speakers. Pause before you stop the proxy.

**Timing and renderer behavior learned from live repros:**

- **Heartbeat race**: `StateReporter` sends a report every 5 s and pauses while LOADING. The server answers a report with a SET_STATE, often one that carries only `nextQueueItem`. If that answer crosses an app command, it lands in the middle of the command. To hit that window on purpose, click about 0.6 s before a predicted heartbeat (the app→server→proxy path took about 0.6 s here).
- **Poll interval**: the DLNA backend polls transport state every 2 s. The player's monitor loop also reads the position every 0.5 s while PLAYING.
- **Position after Stop**: renderers report `RelTime 0:00:00` right after a Stop. Don't treat the latest position as the stop position.
- **upmpdcli versions differ**: 1.9.x clears `CurrentURI` on Stop and 1.8.x keeps it (#35 reproduced only on 1.9). Match the reporter's renderer version before you conclude that a bug does not reproduce.
- **upmpdcli on a failed fetch**: it goes STOPPED, or mpd moves to the armed gapless next track. So upmpdcli hides failures that stall other renderers.

### Known Issues

**Qobuz app shows wrong quality** (FR-DLNA-08): App always displays "Hi-Res 96k" regardless of actual streaming quality. Audio streams correctly at auto-detected quality. Investigation needed: check if a protocol message reports quality capability, review StreamCore32 for quality reporting fields.

## Docker

Uses `network_mode: host` (required for mDNS). Ports: 8689 (HTTP discovery), 7120 (audio proxy). See `docker-compose.yaml` and `.env.example`.
