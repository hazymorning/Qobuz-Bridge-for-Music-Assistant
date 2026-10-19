# Music Assistant backend: how it works

For people working on the code. Users: see the [README](../README.md) and [DOCS.md](../DOCS.md).

## The idea

Every exposed Music Assistant (MA) player or group gets its own upstream `Speaker`: its own mDNS
record, discovery port and Qobuz websocket, exactly like a DLNA speaker. Only the backend differs:
`MusicAssistantBackend` asks MA to play `qobuz://track/<id>` on the player's MA queue (queue id ==
player id), and MA streams it with its own Qobuz provider.

```
Qobuz app ──Qobuz cloud──▶ WsManager ─▶ QobuzPlayer ─▶ MusicAssistantBackend ──┐
   ▲                                      (upstream state machine)            │ one shared
   └──────────── state reports ◀──────────────┘                                ▼ websocket
                                                   MusicAssistantConnection ◀─▶ Music Assistant
```

The QobuzPlayer still looks up a streaming URL before every track. The backend ignores the URL,
but the lookup is the region and availability check: an unavailable track is skipped before MA
is asked.

## Code

| File | Role |
|---|---|
| `backends/music_assistant/connection.py` | One websocket to MA for all players. Builds a new `MusicAssistantClient` per attempt (the client is single-use), reconnects with capped backoff, stops when MA rejects the token, maps errors, times out every call, hands events to listeners by player id. Also the username/password → long-lived token login. |
| `backends/music_assistant/backend.py` | `MusicAssistantBackend`: one MA player or group. |
| `backends/music_assistant/speakers.py` | `MusicAssistantSpeakers`: owns the connection, decides which players to expose, starts and stops their speakers, stable UUIDs (`uuid5(namespace, "music_assistant:<player_id>")`) and ports (8700+, remembered). |
| `backends/music_assistant/settings.py` | `music_assistant.json` in the data dir (mode 0600): address, token, exposure choices, known players and their ports, and the MA queue items each backend created. |
| `webui/music_assistant_routes.py`, `webui/static/panel.*` | The panel and its API. |
| `rootfs/start.py` | Add-on entry point: applies the options, turns on the sidebar panel once, runs the app. |

Hooks in upstream files are kept small so `git merge upstream/main` stays easy: `Speaker(...,
backend=)`, a few `backend_type == "music_assistant"` checks in `speaker.py`, `TrackUnavailableError`,
`reports_external_resume`, `on_volume_change` and the `ExternalPlaybackAware` protocol in
`backends/base.py`, `start_position_ms` in `BackendTrackMetadata`, and the external-resume and
unplayable-track hooks in `player.py`. Expect merge conflicts only in those and in `README.md`,
`index.html` and `routes.py` (keep ours).

The MA client needs Python 3.13. Everything that imports it is imported lazily, so the rest of
QobuzProxy still runs on 3.10.

## Ownership rules

Keep these when changing the backend:

- MA queue items the backend created are "ours" (kept in the settings file across restarts).
- Only while *attached* (the Qobuz app plays through us) may MA events make the QobuzPlayer advance
  (`track_ended`, `next_track_started`). After a stop, an interruption, an end of queue or an
  add-on restart the backend is detached: it follows MA but never advances. That prevents
  fight-back and double plays.
- MA "idle" must last `IDLE_SETTLE_SECONDS`, and an idle right after an MA play action is a stop,
  never a track end.
- Anything else on the queue while attached is external playback: the backend releases the Qobuz
  session (`ExternalPlaybackAware`) and sends MA nothing until `prepare_for_selection()`.

## Qobuz app → MA

| Qobuz app | Music Assistant |
|---|---|
| play | `player_queues/play_media` `replace` (a start mid-track: `clear` + `add` + `play_index(seek_position)`) |
| next track known | `play_media` `replace_next` once MA really plays the current track (gapless) |
| next (already queued) | `play_index` on the queued item |
| pause / resume / stop / seek | `pause` / `play` / `stop` / `seek` (stop only while MA still plays our item) |
| volume | `players/cmd/group_volume` (a plain volume set for a single player) |
| next track changed | `delete_item`; an item the player already buffered is marked stale and stepped past |
| first play after a selection | repeat and autoplay turned off on that queue |

## MA → Qobuz app

| Music Assistant | Qobuz app |
|---|---|
| moves onto our queued next item | next track |
| our item idle near its end, no MA play action just before | track ended → next track or end of queue |
| idle mid-track, stop/next/seek in MA, queue cleared, "previous" to an older item | stopped at that position (session kept) |
| paused / playing again / seek / volume | the same in the app |
| anything else on the queue, or another source on the player | external playback: session released |
| unreachable for 20 s while playing | stopped |

## Tests

- `tests/backends/fake_music_assistant.py`: an in-memory MA 2.10.7 server driving the real
  `music-assistant-client` (only the websocket is fake). Time only moves with `advance_time`, and
  `settle()` never sleeps, so tests patch the backend's timing constants.
- `tests/integration/test_music_assistant_scenarios.py`: the full stack (real `Speaker`,
  `WsManager`, command handlers, QobuzPlayer) for each user-facing behaviour.
- `tests/backends/test_music_assistant_*.py`, `tests/test_music_assistant_speakers.py`,
  `tests/webui/test_music_assistant_routes.py`: units and the panel API.

## Add-on packaging

The repository root is the add-on: `config.json` (JSON so it does not clash with QobuzProxy's
`config.yaml`), `Dockerfile.amd64`/`Dockerfile.aarch64` (the Supervisor prefers them over the
upstream `Dockerfile`; keep them identical), `rootfs/`, `repository.yaml`, `translations/` and
`DOCS.md`. Never add another `config.{yaml,yml,json}` or a `build.*` file to the tree. Bump
`version` in `config.json` so Home Assistant offers the update.
