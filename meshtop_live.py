#!/usr/bin/env python3
import argparse
import curses
import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from pubsub import pub
from meshtastic.tcp_interface import TCPInterface


# ---------- Data classes ----------

@dataclass
class NodeInfo:
    num: int
    node_id: str = ""          # "!abcd1234"
    short_name: str = "?"
    long_name: str = "?"
    hw_model: str = "?"

    last_seen: float = 0.0
    seen_packets: int = 0

    last_rssi: Optional[float] = None
    last_snr: Optional[float] = None
    hop_limit: Optional[int] = None
    hop_start: Optional[int] = None

    battery: Optional[int] = None
    voltage: Optional[float] = None
    channel_util: Optional[float] = None
    air_util: Optional[float] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None

    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_km: Optional[float] = None

    is_me: bool = False
    battery_low: bool = False


@dataclass
class MessageInfo:
    timestamp: float
    direction: str          # "IN" or "OUT"
    from_id: str
    from_short: str
    to_id: str
    to_short: str
    text: str
    portnum: str = "TEXT_MESSAGE_APP"
    status: str = "UNKNOWN"  # RECEIVED / SENT
    pkt_id: Optional[int] = None


# ---------- Helpers ----------

def haversine_km(lat1, lon1, lat2, lon2) -> Optional[float]:
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def safe_text(s: str) -> str:
    """Remove NULL and non-printable chars so curses won't explode."""
    if not s:
        return ""
    out_chars = []
    for ch in s:
        # drop NULL & other nasty control chars
        if ord(ch) < 32 and ch not in ("\t", " "):
            continue
        if ch == "\x00":
            continue
        out_chars.append(ch)
    return "".join(out_chars)


# ---------- Main app ----------

class MeshTopApp:
    def __init__(self, host: str, port: int = 4403):
        self.host = host
        self.port = port
        self.iface: Optional[TCPInterface] = None

        self.nodes: Dict[int, NodeInfo] = {}
        self.messages: List[MessageInfo] = []

        # local node info (static)
        self.local_num: Optional[int] = None
        self.local_id: Optional[str] = None
        self.local_short: str = "ME"
        self.local_long: str = "MyNode"
        self.local_lat: Optional[float] = None
        self.local_lon: Optional[float] = None

        # UI state
        self.running = True
        self.view_mode = "NODES"    # or "MSGS"
        self.selected_node_index = 0
        self.selected_msg_index = 0

        # scrolling offsets
        self.node_view_offset = 0
        self.msg_view_offset = 0

        self.input_mode = False
        self.input_text = ""
        self.input_target_num: Optional[int] = None

    # ---------- Connection and initial load ----------

    def connect(self):
        # In meshtastic 2.7.x: TCPInterface(host, port)
        self.iface = TCPInterface(self.host, self.port)

        info = self.iface.myInfo
        if info:
            # try both attribute and dict styles
            self.local_num = getattr(info, "my_node_num", None) \
                             or (isinstance(info, dict) and info.get("my_node_num"))
            self.local_id = getattr(info, "my_node_id", None) \
                            or getattr(info, "myNodeId", None) \
                            or (isinstance(info, dict) and (info.get("my_node_id") or info.get("myNodeId")))

        # Pre-load nodeDB so we have names immediately
        for num, n in self.iface.nodesByNum.items():
            node = NodeInfo(num=num)

            # n may be protobuf or dict
            u = getattr(n, "user", None)
            if isinstance(n, dict):
                u = n.get("user", u)

            if u:
                if isinstance(u, dict):
                    node.node_id = u.get("id", "") or node.node_id
                    node.short_name = u.get("shortName", node.short_name)
                    node.long_name = u.get("longName", node.long_name)
                    node.hw_model = u.get("hwModel", node.hw_model)
                else:
                    node.node_id = getattr(u, "id", None) or getattr(u, "id_", None) or node.node_id
                    node.short_name = getattr(u, "shortName", None) or node.short_name
                    node.long_name = getattr(u, "longName", None) or node.long_name
                    node.hw_model = getattr(u, "hwModel", None) or node.hw_model

            dev = getattr(n, "deviceMetrics", None)
            if isinstance(n, dict):
                dev = n.get("deviceMetrics", dev)
            if dev:
                if isinstance(dev, dict):
                    node.battery = dev.get("batteryLevel", node.battery)
                    node.voltage = dev.get("voltage", node.voltage)
                    node.channel_util = dev.get("channelUtilization", node.channel_util)
                    node.air_util = dev.get("airUtilTx", node.air_util)
                else:
                    node.battery = getattr(dev, "batteryLevel", None) or getattr(dev, "battery_level", None)
                    node.voltage = getattr(dev, "voltage", None)
                    node.channel_util = getattr(dev, "channelUtilization", None) \
                                        or getattr(dev, "channel_utilization", None)
                    node.air_util = getattr(dev, "airUtilTx", None) or getattr(dev, "air_util_tx", None)
                node.battery_low = node.battery is not None and node.battery <= 20

            env = getattr(n, "environmentMetrics", None)
            if isinstance(n, dict):
                env = n.get("environmentMetrics", env)
            if env:
                if isinstance(env, dict):
                    node.temperature = env.get("temperature", node.temperature)
                    node.humidity = env.get("relativeHumidity", node.humidity)
                else:
                    node.temperature = getattr(env, "temperature", None)
                    node.humidity = getattr(env, "relativeHumidity", None)

            pos = getattr(n, "position", None)
            if isinstance(n, dict):
                pos = n.get("position", pos)
            if pos:
                if isinstance(pos, dict):
                    lat = pos.get("latitude")
                    lon = pos.get("longitude")
                    if lat is None and pos.get("latitudeI") is not None:
                        lat = pos["latitudeI"] * 1e-7
                    if lon is None and pos.get("longitudeI") is not None:
                        lon = pos["longitudeI"] * 1e-7
                else:
                    lat = getattr(pos, "latitude", None)
                    lon = getattr(pos, "longitude", None)
                    if lat is None and getattr(pos, "latitude_i", None) is not None:
                        lat = pos.latitude_i * 1e-7
                    if lon is None and getattr(pos, "longitude_i", None) is not None:
                        lon = pos.longitude_i * 1e-7
                node.lat = lat
                node.lon = lon

            # mark local node
            if (self.local_num is not None and num == self.local_num) or \
               (self.local_id and node.node_id == self.local_id):
                node.is_me = True
                self.local_num = num
                if self.local_id is None:
                    self.local_id = node.node_id or self.local_id
                self.local_short = node.short_name or self.local_short
                self.local_long = node.long_name or self.local_long
                self.local_lat = node.lat
                self.local_lon = node.lon

            self.nodes[num] = node

        # if my node wasn't present, synthesize it
        if self.local_num is not None and self.local_num not in self.nodes:
            n = NodeInfo(num=self.local_num, is_me=True)
            n.node_id = self.local_id or n.node_id
            n.short_name = self.local_short
            n.long_name = self.local_long
            n.lat = self.local_lat
            n.lon = self.local_lon
            self.nodes[self.local_num] = n

        self._update_all_distances()

        # subscribe for live updates
        pub.subscribe(self.on_packet, "meshtastic.receive")
        pub.subscribe(self.on_telemetry, "meshtastic.receive.telemetry")
        pub.subscribe(self.on_position, "meshtastic.receive.position")
        pub.subscribe(self.on_user, "meshtastic.receive.user")

    # ---------- Node helpers ----------

    def _get_or_create_node(self, num: int) -> NodeInfo:
        node = self.nodes.get(num)
        if not node:
            node = NodeInfo(num=num)
            self.nodes[num] = node
        return node

    def _update_distance(self, node: NodeInfo):
        if self.local_lat is None or self.local_lon is None:
            return
        if node.lat is None or node.lon is None:
            return
        node.distance_km = haversine_km(self.local_lat, self.local_lon, node.lat, node.lon)

    def _update_all_distances(self):
        if self.local_lat is None or self.local_lon is None:
            return
        for node in self.nodes.values():
            self._update_distance(node)

    # ---------- meshtastic callbacks ----------

    def on_user(self, packet, interface):
        num = packet.get("from")
        if num is None:
            return
        node = self._get_or_create_node(num)
        d = packet.get("decoded", {})
        u = d.get("user", {})

        node.node_id = u.get("id", node.node_id)
        node.short_name = u.get("shortName", node.short_name)
        node.long_name = u.get("longName", node.long_name)
        node.hw_model = u.get("hwModel", node.hw_model)
        node.last_seen = time.time()
        node.seen_packets += 1

        if self.local_num is not None and num == self.local_num:
            if not self.local_id and "id" in u:
                self.local_id = u["id"]
            self.local_short = node.short_name or self.local_short
            self.local_long = node.long_name or self.local_long

    def on_telemetry(self, packet, interface):
        num = packet.get("from")
        if num is None:
            return
        node = self._get_or_create_node(num)
        d = packet.get("decoded", {})
        t = d.get("telemetry", {})
        dev = t.get("deviceMetrics", {})
        env = t.get("environmentMetrics", {})

        node.last_seen = time.time()
        node.seen_packets += 1

        node.battery = dev.get("batteryLevel", node.battery)
        node.voltage = dev.get("voltage", node.voltage)
        node.channel_util = dev.get("channelUtilization", node.channel_util)
        node.air_util = dev.get("airUtilTx", node.air_util)
        node.battery_low = node.battery is not None and node.battery <= 20

        node.temperature = env.get("temperature", node.temperature)
        node.humidity = env.get("relativeHumidity", node.humidity)

        node.hop_limit = packet.get("hopLimit", node.hop_limit)
        node.hop_start = packet.get("hopStart", node.hop_start)
        node.last_rssi = packet.get("rxRssi", node.last_rssi)
        node.last_snr = packet.get("rxSnr", node.last_snr)

    def on_position(self, packet, interface):
        num = packet.get("from")
        if num is None:
            return
        node = self._get_or_create_node(num)
        d = packet.get("decoded", {})
        p = d.get("position", {})

        lat = p.get("latitude", node.lat)
        lon = p.get("longitude", node.lon)
        if lat is None and p.get("latitudeI") is not None:
            lat = p["latitudeI"] * 1e-7
        if lon is None and p.get("longitudeI") is not None:
            lon = p["longitudeI"] * 1e-7

        node.lat = lat
        node.lon = lon
        node.last_seen = time.time()
        node.seen_packets += 1
        self._update_distance(node)

        if self.local_num is not None and num == self.local_num:
            self.local_lat = node.lat
            self.local_lon = node.lon
            self._update_all_distances()

    def on_packet(self, packet, interface):
        num = packet.get("from")
        if num is not None:
            node = self._get_or_create_node(num)
            node.last_seen = time.time()
            node.seen_packets += 1
            node.last_rssi = packet.get("rxRssi", node.last_rssi)
            node.last_snr = packet.get("rxSnr", node.last_snr)
            node.hop_limit = packet.get("hopLimit", node.hop_limit)
            node.hop_start = packet.get("hopStart", node.hop_start)

        d = packet.get("decoded")
        if not d:
            return

        # Plain text if present
        text = d.get("text")
        if text is None:
            # some builds only put bytes in payload
            payload = d.get("payload")
            if isinstance(payload, (bytes, bytearray)):
                try:
                    text = payload.decode("utf-8", errors="replace")
                except Exception:
                    text = None
        if text is None:
            return

        text = safe_text(text)

        from_num = packet.get("from")
        to_num = packet.get("to")

        from_node = self._get_or_create_node(from_num) if from_num is not None else None
        to_node = self._get_or_create_node(to_num) if to_num is not None else None

        from_id = from_node.node_id if from_node and from_node.node_id else f"#{from_num}"
        to_id = to_node.node_id if to_node and to_node.node_id else f"#{to_num}"
        from_short = from_node.short_name if from_node else "?"
        to_short = to_node.short_name if to_node else "?"

        direction = "IN"
        if self.local_num is not None and from_num == self.local_num:
            direction = "OUT"

        status = "RECEIVED" if direction == "IN" else "SENT"

        msg = MessageInfo(
            timestamp=time.time(),
            direction=direction,
            from_id=from_id,
            from_short=from_short,
            to_id=to_id,
            to_short=to_short,
            text=text,
            portnum=d.get("portnum", ""),
            status=status,
            pkt_id=packet.get("id"),
        )
        self.messages.append(msg)

    # ---------- Sending text ----------

    def send_text_to_node(self, target_num: int, text: str):
        if not self.iface:
            return
        node = self.nodes.get(target_num)
        if not node:
            return

        if not node.node_id:
            # can't send without node_id
            self.messages.append(
                MessageInfo(
                    timestamp=time.time(),
                    direction="OUT",
                    from_id=self.local_id or "^local",
                    from_short=self.local_short,
                    to_id=f"#{target_num}",
                    to_short=node.short_name or "?",
                    text="[ERROR: node has no node_id]",
                    status="UNKNOWN",
                )
            )
            return

        clean_text = safe_text(text)

        # append to log immediately
        self.messages.append(
            MessageInfo(
                timestamp=time.time(),
                direction="OUT",
                from_id=self.local_id or "^local",
                from_short=self.local_short,
                to_id=node.node_id,
                to_short=node.short_name,
                text=clean_text,
                status="SENT",
            )
        )

        try:
            self.iface.sendText(clean_text, destinationId=node.node_id, wantAck=True)
        except Exception as e:
            self.messages.append(
                MessageInfo(
                    timestamp=time.time(),
                    direction="OUT",
                    from_id=self.local_id or "^local",
                    from_short=self.local_short,
                    to_id=node.node_id,
                    to_short=node.short_name,
                    text=f"[SEND ERROR: {e}]",
                    status="UNKNOWN",
                )
            )

    # ---------- Drawing helpers ----------

    def _draw_header(self, stdscr, text: str):
        h, w = stdscr.getmaxyx()
        if h <= 0 or w <= 1:
            return
        maxw = w - 1
        stdscr.attron(curses.A_REVERSE)
        stdscr.addnstr(0, 0, safe_text(text).ljust(maxw), maxw)
        stdscr.attroff(curses.A_REVERSE)

    def _draw_message_log(self, stdscr, start_row: int, max_rows: int):
        h, w = stdscr.getmaxyx()
        maxw = w - 1
        if start_row >= h - 1 or maxw <= 0:
            return

        if not self.messages:
            stdscr.addnstr(start_row, 0, "(no messages yet)".ljust(maxw), maxw)
            return

        msgs = self.messages[-max_rows:]
        for i, msg in enumerate(msgs):
            row_y = start_row + i
            if row_y >= h - 1:
                break
            t_str = time.strftime("%H:%M:%S", time.localtime(msg.timestamp))
            dir_str = "→" if msg.direction == "OUT" else "←"
            from_str = f"{msg.from_id[-4:]}({msg.from_short})"
            to_str = f"{msg.to_id[-4:]}({msg.to_short})"
            status = msg.status
            prefix = f"{t_str} {dir_str} {from_str}->{to_str} [{status}] "
            text_space = max(10, maxw - len(prefix) - 1)
            text = safe_text(msg.text)[:text_space]

            line = (prefix + text).ljust(maxw)
            stdscr.addnstr(row_y, 0, line, maxw)

    # ---------- NODES view ----------

    def _draw_nodes_view(self, stdscr):
        h, w = stdscr.getmaxyx()
        if h < 8 or w < 40:
            return
        maxw = w - 1
        top = 1
        rows_avail = h - 6  # leave space for log + footer

        nodes_list = list(self.nodes.values())
        nodes_list.sort(key=lambda n: (not n.is_me, -(n.last_seen or 0)))

        # 1. Safety check: ensure selection is within bounds
        if self.selected_node_index >= len(nodes_list):
            self.selected_node_index = max(0, len(nodes_list) - 1)

        # 2. SCROLL LOGIC: Adjust offset to keep the selected node visible
        if self.selected_node_index < self.node_view_offset:
            self.node_view_offset = self.selected_node_index
        elif self.selected_node_index >= self.node_view_offset + rows_avail:
            self.node_view_offset = self.selected_node_index - rows_avail + 1

        # 3. Clamp the offset
        if len(nodes_list) <= rows_avail:
            self.node_view_offset = 0
        else:
            max_offset = len(nodes_list) - rows_avail
            if self.node_view_offset > max_offset:
                self.node_view_offset = max_offset

        cols = [
            ("ID", 11),
            ("Short", 8),
            ("Long", 18),
            ("HW", 10),
            ("Seen", 6),
            ("RSSI", 6),
            ("SNR", 5),
            ("Hop", 5),
            ("Bat%", 6),
            ("V", 5),
            ("ChU%", 6),
            ("AirTx%", 7),
            ("Temp", 6),
            ("Hum%", 6),
            ("Dist", 7),
        ]

        total_min = sum(wd for _, wd in cols) + len(cols) - 1
        extra = maxw - total_min
        if extra < 0:
            for i, (name, wd) in enumerate(cols):
                if name == "Long":
                    cols[i] = (name, max(8, wd + extra))
                    break

        x = 0
        stdscr.attron(curses.A_BOLD)
        for name, wd in cols:
            if x >= maxw:
                break
            stdscr.addnstr(top, x, name.ljust(wd), wd)
            x += wd + 1
        stdscr.attroff(curses.A_BOLD)

        # Slice the list based on offset
        visible_nodes = nodes_list[self.node_view_offset:self.node_view_offset + rows_avail]

        for i, node in enumerate(visible_nodes):
            row_y = top + 1 + i
            if row_y >= h - 4:
                break

            # The 'real' index in the full list
            real_index = self.node_view_offset + i

            attrs = 0
            if real_index == self.selected_node_index:
                attrs |= curses.A_REVERSE
            if node.is_me:
                attrs |= curses.color_pair(3)
            if node.battery_low:
                attrs |= curses.color_pair(1)

            id_str = node.node_id or f"#{node.num}"
            if len(id_str) > 11:
                id_str = id_str[-11:]

            seen_str = f"{node.seen_packets:>4}"
            rssi_str = f"{int(node.last_rssi):>4}" if node.last_rssi is not None else "   -"
            snr_str = f"{int(node.last_snr):>3}" if node.last_snr is not None else " --"
            hop_str = "--"
            if node.hop_limit is not None and node.hop_start is not None:
                hop_str = f"{node.hop_start - node.hop_limit:>2}"
            bat_str = f"{node.battery:>3}%" if node.battery is not None else " --%"
            v_str = f"{node.voltage:.2f}" if node.voltage is not None else " -- "
            chu_str = f"{node.channel_util:4.1f}" if node.channel_util is not None else " -- "
            airtx_str = f"{node.air_util:4.2f}" if node.air_util is not None else " -- "
            temp_str = f"{node.temperature:4.1f}" if node.temperature is not None else " -- "
            hum_str = f"{node.humidity:4.1f}" if node.humidity is not None else " -- "
            dist_str = f"{node.distance_km:5.1f}k" if node.distance_km is not None else "  --  "

            data = [
                id_str,
                node.short_name,
                node.long_name,
                node.hw_model,
                seen_str,
                rssi_str,
                snr_str,
                hop_str,
                bat_str,
                v_str,
                chu_str,
                airtx_str,
                temp_str,
                hum_str,
                dist_str,
            ]

            x = 0
            for (name, wd), val in zip(cols, data):
                if x >= maxw:
                    break
                stdscr.addnstr(row_y, x, safe_text(str(val))[:wd].ljust(wd), wd, attrs)
                x += wd + 1

        topo_row = h - 4
        if self.local_lat is None or self.local_lon is None:
            msg = "(my position unknown – Dist will stay --)"
        else:
            msg = f"My position: {self.local_lat:.5f}, {self.local_lon:.5f}"
        stdscr.addnstr(topo_row, 0, msg.ljust(maxw), maxw)

        self._draw_message_log(stdscr, start_row=h - 3, max_rows=2)

        footer = "[↑↓] select node  [Enter] send  [m] messages  [q] quit"
        stdscr.addnstr(h - 1, 0, footer.ljust(maxw), maxw)

    # ---------- MESSAGES view ----------

    def _draw_messages_view(self, stdscr):
        h, w = stdscr.getmaxyx()
        maxw = w - 1
        top = 1
        rows_avail = h - 3  # rows available for messages (excluding header+footer)

        if not self.messages:
            stdscr.addnstr(
                top,
                0,
                "(no messages yet – send from NODES view)".ljust(maxw),
                maxw,
            )
            footer = "[n] nodes view  [q] quit"
            stdscr.addnstr(h - 1, 0, footer.ljust(maxw), maxw)
            return

        # Work with the full list (or cap to last 1000 for sanity)
        msgs = self.messages[-1000:]
        total = len(msgs)

        # selected_msg_index is relative to full self.messages; remap if needed
        # If messages was truncated to last 1000, we also adjust selection
        global_total = len(self.messages)
        if global_total > 1000:
            # The oldest of 'msgs' corresponds to index (global_total - 1000)
            base_index = global_total - 1000
            # Clamp selected index into [base_index, global_total-1]
            if self.selected_msg_index < base_index:
                self.selected_msg_index = base_index
            if self.selected_msg_index >= global_total:
                self.selected_msg_index = global_total - 1
            # local index inside msgs
            selected_local = self.selected_msg_index - base_index
        else:
            # 1-1 mapping
            if self.selected_msg_index >= global_total:
                self.selected_msg_index = max(0, global_total - 1)
            selected_local = self.selected_msg_index

        # Now 'selected_local' is index into msgs[]
        # SCROLLING: adjust msg_view_offset so selected is visible
        if selected_local < self.msg_view_offset:
            self.msg_view_offset = selected_local
        elif selected_local >= self.msg_view_offset + rows_avail:
            self.msg_view_offset = selected_local - rows_avail + 1

        # Clamp msg_view_offset
        if total <= rows_avail:
            self.msg_view_offset = 0
        else:
            max_offset = total - rows_avail
            if self.msg_view_offset > max_offset:
                self.msg_view_offset = max_offset

        visible = msgs[self.msg_view_offset:self.msg_view_offset + rows_avail]

        for i, msg in enumerate(visible):
            row_y = top + i
            if row_y >= h - 1:
                break

            # figure out whether this row is the selected row
            if global_total > 1000:
                base_index = global_total - 1000
                msg_global_index = base_index + self.msg_view_offset + i
            else:
                msg_global_index = self.msg_view_offset + i

            selected = (msg_global_index == self.selected_msg_index)

            attrs = curses.A_NORMAL
            if selected:
                attrs |= curses.A_REVERSE

            t_str = time.strftime("%H:%M:%S", time.localtime(msg.timestamp))
            dir_str = "→" if msg.direction == "OUT" else "←"
            from_str = f"{msg.from_id[-8:]}({msg.from_short})"
            to_str = f"{msg.to_id[-8:]}({msg.to_short})"
            status = msg.status

            line_prefix = f"{t_str} {dir_str} {from_str}->{to_str} [{status}] "
            text_space = max(10, maxw - len(line_prefix) - 1)
            text = safe_text(msg.text)[:text_space]
            line = (line_prefix + text).ljust(maxw)

            stdscr.addnstr(row_y, 0, line, maxw, attrs)

        footer = "[↑↓] scroll / select  [n] nodes view  [q] quit"
        stdscr.addnstr(h - 1, 0, footer.ljust(maxw), maxw)

    # ---------- Input handling ----------

    def _start_input(self, target_num: int):
        self.input_mode = True
        self.input_text = ""
        self.input_target_num = target_num

    def _handle_input_char(self, ch):
        if ch in (curses.KEY_ENTER, 10, 13):
            if self.input_target_num is not None and self.input_text.strip():
                self.send_text_to_node(self.input_target_num, self.input_text.strip())
            self.input_mode = False
            self.input_text = ""
            self.input_target_num = None
            return
        if ch in (27,):  # ESC
            self.input_mode = False
            self.input_text = ""
            self.input_target_num = None
            return
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            self.input_text = self.input_text[:-1]
            return
        if 32 <= ch <= 126:
            self.input_text += chr(ch)

    def _handle_nodes_keys(self, ch):
        nodes_list = list(self.nodes.values())
        nodes_list.sort(key=lambda n: (not n.is_me, -(n.last_seen or 0)))

        if ch == curses.KEY_UP:
            self.selected_node_index = max(0, self.selected_node_index - 1)
        elif ch == curses.KEY_DOWN:
            self.selected_node_index = min(
                max(0, len(nodes_list) - 1), self.selected_node_index + 1
            )
        elif ch in (curses.KEY_ENTER, 10, 13):
            if nodes_list:
                target = nodes_list[self.selected_node_index]
                self._start_input(target.num)

    def _handle_msgs_keys(self, ch):
        total = len(self.messages)
        if total == 0:
            return

        if ch == curses.KEY_UP:
            self.selected_msg_index = max(0, self.selected_msg_index - 1)
        elif ch == curses.KEY_DOWN:
            self.selected_msg_index = min(total - 1, self.selected_msg_index + 1)

    # ---------- curses main loop ----------

    def run_curses(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)    # low battery
        curses.init_pair(3, curses.COLOR_YELLOW, -1) # my node

        while self.running:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            if h < 10 or w < 60:
                msg = "meshtop: enlarge terminal (>=60x10). Press q to quit."
                maxw = max(1, w - 1)
                stdscr.addnstr(0, 0, msg[:maxw].ljust(maxw), maxw)
                stdscr.refresh()
                ch = stdscr.getch()
                if ch in (ord("q"), ord("Q")):
                    self.running = False
                    break
                continue

            if self.input_mode:
                if self.view_mode == "NODES":
                    self._draw_header(stdscr, "meshtop – NODES (message input)")
                    self._draw_nodes_view(stdscr)
                else:
                    self._draw_header(stdscr, "meshtop – MESSAGES (message input)")
                    self._draw_messages_view(stdscr)

                maxw = w - 1
                prompt = "Message (Enter=send, Esc=cancel): "
                txt = safe_text(self.input_text)
                line = (prompt + txt)[:maxw].ljust(maxw)
                stdscr.addnstr(h - 2, 0, line, maxw)
                stdscr.move(h - 2, min(maxw - 1, len(prompt) + len(txt)))
            else:
                if self.view_mode == "NODES":
                    self._draw_header(stdscr, "meshtop – NODES (m messages, q quit)")
                    self._draw_nodes_view(stdscr)
                else:
                    self._draw_header(stdscr, "meshtop – MESSAGES (n nodes, q quit)")
                    self._draw_messages_view(stdscr)

            stdscr.refresh()
            ch = stdscr.getch()
            if ch == -1:
                continue

            if self.input_mode:
                self._handle_input_char(ch)
                continue

            if ch in (ord("q"), ord("Q")):
                self.running = False
                break
            if ch in (ord("m"), ord("M")):
                self.view_mode = "MSGS"
                continue
            if ch in (ord("n"), ord("N")):
                self.view_mode = "NODES"
                continue

            if self.view_mode == "NODES":
                self._handle_nodes_keys(ch)
            else:
                self._handle_msgs_keys(ch)

    # ---------- Public run ----------

    def run(self):
        self.connect()
        curses.wrapper(self.run_curses)


def main():
    parser = argparse.ArgumentParser(description="Meshtastic live curses viewer")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=4403)
    args = parser.parse_args()

    app = MeshTopApp(args.host, args.port)
    app.run()


if __name__ == "__main__":
    main()
