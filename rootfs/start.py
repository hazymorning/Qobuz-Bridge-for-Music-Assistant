"""Start the add-on: apply its options, turn on the sidebar panel once, run the app.

Home Assistant installs add-ons with "Show in sidebar" and the watchdog off.
The first start asks the Supervisor to turn both on and leaves a marker in
/data, so a user who switches them off later is left alone. /addons/self/options
is open to every add-on with its SUPERVISOR_TOKEN, whatever its hassio_role.
"""

import json
import os
import urllib.request
from pathlib import Path

DATA = Path("/data")
MARKER = DATA / ".addon_init_done"


def enable_panel_and_watchdog() -> str:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token or MARKER.exists():
        return ""
    request = urllib.request.Request(
        "http://supervisor/addons/self/options",
        data=json.dumps({"ingress_panel": True, "watchdog": True}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5):
            pass
    except OSError as e:  # never keep the add-on from starting
        return f"Could not turn on the sidebar panel and watchdog: {e}"
    MARKER.touch()
    return "Turned on the sidebar panel and watchdog"


def main() -> None:
    options = json.loads((DATA / "options.json").read_text())
    os.environ["QOBUZPROXY_LOG_LEVEL"] = options["log_level"]
    message = enable_panel_and_watchdog()
    if message:
        print(message, flush=True)
    os.execvp("qobuz-proxy", ["qobuz-proxy"])


if __name__ == "__main__":
    main()
