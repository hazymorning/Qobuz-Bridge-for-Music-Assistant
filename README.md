# Qobuz Bridge for Music Assistant

A Home Assistant add-on that makes each of your Music Assistant players and groups show up as a
**Qobuz Connect** device in the official Qobuz app. Pick one in the app's device list and press
play: the music plays through Music Assistant, so Music Assistant shows the track, artwork, queue
and progress as usual. Control works both ways: play, pause, skip, seek and volume from the Qobuz
app, or from Music Assistant, and the other side follows.

You need Home Assistant OS (or Supervised), Music Assistant (the add-on, or a server on your
network) with Qobuz set up as a provider, and a Qobuz subscription.

## Install (all from your phone)

1. In Home Assistant open **Settings → Add-ons** (called **Apps** in newer versions) **→ Add-on
   Store → ⋮ (top right) → Repositories**, paste
   `https://github.com/hazymorning/Qobuz-Bridge-for-Music-Assistant` and tap **Add**.
2. Find **Qobuz Bridge for Music Assistant** in the store and tap **Install**. It is built on your
   Home Assistant box, which takes a few minutes on a Raspberry Pi. Then tap **Start**.
3. Open **Qobuz Bridge** in the sidebar. The add-on turns that sidebar entry on by itself the first
   time it starts; if you don't see it, tap **Open Web UI** on the add-on page. In the panel:
   - **Log in to Qobuz.** Do this on your phone while it is on your home Wi-Fi. The login opens
     `http://<home-assistant-ip>:8689` in a new tab, so it does not work through Nabu Casa or
     other remote access. Come back to the panel afterwards.
   - **Connect to Music Assistant.** The address is filled in for you. Enter your Music Assistant
     username and password.
   - Leave **All my Music Assistant players** on, or switch it off and tick the players you want.

Now open the Qobuz app, tap the device picker and choose a player or group. Their names end in
**(MA)**.

## What the status checks mean

The top of the panel shows four checks. Each one says what to do when it is not green.

- **Qobuz**: the add-on is logged in to your Qobuz account.
- **Music Assistant**: the add-on is connected to Music Assistant.
- **Qobuz in Music Assistant**: Music Assistant has a working Qobuz provider. It plays the music,
  so it needs one.
- **Speakers in the Qobuz app**: how many of your players and groups the Qobuz app can see.

## Good to know

- **Multi-room:** the Qobuz app cannot group devices. Make a group in Music Assistant instead; it
  shows up in the Qobuz app as one device.
- **Speakers with built-in Qobuz Connect** (a WiiM, for example) show up twice in the Qobuz app.
  The one ending in "(MA)" plays through Music Assistant.
- **Your logins are kept** in the add-on's own storage (`/data`). They survive restarts, updates
  and Home Assistant backups.
- **Testing a branch:** add `#branch-name` to the end of the repository URL. It installs as a
  separate add-on with its own logins.

## Troubleshooting

- **The device is not in the Qobuz app.** Your phone and Home Assistant must be on the same
  network, because the Qobuz app finds devices with mDNS. Then check that all four status checks
  in the panel are green.
- **The Music Assistant login fails.** If you only ever open Music Assistant from the Home
  Assistant sidebar, your Music Assistant user may not have a password. Set one for your user in
  Music Assistant, or create a long-lived token there and choose "use a token instead" in the
  panel.
- **Logs:** they are on the add-on's **Log** tab. For more detail, set `log_level` to `debug` on
  the **Configuration** tab and restart the add-on.

The **Documentation** tab of the add-on has the details.

## Credits

Built on [QobuzProxy](https://github.com/leolobato/qobuz-proxy) by leolobato, which uses the
Qobuz Connect protocol work from [StreamCore32](https://github.com/tobiasguyer/StreamCore32) by
Tobias Guyer. The add-on packaging follows the approach of
[5treamer/qobuz-proxy](https://github.com/5treamer/qobuz-proxy). It talks to Music Assistant
with [music-assistant-client](https://github.com/music-assistant/client).

Not affiliated with Qobuz or Music Assistant. MIT license, see
[LICENSE](https://github.com/hazymorning/Qobuz-Bridge-for-Music-Assistant/blob/main/LICENSE).

To use QobuzProxy on its own (DLNA speakers or local audio, without Home Assistant), see the
[original QobuzProxy README](https://github.com/hazymorning/Qobuz-Bridge-for-Music-Assistant/blob/main/docs/qobuz-proxy.md).
