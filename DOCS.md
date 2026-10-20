# MA Qobuz Bridge

Each of your Music Assistant players and groups shows up as a speaker in the Qobuz app. When you
pick one and press play, the add-on asks Music Assistant to play that Qobuz track on that player.
Music Assistant streams it with its own Qobuz provider and shows the track, artwork, queue and
progress as usual.

Control works both ways. Play, pause, next, previous, seek and volume in the Qobuz app go to Music
Assistant. Pause, resume, next, seek and volume in Music Assistant show up in the Qobuz app; stop,
previous or clearing the queue in Music Assistant show the speaker as stopped.

## Before you start

- Home Assistant OS or Supervised (other installation types have no add-ons).
- Music Assistant (the add-on, or a server on your network) with Qobuz set up under
  **Settings → Providers**. Ideally use the same Qobuz account as in the Qobuz app.
- A Qobuz subscription and the Qobuz app.

## Setup

1. **Add the repository.** **Settings → Add-ons** (called **Apps** in newer versions)
   **→ Add-on Store → ⋮ → Repositories**, paste
   `https://github.com/hazymorning/ma-qobuz-bridge` and tap **Add**.
2. **Install and start.** The add-on is built on your box, which takes a few minutes on a
   Raspberry Pi. If the build fails, the reason is in **Settings → System → Logs → Supervisor**.
3. **Open the panel.** On its first start the add-on turns on **Show in sidebar** and
   **Watchdog** once. Open **MA Qobuz Bridge** in the sidebar, or **Open Web UI** on the add-on page.
4. **Log in to Qobuz.** Qobuz's login page cannot open inside Home Assistant, so the button opens
   the add-on's own address (`http://<home-assistant-ip>:8689`) in a new tab. Sign in there, then
   come back. Do it from your home network: it does not work through Nabu Casa or other remote
   access. If no tab opens, the panel shows the address as a link.
5. **Connect to Music Assistant.** The address is filled in (`http://127.0.0.1:8095` when Music
   Assistant runs as an add-on on the same box). Enter your Music Assistant username and password
   and tap **Connect**. The add-on uses them once to get a long-lived token named "MA Qobuz Bridge"
   and keeps only the token.
6. **Choose the players.** **All my Music Assistant players** is on by default. Switch it off to
   tick only the players and groups you want.
7. **Play.** In the Qobuz app, tap the device picker and choose a player. Names end in **(MA)**.

If you only ever open Music Assistant from the Home Assistant sidebar, your Music Assistant user
may have no password yet. Set one in Music Assistant (your user profile) first.

## Status checks

The top of the panel says what is wrong and what to do:

| Check | When it is not green |
|---|---|
| **Qobuz account** | Log in to Qobuz (step 4). If you were logged in before, the login expired: log in again. |
| **Music Assistant** | "Connecting…": wait a few seconds. "Can't reach Music Assistant": check that it runs and the address is right; the add-on keeps retrying. "No longer accepts the saved login": the token was removed or expired; connect again. |
| **Qobuz in Music Assistant** | Shown once connected. Add Qobuz under **Settings → Providers** in Music Assistant. |
| **Speakers in the Qobuz app** | Turn on **All my Music Assistant players** or tick some players, and fix the checks above. |

## Which players show up

- Players, stereo pairs and groups that are enabled in Music Assistant. With **All my Music
  Assistant players** on, players hidden in Music Assistant are left out.
- Each one is named after the Music Assistant player plus " (MA)", which keeps it apart from
  speakers with Qobuz Connect built in (a WiiM then shows up twice; the "(MA)" one plays through
  Music Assistant).
- Each speaker keeps its identity across restarts, so the Qobuz app remembers it. Renaming a
  player in Music Assistant renames the speaker once it is not playing.
- After a Music Assistant restart players can take a while to come back; their speakers are kept
  for 30 minutes.

## While you play

- **Someone else plays something on the player in Music Assistant:** the add-on lets go, the
  Qobuz app shows the speaker as stopped, and the add-on sends Music Assistant nothing more until
  you pick the speaker again in the Qobuz app.
- **Music Assistant restarts or cannot be reached:** the Qobuz app shows the speaker as stopped
  (after 20 seconds if unreachable). The add-on reconnects on its own; press play again.
- **The add-on restarts:** the music keeps playing in Music Assistant and the speakers come back
  without logging in again. Pick the speaker again and press play; the add-on continues what Music
  Assistant is playing instead of restarting the track.
- **Repeat and autoplay:** when you play from the Qobuz app, the add-on turns off repeat and
  autoplay for that player in Music Assistant, so the album ends where the Qobuz queue ends.

## Networking

The add-on uses the host network, like the Music Assistant add-on. Your phone must be on the same
network as Home Assistant (with VLANs, mDNS must be forwarded). It uses these ports:

| Port | Used for |
|---|---|
| 8689 | The panel and the Qobuz login |
| 8700 and up | One per speaker in the Qobuz app (Qobuz Connect discovery) |

Port 8689 serves the panel on your home network without a Home Assistant login, because the Qobuz
login needs it. Keep Home Assistant on a network you trust.

## Options

`log_level` (`debug`, `info`, `warning`, `error`; default `info`): how much goes into the **Log**
tab. `info` shows every Qobuz app command and every call to Music Assistant. Restart the add-on
after changing it.

## Privacy

Your Qobuz login and the Music Assistant token are stored only in the add-on's storage (`/data`),
never in the log. They are part of Home Assistant backups of the add-on.

## Troubleshooting

- **The speaker is not in the Qobuz app:** check the status checks and that your phone is on the
  same network; then close and reopen the Qobuz app.
- **A track is skipped:** Qobuz or Music Assistant's Qobuz account cannot play it in your region;
  the **Log** tab names the track.
- **Logs:** the add-on's **Log** tab. Set `log_level` to `debug` for more detail and include the
  log when you report a problem at
  https://github.com/hazymorning/ma-qobuz-bridge/issues.

## Testing a branch

Add `#branch-name` to the repository URL when you add it. That installs a separate copy of the
add-on with its own storage, so you log in again. A new commit without a version change is not
offered as an update: use **Check for updates** in the store, then **Rebuild** on the add-on page.
