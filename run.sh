#!/bin/bash
# Run the Meshtastic TUI Live Viewer
# Auto-scans the network if no arguments are provided, or connects to the specified host.
# Uses the project virtualenv (.venv) when it exists, otherwise the system python3.
DIR="$(dirname "$0")"
PY="python3"
[ -x "$DIR/.venv/bin/python" ] && PY="$DIR/.venv/bin/python"
exec "$PY" "$DIR/meshtop_live.py" "$@"
