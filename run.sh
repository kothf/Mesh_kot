#!/bin/bash
# Run the Meshtastic TUI Live Viewer
# Auto-scans the network if no arguments are provided, or connects to the specified host.
python3 "$(dirname "$0")/meshtop_live.py" "$@"
