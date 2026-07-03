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
* **Advanced Curses Visual Highlights**:
  * 🟩 **Green Rows**: Automatically highlights transit router nodes (nodes serving as relays connecting your local node to $\ge 2$ target nodes).
  * 🟥 **Red Highlights**: Marks nodes with low battery levels (**20% or lower**) to warn of power degradation.
  * 🟨 **Yellow Rows**: Highlights your local node.
* **💬 Conversational Messages Screen (`M` Key)**: A split messenger-style dashboard:
  * **Left Column**: Lists active conversations (only shows nodes with which you have exchanged messages).
  * **Right Column**: Displays the message log of the selected conversation.
  * **Color Coding**: Sent messages are highlighted in **Yellow** (Me), and received messages are highlighted in **Cyan** (node name).
* **📈 Stats View (`S` Key)**: A dedicated live dashboard showing:
  * **Node Activity**: Total node count, active (seen in last 30 min), inactive (> 30 min), and unseen/dead.
  * **Routing Statistics**: Total traced stations and calculated **Average Hop Count** of the active topology.
  * **Link Quality (SNR)**: Shows the top 3 best direct links (green edge weights) and top 3 worst direct links (red edge weights).
  * **🌳 Network Connections Map**: A dynamic, recursive text tree graph showing how packets route through intermediate relays. It automatically ranks busier routers first and scales/omits leaf nodes to fit your screen height.
* **🗺️ Network Topology Export (`P` Key)**: Generates a complete Mermaid diagram map showing all nodes, connections, hops, and SNR weights, exporting it locally to `topology.md`.
* **📜 Route History Logging**: Appends all successful traceroute paths (including timestamp, destination ID, short name, hops, forward, and return SNR values) to a local `traces.jsonl` database for long-term telemetry analysis.

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
* **`p` / `P`**: Export the live network topology map to `topology.md` (usable from Nodes or Stats view).
* **`g` / `G`**: Toggle message filtering (ON/OFF) to hide system messages/gibberish.
* **`q` / `Q`**: Quit the application safely.

---

## 📂 Git & Runtime Files

The repository is configured to exclude local runtime logs and databases via `.gitignore`:
* `traces.jsonl`: Local database of all successful traceroute histories.
* `topology.md`: Generated Mermaid graphs from your `P` export keypresses.
* `*.log` & `__pycache__/`: Python cache and application execution logs.
