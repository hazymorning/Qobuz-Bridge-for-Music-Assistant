# Spotify source takeover hotfix (v1.7.2)

On September 7, 2026 at 20:09:31 Europe/Berlin, Kitchen refreshed its Qobuz
WebSocket token, rejoined as active, and applied a cached seek to 4000 ms while
the user was listening to Spotify. The same seek occurred on earlier hourly
refreshes. This matched the reported song restart.

The DLNA backend now checks the actual source before transport and queue
commands. Sonos uses `GetPositionInfo.TrackURI`; other DLNA renderers use
`GetMediaInfo.CurrentURI`. Both the current Qobuz URL and its armed gapless
successor count as owned audio. Unknown/empty URI responses block transport
commands and session snapshots without permanently releasing ownership.

A confirmed foreign source releases Qobuz session ownership, invalidates pending
playback commands, ends the Qobuz listening report, and discards local gapless
state without editing the foreign queue. Source checks also protect shutdown
from stopping another controller's audio.

The September 14 recovery fix also closes the old cloud connection and clears
the discovery session after a takeover. Previously, the renderer stayed
connected but silently ignored every command until another HTTP handshake;
the app could keep selecting this unusable cloud renderer without sending that
handshake. Closing it makes the app discover and connect to the speaker again.
Token refresh alone cannot rejoin the released session or affect the other
source. A new selection restarts the connection manager, waits for old socket
cleanup, and restores playback and volume using the existing device UUID.

`tests/integration/test_external_playback.py` exercises the real player, command
handler, WebSocket protocol and speaker handoff with a simulated DLNA client.
It covers stopped/playing/paused reconnect snapshots, explicit reselection,
gapless ownership, unknown URI recovery, external stops, delayed loads and
renderer loading grace periods. These tests do not constitute live verification
of the released build on a physical speaker.

Recovery regression tests additionally use a real local WebSocket and HTTP
discovery handshake to verify cloud disconnection, session clearing, same-token
reselection, and playback/volume recovery. A delayed socket-close test covers
reselection before teardown has finished.

Validation: the full suite passed (713 tests), followed by all 17 takeover
regression cases after adding the in-flight URI-read guard. A direct comparison
against the v1.7.1 backend reproduced one seek sent to Spotify; the patched
backend sent none. Ruff passes. Mypy retains the previously documented 70 errors
in 13 files.
