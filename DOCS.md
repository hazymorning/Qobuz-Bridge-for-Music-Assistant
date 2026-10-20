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
- Your Music Assistant username and password, or a long-lived token from your Music Assistant
  user profile (if you only ever open Music Assistant from the Home Assistant sidebar, you may
  have no password: use a token).

## Setup

1. **Add the repository.** Tap
   [Add the repository to Home Assistant](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhazymorning%2Fma-qobuz-bridge).
   If the link does not work for you, open **Settings → Add-ons** (called **Apps** in newer
   versions) **→ Add-on Store → ⋮ → Repositories** and add
   `https://github.com/hazymorning/ma-qobuz-bridge`.
2. **Install and start.** The add-on is built on your box, which takes a few minutes on a
   Raspberry Pi. If the build fails, the reason is in **Settings → System → Logs → Supervisor**.
3. **Open the panel.** On its first start the add-on turns on **Show in sidebar** and
   **Watchdog** once. Open **MA Qobuz Bridge** in the sidebar, or **Open Web UI** on the add-on page.
4. **Log in to Qobuz.** Qobuz's login page cannot open inside Home Assistant, so the button opens
   the add-on's own address (`http://<home-assistant-ip>:8689`) in a new tab. Sign in there, then
   come back. Do it from your home network: it does not work through Nabu Casa or other remote
   access. The panel also shows that address as a link, in case no tab opens. If the login does
   not finish, the panel says so; try again.
5. **Connect to Music Assistant.** Enter your Music Assistant username and password and tap
   **Connect**. The add-on uses them once to get a long-lived token named "MA Qobuz Bridge" and
   keeps only the token. Or tap **Use a token instead** and paste a long-lived token you created
   in your Music Assistant user profile. The address is filled in for the Music Assistant add-on
   (`http://127.0.0.1:8095`); change it if yours runs elsewhere.
6. **Choose the players.** **All my Music Assistant players** is on by default. Switch it off to
   tick only the players and groups you want. **Rename** under a player gives its speaker your own
   name.
7. **Play.** In the Qobuz app, tap the device picker and choose a speaker. Names end in **(MA)**.

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
  Music Assistant). **Rename** gives a speaker your own name instead (up to 40 characters, used
  exactly as you type it); clear the name to get the default back.
- Each speaker keeps its identity across restarts and renames, so the Qobuz app remembers it. A
  new name (yours, or the player's in Music Assistant) applies once the speaker is not playing.
- Party speakers you add in the panel show up too, under their own names (see below).
- After a Music Assistant restart players can take a while to come back; their speakers are kept
  for 30 minutes.

## Party speakers

There are two ways to play on several speakers at once from the Qobuz app:

- **A group player in Music Assistant.** Make it in Music Assistant with all the speakers that
  should play together. It shows up as its own speaker (rename it to "Party" if you like) and
  Music Assistant runs the whole group. Pick this when Music Assistant can sync all the speakers
  and you always want the same ones.
- **A party speaker in the panel.** It plays on one main speaker, and the speakers you pick join
  that speaker's group right before the music starts and leave again when you say so. Music
  Assistant or Home Assistant does the grouping. Pick this when only the speakers' own multiroom
  can sync them, or when you want to decide what happens to speakers that are already playing
  and when the group splits.

To add a party speaker, tap **Add a party speaker** under **4. Party speakers**:

- **Name in the Qobuz app:** used exactly as you type it (up to 40 characters, no "(MA)" added).
- **Main speaker (plays the music):** the Music Assistant player the music plays on; the others
  join it. The party speaker shows up in the Qobuz app whether or not this player is ticked under
  **Speakers in the Qobuz app**.
- **Group with:** **Music Assistant** or **Home Assistant**:
  - **Music Assistant** syncs the speakers itself. Pick it when Music Assistant can sync them
    with the main speaker, usually players of the same kind in Music Assistant. Speakers it
    can't sync with the main speaker are greyed out ("can't sync with …").
  - **Home Assistant** asks Home Assistant to group them, which uses the speakers' own
    multiroom (Sonos, WiiM and the like). Pick it when Music Assistant can't sync them but their
    own app can. The form lists only Home Assistant media players that can be grouped, and they
    must be able to group with the main speaker (usually the same brand). This uses the add-on's
    access to Home Assistant, new in 0.4.0.
- **Main speaker in Home Assistant** (Home Assistant only): the main speaker as Home Assistant
  knows it, the one the others join. Pick the same speaker as the main speaker above; the form
  picks it for you when Home Assistant has a speaker with the same name.
- **Speakers that join:** the speakers that join the main speaker.
- **Speakers already playing something else:** **Bring them into the party** stops their music
  and groups them. **Leave them playing** leaves them out of this party.
- **When the music stops:** you press stop (in the Qobuz app or Music Assistant), the queue ends,
  Music Assistant can't be reached or someone plays something else on the main speaker in Music
  Assistant. **Split the group again**: once the music has been stopped for 5 seconds, the
  speakers the party brought in leave the group. **Keep the speakers together**: they stay
  grouped. A pause or the next track never splits the group.
- **When you switch to another device in the Qobuz app** (or the app disconnects): the party's
  music stops either way. **Stop and split the group**: the speakers leave the group at once.
  **Stop and keep the speakers together**: they stay grouped. This does not depend on **When the
  music stops**.

Good to know:

- Only the speakers the party brought in leave the group; speakers that were already grouped with
  the main speaker stay.
- If a speaker can't join, the music still starts on the main speaker and the party speaker's row
  in the panel says why (details in the **Log** tab).
- A change to a party speaker applies once its music stops. Deleting one takes it out of the
  Qobuz app at once; music that plays keeps playing in Music Assistant and the group stays.
- A group a party made before an add-on restart is not split by the add-on after the restart.
- When both options keep the speakers together, they are grouped once: if you split them
  yourself, the party groups them again only once it restarts (after an add-on restart or a
  change to the party speaker).
- Known limit: two party speakers with the same main speaker and some of the same speakers can get
  in each other's way when you switch straight from one to the other, and a shared speaker can
  drop out of the new party. Give them different speakers to join.

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

The add-on's access to Home Assistant is used only for party speakers grouped by Home Assistant:
it reads your media players and groups and ungroups the speakers you picked.

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
add-on with its own storage, so you log in again. Stop the main add-on first: both copies use port
8689 and the same speakers. A new commit without a version change is not offered as an update:
use **Check for updates** in the store, then **Rebuild** on the add-on page.
