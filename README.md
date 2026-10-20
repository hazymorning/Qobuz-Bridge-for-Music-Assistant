# MA Qobuz Bridge

A Home Assistant add-on that puts your Music Assistant players and groups in the device list of
the Qobuz app. Pick one and press play: Music Assistant plays the music and shows the track,
artwork and progress. Pause, skip, seek and volume work from both the Qobuz app and Music
Assistant. Based on [QobuzProxy](https://github.com/leolobato/qobuz-proxy).

You need Home Assistant OS (or Supervised), Music Assistant with Qobuz set up as a provider, and
a Qobuz subscription.

## Install (from your phone)

1. [Add the repository to Home Assistant](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhazymorning%2Fma-qobuz-bridge).
   If the link does not work for you, open **Settings → Add-ons** (called **Apps** in newer
   versions) **→ Add-on Store → ⋮ → Repositories** and add
   `https://github.com/hazymorning/ma-qobuz-bridge`.
2. Open **MA Qobuz Bridge**, tap **Install**, then **Start**. The add-on is built
   on your Home Assistant box, which takes a few minutes on a Raspberry Pi.
3. Open **MA Qobuz Bridge** in the sidebar. If it is not there, tap **Open Web UI** on the add-on page.
4. Tap **Log in to Qobuz**, sign in on the page that opens, then come back to the panel. Do this
   while your phone is on your home Wi-Fi.
5. Under **Music Assistant**, enter your Music Assistant username and password and tap
   **Connect**. No Music Assistant password (you only open it from the sidebar)? Tap **Use a
   token instead**, create a long-lived token in your Music Assistant user profile and paste it.
   The address is filled in for the Music Assistant add-on; change it if yours runs elsewhere.
6. Under **Speakers in the Qobuz app**, leave **All my Music Assistant players** on, or switch it
   off and tick the players you want. Tap **Rename** under a player to give its speaker your own
   name.

Now open the Qobuz app, tap the device picker and choose a speaker. Their names end in **(MA)**.

For other speakers to join one speaker while you play, add a party speaker under **4. Party
speakers** in the panel ([how it works](DOCS.md#party-speakers)).

## Update

When a new version is out, the add-on page in Home Assistant shows an **Update** button. Your
logins and choices are kept.

## Known limits

- The Qobuz login only works from your home network, not through Nabu Casa or other remote
  access.
- Your phone and Home Assistant must be on the same network, because the Qobuz app finds the
  speakers with mDNS.
- Music Assistant plays the music with its own Qobuz account. A track that account cannot play is
  skipped.
- The Qobuz app cannot group speakers. Make a group in Music Assistant or a party speaker in the
  panel; either shows up as one speaker.
- When someone plays something else on the player in Music Assistant, the add-on lets go and the
  Qobuz app shows the speaker as stopped.
- If another app plays directly on the device behind a player (for example a WiiM's own Qobuz
  Connect), the Qobuz app only notices when the track would have ended.

More: [DOCS.md](DOCS.md) (also the add-on's **Documentation** tab) and, for working on the code,
[CONTRIBUTING.md](CONTRIBUTING.md).
