# Qobuz Bridge for Music Assistant

A Home Assistant add-on that puts your Music Assistant players and groups in the device list of
the Qobuz app. Pick one and press play: Music Assistant plays the music and shows the track,
artwork and progress. Pause, skip, seek and volume work from both the Qobuz app and Music
Assistant. Based on [QobuzProxy](https://github.com/leolobato/qobuz-proxy).

You need Home Assistant OS (or Supervised), Music Assistant with Qobuz set up as a provider, and
a Qobuz subscription.

## Install (from your phone)

1. In Home Assistant open **Settings → Add-ons** (called **Apps** in newer versions) **→ Add-on
   Store → ⋮ → Repositories**, paste
   `https://github.com/hazymorning/Qobuz-Bridge-for-Music-Assistant` and tap **Add**.
2. Open **Qobuz Bridge for Music Assistant**, tap **Install**, then **Start**. The add-on is built
   on your Home Assistant box, which takes a few minutes on a Raspberry Pi.
3. Open **Qobuz Bridge** in the sidebar. If it is not there, tap **Open Web UI** on the add-on page.
4. Tap **Log in to Qobuz**, sign in on the page that opens, then come back to the panel. Do this
   while your phone is on your home Wi-Fi.
5. Under **Music Assistant**, enter your Music Assistant username and password and tap
   **Connect**. The address is already filled in.
6. Under **Speakers in the Qobuz app**, leave **All my Music Assistant players** on, or switch it
   off and tick the players you want.

Now open the Qobuz app, tap the device picker and choose a player. Their names end in **(MA)**.

## Update

When a new version is out, the add-on page in Home Assistant shows an **Update** button. Your
logins and choices are kept.

## Known limits

- The Qobuz login only works from your home network, not through Nabu Casa or other remote
  access.
- Your phone and Home Assistant must be on the same network, because the Qobuz app finds the
  speakers with mDNS.
- If you only ever open Music Assistant from the Home Assistant sidebar, set a password for your
  user in Music Assistant first.
- Music Assistant plays the music with its own Qobuz account. A track that account cannot play is
  skipped.
- The Qobuz app cannot group speakers. Make a group in Music Assistant; it shows up as one speaker.
- When someone plays something else on the player in Music Assistant, the add-on lets go and the
  Qobuz app shows the speaker as stopped.
- If another app plays directly on a speaker (for example a WiiM's own Qobuz Connect), the Qobuz
  app only notices when the track would have ended.
