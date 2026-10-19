"""One-time add-on setup: turn on "Show in sidebar" and the watchdog.

Home Assistant installs add-ons with both switched off. This asks the
Supervisor to turn them on once per install, then writes a marker file in
/data, so a user who later switches either off again is left alone.

Supervisor API (checked against home-assistant/supervisor):
- POST /addons/{app}/options is routed in supervisor/api/__init__.py:772 to
  APIApps.options (supervisor/api/apps.py:328-374); "self" resolves to the
  calling add-on (apps.py:169-174). Its body schema SCHEMA_OPTIONS
  (apps.py:124-135) accepts "ingress_panel" and "watchdog" booleans.
- /addons/self/options matches the api_bypass pattern
  "/addons/self/(?!security|update)[^/]+" (supervisor/api/middleware/security.py:100-110),
  so any add-on may call it with its SUPERVISOR_TOKEN, whatever its hassio_role.
- Success is HTTP 200 with {"result": "ok"} (supervisor/api/utils.py api_process).

Standard library only. Best effort: it prints one line and always exits 0,
so it can never stop the add-on from starting.
"""

import json
import os
import signal
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MARKER = Path(os.environ.get("QOBUZPROXY_DATA_DIR", "/data")) / ".addon_init_done"
URL = "http://supervisor/addons/self/options"
BODY = {"ingress_panel": True, "watchdog": True}
REQUEST_TIMEOUT_S = 5
HARD_LIMIT_S = 8  # whole script, in case the name lookup or a socket hangs

NOT_DONE = "Did not enable the sidebar panel and watchdog"


def _on_alarm(signum: int, frame: Any) -> None:
    raise TimeoutError(f"gave up after {HARD_LIMIT_S} s")


def run() -> str:
    """Do the setup if it is due and return the line to log."""
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not token:
        return f"{NOT_DONE}: not running under the Home Assistant Supervisor"
    if MARKER.exists():
        return f"{NOT_DONE}: already done once for this install, so your settings are kept"

    request = urllib.request.Request(
        URL,
        data=json.dumps(BODY).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            status = response.status
    except urllib.error.HTTPError as err:
        return f"{NOT_DONE}: the Supervisor answered HTTP {err.code}"
    except (urllib.error.URLError, OSError) as err:
        return f"{NOT_DONE}: {getattr(err, 'reason', err)}"
    if status != 200:
        return f"{NOT_DONE}: the Supervisor answered HTTP {status}"

    try:
        MARKER.write_text("ingress_panel and watchdog were enabled once by addon_init.py\n")
    except OSError as err:
        return f"Enabled the sidebar panel and watchdog, but could not write {MARKER}: {err}"
    return "Enabled the sidebar panel and watchdog"


def main() -> None:
    try:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(HARD_LIMIT_S)
        line = run()
    except BaseException as err:  # never let anything block the add-on start
        line = f"{NOT_DONE}: {type(err).__name__}: {err}"
    finally:
        signal.alarm(0)
    print(line, flush=True)


if __name__ == "__main__":
    main()
    sys.exit(0)
