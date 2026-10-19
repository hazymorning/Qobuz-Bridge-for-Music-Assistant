# Music Assistant backend: how it works

This page is for people working on the code. For installing and using the
add-on, see the [README](../README.md) and [DOCS.md](../DOCS.md).

## The idea

Every exposed Music Assistant (MA) player or group becomes its own Qobuz
Connect device: one `Speaker` with its own mDNS record, discovery port and
Qobuz websocket, exactly like a DLNA speaker. The difference is the backend.
Instead of sending audio to a renderer, `MusicAssistantBackend` asks MA to play
`qobuz://track/<id>` on the player's MA queue (queue id == player id). MA
streams the track with its own Qobuz provider, so MA shows the track, artwork,
queue and progress like anything else it plays.

```
Qobuz app ──Qobuz cloud──▶ WsManager ─▶ QobuzPlayer ─▶ MusicAssistantBackend ──┐
   ▲                                       ▲   (unchanged state machine)        │ MA websocket
   └──────────── state reports ────────────┘                                     ▼ (one shared)
                                                     MusicAssistantConnection ◀─▶ Music Assistant
                                                                                  │ Qobuz provider
                                                                                  ▼
                                                                            speaker / group
```

The Player still resolves a Qobuz streaming URL before every track. The MA
backend ignores the URL, but the lookup stays useful: it is the region and
availability check, and an unavailable track is skipped before MA is asked.

## Components

| File | Role |
|---|---|
| `connection.py` | One websocket to MA for all players. Builds a new `MusicAssistantClient` per attempt (the client is single-use), reconnects with backoff, stops retrying when MA rejects the token, maps errors, times out MA calls, fans events out by player id. Also the username/password → long-lived token login. |
| `backend.py` | `MusicAssistantBackend(AudioBackend, ExternalPlaybackAware)` for one player or group. |
| `manager.py` | `MusicAssistantManager`: owns the connection, decides which players to expose, starts/stops their Speakers, assigns stable UUIDs (`uuid5(namespace, "music_assistant:<player_id>")`) and ports (8700+, remembered). |
| `settings.py` | `music_assistant.json` (address, token, exposure choices, known players; mode 0600) and `music_assistant_sessions.json` (which MA queue items are ours, for add-on restarts). |
| `supervisor.py` | Finds MA when running as a Home Assistant add-on. |
| `webui/ma_routes.py`, `webui/static/ma.js` | Panel API and UI. |

## How Qobuz commands map to MA

| Qobuz app / Player | Music Assistant |
|---|---|
| play track | `player_queues/play_media` `option=replace` (a start mid-track uses `clear` + `add` + `play_index(seek_position)`, one stream start) |
| next track known (gapless) | `play_media` `option=replace_next` right after MA really plays the current track; MA hands it to the speaker itself |
| next (already queued) | `player_queues/play_index` on the queued item |
| pause / resume / stop / seek | `player_queues/pause` / `play` / `stop` / `seek` (stop only while MA still plays our item) |
| volume | `players/cmd/group_volume` (a plain volume set for single speakers) |
| next track changed | `player_queues/delete_item`; an item the speaker already buffered can't be deleted, so it is marked stale and stepped past if MA reaches it |
| first play after selection | repeat and autoplay switched off on that MA queue |

## How MA events map to the Qobuz app

The backend is **attached** while the Qobuz app plays through it. Only then do
MA events drive the Player:

| MA | Qobuz app |
|---|---|
| moves onto our queued next item | next track (gapless or "next" pressed in MA) |
| our item idle for 3 s near its end, no MA play action just before | track ended → the Player advances or stops at the end of the queue |
| our item idle mid-track, or right after a stop/next/seek in MA, or the queue cleared, or "previous" to an older track | stopped at that position (session kept, no auto-advance) |
| paused / playing again | paused / playing (the Player follows a resume made on the device) |
| seek in MA | position jump |
| volume changed in MA | volume shown in the app |
| anything else on the queue, or the speaker switched to another source | someone else took over: the Qobuz session is released, no further MA command until the device is selected again in the Qobuz app |
| MA unreachable for 20 s while playing | stopped |

After a stop, an interruption, an end of queue or an add-on restart the backend
is **detached**: it keeps following MA (so it knows which item is ours) but
never makes the Player advance. That is what prevents fight-back and double
plays.

## Add-on restart mid-play

MA keeps playing. The sessions file tells the new backend which MA item is
ours. When the user picks the device again, the Qobuz app sends the track it
remembers; the backend adopts what MA is playing instead of restarting it, and
skips the stale seek back to the position the app had before the restart.

## Known limits

- MA describes a speaker from its own queue. If another app plays on the speaker
  directly (a WiiM's built-in Qobuz Connect, BubbleUPnP on a DLNA speaker), MA
  does not report it. The backend notices only when MA's progress runs far past
  the end of the track. Switching from the "(MA)" device to the WiiM's own
  Qobuz Connect device in the Qobuz app works because the app first tells the
  bridge to stop, before the WiiM starts.
- Availability is checked with the add-on's Qobuz login; MA plays with its own.
  If MA cannot play a track the app account can, the track is skipped.
- Turning off repeat/autoplay changes those settings on that MA queue.

## Tests

- `tests/backends/fake_music_assistant.py`: an in-memory MA 2.10.7 server that
  drives the real `music-assistant-client` (only the websocket is swapped).
  `test_ma_fake_contract.py` checks it against the real client's API.
- `tests/backends/test_ma_*.py`: connection, settings, backend commands and
  event mapping.
- `tests/integration/test_ma_scenarios.py`: the full stack (real Player,
  WsManager, command handlers) for each requirement scenario.
- `tests/test_ma_manager.py`, `tests/webui/test_ma_*.py`: exposure, ports,
  UUIDs, the panel API and the static UI.
