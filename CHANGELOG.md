# Changelog

## 0.2.0

- New, shorter name: MA Qobuz Bridge.
- The Music Assistant login takes a username and password only. The pasted-token option is gone;
  if you have no Music Assistant password, set one in your Music Assistant user profile.
- Speakers always end in " (MA)"; the switch to leave it off is gone.
- The panel no longer manages DLNA or local-audio speakers, and no longer guesses the Music
  Assistant address (it starts at `http://127.0.0.1:8095`).
- The panel refuses requests from other websites and can't be framed by them.
- The add-on runs as an unprivileged user and keeps its files in `/data` readable only by itself.
- A speaker the next start would reject is refused in the web UI instead of stopping the add-on.
- A Music Assistant connection that keeps dropping right away now waits longer between retries.
- Upgraded aiohttp, multidict and PyJWT past known vulnerabilities.
- After this update, music that was already playing is not picked up again by the Qobuz app;
  start it once from the Qobuz app.

## 0.1.0

First release.

- Each Music Assistant player, stereo pair and group shows up as a Qobuz Connect device in the
  Qobuz app, named with " (MA)" at the end.
- Playback runs through Music Assistant, which shows the track, artwork, queue and progress.
  The next Qobuz track is queued in Music Assistant ahead of time for gapless playback.
- Control works both ways: Qobuz app commands go to Music Assistant, and pause, resume, next,
  stop, seek and volume in Music Assistant show up in the Qobuz app.
- When someone plays something else on the player in Music Assistant, the add-on lets go and the
  Qobuz app shows the device as stopped.
- Reconnects on its own when Music Assistant restarts or was unreachable. After an add-on
  restart the music keeps playing and the devices come back without logging in again.
- A track that Qobuz or Music Assistant cannot play in your region is skipped.
- Sidebar panel: Qobuz login, Music Assistant login (username and password, or a long-lived
  token), player selection and plain-language status checks.
- Turns on "Show in sidebar" and the watchdog on its first start.
- Built locally on your Home Assistant box (amd64 and aarch64), from QobuzProxy 1.7.7.
