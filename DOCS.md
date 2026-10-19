# Qobuz Bridge for Music Assistant

This add-on makes each of your Music Assistant players and groups show up as a Qobuz Connect
device in the official Qobuz app. When you pick one and press play, the add-on tells Music
Assistant to play that Qobuz track on that player. Music Assistant streams it with its own Qobuz
provider and shows the track, artwork, queue and progress as usual.

Control works both ways. Play, pause, next, previous, seek and volume in the Qobuz app go to Music
Assistant. Pause, resume, next, stop, seek and volume in Music Assistant show up in the Qobuz app.

## Before you start

- Home Assistant OS or Supervised. Other installation types have no add-ons.
- The Music Assistant add-on, or a Music Assistant server on your network. Tested with Music
  Assistant 2.10.7.
- Qobuz set up as a provider in Music Assistant (**Settings → Providers** in Music Assistant).
  Ideally use the same Qobuz account as in the Qobuz app; see [Limitations](#limitations).
- A Qobuz subscription and the Qobuz app on your phone.

## Setup

1. **Add the repository.** In Home Assistant open **Settings → Add-ons** (called **Apps** in newer
   versions) **→ Add-on Store → ⋮ (top right) → Repositories**. Paste
   `https://github.com/hazymorning/Qobuz-Bridge-for-Music-Assistant` and tap **Add**.
2. **Install and start.** Find **Qobuz Bridge for Music Assistant** in the store and tap
   **Install**. Home Assistant builds the add-on on your own box, which takes a few minutes on a
   Raspberry Pi. If the build fails, the reason is in **Settings → System → Logs → Supervisor**.
   Then tap **Start**.
3. **Open the panel.** On its first start the add-on turns on **Show in sidebar** and
   **Watchdog** for itself, once. Open **Qobuz Bridge** in the sidebar, or tap **Open Web UI** on
   the add-on page. If you switch either option off later, the add-on leaves it off.
4. **Log in to Qobuz.** Do this on your phone while it is on your home Wi-Fi. Qobuz's login page
   cannot open inside Home Assistant, so the button opens `http://<home-assistant-ip>:8689` in a
   new tab. Sign in there, then come back to the panel. This step does not work through Nabu Casa
   or any other remote access, because the new tab needs to reach your Home Assistant box
   directly. If the tab does not open, the panel shows the address as a link you can tap.
5. **Connect to Music Assistant.** The address is filled in for you (`http://127.0.0.1:8095` when
   Music Assistant runs as an add-on on the same box). Enter your Music Assistant username and
   password and tap **Connect**. See [Music Assistant login](#music-assistant-login) if you have no
   password.
6. **Choose the players.** **All my Music Assistant players** is on by default. Switch it off to
   tick only the players and groups you want in the Qobuz app.
7. **Play.** Open the Qobuz app, tap the device picker and choose a player or group. Their names
   end in **(MA)**.

## Status checks

The top of the panel shows four checks. Green means fine. Otherwise, the check says what is wrong:

| Check | Not green? Here is what to do |
|---|---|
| **Qobuz** | Not logged in yet: tap **Log in to Qobuz**, from your phone on your home Wi-Fi (step 4). If you were logged in before, the Qobuz login has expired: log in again. |
| **Music Assistant** | "Not connected yet": enter your login (step 5). "Connecting…": wait a few seconds. Login rejected: your password changed or the token was deleted or has expired; connect again. Cannot reach Music Assistant: check that the Music Assistant add-on is running and the address is right. The add-on keeps retrying on its own. |
| **Qobuz in Music Assistant** | Music Assistant has no working Qobuz provider. In Music Assistant open **Settings → Providers**, add Qobuz and log in. |
| **Speakers in the Qobuz app** | "No speakers are shared": switch on **All my Music Assistant players** or tick some players. "Speakers appear once Qobuz and Music Assistant are both connected": fix the checks above first. |

## Music Assistant login

The add-on needs its own access to Music Assistant. There are two ways:

- **Username and password** (the default). The add-on uses them once to get a long-lived token
  from Music Assistant, and then forgets the password. Only the token is stored.
- **Paste a token.** Tap **use a token instead** and paste a long-lived token that you created in
  Music Assistant.

If you only ever open Music Assistant from the Home Assistant sidebar, your Music Assistant user
probably has no password: Music Assistant creates that user automatically, and your Home Assistant
password does not work there. Either set a password for your user in Music Assistant, or create a
long-lived token there and paste it.

Music Assistant's long-lived tokens are valid for one year. When the token runs out, the **Music
Assistant** check turns red; connect again. **Disconnect** in the panel deletes the token from
the add-on. To revoke it in Music Assistant too, delete it in your user's token list there.

## Which players show up in the Qobuz app

- Players, stereo pairs and groups that are enabled in Music Assistant.
- With **All my Music Assistant players** on, players that are hidden in Music Assistant are left
  out. Switch the toggle off to pick any player by hand.
- Each one gets its Music Assistant name plus " (MA)". The suffix keeps them apart from speakers
  that have Qobuz Connect built in, such as a WiiM, which then shows up twice: the "(MA)" entry
  plays through Music Assistant. You can switch the suffix off in the panel.
- Each device keeps its identity across restarts, so the Qobuz app remembers it. Renaming a player
  in Music Assistant renames the device in the Qobuz app.
- After a Music Assistant restart, players can take a while to come back. The add-on keeps their
  devices for 30 minutes before it removes any that are gone.

## Multi-room

The Qobuz app cannot group devices itself. Create a group in Music Assistant (for example, a sync
group of the kitchen and living room speakers). The group shows up in the Qobuz app as one device,
and its volume slider controls the whole group.

## When someone else uses the player in Music Assistant

If someone starts something else on that player in Music Assistant (a radio station, a playlist,
another app), the add-on lets go: the Qobuz app shows the device as stopped. The add-on never
fights back and never touches that player again on its own. To take it over again, pick the
device again in the Qobuz app and press play.

## Music Assistant restarts or is unreachable

- When Music Assistant restarts (for example, after an update), it stops all of its players. The
  Qobuz app then shows the device as stopped. The add-on reconnects on its own; press play again.
- While Music Assistant cannot be reached, the devices stay in the Qobuz app, but playing fails
  until it is back. The **Music Assistant** status check shows the error.

## When the add-on restarts during playback

The music keeps playing in Music Assistant. The Qobuz app loses the device for a moment. Pick the
device again in the Qobuz app to take control again.

## Networking

- The add-on uses the host network, as the Music Assistant add-on does. The Qobuz app finds the
  devices with mDNS, so your phone and Home Assistant must be on the same network. With separate
  VLANs, mDNS must be forwarded between them.
- Ports on your Home Assistant box:

  | Port | Used for |
  |---|---|
  | 8689 | The web panel and the Qobuz login |
  | 8700 and up | One per device shown in the Qobuz app (Qobuz Connect discovery) |
  | 7120 and up | Only if you also add DLNA or local speakers under "Other speakers" |

  These ports must be free. Do not run another QobuzProxy (for example the "Qobuz Proxy" add-on)
  on the same box at the same time.
- The panel works from anywhere through Home Assistant, including Nabu Casa. Only the Qobuz login
  needs the direct port 8689 from your home network.
- Port 8689 also serves the panel on your home network without a Home Assistant login, because
  the Qobuz login needs it. Keep your Home Assistant box on a network you trust.

## Options

| Option | Values | Default | What it does |
|---|---|---|---|
| `log_level` | `debug`, `info`, `warning`, `error` | `info` | How much detail goes into the **Log** tab. `info` shows every Qobuz command and every call to Music Assistant. Use `debug` only while troubleshooting. |

Restart the add-on after you change an option.

## Updating

When a new version is out, Home Assistant shows an **Update** button. It checks the repository
every few hours, or right away with **Check for updates** in the store's **⋮** menu. Updating
rebuilds the add-on on your box. Your logins and settings are kept.

## Privacy

- Your Qobuz login token and the Music Assistant token are stored only in the add-on's own
  storage (`/data`). They are never written to the log or to the repository.
- They are included in Home Assistant backups of this add-on, so keep your backups safe.
- The add-on talks only to Qobuz, to your Music Assistant server and to Home Assistant itself.

## Troubleshooting

- **The device is not in the Qobuz app.** Check that your phone is on the same network as Home
  Assistant and that all four status checks are green. Close and reopen the Qobuz app. If you use
  VLANs or a guest Wi-Fi, mDNS must reach your phone.
- **The Qobuz login tab does not load.** Open `http://<home-assistant-ip>:8689` yourself, from a
  phone or computer on your home network. It does not work through Nabu Casa.
- **The Music Assistant login is rejected.** See [Music Assistant login](#music-assistant-login).
- **A track stops right away with an error.** Music Assistant could not play it with its Qobuz
  account; see [Limitations](#limitations). The **Log** tab names the track.
- **The add-on keeps restarting.** Another program may be using port 8689. Check the **Log** tab.
- **Logs:** on the add-on's **Log** tab. For more detail, set `log_level` to `debug` on the
  **Configuration** tab and restart the add-on. Please include the log when you report a problem
  at https://github.com/hazymorning/Qobuz-Bridge-for-Music-Assistant/issues.

## Limitations

- Music Assistant plays the music with its own Qobuz account. If that account cannot play a track
  that your Qobuz app account can (a different subscription or country, for example), the track
  stops with an error in the log.
- Playback starts a few seconds after you press play, because Music Assistant fetches and buffers
  the stream first.
- The audio quality is whatever Music Assistant's Qobuz provider and the player support. The
  quality shown in the Qobuz app may not match it.
- The Qobuz app cannot group devices; use Music Assistant groups (see [Multi-room](#multi-room)).

## Testing a branch

Add `#branch-name` to the end of the repository URL when you add the repository. That installs a
separate copy of the add-on with its own storage, so you log in to Qobuz and connect to Music
Assistant again. A new commit without a version change is not offered as an update: use
**Check for updates** in the store, then **Rebuild** on the add-on page.
