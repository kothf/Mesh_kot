# Mesh_kot

A powerful, interactive Meshtastic Live TUI (Text User Interface) application for viewing and monitoring Meshtastic nodes, real-time messages, and signal traces in a curses-based dashboard.

---

## 🚀 Key Features

* **Network Auto-Discovery**: Scans your local `/24` subnet on port `4403` to find WiFi-connected Meshtastic nodes, offering a menu if multiple nodes are discovered.
* **Smart Background Auto-Tracer**:
  * **Startup Wait**: Starts automatically once the first external node is heard from (heard/seen on mesh).
  * **Dynamic Queueing**: Traces all seen nodes sequentially with a **30-second pause** between scans to avoid channel congestion.
  * **Two-Pass Retry Loop**: Nodes that fail to respond are deferred to a retry queue and retried once at the end of the pass.
  * **Selective 24h Cooldown**: Successfully traced nodes are placed on a **24-hour cooldown**. Unsuccessful traces are immediately rescheduled for retry if they exhibit new mesh activity (with a 5-minute safety cooldown).
  * **Manual Lockout**: Suppresses manual traceroute requests when auto-trace is active (`TRACE suppressed (auto-trace active)`) to prevent packet collisions.
* **📋 Nodes Table**: Name, how long ago each node was **heard** (`12s`, `4m`, `3h` – also for nodes known only from the radio's node DB), SNR, hops, battery (`PWR` when externally powered), channel utilization, traced route and distance.
  * 🟩 **Green Rows**: transit router nodes (relays connecting your local node to $\ge 2$ traced nodes).
  * 🟥 **Red Battery**: low battery (**20% or lower**).
  * 🟨 **Yellow Row**: your local node.
  * **Dimmed Rows**: nodes not heard for over 2 hours.
  * **SNR colors**: green ≥ 0 dB, yellow down to -10 dB, red below.
* **🔎 Node Detail Pane**: everything about the selected node – ID, hardware, signal, power, sensors, position/distance, which nodes it relays for, and the last trace path with per-hop SNR (plus the return path when it differs). On terminals ≥ 130 columns it sits on the right; on narrower ones it is a 3-line strip under the table.
* **💬 Messages Screen (`M` Key)**: a messenger-style split view:
  * **Channels** (named from the radio config, e.g. `# MediumSlow`) are separate from **direct chats**, so replying to a channel post goes to the channel, not privately.
  * **Unread counts** per conversation, and a `✉ N unread` badge in the header on every screen.
  * **Delivery status** on sent messages: `…` pending, `✓` relayed by a neighbour, `✓✓` delivered to the recipient, `✗ REASON` failed.
  * Long messages wrap; day separators between dates. Sent messages are **Yellow**, received **Cyan**.
* **📈 Stats View (`S` Key)**: A dedicated live dashboard showing:
  * **Node Activity**: Total node count, active (seen in last 30 min), inactive (> 30 min), and unseen/dead.
  * **Routing Statistics**: Total traced stations and calculated **Average Hop Count** of the active topology.
  * **Link Quality (SNR)**: Shows the top 3 best direct links (green edge weights) and top 3 worst direct links (red edge weights).
  * **🌳 Network Connections Map**: A dynamic, recursive text tree graph showing how packets route through intermediate relays. It automatically ranks busier routers first, wraps the tree across multiple columns to utilize extra screen width, and supports vertical scrolling (`[↑/↓]` or `j`/`k`) to navigate larger topologies.
* **🗺️ Network Topology Export (`P` Key)**: Generates a complete Mermaid diagram map showing all nodes, connections, hops, and SNR weights, exporting it locally to `topology.md`.
* **📜 Route History Logging**: Appends all successful traceroute paths (including timestamp, destination ID, short name, hops, forward, and return SNR values) to a local `traces.jsonl` database for long-term telemetry analysis.
* **💾 Traced Stations State Persistence**: Automatically saves all active node trace routes to `traced_stations.json`. On application startup, it reloads this data to restore the connections map immediately (unless the file is older than 24 hours).

---

## 🛠️ Requirements

Ensure you have Python 3 and the required dependencies installed:

```bash
pip install meshtastic
```

---

## 🏃 How to Run

1. **Auto-scan and Connect**:
   ```bash
   ./run.sh
   ```
   This scans your local subnet for Meshtastic devices on port `4403`, connects automatically, or prompts you with a menu if multiple are found.

2. **Connect to a Specific Device**:
   ```bash
   ./run.sh --host <device_ip>
   ```

3. **Specify a Custom Port**:
   ```bash
   ./run.sh --host <device_ip> --port <custom_port>
   ```

---

## 🎮 Keyboard Controls

Inside the dashboard:
* **`n` / `N`**: Switch to **Nodes List** view.
* **`m` / `M`**: Switch to **Messages / Conversations** view.
* **`s` / `S`**: Switch to the **Stats Dashboard** view.
* **`t` / `T`**: Switch to **Signal Trace** view (or retry/run a manual trace on the selected node).
* **`p` / `P`**: Export the live network topology map to `topology.md` (from any view).
* **`g` / `G`**: Toggle message filtering (ON/OFF) to hide system messages/gibberish.
* **`q` / `Q`**: Quit the application safely.
* **`↑/↓`, `PgUp/PgDn`, `Home/End`**: Move the selection (it stays on the same node when the list re-sorts).
* **`Enter`**: Nodes view – write a direct message to the selected node; Messages view – write to the selected channel or chat.

Shortcuts also work on a Russian or Hebrew keyboard layout (same physical keys). Messages can be typed in any language; the prompt shows the UTF-8 byte count against the 200-byte limit (`Ctrl-U` clears the line).

---

## 🔌 Connection & Logging

* If the radio reboots or WiFi drops, the header shows `[RECONNECTING]` and the app reconnects by itself with backoff (TCP keepalive detects dead links within about a minute).
* Library messages, tracebacks and UI errors go to `meshtop.log` next to the script instead of over the screen. A drawing error is shown in the header and logged; it no longer closes the app.

---

## 📂 Git & Runtime Files

The repository is configured to exclude local runtime logs and databases via `.gitignore`:
* `traces.jsonl`: Local database of all successful traceroute histories.
* `topology.md`: Generated Mermaid graphs from your `P` export keypresses.
* `*.log` & `__pycache__/`: Python cache and application execution logs (`meshtop.log`).
