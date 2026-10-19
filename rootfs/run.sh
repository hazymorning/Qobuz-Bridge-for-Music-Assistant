#!/bin/sh
# Home Assistant add-on entry point: map the add-on options to environment
# variables, turn on the sidebar panel and watchdog once, then start the app.
# tini is PID 1 (config.json "init" defaults to true); exec hands it the app
# so SIGTERM from the Supervisor reaches Python.
set -e

OPTIONS=/data/options.json
if [ -f "$OPTIONS" ]; then
    QOBUZPROXY_LOG_LEVEL=$(python3 -I -c \
        'import json, sys; print(json.load(open(sys.argv[1])).get("log_level", "info"))' \
        "$OPTIONS" 2>/dev/null || echo info)
    export QOBUZPROXY_LOG_LEVEL
fi

# Best effort (one 5 s request at most); it never stops the add-on from starting.
python3 -I /addon_init.py || true

exec qobuz-proxy
