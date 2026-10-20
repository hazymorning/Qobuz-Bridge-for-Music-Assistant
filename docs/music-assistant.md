# Music Assistant backend: how it works

For people working on the code. Users: see the [README](../README.md) and [DOCS.md](../DOCS.md).
Checks, tests, merging upstream and releases: [CONTRIBUTING.md](../CONTRIBUTING.md).

## The idea

Every exposed Music Assistant (MA) player or group, and every party speaker, gets its own
upstream `Speaker`: its own mDNS record, discovery port and Qobuz websocket, exactly like a DLNA
speaker. Only the backend differs:
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
| `qobuz_proxy/backends/music_assistant/connection.py` | One websocket to MA for all players. Builds a new `MusicAssistantClient` per attempt (the client is single-use), reconnects with capped backoff, stops when MA rejects the token, maps errors, times out every call, hands events to listeners by player id, remembers which backend continued a queue item last. Also the logins: username/password → a long-lived token of our own, or a check of a token made in MA. |
| `qobuz_proxy/backends/music_assistant/backend.py` | `MusicAssistantBackend`: one MA player or group. |
| `qobuz_proxy/backends/music_assistant/party.py` | `Party` (gathers and releases a party speaker's members) and `PartyBackend` (the backend hook). See [Party speakers](#party-speakers). |
| `qobuz_proxy/backends/music_assistant/home_assistant.py` | `HomeAssistant`: a small client for Home Assistant's media players, for parties grouped there. |
| `qobuz_proxy/backends/music_assistant/speakers.py` | `MusicAssistantSpeakers`: owns the connection, decides which players to expose, starts and stops their speakers and the party speakers, stable UUIDs (`uuid5(namespace, "music_assistant:<player_id>")`, `"music_assistant:party:<party_id>"` for a party speaker) and ports (8700+, remembered). Adds, changes and removes party speakers for the panel. |
| `qobuz_proxy/backends/music_assistant/settings.py` | `music_assistant.json` in the data dir (mode 0600): address, token, username, exposure choices, known players and their ports, the speakers' own names, the party speakers, and the MA queue items each backend created. |
| `qobuz_proxy/webui/music_assistant_routes.py`, `qobuz_proxy/webui/static/index.html`, `qobuz_proxy/webui/static/panel.js`, `qobuz_proxy/webui/static/panel.css` | The panel and its API. |
| `rootfs/start.py` | Add-on entry point: applies the options, turns on the sidebar panel and watchdog once, makes `/data` private, drops to an unprivileged user, runs the app. |

The MA client needs Python 3.13. Everything that imports it is imported lazily, so the rest of
QobuzProxy still runs on 3.10.

## Hooks in upstream files

Kept small so `git merge upstream/main` stays easy; `tests/test_upstream_changes.py` fails if a
merge loses one. How to resolve conflicts: [CONTRIBUTING.md](../CONTRIBUTING.md#merging-upstream).

| File | Hooks |
|---|---|
| `qobuz_proxy/app.py` | Builds, starts and stops `MusicAssistantSpeakers`; calls `set_qobuz_logged_in` on Qobuz login and logout; `_make_music_assistant_speaker`; `web_app["music_assistant"]` for the panel; `_check_speaker` (the web UI refuses a speaker the next start would reject). |
| `qobuz_proxy/speaker.py` | `Speaker(..., backend=)`; the `backend_type == "music_assistant"` checks (no play reports, no track reload on a quality change); volume dispatch (`_dispatch_volume`: a volume broadcast reaches only the active speaker) and device-volume reporting (`_on_device_volume_change`); `ExternalPlaybackAware` instead of the DLNA-only checks; playback dispatch (`_dispatch_playback`: after the stop for `SrvrRndrSetActive(active=false)` it calls the backend's `app_left()`, if it has one, unless the speaker was picked again). |
| `qobuz_proxy/playback/player.py` | Follows a resume made on the device (`_follow_external_resume`); skips a track the backend cannot play (`TrackUnavailableError`). |
| `qobuz_proxy/backends/base.py` | `TrackUnavailableError`, `reports_external_resume`, `on_volume_change` and the `ExternalPlaybackAware` protocol. |
| `qobuz_proxy/backends/types.py` | `start_position_ms` in `BackendTrackMetadata`. |
| `qobuz_proxy/webui/routes.py` | One `register_music_assistant_routes` call (and its import). |
| `pyproject.toml` | The `music-assistant` extra, the extra dev dependencies, and the mypy and pytest settings. |

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
- A party speaker shares its main player's queue with that player's own speaker (and with other
  party speakers on it). When one backend continues an item another one played (`_adopt`, e.g.
  the app moves from the party speaker to the main player's speaker), it records that with
  `continue_item`; the other one's late stop, pause or seek then leaves the item alone.

## Qobuz app → MA

| Qobuz app | Music Assistant |
|---|---|
| play | `player_queues/play_media` `replace` (a start mid-track: `clear` + `add` + `play_index(seek_position)`) |
| play of the track MA already plays or has paused (e.g. after an add-on restart) | continued where it is (`play` if paused), not started again |
| next track known | `play_media` `replace_next` once MA really plays the current track (gapless) |
| next (already queued) | `play_index` on the queued item |
| pause / resume / stop / seek | `pause` / `play` / `stop` / `seek` (stop only while MA still plays our item) |
| resume, and the player stays paused (a WiiM) | `seek` to where it paused, which starts MA's stream again; from then on every resume on that speaker does this right away |
| seek while paused | nothing yet (MA would start playing to seek); resume then does a `seek` to that spot |
| volume | `players/cmd/group_volume` (MA applies it as a plain volume set on a single player) |
| next track changed | `delete_item`; an item the player already buffered is marked stale and stepped past |
| first play after a selection | repeat and autoplay turned off on that queue |

## MA → Qobuz app

| Music Assistant | Qobuz app |
|---|---|
| moves onto our queued next item | next track |
| our item idle near its end, no MA play action just before | track ended → next track or end of queue |
| stop in MA, idle mid-track or right after a next/seek in MA, queue cleared, "previous" to an older item | stopped at that position (session kept) |
| the player never starts our track, or reports it playing far past its end | stopped |
| paused / playing again / seek / volume | the same in the app |
| anything else on the queue, or another source on the player | external playback: session released |
| unreachable for 20 s while playing | stopped |

MA events reach `_on_event`, which runs `_check_queue` and, for our current item,
`_check_our_item`. What the QobuzPlayer must act on is pushed through the `AudioBackend`
callbacks: track ends (`_report_ended`), next tracks (`_promote_armed`), stops
(`_report_interrupted`), seeks (`_update_position`) and volume (`_report_device_volume`). A pause
or resume in MA only changes what `get_state()` returns: the app sees it when the QobuzPlayer
polls `get_state()` every 0.5 s (`qobuz_proxy/playback/player.py`). Each poll also runs
`_check_queue`, so a decision still waiting when MA goes quiet is made anyway. `pause()`,
`resume()`, `seek()` and `stop()` are the other direction.

## Party speakers

A party speaker (`PartySpeaker` in the settings, by an 8-hex-digit party id) is a speaker of its
own whose music plays on a main player's MA queue while other speakers join that player. Its
speaker key is `party:<party_id>` (UUID, session and port as for players). It is wanted while its
main player is known, whatever the exposure choices. A change restarts it once it is not playing
(`_party_changed`, like a rename); a removed one stops at once, music and group left as they are
(apart from a release that was waiting, see `close()`).

The hook is in the backend. `PartyBackend` is a `MusicAssistantBackend` on the main player, so
everything above holds unchanged; it only adds:

| `PartyBackend` | `Party` |
|---|---|
| `play()` | `gather()` first (skipped while MA is unreachable or external playback holds the player), then the normal play. A grouping problem never fails the play; one that does not answer delays it (10 s per call at most). |
| `_notify_state_change(STOPPED)` | `on_state()`: with `after_party="split"`, a release after `PARTY_RELEASE_DELAY_SECONDS` (5 s). A play in between cancels it, as a track change is a stop and a play; a pause never starts one. STOPPED covers a stop in the app or in MA, the end of the queue, MA unreachable and external playback; a play that fails calls it too. |
| `app_left()` (the `speaker.py` hook) | `leave()`: cancels a waiting release; with `on_leave="split"`, a release at once. |
| `disconnect()` | `close()`: a release that was waiting happens now. |

`Party` gathers and releases under one lock. A gather groups the members that are not with the
main player yet (none while the main player is unavailable; with `busy_speakers="skip"`, not
those playing something else) and records exactly the ones it added; a release removes only
those. What fails is logged and kept as `problem`, which the panel shows; the next gather
replaces it. The state is in memory only: after an add-on restart nothing gathered before is
released.

| `grouping` | Gather | Release | Members |
|---|---|---|---|
| `music_assistant` | `players/cmd/set_members` with `player_ids_to_add` on the main player | the same with `player_ids_to_remove` | MA player ids; must be in the main player's `can_group_with` when MA reports it. With the main = in its `group_members` or `synced_to` it. |
| `home_assistant` | `media_player.join` (`entity_id` = `main_entity_id`, `group_members`) | `media_player.unjoin` per member, all at once | Home Assistant `media_player.*` ids. With the main = in the main entity's `group_members`. |

Home Assistant grouping uses the speakers' own multiroom; MA still plays only to the main
player. `HomeAssistant` reaches Home Assistant Core through the Supervisor
(`http://supervisor/core/api`, `Bearer $SUPERVISOR_TOKEN`, allowed by `"homeassistant_api": true`
in `config.json`): `GET /states` for the media players (`can_group` from `supported_features`),
`POST /services/media_player/join` and `/unjoin`. Every call times out after 10 s, every failure
becomes a `HomeAssistantError` with a plain message, and the token is never logged. Without a
token (outside Home Assistant) it is unavailable and the panel says so.

Known limit: two party speakers on the same main player with shared members can race when the
app switches straight from one to the other. A release of the first can remove a member the
second's gather found already grouped and so did not record.

## Tests

How the tests are built and what each file covers: [CONTRIBUTING.md](../CONTRIBUTING.md#tests).

## Add-on packaging

The repository root is the add-on: `config.json` (JSON so it does not clash with QobuzProxy's
`config.yaml`), `Dockerfile.amd64`/`Dockerfile.aarch64` (the Supervisor prefers them over the
upstream `Dockerfile`; keep them identical), `rootfs/`, `repository.yaml`, `translations/`,
`icon.png`, `logo.png` and `DOCS.md`. Never add another `config.{yaml,yml,json}` or a `build.*`
file to the tree.
