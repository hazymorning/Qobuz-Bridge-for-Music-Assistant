# Working on MA Qobuz Bridge

The add-on is upstream [QobuzProxy](https://github.com/leolobato/qobuz-proxy) plus a Music
Assistant backend, a panel and the Home Assistant packaging. How the bridge works is in
[docs/music-assistant.md](docs/music-assistant.md).

## Set up and check

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.13 and everything else. These are
the checks, exactly as CI runs them:

```bash
uv sync --locked
uv run ruff format --check qobuz_proxy tests rootfs
uv run ruff check qobuz_proxy tests rootfs
uv run mypy qobuz_proxy rootfs/start.py
uv run pytest -n auto
```

`uv run ruff format qobuz_proxy tests rootfs` fixes the formatting. The **Checks** workflow runs
these and an image build on every push and pull request.

## Where things are

| Path | What |
|---|---|
| `qobuz_proxy/backends/music_assistant/` | The bridge: connection, backend, speakers, settings |
| `qobuz_proxy/webui/music_assistant_routes.py`, `qobuz_proxy/webui/static/index.html`, `qobuz_proxy/webui/static/panel.js`, `qobuz_proxy/webui/static/panel.css` | The panel and its API. `qobuz_proxy/webui/static/app.js` and the `/api/speakers` routes are upstream's UI; our `index.html` does not load them. |
| `config.json`, `Dockerfile.amd64`, `Dockerfile.aarch64`, `rootfs/start.py`, `translations/`, `repository.yaml`, `icon.png`, `logo.png`, `DOCS.md`, `CHANGELOG.md` | The Home Assistant add-on |
| `tests/` | The tests (see below) |
| `docs/music-assistant.md` | How the bridge works. `docs/images/addon-icon.svg` and `addon-logo.svg` are the drawings behind `icon.png` and `logo.png`. |
| Everything else in `qobuz_proxy/` | Upstream QobuzProxy, unchanged apart from a few small hooks (listed in `docs/music-assistant.md`) |
| `Dockerfile`, `docker-compose.yaml`, `config.yaml.example`, `.env.example`, `contrib/`, `protos/` | Upstream QobuzProxy's standalone packaging and protocol files, kept so merges stay clean; the add-on does not use them (its image is built from `Dockerfile.amd64`/`.aarch64`) |

## Tests

Tests describe what a user or the Qobuz app can see. Upstream code is unchanged here and tested
upstream, so this repo tests only what it adds. Only the edges are fake:

- `tests/fake_music_assistant.py`: an in-memory Music Assistant 2.10.7 server. The real
  `music-assistant-client` talks to it (only the websocket is fake). Playback time moves only
  with `advance_time`, and `settle()` never sleeps.
- `tests/fake_qobuz.py`: the Qobuz Connect cloud (it records what the app is shown), the track
  API and the messages the app sends.
- `tests/rig.py`: one Music Assistant player wired like production, from the Qobuz websocket
  down to the Music Assistant client.
- `tests/conftest.py`: gives every test its own data directory and shrinks the backend's timing
  constants. Fixtures: `server` (the fake, with Qobuz set up and one player, Kitchen), `rig`
  (Kitchen's speaker, already picked in the Qobuz app), `start_speaker` (starts and picks any
  player's speaker, from saved sessions if given), `fake_login` (Music Assistant logins go to the
  fake) and `fast_heartbeat` (a state report every 0.2 s).

The test files:

- `tests/test_from_qobuz_app.py`: playing from the Qobuz app: an album from start to end, the
  app's commands, tracks or players that cannot play, a group, other Qobuz devices, a second app.
- `tests/test_from_music_assistant.py`: what the app shows when the player is used in Music
  Assistant: pause, next, previous, seek, stop and clear there, a track that ends or stalls,
  external playback.
- `tests/test_recovery.py`: Music Assistant unreachable at start, going away or restarting
  mid-play, a revoked token, an add-on restart while the music plays.
- `tests/test_connection.py`: the one connection to Music Assistant: status, the player list,
  reconnects and backoff, logins and tokens, calls and their errors, event listeners.
- `tests/test_speakers.py`: which players appear in the app, under which name, uuid and port,
  and how that survives restarts, renames, outages and logins; the settings file.
- `tests/test_panel.py`: the panel and its API: the page, the status checks, the Music Assistant
  login, the player choice, request validation and the guard against other sites.
- `tests/test_upstream_changes.py`: the hooks in upstream files; each test fails if a merge lost
  one.

## Merging upstream

```bash
git remote add upstream https://github.com/leolobato/qobuz-proxy.git
git fetch upstream
git merge upstream/main
```

- `README.md`, `CONTRIBUTING.md`, `.github/` and `qobuz_proxy/webui/static/index.html`: keep ours.
- The hooks in `qobuz_proxy/app.py`, `qobuz_proxy/speaker.py`, `qobuz_proxy/playback/player.py`,
  `qobuz_proxy/backends/base.py`, `qobuz_proxy/backends/types.py` and
  `qobuz_proxy/webui/routes.py`: keep both sides. Never take ours for `routes.py`: we only add two
  lines there. Then run `uv run pytest tests/test_upstream_changes.py` and the rest of the checks.
- `pyproject.toml`, `.gitignore`, `.dockerignore`: keep both sides.
- `uv.lock`: take ours, then run `uv lock` and `uv sync`.
- Anything upstream has that this repo deleted: its tests (every file in `tests/` except
  `__init__.py` and the ones listed above), `scripts/`, `.github/workflows/release.yml`, its notes
  and its editor settings. `git rm` them again, whether they conflict or are new.

## Try the add-on image

```bash
docker build --file Dockerfile.amd64 --tag ma-qobuz-bridge .
data=$(mktemp -d) && echo '{"log_level": "debug"}' > "$data/options.json"
docker run --rm --init --network host --volume "$data:/data" ma-qobuz-bridge
```

The panel is then on http://localhost:8689. The container takes the data directory over and
makes it readable only by itself, so remove it with `sudo rm -rf "$data"`.

## Release

Add a `CHANGELOG.md` entry and bump `version` in `config.json`. Home Assistant offers the update
once the change is on `main`.
