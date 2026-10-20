# Working on MA Qobuz Bridge

The add-on is upstream [QobuzProxy](https://github.com/leolobato/qobuz-proxy) plus a Music
Assistant backend, a panel and the Home Assistant packaging. How the bridge works is in
[docs/music-assistant.md](docs/music-assistant.md).

## Set up and check

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.13 and everything else:

```bash
uv sync
uv run ruff format qobuz_proxy tests
uv run ruff check qobuz_proxy tests
uv run mypy qobuz_proxy
uv run pytest
```

All four checks are clean on `main`, and the **Checks** workflow runs them on every push, plus a
build of the add-on image.

## Where things are

| Path | What |
|---|---|
| `qobuz_proxy/backends/music_assistant/` | The bridge: connection, backend, speakers, settings |
| `qobuz_proxy/webui/music_assistant_routes.py`, `qobuz_proxy/webui/static/panel.*` | The panel and its API |
| `config.json`, `Dockerfile.*`, `rootfs/start.py`, `translations/`, `DOCS.md`, `CHANGELOG.md` | The Home Assistant add-on |
| Everything else in `qobuz_proxy/` | Upstream QobuzProxy, unchanged apart from a few small hooks |

## Tests

Tests describe what a user or the Qobuz app can see. Only the edges are fake:

- `tests/fake_music_assistant.py`: an in-memory Music Assistant server. The real
  `music-assistant-client` talks to it; playback time moves only with `advance_time`.
- `tests/fake_qobuz.py`: the Qobuz Connect cloud (it records what the app is shown), the track API
  and the messages the app sends.
- `tests/rig.py`: one Music Assistant player wired like production, from the Qobuz websocket down
  to the Music Assistant client. The `rig` fixture in `tests/conftest.py` hands you one that is
  already picked in the Qobuz app.

Upstream code is unchanged here and tested upstream, so this repo tests only what it adds;
`tests/test_upstream_changes.py` covers the hooks in upstream files.

## Merging upstream

```bash
git remote add upstream https://github.com/leolobato/qobuz-proxy.git
git fetch upstream
git merge upstream/main
```

- Upstream test files: delete them (`git rm`), whether they conflict or are new.
- `README.md`, `CONTRIBUTING.md`, `index.html`, `routes.py`: keep ours.
- The hooks (see docs/music-assistant.md): keep both sides, then run
  `uv run pytest tests/test_upstream_changes.py` and the rest.

## Try the add-on image

```bash
docker build --file Dockerfile.amd64 --tag ma-qobuz-bridge .
mkdir -p data && echo '{"log_level": "debug"}' > data/options.json
docker run --rm --init --network host --volume "$PWD/data:/data" ma-qobuz-bridge
```

The panel is then on http://localhost:8689.

## Release

Add a `CHANGELOG.md` entry and bump `version` in `config.json`. Home Assistant offers the update
once the change is on `main`.
