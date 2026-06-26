# Mesh_kot

Meshtastic Live TUI (Text User Interface) application for viewing and monitoring Meshtastic nodes, messages, and signal traces.

## Features

- **Network Auto-Discovery**: Automatically scans your local `/24` subnet on port `4403` to find WiFi-connected Meshtastic devices.
- **Interactive Selector**: If multiple Meshtastic devices are found, it prompts you to select one.
- **Direct IP Connection**: Pass a specific host via parameters to bypass network scanning.
- **Curses UI**: Interactive dashboard showing active nodes, battery levels, messages, and a signal trace.

## Requirements

Ensure you have Python 3 and the required dependencies installed:

```bash
pip install meshtastic
```

## How to Run

You can run the application using the provided startup script:

1. **Auto-scan and Connect**:
   ```bash
   ./run.sh
   ```
   This will automatically scan your local network for Meshtastic devices on port `4403`. If one is found, it connects immediately. If multiple are found, it displays a menu for selection.

2. **Connect to a Specific Device**:
   ```bash
   ./run.sh --host <device_ip>
   ```

3. **Specify a Custom Port**:
   ```bash
   ./run.sh --host <device_ip> --port <custom_port>
   ```

## Controls

Inside the dashboard:
- **`n`**: Switch to Node List view
- **`m`**: Switch to Messages list view
- **`t`**: Switch to Signal Trace view
- **`g` / `G`**: Toggle message filter (gibberish/system messages)
- **`q` / `Q`**: Quit the application
