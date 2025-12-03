#!/usr/bin/env python3
import argparse
import curses
import math
import time
import threading
import json
from meshtastic import mesh_pb2
from google.protobuf.json_format import MessageToDict
import logging
logging.getLogger("meshtastic").setLevel(logging.WARNING)
logging.getLogger("pubsub").setLevel(logging.WARNING)
import re
import unicodedata

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from pubsub import pub
from meshtastic.tcp_interface import TCPInterface
from archiver import MessageArchiver



# ---------- Data classes ----------

@dataclass
class NodeInfo:
    num: int
    node_id: str = ""  # "!abcd1234"
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
    direction: str  # "IN" or "OUT"
    from_id: str
    from_short: str
    to_id: str
    to_short: str
    text: str
    portnum: str = "TEXT_MESSAGE_APP"
    status: str = "UNKNOWN"  # RECEIVED / SENT
    pkt_id: Optional[int] = None


# ---------- Helpers ----------
def looks_like_human_text(s: str) -> bool:
    """
    Keep real text in any language. Drop binary-ish noise.
    - Accept letters/numbers from all scripts, whitespace, punctuation & symbols.
    - Reject if too many 'other' chars or very long repeated runs.
    """
    if not s:
        return False
    n = len(s)

    # long repeated runs of the same char (e.g., "%%%%%%%%%%%%%")
    if re.search(r'(.)\1{8,}', s):
        return False

    letters = digits = ok = 0
    for ch in s:
        cat = unicodedata.category(ch)  # e.g. 'Ll', 'Nd', 'Zs', 'Po', 'So'
        if cat[0] in ('L', 'N'):      # letters & numbers — keep (all languages)
            letters += (cat[0] == 'L')
            digits  += (cat[0] == 'N')
            ok += 1
        elif cat[0] in ('Z', 'P', 'S'):  # spaces, punctuation, symbols (includes emoji)
            ok += 1
        elif ch in '\t':
            ok += 1

    # Heuristics:
    ratio_ok = ok / n
    ratio_letters = letters / n
    # If most chars are acceptable, and at least a few are letters, keep it
    if ratio_ok >= 0.70 and (letters >= 3 or ratio_letters >= 0.15):
        return True
    return False

def _wrap_hops(names: List[str], snrs_raw: List[Optional[int]], maxw: int,
               indent: str = "  ", hops_per_line: int = 1) -> List[str]:
    """
    Emit compact, wrapped hop lines like:
      [ME] → [A] (4.5dB)
      [A]  → [B] (?dB)
    Set hops_per_line=1 to guarantee a newline after each hop.
    """
    def db(i):
        if i >= len(snrs_raw) or snrs_raw[i] in (None, -128):
            return "?dB"
        return f"{snrs_raw[i] / 4.0:.1f}dB"

    lines = []
    buf = indent
    count = 0

    for i in range(len(names) - 1):
        piece = f"[{names[i]}] → [{names[i+1]}] ({db(i)})"
        # hard wrap if the next piece would exceed screen width
        next_buf = (buf + ("" if count == 0 else "  ") + piece)
        if count and (len(next_buf) > maxw or count >= hops_per_line):
            lines.append(buf)
            buf = indent + piece
            count = 1
        else:
            buf = next_buf
            count += 1

    if len(names) == 1:
        lines.append(indent + f"[{names[0]}]")
    elif buf.strip():
        lines.append(buf)

    return lines

def _db_from_raw(v: Optional[int]) -> Optional[float]:
    """Meshtastic SNR is quarter-dB ints; -128 means 'not reported'."""
    if v is None or v == -128:
        return None
    return v / 4.0

def _hops_line(names: List[str], snrs_raw: List[Optional[int]], maxw: int) -> str:
    """
    Build a single-line hop string like:
    [EYE] ──( ? dB )── [RtrC] ──( 4.5 dB )── [DEST]
    Clips to maxw.
    """
    parts = []
    for i, name in enumerate(names):
        parts.append(f"[{name}]")
        if i < len(names) - 1:
            db = _db_from_raw(snrs_raw[i] if i < len(snrs_raw) else None)
            snr_txt = f"{db:.1f} dB" if db is not None else "? dB"
            parts.append(f" ──( {snr_txt} )── ")
    line = "".join(parts)
    return line[:maxw]

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

def _norm(x) -> str:
    """Normalize Meshtastic IDs: strip '!' / '#' and ensure string."""
    return (str(x) if x is not None else '').replace('!', '').replace('#', '')
def _msg_involves_node(msg: MessageInfo, node: NodeInfo, local_id: Optional[str], local_num: Optional[int]) -> bool:
    """
    True if this message is between ME and the given node (either direction).
    We match on the node_id or the numeric node number (e.g., '#12').
    """
    nid = _norm(node.node_id)
    nnum = str(node.num)
    return _norm(msg.from_id) in (nid, nnum) or _norm(msg.to_id) in (nid, nnum)



# ---------- Main app ----------

class MeshTopApp:
    def _handle_trace_keys(self, ch):
        if ch in (ord('t'), ord('T')):
            if time.time() < self.trace_cooldown_end or self._trace_active():
                with self.trace_lock:
                    remain = max(0, int(self.trace_cooldown_end - time.time()))
                    self.trace_result = self.trace_result or {}
                    self.trace_result["error"] = f"TRACE busy (wait {remain}s)"
                return
            self._send_trace_to_current()

    def _do_traceroute(self, dest, hop_limit=7, channel_index=0):
        """
        Runs in a background thread so the curses UI never blocks.
        """
        try:
            # Make the radio call (this can block)
            if hasattr(self.iface, "sendTraceRoute"):
                self.iface.sendTraceRoute(dest=dest, hopLimit=hop_limit, channelIndex=channel_index)
            elif hasattr(self.iface, "traceroute"):
                self.iface.traceroute(dest=dest, hopLimit=hop_limit, channelIndex=channel_index)
            else:
                raise AttributeError("Traceroute not supported by this meshtastic-python.")
        except Exception as e:
            with self.trace_lock:
                if self.trace_result is not None:
                    self.trace_result["error"] = f"TRACE SEND ERROR: {e}"

    def _trace_active(self) -> bool:
        """True while we're waiting for a result and haven't timed out yet."""
        with self.trace_lock:
            tr = self.trace_result or {}
            if tr.get("error"):
                return False
            ts = tr.get("ts")
            route = tr.get("route") or []
        return bool(ts) and not route and time.time() < self.trace_deadline

    def _send_trace_to_current(self):
        # record start time for timeout logic
        self.trace_started_at = time.time()

        if self.trace_target_num is None:
            return
        target = self.nodes.get(self.trace_target_num)
        if not target:
            return

        now = time.time()

        # Respect cooldown or an active in-flight attempt
        if now < self.trace_cooldown_end or self._trace_active():
            with self.trace_lock:
                remain = max(0, int(self.trace_cooldown_end - now))
                self.trace_result = self.trace_result or {}
                self.trace_result["error"] = f"TRACE suppressed (cooldown {remain}s)"
            return

        # seed UI state
        with self.trace_lock:
            self.trace_result = {
                "dest_num": target.num,
                "route": [],
                "snr_towards": [],
                "snr_back": [],
                "ts": now,
                "error": None,
            }

        # set timeout and cooldown windows
        self.trace_deadline = now + self.trace_timeout_sec
        self.trace_cooldown_end = now + self.trace_cooldown_sec

        dest = target.node_id or target.num  # prefer node_id
        # launch background worker
        self.trace_thread = threading.Thread(
            target=self._do_traceroute, args=(dest, 7, 0), daemon=True
        )
        self.trace_thread.start()

    def __init__(self, host: str, port: int = 4403):
        self.hide_gibberish = True  # default on; press 'g' to toggle

        # --- trace timing (auto-timeout + cooldown) ---
        self.trace_timeout_sec = 30.0  # how long we wait for a result
        self.trace_cooldown_sec = 3.0  # minimum pause between traces
        self.trace_started_at: Optional[float] = None

        self.host = host
        self.port = port
        self.iface: Optional[TCPInterface] = None
        self.trace_target_num: Optional[int] = None
        self.trace_result = {
            "dest_num": None,
            "route": [],  # list of node numbers (hops, newest result)
            "snr_towards": [],  # optional SNR toward dest (fw dependent)
            "snr_back": [],  # optional SNR on return (fw dependent)
            "ts": None,
            "error": None,
        }

        self.nodes: Dict[int, NodeInfo] = {}
        self.messages: List[MessageInfo] = []

        # message archiver
        self.archiver = MessageArchiver(max_size_mib=5)
        # load recent history (up to 1000 lines)
        self._load_archived_messages(max_lines=1000)

        # local node info (static)
        self.local_num: Optional[int] = None
        self.local_id: Optional[str] = None
        self.local_short: str = "ME"
        self.local_long: str = "MyNode"
        self.local_lat: Optional[float] = None
        self.local_lon: Optional[float] = None

        # UI state
        self.running = True
        self.view_mode = "NODES"  # or "MSGS"
        self.selected_node_index = 0
        self.selected_msg_index = 0

        # scrolling offsets
        self.node_view_offset = 0
        self.msg_view_offset = 0

        self.input_mode = False
        self.input_text = ""
        self.input_target_num: Optional[int] = None
        self.trace_thread = None
        self.trace_lock = threading.Lock()

        # ---- add these ----
        self.trace_deadline = 0.0
        self.trace_cooldown_end = 0.0

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

        # --- normalize portnum to a string name ---
        pn = d.get("portnum", "")
        try:
            if isinstance(pn, int):
                pn_name = mesh_pb2.PortNum.Name(pn)
            elif isinstance(pn, str):
                pn_name = pn
            else:
                pn_name = ""
        except Exception:
            pn_name = str(pn)

        # ============================================================
        # 1) HANDLE ROUTING/TRACEROUTE FIRST (populate self.trace_result)
        # ============================================================

        # Some firmwares send "NO_RESPONSE" via ROUTING_APP
        if pn_name == "ROUTING_APP":
            routing = d.get("routing", {})
            if routing.get("errorReason") == "NO_RESPONSE":
                with self.trace_lock:
                    if self.trace_result and self.trace_result.get("dest_num") is not None:
                        self.trace_result["error"] = "NO_RESPONSE"
                self.trace_deadline = 0.0
                self.trace_cooldown_end = time.time() + 0.5

        # Traceroute / RouteDiscovery can arrive under several names or even as a raw int
        looks_like_trace = pn_name in {
            "TRACEROUTE_APP", "ROUTE_DISCOVERY_APP", "TRACE_ROUTE_APP", "ROUTE_TRACE_APP"
        } or (isinstance(pn, int) and pn in (9, 48, 49))  # 9 is TRACEROUTE_APP in current enums

        if looks_like_trace:
            try:
                payload = d.get("payload")
                rd_dict = None

                # Case A: some builds include a decoded dict in the message
                for k in ("routeDiscovery", "traceroute", "route_tracer", "route"):
                    v = d.get(k)
                    if isinstance(v, dict) and any(
                            x in v for x in ("route", "snrTowards", "snrBack", "snr_forward", "snr_return")):
                        rd_dict = v
                        break

                # Case B: payload is a protobuf RouteDiscovery
                if rd_dict is None and isinstance(payload, (bytes, bytearray)):
                    rd = mesh_pb2.RouteDiscovery()
                    rd.ParseFromString(payload)
                    rd_dict = MessageToDict(rd)

                if rd_dict:
                    def _ints(v):
                        if not v:
                            return []
                        return v if all(isinstance(x, int) for x in v) else [int(x) for x in v]

                    route = _ints(rd_dict.get("route"))
                    snr_towards = _ints(rd_dict.get("snrTowards") or rd_dict.get("snr_forward"))
                    snr_back = _ints(rd_dict.get("snrBack") or rd_dict.get("snr_return"))

                    far_end = route[-1] if route else packet.get("from")
                    with self.trace_lock:
                        self.trace_result = {
                            "dest_num": far_end,
                            "route": route,
                            "snr_towards": snr_towards,
                            "snr_back": snr_back,
                            "ts": time.time(),
                            "error": None,
                        }
                    # allow quick re-run
                    self.trace_deadline = 0.0
                    self.trace_cooldown_end = time.time() + 0.5
            except Exception as _e:
                with self.trace_lock:
                    self.trace_result = self.trace_result or {}
                    self.trace_result["error"] = f"PARSE ERROR: {_e}"
                self.trace_deadline = 0.0
                self.trace_cooldown_end = time.time() + 0.5

        # ============================================================
        # 2) AFTER traceroute handling, skip non-text for the message log
        # ============================================================
        if pn_name and pn_name != "TEXT_MESSAGE_APP":
            return

        # 3) Plain text for message log (with gibberish filter)
        text = d.get("text")
        if text is None:
            payload = d.get("payload")
            if isinstance(payload, (bytes, bytearray)):
                try:
                    text = payload.decode("utf-8", errors="replace")
                except Exception:
                    text = None
        if text is None:
            return

        text = safe_text(text)
        if self.hide_gibberish and not looks_like_human_text(text):
            return

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

        self.messages.append(
            MessageInfo(
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
        )

        # best-effort archive
        try:
            self.archiver.write({
                'ts_iso': MessageArchiver.now_iso(),
                'direction': 'IN' if status == 'RECEIVED' else 'OUT',
                'from_id': from_id,
                'from_name': from_short,
                'to_id': to_id,
                'broadcast': False,
                'text': text,
                'msg_id': packet.get('id'),
                'ack': (status == 'RECEIVED' and d.get('requestId') is None),
                'hop_limit': packet.get('hopLimit'),
                'rssi': packet.get('rxRssi'),
                'snr': packet.get('rxSnr'),
                'channel': packet.get('channel', 0),
                'errors': []
            })
        except Exception:
            pass

    # ---------- Sending text ----------

    def send_text_to_node(self, target_num: int, text: str):
        # best-effort outbound log (pre-send)
        try:
            to_id = self.nodes.get(target_num).node_id if self.nodes.get(target_num) else f'#{target_num}'
            self.archiver.write({
                'ts_iso': MessageArchiver.now_iso(),
                'direction': 'OUT',
                'from_id': self.local_id or '^local',
                'from_name': self.local_short,
                'to_id': to_id,
                'broadcast': False,
                'text': text,
                'msg_id': None,
                'ack': False,
                'hop_limit': None,
                'rssi': None,
                'snr': None,
                'channel': 0,
                'errors': []
            })
        except Exception as _e:
            import sys
            print(f"[archiver] outbound pre-log failed: {_e}", file=sys.stderr)
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

        msgs = list(reversed(self.messages[-max_rows:]))
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

            # build the rendered line
            # build the rendered line
            # build the rendered line
            line = (prefix + text).ljust(maxw)

            # highlight "to me" rows using the SAME mechanism as NODES (pair 3)
            # highlight ONLY messages explicitly sent TO *my* node
            attrs = 0
            to_me = False
            if (msg.direction or "").upper() == "IN":  # must be inbound
                norm_to = _norm(msg.to_id)
                if (self.local_id and norm_to == _norm(self.local_id)) \
                        or (self.local_num is not None and norm_to == str(self.local_num)) \
                        or ((msg.to_short or "") and (self.local_short or "") and msg.to_short == self.local_short):
                    to_me = True

            # only color in the full MESSAGES window; reuse pair(3) that already works in NODES
            if self.view_mode == "MSGS" and to_me and curses.has_colors():
                attrs |= curses.color_pair(3) | curses.A_BOLD

            # IMPORTANT: pass attrs as the 5th argument
            stdscr.addnstr(row_y, 0, line, maxw, attrs)



    # ---------- NODES view ----------
    def _format_hops_table(self, names: List[str], snrs_raw: List[Optional[int]], maxw: int, title: str) -> List[str]:
        def snr_txt(i: int) -> str:
            if i >= len(snrs_raw) or snrs_raw[i] in (None, -128):
                return "? dB"
            return f"{snrs_raw[i] / 4.0:.1f} dB"

        col_no, col_from, col_to, col_snr = 3, 14, 14, 8
        rule = "─" * min(maxw, col_no + 1 + col_from + 1 + 1 + 1 + col_to + 2 + col_snr)
        lines = [title[:maxw], rule]

        for i in range(len(names) - 1):
            left = names[i][:col_from].ljust(col_from)
            right = names[i + 1][:col_to].ljust(col_to)
            snr = snr_txt(i).rjust(col_snr)
            row = f"{str(i + 1).rjust(col_no - 1)} {left} → {right}  {snr}"
            lines.append(row[:maxw])

        if len(names) == 1:
            lines.append(f"{'1'.rjust(col_no - 1)} {names[0][:col_from].ljust(col_from)}")

        return lines

    def _draw_trace_view(self, stdscr):
        h, w = stdscr.getmaxyx()
        maxw = w - 1
        top = 1
        for i in range(1, h - 1):
            stdscr.move(i, 0)
            stdscr.clrtoeol()
        # snapshot trace state
        with self.trace_lock:
            tr = dict(self.trace_result) if self.trace_result is not None else {}




        if self.trace_target_num is None or self.trace_target_num not in self.nodes:
            stdscr.addnstr(top, 0, "No node selected for trace.".ljust(maxw), maxw)
            stdscr.addnstr(h - 1, 0, "[n] nodes  [m] messages  [q] quit".ljust(maxw), maxw)
            return

        node = self.nodes[self.trace_target_num]
        target_name = node.short_name or node.long_name or node.node_id or f"#{node.num}"
        stdscr.addnstr(top, 0, f"TRACE → {target_name}".ljust(maxw), maxw, curses.A_BOLD)

        started = tr.get("ts") or time.time()
        elapsed = time.time() - started
        status = "OK" if tr.get("route") else ("Error" if tr.get("error") else "Waiting…")
        stdscr.addnstr(top + 1, 0, f"Status: {status}    Elapsed: {elapsed:.1f}s".ljust(maxw), maxw)

        route = tr.get("route", []) or []
        snr_fwd = tr.get("snr_towards", []) or []
        snr_back = tr.get("snr_back", []) or []
        err = tr.get("error")

        y = top + 3

        # Waiting / Error
        if err or not route:
            if err:
                stdscr.addnstr(y, 0, f"Error: {err}".ljust(maxw), maxw)
                y += 1
                stdscr.addnstr(y, 0, "Press 't' to retry, or 'n' to return to nodes.".ljust(maxw), maxw)
                y += 2
            else:
                remain = max(0.0, self.trace_timeout_sec - (time.time() - started))
                stdscr.addnstr(y, 0, f"Auto-return in ~{remain:.1f}s (press 't' to retry)".ljust(maxw), maxw)
                y += 1
                if remain is not None:

                    y += 1
                stdscr.addnstr(y, 0, "No traceroute result yet.".ljust(maxw), maxw)
                y += 2

            stdscr.addnstr(h - 1, 0, "[t] retry  [n] nodes  [m] messages  [q] quit".ljust(maxw), maxw)
            return

        # Build hop names
        def name_from_num(n):
            n = int(n)
            if n in self.nodes:
                nn = self.nodes[n]
                return nn.short_name or nn.node_id or f"#{n}"
            return f"#{n}"

        names = [name_from_num(n) for n in route]
        if self.local_num is not None and route and route[0] == self.local_num:
            names[0] = self.local_short or "ME"

        # To destination
        # To destination

        for line in self._format_hops_table(names, snr_fwd, maxw, "Route to destination"):
            stdscr.addnstr(y, 0, line.ljust(maxw), maxw);
            y += 1

        y += 1

        stdscr.addnstr(y, 0, "Route back to us".ljust(maxw), maxw, curses.A_BOLD);
        y += 1
        if snr_back and len(names) >= 2:
            back_names = list(reversed(names))
            for line in self._format_hops_table(back_names, snr_back, maxw, ""):
                stdscr.addnstr(y, 0, line.ljust(maxw), maxw);
                y += 1
        else:
            stdscr.addnstr(y, 0, "  (no return SNRs reported)".ljust(maxw), maxw);
            y += 1

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
        msgs = list(reversed(self.messages[-1000:]))
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

            # highlight "to me" using the SAME attrs pattern you use in NODES
            # highlight ONLY messages explicitly sent TO *my* node
            to_me = False
            if (msg.direction or "").upper() == "IN":  # must be inbound
                norm_to = _norm(msg.to_id)
                if (self.local_id and norm_to == _norm(self.local_id)) \
                        or (self.local_num is not None and norm_to == str(self.local_num)) \
                        or ((msg.to_short or "") and (self.local_short or "") and msg.to_short == self.local_short):
                    to_me = True

            if curses.has_colors() and to_me:
                attrs |= curses.color_pair(3) | curses.A_BOLD

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
        elif ch in (ord('t'), ord('T')):
            if nodes_list:
                target = nodes_list[self.selected_node_index]
                self.trace_target_num = target.num
                self.view_mode = "TRACE"
                self._send_trace_to_current()






    def _handle_msgs_keys(self, ch):
        total = len(self.messages)
        if total == 0:
            return

        if ch == curses.KEY_UP:
            self.selected_msg_index = max(0, self.selected_msg_index - 1)
        elif ch == curses.KEY_DOWN:
            self.selected_msg_index = min(total - 1, self.selected_msg_index + 1)

    # ---------- curses main loop ----------

    def _load_archived_messages(self, max_lines: int = 1000):
        """
        Load recent messages from JSONL archives in the current directory
        and prepend them to self.messages as historical context.
        """
        try:
            base_dir = Path(__file__).resolve().parent
        except Exception:
            base_dir = Path(".").resolve()
        files = sorted(list(base_dir.glob("messages.jsonl")) + list(base_dir.glob("messages.*.jsonl")))
        if not files:
            return
        # Read from newest to oldest until max_lines collected
        needed = max_lines
        buf = []
        for f in reversed(files):
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    # Efficiently collect last N lines from each file if still needed
                    lines = fh.readlines()
                    if not lines:
                        continue
                    take = min(len(lines), needed)
                    buf.extend(lines[-take:])
                    needed -= take
                    if needed <= 0:
                        break
            except Exception:
                continue

        # Oldest first for chronological order
        buf = buf[-max_lines:]
        for line in buf:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            # Map to MessageInfo
            direction = rec.get("direction", "in").upper()
            from_id = rec.get("from_id") or "^unknown"
            to_id = rec.get("to_id") or "^unknown"
            from_short = rec.get("from_name") or from_id
            to_short = rec.get("to_name") or to_id
            text = rec.get("text") or ""
            ts = rec.get("ts_iso")
            # convert ts to epoch if present
            try:
                import datetime
                from datetime import datetime as _dt
                # Attempt parsing ISO
                if isinstance(ts, str):
                    if ts.endswith("Z"):
                        ts = ts[:-1] + "+00:00"
                    epoch = _dt.fromisoformat(ts).timestamp()
                else:
                    epoch = time.time()
            except Exception:
                epoch = time.time()
            msg = MessageInfo(
                timestamp=epoch,
                direction=direction,
                from_id=from_id,
                from_short=from_short,
                to_id=to_id,
                to_short=to_short,
                text=safe_text(text),
                portnum="TEXT_MESSAGE_APP",
                status="RECEIVED" if direction == "IN" else "SENT",
                pkt_id=rec.get("msg_id")
            )
            self.messages.append(msg)

    def run_curses(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)

        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            # init pairs (use 4 for blue to avoid clashes)
            try:
                curses.init_pair(1, curses.COLOR_RED, -1)  # low battery
                curses.init_pair(3, curses.COLOR_YELLOW, -1)  # my node
                curses.init_pair(4, curses.COLOR_BLUE, -1)  # messages to me (pair 4)
            except curses.error:
                pass

        while self.running:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            # --- auto-timeout from TRACE ---
            if self.view_mode == "TRACE":
                with self.trace_lock:
                    tr = dict(self.trace_result) if self.trace_result is not None else {}
                started = tr.get("ts") or self.trace_started_at
                no_result_yet = not tr.get("route") and not tr.get("error")
                if started and no_result_yet:
                    elapsed = time.time() - started
                    if elapsed >= self.trace_timeout_sec:
                        with self.trace_lock:
                            if self.trace_result is not None and not self.trace_result.get("error"):
                                self.trace_result["error"] = "Timeout (no response)"
                        # if the background call is still stuck, we still return to NODES;
                        # the UI won't block and the thread will end whenever it ends.
                        self.view_mode = "NODES"

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
                    self._draw_header(stdscr, "meshtop – NODES (m messages, t trace, q quit)")
                    self._draw_nodes_view(stdscr)
                elif self.view_mode == "TRACE":
                    self._draw_header(stdscr, "meshtop – TRACE (t retry, n nodes, m messages, q quit)")
                    self._draw_trace_view(stdscr)
                else:
                    # where you draw the MESSAGES header:
                    self._draw_header(stdscr,
                                      f"meshtop – MESSAGES (n nodes, q quit)  [g] filter={'ON' if self.hide_gibberish else 'OFF'}")

                    self._draw_messages_view(stdscr)

                maxw = w - 1
                prompt = "Message (Enter=send, Esc=cancel): "
                txt = safe_text(self.input_text)
                line = (prompt + txt)[:maxw].ljust(maxw)
                stdscr.addnstr(h - 2, 0, line, maxw)
                stdscr.move(h - 2, min(maxw - 1, len(prompt) + len(txt)))
            else:
                if self.view_mode == "NODES":
                    self._draw_header(stdscr, "meshtop – NODES (m messages, t trace, q quit)")
                    self._draw_nodes_view(stdscr)
                elif self.view_mode == "TRACE":
                    self._draw_header(stdscr, "meshtop – TRACE (t retry, n nodes, m messages, q quit)")
                    self._draw_trace_view(stdscr)
                else:
                    # where you draw the MESSAGES header:
                    self._draw_header(stdscr,
                                      f"meshtop – MESSAGES (n nodes, q quit)  [g] filter={'ON' if self.hide_gibberish else 'OFF'}")

                    self._draw_messages_view(stdscr)



            stdscr.refresh()
            ch = stdscr.getch()
            if ch == -1:
                continue

            if self.input_mode:
                self._handle_input_char(ch)
                continue
            if ch in (ord('g'), ord('G')):
                self.hide_gibberish = not self.hide_gibberish
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
            elif self.view_mode == "TRACE":
                self._handle_trace_keys(ch)
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
