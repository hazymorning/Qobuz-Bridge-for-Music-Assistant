# Changelog

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
- Sidebar panel: Qobuz login, Music Assistant login (username and password, or a long-lived
  token), player selection and plain-language status checks.
- Turns on "Show in sidebar" and the watchdog on its first start.
- Built locally on your Home Assistant box (amd64 and aarch64), from QobuzProxy 1.7.7.
