#!/usr/bin/env python3
import argparse
import curses
import math
import time
import threading
import json
import socket
import sys
import traceback
from meshtastic.protobuf import mesh_pb2, portnums_pb2
import logging
logging.getLogger("meshtastic").setLevel(logging.WARNING)
logging.getLogger("pubsub").setLevel(logging.WARNING)
log = logging.getLogger("meshtop")
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

    last_seen: float = 0.0  # heard live during this session
    seen_packets: int = 0
    db_last_heard: float = 0.0  # "lastHeard" from the radio's node DB (before we started)
    hops_away: Optional[int] = None  # from the node DB

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

    last_route: Optional[List[int]] = None
    last_route_snr_towards: Optional[List[int]] = None
    last_route_snr_back: Optional[List[int]] = None
    last_route_back: Optional[List[int]] = None
    last_route_time: Optional[float] = None

    _app_instance = None

    def trace(self, switch_view: bool = False):
        """Programmatic trace call to trigger a traceroute for this node."""
        if NodeInfo._app_instance:
            NodeInfo._app_instance.trigger_trace_to_node(self.num, switch_view=switch_view)
        else:
            raise RuntimeError("No active MeshTopApp instance registered.")


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
    channel: int = 0
    broadcast: bool = False  # channel message rather than a direct message
    ack: str = ""  # outgoing only: "pending", "relayed", "delivered" or "failed: REASON"


# ---------- Helpers ----------
def looks_like_human_text(s: str) -> bool:
    """
    Human-friendly filter:
    - Allows emoji and classic ASCII emoticons (:-), <3, :D, etc.)
    - Lenient with stretched letters ("helllllooo")
    - Blocks binary-ish garbage and extreme repeats
    - Works for any language/script without extra deps
    """
    if not s:
        return False

    s = s.replace("\x00", "")

    # Hard garbage: a single char repeated 21+ times (e.g., "%%%%%%%%%%%%%%%")
    if re.search(r'(.)\1{20,}', s):
        return False

    # Detect emoji (basic, wide coverage of modern ranges)
    emoji_re = re.compile(
        '['
        '\U0001F300-\U0001FAD6'  # Misc Symbols & Pictographs + Extended
        '\U0001F680-\U0001F6FF'  # Transport & Map
        '\U0001F900-\U0001FAFF'  # Supplemental Symbols & Pictographs
        '\U00002700-\U000027BF'  # Dingbats
        '\U00002600-\U000026FF'  # Misc Symbols
        '\U0001FA70-\U0001FAFF'  # Symbols & Pictographs Extended-A
        ']+',
        flags=re.UNICODE
    )

    # Common ASCII emoticons (keep this list compact but useful)
    emoticon_re = re.compile(
        r'(\:\-?\)|\:\-?\(|\;\-?\)|\:D|\:P|\:p|\;P|\;p|\:\-?D|\:\-?P|\:\-?p|\<3|\:\-?O|\:\-?o|\:\-?\/|\:\-?\||\^_\^)',
        flags=re.UNICODE
    )

    # Quick whitelist: any emoji or common emoticon -> probably human
    has_emoji = bool(emoji_re.search(s))
    has_emoticon = bool(emoticon_re.search(s))
    if has_emoji or has_emoticon:
        # still reject if it's *only* one repeated symbol character
        if re.fullmatch(r'[\W_]{1,}', s) and len(set(s)) == 1 and len(s) > 10:
            return False
        return True

    # Un-stretch letters/numbers for analysis so "cooooool" becomes "coool" -> "cool"
    s2 = re.sub(r'([0-9A-Za-zА-Яа-я])\1{2,}', r'\1\1', s)
    if not s2:
        return False

    n = len(s2)
    ok = 0
    letters = 0
    for ch in s2:
        cat = unicodedata.category(ch)  # L*, N*, Z*, P*, S*
        if cat[0] in ('L', 'N', 'Z', 'P', 'S'):
            ok += 1
            if cat[0] == 'L':
                letters += 1

    # Must be mostly printable/normal characters
    if ok / n < 0.60:
        return False

    # Any 3+ char token with >=2 distinct letters counts as human
    tokens = re.findall(r'\w{3,}', s2, flags=re.UNICODE)
    for t in tokens:
        t_letters = [c for c in t if unicodedata.category(c).startswith('L')]
        if len(set(t_letters)) >= 2:
            return True

    # Fallback: enough letters overall
    return (letters / n) >= 0.30


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
        piece = f"[{names[i]}] -> [{names[i+1]}] ({db(i)})"
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
    # 0.0, 0.0 indicates uninitialized or missing GPS fix in Meshtastic
    if (lat1 == 0.0 and lon1 == 0.0) or (lat2 == 0.0 and lon2 == 0.0):
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
    """Remove control chars so curses won't explode (tabs become spaces)."""
    if not s:
        return ""
    out_chars = []
    for ch in str(s):
        if ch == "\t":
            out_chars.append(" ")
        elif unicodedata.category(ch) == "Cc":
            continue
        else:
            out_chars.append(ch)
    return "".join(out_chars)


def _cell_width(ch: str) -> int:
    """Terminal cells taken by one character (emoji/CJK are 2, combining marks 0)."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(s: str) -> int:
    return sum(_cell_width(ch) for ch in s)


def clip_cells(s: str, max_cells: int) -> str:
    """Cut s so it takes at most max_cells terminal cells."""
    out, used = [], 0
    for ch in s:
        cw = _cell_width(ch)
        if used + cw > max_cells:
            break
        out.append(ch)
        used += cw
    return "".join(out)


def ljust_cells(s: str, width: int) -> str:
    """Like str.ljust, but by terminal cells, and clipping overlong text."""
    s = clip_cells(s, width)
    return s + " " * max(0, width - text_width(s))


def wrap_cells(s: str, width: int) -> List[str]:
    """Word-wrap s into lines of at most width cells."""
    width = max(1, width)
    lines: List[str] = []
    line, used = "", 0
    for word in re.split(r"(\s+)", s):
        if not word:
            continue
        ww = text_width(word)
        if used + ww <= width:
            line += word
            used += ww
            continue
        if line.strip():
            lines.append(line.rstrip())
        line, used = "", 0
        if word.isspace():
            continue
        while text_width(word) > width:  # hard-split words longer than a line
            part = clip_cells(word, width) or word[0]
            lines.append(part)
            word = word[len(part):]
        line, used = word, text_width(word)
    if line.strip() or not lines:
        lines.append(line.rstrip())
    return lines


class SafeWin:
    """
    Proxy around the curses screen that clips every write to the visible area
    (measured in terminal cells) and swallows curses.error. Writing past the
    bottom of a short terminal used to raise and kill the whole app.
    """

    def __init__(self, win):
        self._win = win

    def __getattr__(self, name):
        return getattr(self._win, name)

    def addnstr(self, y, x, s, n, attr=0):
        h, w = self._win.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w or n <= 0:
            return
        s = clip_cells(safe_text(s), min(n, w - x))
        if not s:
            return
        try:
            self._win.addstr(y, x, s, attr)
        except curses.error:
            pass  # e.g. writing the bottom-right cell; the text is drawn anyway

    def addstr(self, y, x, s, attr=0):
        self.addnstr(y, x, s, 1 << 30, attr)

    def addch(self, y, x, ch, attr=0):
        h, w = self._win.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            try:
                self._win.addch(y, x, ch, attr)
            except curses.error:
                pass

    def move(self, y, x):
        h, w = self._win.getmaxyx()
        try:
            self._win.move(max(0, min(y, h - 1)), max(0, min(x, w - 1)))
        except curses.error:
            pass


# Command keys typed on a Russian or Hebrew layout map to the Latin key at the
# same physical position, so shortcuts work without switching layouts.
_LAYOUT_TO_LATIN = {}
for _lat, _ru, _he in zip("qwertyuiopasdfghjklzxcvbnm",
                          "йцукенгшщзфывапролдячсмить",
                          "  קראטוןםפשדגכעיחלךזסבהנמצ"):
    _LAYOUT_TO_LATIN[_ru] = _lat
    _LAYOUT_TO_LATIN[_ru.upper()] = _lat.upper()
    if _he != " ":
        _LAYOUT_TO_LATIN[_he] = _lat

# Meshtastic text payload limit (bytes of UTF-8), leaving room for encryption overhead
MAX_TEXT_BYTES = 200
UNKNOWN_HOP = 0xFFFFFFFF  # firmware puts this in a route for relays it could not identify
BROADCAST_NUM = 0xFFFFFFFF


def _norm(x) -> str:
    """Normalize Meshtastic IDs: strip '!' / '#' and ensure string."""
    return (str(x) if x is not None else '').replace('!', '').replace('#', '')
BROADCAST_IDS = ("^all", "!ffffffff", "#4294967295")


def _id_to_num(node_id) -> Optional[int]:
    """'!b9e5da11' or '#12345' -> node number; None for anything else."""
    s = str(node_id or "")
    try:
        if s.startswith("!") and len(s) == 9:
            return int(s[1:], 16)
        if s.startswith("#"):
            return int(s[1:])
    except ValueError:
        pass
    return None


def fmt_ago(seconds: Optional[float]) -> str:
    """Compact age: 12s, 4m, 3h, 2d."""
    if seconds is None:
        return "--"
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _ints(v) -> List[int]:
    res = []
    for x in v or []:
        try:
            res.append(int(x))
        except (ValueError, TypeError):
            pass
    return res


# ---------- Main app ----------

class MeshTopApp:
    def _wipe_body(self, stdscr, keep_header=True, keep_footer=True):
        h, w = stdscr.getmaxyx()
        start = 1 if keep_header else 0
        end = h - 1 if keep_footer else h
        for y in range(start, end):
            stdscr.move(y, 0)
            stdscr.clrtoeol()

    def _handle_trace_keys(self, ch):
        if ch in (ord('t'), ord('T')):
            self._send_trace_to_current()

    # ---------- Traceroute plumbing ----------
    #
    # Only one traceroute is in flight at a time (manual or auto), tracked in
    # self.trace_inflight. Each request registers its own response callback with
    # the meshtastic library, keyed by packet id, so a reply (or a NO_RESPONSE
    # routing error) is always attributed to the node it was actually sent to.

    def _send_traceroute(self, dest_num: int, kind: str) -> dict:
        """Send a traceroute to dest_num; returns the in-flight record. Raises on send failure."""
        iface = self.iface
        if iface is None:
            raise RuntimeError("not connected")
        inflight = {
            "dest_num": dest_num,
            "kind": kind,  # "manual" or "auto"
            "started": time.time(),
            "deadline": time.time() + self.trace_timeout_sec,
            "done": threading.Event(),
            "ok": False,
            "req_id": None,
        }

        def on_trace_response(p, inflight=inflight):
            self._on_trace_response(p, inflight)

        if not iface.isConnected.is_set():
            raise RuntimeError("not connected")  # sendData would block the caller for minutes
        with self.trace_lock:
            if self.trace_inflight is not None:
                raise RuntimeError("another traceroute is in flight")
            self.trace_inflight = inflight
        try:
            pkt = iface.sendData(
                mesh_pb2.RouteDiscovery(),
                destinationId=dest_num,
                portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                wantResponse=True,
                onResponse=on_trace_response,
                channelIndex=0,
                hopLimit=7,
            )
            inflight["req_id"] = getattr(pkt, "id", None)
        except BaseException:  # the library may sys.exit() on bad input; never let it kill the UI
            with self.trace_lock:
                if self.trace_inflight is inflight:
                    self.trace_inflight = None
            raise
        return inflight

    def _finish_trace(self, inflight: dict, ok: bool):
        inflight["ok"] = ok
        inflight["done"].set()
        with self.trace_lock:
            if self.trace_inflight is inflight:
                self.trace_inflight = None
        iface = self.iface
        if inflight.get("req_id") is not None and iface is not None:
            try:  # drop the library's handler for a request we gave up on
                iface.responseHandlers.pop(inflight["req_id"], None)
            except Exception:
                pass

    def _on_trace_response(self, packet: dict, inflight: dict):
        """Called by the meshtastic reader thread with the reply to one of our traceroutes."""
        d = packet.get("decoded") or {}
        dest_num = inflight["dest_num"]
        is_manual_view = inflight["kind"] == "manual" or self.trace_target_num == dest_num
        if d.get("requestId") is not None:
            with self.trace_lock:
                self._handled_trace_ids.add(d["requestId"])

        rd = d.get("traceroute")
        if isinstance(rd, dict):
            route = _ints(rd.get("route"))
            snr_towards = _ints(rd.get("snrTowards"))
            route_back = _ints(rd.get("routeBack"))
            snr_back = _ints(rd.get("snrBack"))
            self._record_route(dest_num, route, snr_towards, route_back, snr_back)
            if is_manual_view:
                with self.trace_lock:
                    self.trace_result = {
                        "dest_num": dest_num,
                        "route": route,
                        "snr_towards": snr_towards,
                        "route_back": route_back,
                        "snr_back": snr_back,
                        "ts": inflight["started"],
                        "error": None,
                        "elapsed": time.time() - inflight["started"],
                    }
            self._finish_trace(inflight, ok=True)
            return

        reason = ((d.get("routing") or {}).get("errorReason")) or "unexpected reply"
        if is_manual_view and inflight["kind"] == "manual":
            self._set_trace_error(f"{reason} (no traceroute reply)", inflight["started"])
        self._finish_trace(inflight, ok=False)

    def _record_route(self, dest_num, route, snr_towards, route_back, snr_back):
        node = self._get_or_create_node(dest_num)
        node.last_route = route
        node.last_route_snr_towards = snr_towards
        node.last_route_back = route_back
        node.last_route_snr_back = snr_back
        node.last_route_time = time.time()
        self._log_trace_history(dest_num, route, snr_towards, snr_back, route_back)
        self._save_traced_stations()

    def _set_trace_error(self, msg: str, started: Optional[float] = None):
        with self.trace_lock:
            self.trace_result = dict(self.trace_result or {})
            self.trace_result["error"] = msg
            self.trace_result["elapsed"] = time.time() - (started or time.time())

    def _trace_active(self) -> bool:
        """True while any traceroute (manual or auto) is waiting for its reply."""
        with self.trace_lock:
            return self.trace_inflight is not None

    def _check_trace_timeout(self):
        """Expire the in-flight traceroute once its deadline passes (called from the UI loop)."""
        with self.trace_lock:
            inflight = self.trace_inflight
        if inflight is None or time.time() < inflight["deadline"]:
            return
        if inflight["kind"] == "manual":
            self._set_trace_error("Timeout (no response)", inflight["started"])
        self._finish_trace(inflight, ok=False)

    def _send_trace_to_current(self):
        if self.trace_target_num is None:
            return
        target = self.nodes.get(self.trace_target_num)
        if not target:
            return
        now = time.time()

        with self.trace_lock:
            inflight = self.trace_inflight
        if inflight is not None:
            who = self._node_label(inflight["dest_num"])
            if inflight["kind"] == "auto":
                self._set_trace_error(f"TRACE busy: auto-trace to {who} in flight, try again shortly")
            elif inflight["dest_num"] != target.num:
                self._set_trace_error(f"TRACE busy: still waiting for {who}")
            return  # a manual trace to this node is already running; keep waiting
        if now < self.trace_cooldown_end:
            self._set_trace_error(f"TRACE cooldown ({self.trace_cooldown_end - now:.0f}s)")
            return

        with self.trace_lock:
            self.trace_result = {
                "dest_num": target.num,
                "route": [],
                "snr_towards": [],
                "route_back": [],
                "snr_back": [],
                "ts": now,
                "error": None,
                "elapsed": None,
            }
        self.trace_started_at = now
        self.trace_cooldown_end = now + self.trace_cooldown_sec
        try:
            self._send_traceroute(target.num, "manual")
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            self._set_trace_error(f"TRACE SEND ERROR: {e}", now)

    def trigger_trace_to_node(self, node_num: int, switch_view: bool = True):
        self.trace_target_num = node_num
        if switch_view:
            self.view_mode = "TRACE"
            self._need_full_clear = True
        self._send_trace_to_current()

    def _node_label(self, num, prefer_long: bool = False) -> str:
        """Human name for a node number ('?' for the firmware's unknown-relay placeholder)."""
        if num is None:
            return "?"
        if num == UNKNOWN_HOP:
            return "?"
        if self.local_num is not None and num == self.local_num:
            return (self.local_long if prefer_long else self.local_short) or "ME"
        n = self.nodes.get(num)
        if n:
            names = (n.long_name, n.short_name) if prefer_long else (n.short_name, n.long_name)
            for name in names + (n.node_id,):
                if name and name != "?":
                    return name
        return f"#{num}"

    def __init__(self, host: str, port: int = 4403):
        NodeInfo._app_instance = self
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
        self.selected_chat_index = 0

        # scrolling offsets
        self.node_view_offset = 0
        self.msg_view_offset = 0
        self.stats_scroll_offset = 0

        self.input_mode = False
        self.input_text = ""
        self.trace_thread = None
        self.trace_lock = threading.RLock()
        self.data_lock = threading.RLock()

        # ---- add these ----
        self.trace_cooldown_end = 0.0
        self._need_full_clear = False
        self.auto_trace_active_node = None
        self.auto_trace_in_progress = False
        self.trace_inflight: Optional[dict] = None
        self._handled_trace_ids: set = set()

        # selection is tracked by node number so it doesn't jump when lists re-sort
        self.selected_node_num: Optional[int] = None
        self.selected_chat_key: Optional[tuple] = None  # ("ch", index) or ("dm", node_num)
        self.input_target: Optional[tuple] = None
        # unread tracking: messages loaded from the archive count as read
        self.last_read: Dict[tuple, float] = {}
        self.read_baseline = time.time()

        # connection supervision
        self.conn_state = "CONNECTING"
        self.conn_error = ""
        self.ui_error = ""
        self._subscribed = False
        self._keepalive_sock = None

    # ---------- Connection and initial load ----------

    def connect(self):
        self.iface = self._open_interface()
        self._load_nodedb(self.iface)
        self._load_traced_stations()
        if not self._subscribed:
            # subscribe for live updates (once; callbacks survive reconnects)
            pub.subscribe(self.on_packet, "meshtastic.receive")
            pub.subscribe(self.on_telemetry, "meshtastic.receive.telemetry")
            pub.subscribe(self.on_position, "meshtastic.receive.position")
            pub.subscribe(self.on_user, "meshtastic.receive.user")
            pub.subscribe(self.on_connection_lost, "meshtastic.connection.lost")
            self._subscribed = True
        self.conn_state = "CONNECTED"

    def _open_interface(self) -> TCPInterface:
        iface = TCPInterface(self.host, portNumber=self.port)
        self._enable_keepalive(iface)
        return iface

    def _enable_keepalive(self, iface):
        """
        TCP keepalive turns a silently dead WiFi link into a socket error within
        about a minute; otherwise recv() blocks forever and the screen just freezes.
        """
        sock = getattr(iface, "socket", None)
        if sock is None or sock is self._keepalive_sock:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
            self._keepalive_sock = sock
        except OSError:
            pass

    def on_connection_lost(self, interface=None):
        if not self.running or (interface is not None and interface is not self.iface):
            return
        self.conn_state = "LOST"
        log.warning("connection to %s lost", self.host)

    def _iface_alive(self, iface) -> bool:
        rx = getattr(iface, "_rxThread", None)
        # socket may be None for a moment while the library reconnects by itself
        return rx is not None and rx.is_alive()

    def _connection_supervisor(self):
        """
        The meshtastic reader thread dies for good on socket errors (device reboot,
        WiFi drop) and only retries once on a clean close. Watch it and rebuild
        the interface with backoff so the app keeps running instead of going stale.
        """
        delay = 5
        while self.running:
            for _ in range(20):
                if not self.running:
                    return
                time.sleep(0.1)
            iface = self.iface
            if iface is not None and self._iface_alive(iface):
                self._enable_keepalive(iface)  # the library may have re-created the socket
                if self.conn_state != "CONNECTED":
                    self.conn_state = "CONNECTED"
                delay = 5
                continue

            self.conn_state = "RECONNECTING"
            self.iface = None
            with self.trace_lock:
                inflight = self.trace_inflight
            if inflight is not None:
                if inflight["kind"] == "manual":
                    self._set_trace_error("Connection lost", inflight["started"])
                self._finish_trace(inflight, ok=False)
            if iface is not None:
                threading.Thread(target=self._close_quietly, args=(iface,), daemon=True).start()
            try:
                new = self._open_interface()
            except Exception as e:
                self.conn_error = str(e) or e.__class__.__name__
                log.warning("reconnect to %s failed: %s (retry in %ss)", self.host, self.conn_error, delay)
                for _ in range(delay * 10):
                    if not self.running:
                        return
                    time.sleep(0.1)
                delay = min(delay * 2, 60)
                continue
            if not self.running:
                self._close_quietly(new)
                return
            self.iface = new
            self.conn_error = ""
            self._load_nodedb(new)
            self.conn_state = "CONNECTED"
            log.info("reconnected to %s", self.host)
            delay = 5

    @staticmethod
    def _close_quietly(iface):
        try:
            iface.close()
        except BaseException:
            pass

    def _load_nodedb(self, iface):
        info = iface.myInfo
        if info:
            # try both attribute and dict styles
            self.local_num = getattr(info, "my_node_num", None) \
                             or (isinstance(info, dict) and info.get("my_node_num")) or self.local_num
            self.local_id = getattr(info, "my_node_id", None) \
                            or getattr(info, "myNodeId", None) \
                            or (isinstance(info, dict) and (info.get("my_node_id") or info.get("myNodeId"))) \
                            or self.local_id

        # Pre-load nodeDB so we have names immediately
        for num, n in list((iface.nodesByNum or {}).items()):
            node = self._get_or_create_node(num)
            if isinstance(n, dict):
                node.db_last_heard = n.get("lastHeard") or node.db_last_heard
                node.hops_away = n.get("hopsAway", node.hops_away)
                if node.last_snr is None:
                    node.last_snr = n.get("snr")

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

        # if my node wasn't present, synthesize it
        if self.local_num is not None and self.local_num not in self.nodes:
            n = NodeInfo(num=self.local_num, is_me=True)
            n.node_id = self.local_id or n.node_id
            n.short_name = self.local_short
            n.long_name = self.local_long
            n.lat = self.local_lat
            n.lon = self.local_lon
            with self.data_lock:
                self.nodes[self.local_num] = n

        self._update_all_distances()

    # ---------- Node helpers ----------

    def _get_or_create_node(self, num: int) -> NodeInfo:
        with self.data_lock:
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
        with self.data_lock:
            nodes = list(self.nodes.values())
        for node in nodes:
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

        node.battery = dev.get("batteryLevel", node.battery)
        node.voltage = dev.get("voltage", node.voltage)
        node.channel_util = dev.get("channelUtilization", node.channel_util)
        node.air_util = dev.get("airUtilTx", node.air_util)
        node.battery_low = node.battery is not None and node.battery <= 20

        node.temperature = env.get("temperature", node.temperature)
        node.humidity = env.get("relativeHumidity", node.humidity)

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
                from meshtastic.protobuf import portnums_pb2
                pn_name = portnums_pb2.PortNum.Name(pn)
            elif isinstance(pn, str):
                pn_name = pn
            else:
                pn_name = ""
        except Exception:
            pn_name = str(pn)

        # Replies to our own traceroutes are handled by _on_trace_response (registered
        # per request). Here we only pick up traceroute replies addressed to our node
        # for requests someone else made (e.g. the phone app on the same radio).
        if pn_name == "TRACEROUTE_APP":
            rd = d.get("traceroute")
            req = d.get("requestId")
            with self.trace_lock:
                ours = req in self._handled_trace_ids
                self._handled_trace_ids.discard(req)
            if (isinstance(rd, dict) and req is not None and not ours and num is not None
                    and self.local_num is not None and packet.get("to") == self.local_num):
                self._record_route(num, _ints(rd.get("route")), _ints(rd.get("snrTowards")),
                                   _ints(rd.get("routeBack")), _ints(rd.get("snrBack")))
            return

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
        is_broadcast = to_num == UNKNOWN_HOP  # 0xFFFFFFFF is also the broadcast address
        # don't invent a node for the broadcast address
        to_node = self._get_or_create_node(to_num) if to_num is not None and not is_broadcast else None

        from_id = from_node.node_id if from_node and from_node.node_id else f"#{from_num}"
        to_id = "^all" if is_broadcast else (to_node.node_id if to_node and to_node.node_id else f"#{to_num}")
        from_short = from_node.short_name if from_node else "?"
        to_short = "ALL" if is_broadcast else (to_node.short_name if to_node else "?")

        direction = "IN"
        if self.local_num is not None and from_num == self.local_num:
            direction = "OUT"
        status = "RECEIVED" if direction == "IN" else "SENT"

        with self.data_lock:
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
                    channel=packet.get("channel", 0) or 0,
                    broadcast=is_broadcast,
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
                'to_name': to_short,
                'broadcast': is_broadcast,
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

    def send_text(self, target: tuple, text: str):
        """Send to a conversation: ("dm", node_num) or ("ch", channel_index)."""
        kind, ident = target
        broadcast = kind == "ch"
        if broadcast:
            dest, channel = BROADCAST_NUM, ident
            to_id, to_short = "^all", self._channel_name(ident)
        else:
            with self.data_lock:
                node = self.nodes.get(ident)
            dest, channel = ident, 0
            to_id = (node.node_id if node and node.node_id else f"#{ident}")
            to_short = (node.short_name if node and node.short_name else "?")
        clean_text = safe_text(text)
        while len(clean_text.encode("utf-8")) > MAX_TEXT_BYTES:
            clean_text = clean_text[:-1]

        msg = MessageInfo(
            timestamp=time.time(), direction="OUT",
            from_id=self.local_id or "^local", from_short=self.local_short,
            to_id=to_id, to_short=to_short, text=clean_text, status="SENT",
            channel=channel, broadcast=broadcast, ack="pending")

        iface = self.iface
        if iface is None or not iface.isConnected.is_set():
            msg.status, msg.ack = "UNKNOWN", "failed: not connected"
            with self.data_lock:
                self.messages.append(msg)
            return

        # The library only passes plain ACKs to a callback with this exact name.
        def onAckNak(p, msg=msg, dest=dest):
            err = ((p.get("decoded") or {}).get("routing") or {}).get("errorReason", "NONE")
            if err != "NONE":
                msg.ack = f"failed: {err}"
            elif not msg.broadcast and p.get("from") == dest:
                msg.ack = "delivered"  # the destination itself acknowledged
            else:
                msg.ack = "relayed"  # implicit ACK: a neighbour rebroadcast it
                req = (p.get("decoded") or {}).get("requestId")
                iface_now = self.iface
                if not msg.broadcast and req is not None and iface_now is not None:
                    # the library drops the handler after one call; keep waiting for the real ACK
                    try:
                        iface_now._addResponseHandler(req, onAckNak)
                    except Exception:
                        pass

        try:
            pkt = iface.sendText(clean_text, destinationId=dest, wantAck=True,
                                 onResponse=onAckNak, channelIndex=channel)
            msg.pkt_id = getattr(pkt, "id", None)
        except BaseException as e:
            # BaseException: the meshtastic library calls sys.exit() on some bad inputs,
            # which would otherwise close the whole app.
            if isinstance(e, KeyboardInterrupt):
                raise
            msg.status, msg.ack = "UNKNOWN", f"failed: {e or e.__class__.__name__}"
            with self.data_lock:
                self.messages.append(msg)
            return

        with self.data_lock:
            self.messages.append(msg)
        self.last_read[target] = time.time()
        try:
            self.archiver.write({
                'ts_iso': MessageArchiver.now_iso(),
                'direction': 'OUT',
                'from_id': self.local_id or '^local',
                'from_name': self.local_short,
                'to_id': to_id,
                'to_name': to_short,
                'broadcast': broadcast,
                'text': clean_text,
                'msg_id': msg.pkt_id,
                'ack': False,
                'hop_limit': None,
                'rssi': None,
                'snr': None,
                'channel': channel,
                'errors': []
            })
        except Exception:
            pass

    def send_text_to_node(self, target_num: int, text: str):
        self.send_text(("dm", target_num), text)

    # ---------- Conversations ----------

    def _channel_name(self, index: int) -> str:
        """Channel name from the radio config; the primary channel falls back to its modem preset."""
        try:
            local = self.iface.localNode
            for ch in local.channels or []:
                if ch.index == index and ch.settings.name:
                    return ch.settings.name
            if index == 0:
                from meshtastic.protobuf import config_pb2
                preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(local.localConfig.lora.modem_preset)
                return "".join(part.capitalize() for part in preset.split("_"))
        except Exception:
            pass
        return "Primary" if index == 0 else f"Channel {index}"

    def _conv_key(self, msg: MessageInfo) -> Optional[tuple]:
        if msg.broadcast:
            return ("ch", msg.channel)
        num = _id_to_num(msg.to_id if msg.direction == "OUT" else msg.from_id)
        if num is None or num == self.local_num:
            return None
        return ("dm", num)

    def _conversations(self) -> List[dict]:
        """Channels first (the primary channel always), then direct chats, newest first."""
        with self.data_lock:
            messages = list(self.messages)
        convs: Dict[tuple, dict] = {("ch", 0): {"key": ("ch", 0), "last_ts": 0.0, "unread": 0}}
        for msg in messages:
            key = self._conv_key(msg)
            if key is None:
                continue
            c = convs.setdefault(key, {"key": key, "last_ts": 0.0, "unread": 0})
            c["last_ts"] = max(c["last_ts"], msg.timestamp)
            if msg.direction == "IN" and msg.timestamp > max(self.read_baseline, self.last_read.get(key, 0)):
                c["unread"] += 1
        chans = sorted((c for k, c in convs.items() if k[0] == "ch"), key=lambda c: c["key"][1])
        dms = sorted((c for k, c in convs.items() if k[0] == "dm"), key=lambda c: -c["last_ts"])
        return chans + dms

    def _conv_label(self, key: tuple) -> str:
        if key[0] == "ch":
            return "# " + self._channel_name(key[1])
        n = self.nodes.get(key[1])
        if not n:
            return f"#{key[1]}"
        name = n.short_name if n.short_name and n.short_name != "?" else (n.node_id or f"#{n.num}")
        if n.long_name and n.long_name not in ("?", name):
            name = f"{name} {n.long_name}"
        return name

    def _unread_total(self) -> int:
        return sum(c["unread"] for c in self._conversations())

    @staticmethod
    def _ack_mark(msg: MessageInfo) -> str:
        if msg.direction != "OUT" or not msg.ack:
            return ""
        if msg.ack == "pending":
            return " …"
        if msg.ack == "relayed":
            return " ✓"
        if msg.ack == "delivered":
            return " ✓✓"
        return f" ✗ {msg.ack.removeprefix('failed: ')}"

    # ---------- Drawing helpers ----------

    def _draw_header(self, stdscr, text: str):
        h, w = stdscr.getmaxyx()
        if h <= 0 or w <= 1:
            return
        maxw = w - 1
        if getattr(self, "auto_trace_in_progress", False) and getattr(self, "auto_trace_active_node", None):
            text += f" | [Auto-tracing: {self.auto_trace_active_node}]"
        unread = self._unread_total()
        if unread:
            text = f"✉ {unread} unread | " + text
        attr = curses.A_REVERSE
        if self.conn_state != "CONNECTED":
            state = self.conn_state + (f": {self.conn_error}" if self.conn_error else "")
            text = f"[{state}] " + text
            attr |= curses.color_pair(1) | curses.A_BOLD
        elif self.ui_error:
            text += f" | UI error: {self.ui_error} (see meshtop.log)"
        stdscr.addnstr(0, 0, ljust_cells(safe_text(text), maxw), maxw, attr)

    def _msg_line(self, msg: MessageInfo, with_where: bool) -> str:
        """One-line summary of a message for the nodes-view log."""
        t_str = time.strftime("%H:%M", time.localtime(msg.timestamp))
        sender = "Me" if msg.direction == "OUT" else (msg.from_short or msg.from_id)
        if msg.broadcast:
            where = f"#{self._channel_name(msg.channel)} "
        elif msg.direction == "OUT":
            where = f"→{msg.to_short} "
        else:
            where = "→Me " if with_where else ""
        return f"{t_str} {where}{sender}: {safe_text(msg.text)}{self._ack_mark(msg)}"

    def _draw_message_log(self, stdscr, start_row: int, max_rows: int):
        """Latest messages, newest first, under the nodes table. Unread ones are bold."""
        h, w = stdscr.getmaxyx()
        maxw = w - 1
        with self.data_lock:
            msgs = list(reversed(self.messages[-max_rows:]))
        if not msgs:
            stdscr.addnstr(start_row, 0, "(no messages yet)", maxw, curses.A_DIM)
            return
        for i, msg in enumerate(msgs):
            key = self._conv_key(msg)
            unread = (msg.direction == "IN" and key is not None
                      and msg.timestamp > max(self.read_baseline, self.last_read.get(key, 0)))
            attrs = curses.A_BOLD if unread else 0
            if msg.direction == "IN" and not msg.broadcast:
                attrs |= curses.color_pair(5)  # direct message to me
            stdscr.addnstr(start_row + i, 0, ljust_cells(self._msg_line(msg, True), maxw), maxw, attrs)

    def _draw_trace_view(self, stdscr):
        h, w = stdscr.getmaxyx()
        maxw = w - 1
        top = 1
        self._wipe_body(stdscr)

        # snapshot trace state
        with self.trace_lock:
            tr = dict(self.trace_result) if self.trace_result is not None else {}

        if self.trace_target_num is None or self.trace_target_num not in self.nodes:
            stdscr.addnstr(top, 0, "No node selected for trace.".ljust(maxw), maxw)
            stdscr.addnstr(h - 1, 0, "[n] nodes  [m] messages  [q] quit".ljust(maxw), maxw)
            return

        node = self.nodes[self.trace_target_num]
        target_name = node.short_name or node.long_name or node.node_id or f"#{node.num}"
        stdscr.addnstr(top, 0, f"TRACE -> {target_name}".ljust(maxw), maxw, curses.A_BOLD)

        started = tr.get("ts") or time.time()
        elapsed_frozen = tr.get("elapsed")
        if elapsed_frozen is not None:
            elapsed = elapsed_frozen
        else:
            elapsed = time.time() - started

        route = tr.get("route", [])
        err = tr.get("error")
        status = "OK" if (elapsed_frozen is not None and not err) else ("Error" if err else "Waiting...")

        stdscr.addnstr(top + 1, 0, f"Status: {status}    Elapsed: {elapsed:.1f}s".ljust(maxw), maxw)

        route = tr.get("route", []) or []
        snr_fwd = tr.get("snr_towards", []) or []
        snr_back = tr.get("snr_back", []) or []

        y = top + 3

        if err:
            stdscr.addnstr(y, 0, f"Error: {err}".ljust(maxw), maxw)
            y += 1
            stdscr.addnstr(y, 0, "Press 't' to retry, or 'n' to return to nodes.".ljust(maxw), maxw)
            y += 2
            stdscr.addnstr(h - 1, 0, "[t] retry  [n] nodes  [m] messages  [s] stats  [q] quit".ljust(maxw), maxw)
            return
        elif elapsed_frozen is None:
            remain = max(0.0, self.trace_timeout_sec - elapsed)
            stdscr.addnstr(y, 0, f"Waiting for trace... (~{remain:.1f}s left)".ljust(maxw), maxw)
            y += 1
            stdscr.addnstr(y, 0, "Press 't' to retry, or 'n' to return to nodes.".ljust(maxw), maxw)
            y += 1
            stdscr.addnstr(h - 1, 0, "[t] retry  [n] nodes  [m] messages  [s] stats  [q] quit".ljust(maxw), maxw)
            return

        def name_from_num(n):
            return self._node_label(int(n), prefer_long=True)

        # Build the full route including us and the destination
        local_num = self.local_num
        dest_num = tr.get("dest_num")
        route_back = tr.get("route_back", []) or []

        # Unknown relays (0xFFFFFFFF) may repeat, so they are not de-duplicated
        full_route = [local_num] if local_num is not None else []
        full_route += [h for h in route if h != dest_num and h != local_num]
        if dest_num is not None:
            full_route.append(dest_num)

        names = [name_from_num(n) for n in full_route]
        # back SNRs line up with the forward hops only if the reply took the same path
        same_path = not route_back or list(reversed(route_back)) == list(route)

        # Draw vertical path
        stdscr.addnstr(y, 0, "Trace Path Discovery:".ljust(maxw), maxw, curses.A_BOLD)
        y += 2

        for i in range(len(names)):
            if y >= h - 2:
                break
            stdscr.addnstr(y, 2, f"[ {names[i]} ]", maxw)
            y += 1

            if i < len(names) - 1:
                # Forward SNR
                fwd_val = snr_fwd[i] if i < len(snr_fwd) else None
                fwd_txt = f"{fwd_val / 4.0:+.1f} dB" if (fwd_val is not None and fwd_val != -128) else "? dB"

                # Backward SNR
                back_idx = len(names) - 2 - i
                back_val = snr_back[back_idx] if (same_path and 0 <= back_idx < len(snr_back)) else None
                back_txt = f"{back_val / 4.0:+.1f} dB" if (back_val is not None and back_val != -128) else "? dB"

                if y >= h - 2:
                    break
                stdscr.addnstr(y, 2, "   |", maxw)
                y += 1

                if y >= h - 2:
                    break
                stdscr.addnstr(y, 2, f"   |   fwd: {fwd_txt}  |  back: {back_txt}", maxw)
                y += 1

                if y >= h - 2:
                    break
                stdscr.addnstr(y, 2, "   v", maxw)
                y += 1

        if not same_path and y < h - 2:
            back_names = [name_from_num(dest_num)] + [name_from_num(n) for n in route_back] + [names[0]]
            parts = []
            for i, nm in enumerate(back_names):
                parts.append(f"[{nm}]")
                if i < len(back_names) - 1:
                    v = snr_back[i] if i < len(snr_back) else None
                    parts.append(f" -({v / 4.0:+.1f}dB)-> " if v not in (None, -128) else " -> ")
            y += 1
            stdscr.addnstr(y, 0, "Return path (differs from forward):", maxw, curses.A_BOLD)
            y += 1
            for line in wrap_cells("".join(parts), maxw - 2):
                if y >= h - 2:
                    break
                stdscr.addnstr(y, 2, line, maxw - 2)
                y += 1

        stdscr.addnstr(h - 1, 0, "[t] retry  [n] nodes  [m] messages  [s] stats  [q] quit".ljust(maxw), maxw)

    # ---------- Nodes view ----------

    def _heard_at(self, n: NodeInfo) -> float:
        """Last time we know the node was heard: live, or from the radio's node DB."""
        return max(n.last_seen or 0, n.db_last_heard or 0)

    def _hops(self, n: NodeInfo) -> Optional[int]:
        if n.hop_start is not None and n.hop_limit is not None:
            return n.hop_start - n.hop_limit
        return n.hops_away

    @staticmethod
    def _snr_attr(snr) -> int:
        if snr is None:
            return 0
        if snr >= 0:
            return curses.color_pair(2)
        if snr >= -10:
            return curses.color_pair(3)
        return curses.color_pair(1)

    def _route_text(self, node: NodeInfo) -> Optional[str]:
        """'ME ─(+7.8)→ MMA ─(+3.0)→ FKAT' for the last trace, or None if never traced."""
        if node.last_route is None:
            return None
        hops = [h for h in node.last_route if h not in (node.num, self.local_num)]
        full = [self.local_num] + hops + [node.num]
        snrs = node.last_route_snr_towards or []
        out = self._node_label(full[0]) if self.local_num is not None else "ME"
        for i, num in enumerate(full[1:]):
            v = snrs[i] if i < len(snrs) else None
            out += f" ─({v / 4.0:+.1f})→ " if v not in (None, -128) else " → "
            out += self._node_label(num)
        return out

    def _return_route_text(self, node: NodeInfo) -> Optional[str]:
        back = node.last_route_back or []
        if not back or list(reversed(back)) == list(node.last_route or []):
            return None
        full = [node.num] + back + [self.local_num]
        snrs = node.last_route_snr_back or []
        out = self._node_label(full[0])
        for i, num in enumerate(full[1:]):
            v = snrs[i] if i < len(snrs) else None
            out += f" ─({v / 4.0:+.1f})→ " if v not in (None, -128) else " → "
            out += self._node_label(num)
        return out

    def _node_detail(self, node: NodeInfo, width: int, compact: bool) -> List[tuple]:
        """Lines of (text, attr) describing the selected node."""
        now = time.time()
        title = node.short_name or "?"
        if node.long_name and node.long_name not in ("?", title):
            title += f" – {node.long_name}"
        ident = " · ".join(x for x in (node.node_id or f"#{node.num}", str(node.hw_model or "")) if x and x != "?")
        heard = self._heard_at(node)
        hops = self._hops(node)
        radio = [] if node.is_me else [f"heard {fmt_ago(now - heard)} ago" if heard else "never heard"]
        if node.last_snr is not None:
            radio.append(f"SNR {node.last_snr:+.1f} dB")
        if node.last_rssi is not None:
            radio.append(f"RSSI {int(node.last_rssi)}")
        if hops is not None:
            radio.append("direct" if hops == 0 else f"{hops} hop{'s' if hops != 1 else ''}")
        if node.seen_packets:
            radio.append(f"{node.seen_packets} pkts")
        power = []
        if node.battery is not None:
            power.append("powered" if node.battery > 100 else f"bat {node.battery}%")
        if node.voltage:
            power.append(f"{node.voltage:.2f}V")
        if node.channel_util is not None:
            power.append(f"ChU {node.channel_util:.1f}%")
        if node.air_util is not None:
            power.append(f"Air {node.air_util:.2f}%")
        env = []
        if node.temperature is not None:
            env.append(f"{node.temperature:.1f}°C")
        if node.humidity is not None:
            env.append(f"{node.humidity:.0f}% RH")
        pos = ""
        if node.lat is not None and node.lon is not None and (node.lat, node.lon) != (0.0, 0.0):
            pos = f"{node.lat:.5f}, {node.lon:.5f}"
            if node.distance_km is not None and not node.is_me:
                pos += f" · {node.distance_km:.1f} km"
        route = self._route_text(node)
        if node.is_me:
            route_line = "This is your node"
        elif route:
            ago = f" ({fmt_ago(now - node.last_route_time)} ago)" if node.last_route_time else ""
            route_line = f"Trace{ago}: {route}"
        else:
            route_line = "No trace yet – press t"
        router_for = self._router_counts().get(node.num, 0)

        bold = curses.A_BOLD | (curses.color_pair(3) if node.is_me else 0)
        dim = curses.A_DIM
        if compact:
            line2 = " · ".join(radio + power + env + ([pos] if pos else []))
            return [(f"{title}  ({ident})", bold), (line2, 0), (route_line, dim if not route else 0)]

        lines = [(title, bold), (ident, dim), ("", 0), (" · ".join(radio), 0)]
        if power:
            lines.append((" · ".join(power), curses.color_pair(1) if node.battery_low else 0))
        if env:
            lines.append((" · ".join(env), 0))
        if pos:
            lines.append((pos, 0))
        if router_for:
            lines.append((f"Relay for {router_for} traced node{'s' if router_for != 1 else ''}", curses.color_pair(2)))
        lines.append(("", 0))
        if route:
            ago = f" ({fmt_ago(now - node.last_route_time)} ago)" if node.last_route_time else ""
            lines.append((f"Trace{ago}:", curses.A_BOLD))
            lines += [(l, 0) for l in wrap_cells(route, width)]
            back = self._return_route_text(node)
            if back:
                lines.append(("Return path:", curses.A_BOLD))
                lines += [(l, 0) for l in wrap_cells(back, width)]
        else:
            lines.append((route_line, dim))
        lines += [("", 0), ("[Enter] message  [t] trace", dim)]
        out = []
        for text, attr in lines:
            wrapped = wrap_cells(text, width) if text else [""]
            out += [(l, attr) for l in wrapped]
        return out

    def _draw_nodes_view(self, stdscr):
        h, w = stdscr.getmaxyx()
        if h < 8 or w < 40:
            return
        self._wipe_body(stdscr)
        maxw = w - 1
        top = 1
        now = time.time()

        # Wide terminals: detail pane on the right; narrow: 3 detail rows at the bottom
        wide = w >= 130
        if wide:
            pane_w = min(50, w // 3)
            table_w = maxw - pane_w - 1
            log_rows = 2
            table_bottom = h - 2 - log_rows  # last row the table may use
        else:
            pane_w = 0
            table_w = maxw
            log_rows = 1
            table_bottom = h - 2 - log_rows - 3
        rows_avail = max(1, table_bottom - top)  # rows below the header row

        nodes_list = self._sorted_nodes()
        self._sync_node_selection(nodes_list)

        # keep the selected node visible
        if self.selected_node_index < self.node_view_offset:
            self.node_view_offset = self.selected_node_index
        elif self.selected_node_index >= self.node_view_offset + rows_avail:
            self.node_view_offset = self.selected_node_index - rows_avail + 1
        self.node_view_offset = max(0, min(self.node_view_offset, max(0, len(nodes_list) - rows_avail)))

        # Column layout: Name and Route share whatever width is left
        fixed = [("Heard", 5), ("SNR", 5), ("Hop", 3), ("Bat", 4), ("ChU%", 4), ("Dist", 6)]
        rest = table_w - 2 - sum(wd for _, wd in fixed) - len(fixed) - 1  # 2 = selection marker
        name_w = max(12, min(30, int(rest * 0.55)))
        route_w = rest - name_w - 1
        cols = [("Name", name_w)] + fixed[:5] + ([("Route", route_w)] if route_w >= 6 else []) + fixed[5:]
        if route_w < 6:
            cols[0] = ("Name", rest)

        x = 2
        for name, wd in cols:
            stdscr.addnstr(top, x, name, min(wd, table_w - x), curses.A_BOLD | curses.A_UNDERLINE)
            x += wd + 1

        routers = self._router_nums()
        for i, node in enumerate(nodes_list[self.node_view_offset:self.node_view_offset + rows_avail]):
            row_y = top + 1 + i
            selected = self.node_view_offset + i == self.selected_node_index
            heard = self._heard_at(node)
            age = now - heard if heard else None

            base = 0
            if node.is_me:
                base = curses.color_pair(3) | curses.A_BOLD
            elif node.num in routers:
                base = curses.color_pair(2)
            if age is None or age > 7200:
                base |= curses.A_DIM  # stale: not heard for 2 hours
            if selected:
                base |= curses.A_REVERSE

            name = node.short_name or "?"
            if node.long_name and node.long_name not in ("?", name):
                name += " " + node.long_name
            hops = self._hops(node)
            if node.last_route is None:
                route_str = "--"
            elif not node.last_route:
                route_str = "DIRECT"
            else:
                route_str = ">".join(self._node_label(n) for n in node.last_route)
            bat = node.battery
            cells = {
                "Name": (name, 0),
                "Heard": ("me" if node.is_me else fmt_ago(age) if age is not None else "never", 0),
                "SNR": (f"{node.last_snr:+.1f}" if node.last_snr is not None else "--",
                        self._snr_attr(node.last_snr) if not node.is_me else 0),
                "Hop": (str(hops) if hops is not None else "--", 0),
                "Bat": ("--" if bat is None else "PWR" if bat > 100 else f"{bat}%",
                        curses.color_pair(1) | curses.A_BOLD if node.battery_low else 0),
                "ChU%": (f"{node.channel_util:.1f}" if node.channel_util is not None else "--", 0),
                "Route": (route_str, 0),
                "Dist": (f"{node.distance_km:.1f}k" if node.distance_km is not None and not node.is_me else "--", 0),
            }
            stdscr.addnstr(row_y, 0, ("▶ " if selected else "  "), 2, base)
            x = 2
            for col, wd in cols:
                text, color = cells[col]
                attr = base
                if color and not selected:
                    attr = (base & ~curses.A_COLOR) | color
                right = col not in ("Name", "Route")
                cell = text.rjust(wd) if right and text_width(text) < wd else ljust_cells(text, wd)
                stdscr.addnstr(row_y, x, cell + " ", min(wd + 1, table_w - x), attr)
                x += wd + 1

        if len(nodes_list) > rows_avail:
            more = f" {self.node_view_offset + 1}-{min(len(nodes_list), self.node_view_offset + rows_avail)}/{len(nodes_list)} "
            stdscr.addnstr(table_bottom, max(0, table_w - len(more)), more, len(more), curses.A_DIM)

        selected_node = nodes_list[self.selected_node_index] if nodes_list else None
        if wide:
            for y in range(top, h - 1 - log_rows):
                stdscr.addch(y, table_w, curses.ACS_VLINE)
            if selected_node:
                for i, (text, attr) in enumerate(self._node_detail(selected_node, pane_w - 1, compact=False)):
                    if top + i >= h - 1 - log_rows:
                        break
                    stdscr.addnstr(top + i, table_w + 2, text, pane_w - 1, attr)
        elif selected_node:
            y0 = table_bottom + 1
            for i, (text, attr) in enumerate(self._node_detail(selected_node, maxw, compact=True)):
                stdscr.addnstr(y0 + i, 0, ljust_cells(text, maxw), maxw, attr)

        self._draw_message_log(stdscr, start_row=h - 1 - log_rows, max_rows=log_rows)

        footer = "[↑↓] select  [Enter] message  [t] trace  [m] messages  [s] stats  [p] export  [q] quit"
        stdscr.addnstr(h - 1, 0, ljust_cells(footer, maxw), maxw, curses.A_DIM)

    def _sorted_nodes(self) -> List[NodeInfo]:
        with self.data_lock:
            nodes_list = list(self.nodes.values())
        nodes_list.sort(key=lambda n: (not n.is_me, -self._heard_at(n)))
        return nodes_list

    def _sync_node_selection(self, nodes_list: List[NodeInfo]):
        nums = [n.num for n in nodes_list]
        if self.selected_node_num in nums:
            self.selected_node_index = nums.index(self.selected_node_num)
        else:
            self.selected_node_index = max(0, min(self.selected_node_index, len(nums) - 1))
            self.selected_node_num = nums[self.selected_node_index] if nums else None

    # ---------- Messages view ----------

    def _draw_messages_view(self, stdscr):
        h, w = stdscr.getmaxyx()
        if h < 8 or w < 40:
            return
        maxw = w - 1
        top = 1
        self._wipe_body(stdscr)

        convs = self._conversations()
        self._sync_chat_selection(convs)
        active = convs[self.selected_chat_index]
        key = active["key"]

        left_w = max(18, min(28, w // 4))
        right_x = left_w + 2
        right_w = maxw - right_x

        # --- Left column: channels, then direct chats ---
        y = top
        prev_kind = None
        for idx, c in enumerate(convs):
            if y >= h - 2:
                break
            if c["key"][0] != prev_kind:
                header = "CHANNELS" if c["key"][0] == "ch" else "DIRECT"
                if prev_kind is not None:
                    y += 1
                stdscr.addnstr(y, 0, header, left_w, curses.A_DIM | curses.A_BOLD)
                y += 1
                prev_kind = c["key"][0]
            badge = f" ({c['unread']})" if c["unread"] else ""
            label = clip_cells(self._conv_label(c["key"]), left_w - text_width(badge) - 1)
            attrs = curses.A_REVERSE if idx == self.selected_chat_index else 0
            if c["unread"]:
                attrs |= curses.A_BOLD
            stdscr.addnstr(y, 0, ljust_cells(" " + label + badge, left_w), left_w, attrs)
            y += 1

        for yy in range(top, h - 1):
            stdscr.addch(yy, left_w + 1, curses.ACS_VLINE)

        # --- Right column: title, then the conversation ---
        if key[0] == "ch":
            title = f"# {self._channel_name(key[1])} – public channel, everyone on it can read"
        else:
            n = self.nodes.get(key[1])
            title = f"@ {self._conv_label(key)}"
            if n:
                heard = self._heard_at(n)
                hops = self._hops(n)
                bits = [n.node_id or f"#{n.num}"]
                if heard:
                    bits.append(f"heard {fmt_ago(time.time() - heard)} ago")
                if hops is not None:
                    bits.append("direct" if hops == 0 else f"{hops} hops")
                title += "  (" + " · ".join(bits) + ")"
        stdscr.addnstr(top, right_x, title, right_w, curses.A_BOLD)

        with self.data_lock:
            chat_msgs = [m for m in self.messages if self._conv_key(m) == key]

        first_row = top + 2
        msg_rows_avail = (h - 3) - first_row + 1  # h-2 is the input line, h-1 the footer
        if not chat_msgs:
            hint = "(no messages yet – press Enter to write)"
            stdscr.addnstr(first_row, right_x, hint, right_w, curses.A_DIM)
        else:
            rendered = []
            prev_day = None
            for msg in chat_msgs[-msg_rows_avail:]:
                day = time.strftime("%a %d %b", time.localtime(msg.timestamp))
                if day != prev_day:
                    rendered.append((f"── {day} ──", curses.A_DIM))
                    prev_day = day
                t_str = time.strftime("%H:%M", time.localtime(msg.timestamp))
                if msg.direction == "OUT":
                    sender, attrs = "Me", curses.color_pair(3)
                else:
                    sender, attrs = (msg.from_short or msg.from_id), curses.color_pair(5)
                if msg.ack.startswith("failed"):
                    attrs = curses.color_pair(1)
                prefix = f"{t_str} {sender}: "
                indent = min(text_width(prefix), right_w // 3)
                lines = wrap_cells(prefix + safe_text(msg.text) + self._ack_mark(msg), max(5, right_w - indent))
                rendered.append((lines[0], attrs))
                rendered.extend((" " * indent + line, attrs) for line in lines[1:])
            for idx, (line, attrs) in enumerate(rendered[-msg_rows_avail:]):
                stdscr.addnstr(first_row + idx, right_x, line, right_w, attrs)

        # Looking at a conversation marks it read
        if not self.input_mode or self.input_target == key:
            self.last_read[key] = time.time()

        target = "channel" if key[0] == "ch" else "chat"
        footer = f"[↑↓] select  [Enter] write to {target}  [n] nodes  [s] stats  [q] quit   ✓ relayed  ✓✓ delivered"
        stdscr.addnstr(h - 1, 0, ljust_cells(footer, maxw), maxw, curses.A_DIM)

    def _sync_chat_selection(self, convs: List[dict]):
        keys = [c["key"] for c in convs]
        if self.selected_chat_key in keys:
            self.selected_chat_index = keys.index(self.selected_chat_key)
        else:
            self.selected_chat_index = max(0, min(self.selected_chat_index, len(keys) - 1))
            self.selected_chat_key = keys[self.selected_chat_index]

    # ---------- Input handling ----------

    def _start_input(self, target: tuple):
        self.input_mode = True
        self.input_text = ""
        self.input_target = target

    def _handle_input_char(self, ch):
        """ch is a str (typed character, any language) or an int (special key)."""
        if ch in (curses.KEY_ENTER, "\n", "\r"):
            if self.input_target is not None and self.input_text.strip():
                self.send_text(self.input_target, self.input_text.strip())
            self.input_mode = False
            self.input_text = ""
            self.input_target = None
            return
        if ch == "\x1b":  # ESC
            self.input_mode = False
            self.input_text = ""
            self.input_target = None
            return
        if ch in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            self.input_text = self.input_text[:-1]
            return
        if ch == "\x15":  # Ctrl-U clears the line
            self.input_text = ""
            return
        if isinstance(ch, str) and ch.isprintable():
            if len((self.input_text + ch).encode("utf-8")) <= MAX_TEXT_BYTES:
                self.input_text += ch
            else:
                curses.beep()

    def _handle_nodes_keys(self, ch):
        nodes_list = self._sorted_nodes()
        self._sync_node_selection(nodes_list)

        if ch in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            step = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_PPAGE: -10,
                    curses.KEY_NPAGE: 10, curses.KEY_HOME: -10**9, curses.KEY_END: 10**9}[ch]
            if nodes_list:
                self.selected_node_index = max(0, min(len(nodes_list) - 1, self.selected_node_index + step))
                self.selected_node_num = nodes_list[self.selected_node_index].num
        elif ch in (curses.KEY_ENTER, 10, 13):
            if nodes_list and not nodes_list[self.selected_node_index].is_me:
                self._start_input(("dm", nodes_list[self.selected_node_index].num))
        elif ch in (ord('t'), ord('T')):
            if nodes_list:
                target = nodes_list[self.selected_node_index]
                self.trace_target_num = target.num
                self.view_mode = "TRACE"
                self._need_full_clear = True
                self._send_trace_to_current()

    def _handle_msgs_keys(self, ch):
        convs = self._conversations()
        self._sync_chat_selection(convs)
        if ch in (curses.KEY_UP, curses.KEY_DOWN):
            step = -1 if ch == curses.KEY_UP else 1
            self.selected_chat_index = max(0, min(len(convs) - 1, self.selected_chat_index + step))
            self.selected_chat_key = convs[self.selected_chat_index]["key"]
        elif ch in (10, 13, curses.KEY_ENTER):
            self._start_input(convs[self.selected_chat_index]["key"])

    def _handle_stats_keys(self, ch):
        if ch in (curses.KEY_UP, ord('k'), ord('K')):
            if self.stats_scroll_offset > 0:
                self.stats_scroll_offset -= 1
        elif ch in (curses.KEY_DOWN, ord('j'), ord('J')):
            self.stats_scroll_offset += 1
        elif ch == curses.KEY_NPAGE: # Page Down
            self.stats_scroll_offset += 10
        elif ch == curses.KEY_PPAGE: # Page Up
            self.stats_scroll_offset = max(0, self.stats_scroll_offset - 10)

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
                pkt_id=rec.get("msg_id"),
                channel=rec.get("channel") or 0,
                broadcast=bool(rec.get("broadcast")) or to_id in BROADCAST_IDS,
            )
            self.messages.append(msg)

    def _save_traced_stations(self):
        """
        Saves the last_route, last_route_snr_towards, last_route_snr_back,
        and last_route_time for all nodes that have been traced to a local file.
        """
        try:
            base_dir = Path(__file__).resolve().parent
        except Exception:
            base_dir = Path(".").resolve()

        file_path = base_dir / "traced_stations.json"
        tmp_path = base_dir / "traced_stations.json.tmp"

        data_to_save = {
            "saved_at": time.time(),
            "traces": {}
        }

        with self.data_lock:
            for num, node in self.nodes.items():
                if getattr(node, "last_route_time", None) is not None and node.last_route_time > 0:
                    data_to_save["traces"][str(num)] = {
                        "last_route": node.last_route,
                        "last_route_snr_towards": node.last_route_snr_towards,
                        "last_route_snr_back": node.last_route_snr_back,
                        "last_route_back": node.last_route_back,
                        "last_route_time": node.last_route_time
                    }

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data_to_save, f, indent=4)
            tmp_path.replace(file_path)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    def _load_traced_stations(self):
        """
        Loads the trace data from traced_stations.json if the file was saved
        less than 24 hours ago.
        """
        try:
            base_dir = Path(__file__).resolve().parent
        except Exception:
            base_dir = Path(".").resolve()
            
        file_path = base_dir / "traced_stations.json"
        if not file_path.exists():
            return
            
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
            
        saved_at = data.get("saved_at", 0)
        if time.time() - saved_at >= 86400: # 24 hours in seconds
            return
            
        traces = data.get("traces", {})
        with self.data_lock:
            for num_str, trace_info in traces.items():
                try:
                    num = int(num_str)
                except ValueError:
                    continue
                    
                node = self._get_or_create_node(num)
                node.last_route = trace_info.get("last_route")
                node.last_route_snr_towards = trace_info.get("last_route_snr_towards")
                node.last_route_snr_back = trace_info.get("last_route_snr_back")
                node.last_route_back = trace_info.get("last_route_back")
                node.last_route_time = trace_info.get("last_route_time")

    def run_curses(self, stdscr):
        stdscr = SafeWin(stdscr)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.nodelay(True)
        stdscr.timeout(200)

        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            try:
                curses.init_pair(1, curses.COLOR_RED, bg)  # low battery / errors
                curses.init_pair(2, curses.COLOR_GREEN, bg)  # router nodes
                curses.init_pair(3, curses.COLOR_YELLOW, bg)  # my node
                curses.init_pair(4, curses.COLOR_BLUE, bg)  # messages to me
                curses.init_pair(5, curses.COLOR_CYAN, bg)  # received messages
            except curses.error:
                pass

        while self.running:
            # A bug in one frame must not take the whole app down: log it, show it, keep going.
            try:
                self._draw_frame(stdscr)
            except Exception as e:
                self._report_ui_error(e)

            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue  # timeout, no key
            if ch == curses.KEY_RESIZE:
                self._need_full_clear = True
                continue
            try:
                self._handle_key(ch)
            except Exception as e:
                self._report_ui_error(e)

    def _report_ui_error(self, e: Exception):
        self.ui_error = f"{e.__class__.__name__}: {e}"
        log.error("UI error:\n%s", traceback.format_exc())

    def _draw_frame(self, stdscr):
        stdscr.erase()
        # force a one-shot full clear if requested
        if self._need_full_clear:
            stdscr.clear()
            self._need_full_clear = False

        self._check_trace_timeout()

        h, w = stdscr.getmaxyx()
        if h < 10 or w < 60:
            msg = f"meshtop: enlarge terminal (>=60x10, now {w}x{h}). Press q to quit."
            stdscr.addnstr(0, 0, msg, max(1, w - 1))
            stdscr.refresh()
            return

        filt = f"  [g] filter={'ON' if self.hide_gibberish else 'OFF'}"
        if self.view_mode == "NODES":
            self._draw_header(stdscr, "meshtop – NODES (m messages, t trace, s stats, p export, q quit)")
            self._draw_nodes_view(stdscr)
        elif self.view_mode == "TRACE":
            self._draw_header(stdscr, "meshtop – TRACE (t retry, n nodes, m messages, s stats, q quit)")
            self._draw_trace_view(stdscr)
        elif self.view_mode == "STATS":
            self._draw_header(stdscr, "meshtop – STATS (n nodes, m messages, p export, q quit)")
            self._draw_stats_view(stdscr)
        else:
            self._draw_header(stdscr, "meshtop – MESSAGES (n nodes, s stats, p export, q quit)" + filt)
            self._draw_messages_view(stdscr)

        if self.input_mode:
            maxw = w - 1
            to = self._conv_label(self.input_target) if self.input_target else "?"
            used = len(self.input_text.encode("utf-8"))
            prompt = f"To {to} [{used}/{MAX_TEXT_BYTES}] (Enter=send, Esc=cancel): "
            txt = safe_text(self.input_text)
            room = max(1, maxw - text_width(prompt) - 1)
            while text_width(txt) > room:  # keep the end of a long message visible
                txt = txt[1:]
            stdscr.addnstr(h - 2, 0, ljust_cells(prompt + txt, maxw), maxw, curses.A_BOLD)
            stdscr.move(h - 2, min(maxw - 1, text_width(prompt) + text_width(txt)))
        self._set_cursor(self.input_mode)
        stdscr.refresh()

    def _set_cursor(self, visible: bool):
        if getattr(self, "_cursor_visible", None) != visible:
            try:
                curses.curs_set(1 if visible else 0)
            except curses.error:
                pass
            self._cursor_visible = visible

    def _handle_key(self, ch):
        if self.input_mode:
            self._handle_input_char(ch)
            return
        if isinstance(ch, str):
            ch = _LAYOUT_TO_LATIN.get(ch, ch)
            if len(ch) != 1:
                return
            ch = ord(ch)

        if ch in (ord('g'), ord('G')):
            self.hide_gibberish = not self.hide_gibberish
            return
        if ch in (ord("q"), ord("Q")):
            self.running = False
            return
        if ch in (ord("m"), ord("M")):
            self.view_mode = "MSGS"
            self._need_full_clear = True
            return
        if ch in (ord("n"), ord("N")):
            self.view_mode = "NODES"
            self._need_full_clear = True
            return
        if ch in (ord("s"), ord("S")):
            self.view_mode = "STATS"
            self._need_full_clear = True
            return
        if ch in (ord("p"), ord("P")):
            self._export_topology()
            return

        if self.view_mode == "NODES":
            self._handle_nodes_keys(ch)
        elif self.view_mode == "TRACE":
            self._handle_trace_keys(ch)
        elif self.view_mode == "STATS":
            self._handle_stats_keys(ch)
        else:
            self._handle_msgs_keys(ch)

    def _log_trace_history(self, dest_num, route, snr_towards, snr_back, route_back=None):
        dest_short = ""
        with self.data_lock:
            n = self.nodes.get(dest_num)
            if n:
                dest_short = n.short_name or ""
                
        record = {
            "timestamp": time.time(),
            "dest_num": dest_num,
            "dest_short": dest_short,
            "route": route,
            "snr_towards": snr_towards,
            "route_back": route_back or [],
            "snr_back": snr_back
        }
        
        try:
            try:
                base_dir = Path(__file__).resolve().parent
            except Exception:
                base_dir = Path(".").resolve()
            filepath = base_dir / "traces.jsonl"
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _draw_stats_view(self, stdscr):
        h, w = stdscr.getmaxyx()
        if h < 8 or w < 40:
            return
        self._wipe_body(stdscr)
        maxw = w - 1
        
        # Calculate stats
        with self.data_lock:
            nodes_list = list(self.nodes.values())
        
        total_nodes = len(nodes_list)
        active_nodes = 0
        inactive_nodes = 0
        unseen_nodes = 0
        now = time.time()
        
        hop_counts = []
        direct_links = []
        
        for n in nodes_list:
            if n.is_me:
                continue
            heard = self._heard_at(n)
            if heard > 0:
                if now - heard < 1800:
                    active_nodes += 1
                else:
                    inactive_nodes += 1
            else:
                unseen_nodes += 1
                
            if getattr(n, "last_route", None) is not None:
                hop_counts.append(len(n.last_route))
                
            if n.last_snr is not None:
                direct_links.append((n.short_name or n.node_id or f"#{n.num}", n.last_snr))
                
        avg_hops = sum(hop_counts) / len(hop_counts) if hop_counts else 0.0
        
        direct_links.sort(key=lambda x: x[1], reverse=True)
        best_links = direct_links[:3]
        worst_links = sorted(direct_links, key=lambda x: x[1])[:3]
        
        row = 2
        
        # --- Node Activity Panel ---
        stdscr.attron(curses.A_BOLD | curses.color_pair(3))
        stdscr.addnstr(row, 2, "■ NODE ACTIVITY STATISTICS", maxw - 4)
        stdscr.attroff(curses.A_BOLD | curses.color_pair(3))
        row += 1
        stdscr.addnstr(row, 4, f"Active (last 30m): {active_nodes}", maxw - 6)
        row += 1
        stdscr.addnstr(row, 4, f"Inactive (> 30m):  {inactive_nodes}", maxw - 6)
        row += 1
        stdscr.addnstr(row, 4, f"Unseen / Dead:     {unseen_nodes}", maxw - 6)
        row += 1
        stdscr.addnstr(row, 4, f"Total Nodes:       {total_nodes}", maxw - 6)
        
        row += 2
        
        # --- Routing Panel ---
        stdscr.attron(curses.A_BOLD | curses.color_pair(3))
        stdscr.addnstr(row, 2, "■ ROUTING STATISTICS", maxw - 4)
        stdscr.attroff(curses.A_BOLD | curses.color_pair(3))
        row += 1
        stdscr.addnstr(row, 4, f"Traced Stations:   {len(hop_counts)}", maxw - 6)
        row += 1
        stdscr.addnstr(row, 4, f"Average Hop Count: {avg_hops:.2f} hops", maxw - 6)
        
        row += 2
        
        # --- Link Quality Panel ---
        stdscr.attron(curses.A_BOLD | curses.color_pair(3))
        stdscr.addnstr(row, 2, "■ DIRECT LINK QUALITY (SNR)", maxw - 4)
        stdscr.attroff(curses.A_BOLD | curses.color_pair(3))
        row += 1
        
        stdscr.attron(curses.A_BOLD)
        stdscr.addnstr(row, 4, "Best Links:", maxw - 6)
        stdscr.attroff(curses.A_BOLD)
        row += 1
        if best_links:
            for name, snr in best_links:
                pair = 2 if snr >= 0 else 0
                stdscr.addnstr(row, 6, f"{name}: {snr:+.1f} dB", maxw - 8, curses.color_pair(pair))
                row += 1
        else:
            stdscr.addnstr(row, 6, "(no links recorded)", maxw - 8)
            row += 1
            
        row += 1
        stdscr.attron(curses.A_BOLD)
        stdscr.addnstr(row, 4, "Worst Links:", maxw - 6)
        stdscr.attroff(curses.A_BOLD)
        row += 1
        if worst_links:
            for name, snr in worst_links:
                pair = 1 if snr < -10 else 0
                stdscr.addnstr(row, 6, f"{name}: {snr:+.1f} dB", maxw - 8, curses.color_pair(pair))
                row += 1
        else:
            stdscr.addnstr(row, 6, "(no links recorded)", maxw - 8)
            row += 1

        # --- Connections Map Tree Logic ---
        tree = {}
        for n in nodes_list:
            if n.is_me or getattr(n, "last_route", None) is None:
                continue
            path = list(n.last_route) + [n.num]
            curr = tree
            seen_steps = set()
            for idx, step in enumerate(path):
                if step is None or step == self.local_num or step in seen_steps:
                    continue
                seen_steps.add(step)
                if step not in curr:
                    curr[step] = {"children": {}, "is_target": False, "snr": None}
                if idx == len(path) - 1:
                    curr[step]["is_target"] = True
                    snrs = getattr(n, "last_route_snr_towards", []) or []
                    if snrs and snrs[-1] is not None and snrs[-1] != -128:
                        curr[step]["snr"] = snrs[-1] / 4.0
                curr = curr[step]["children"]

        def get_subtree_size(node_data, visited=None):
            if visited is None:
                visited = set()
            nid = id(node_data)
            if nid in visited:
                return 1
            visited.add(nid)
            return 1 + sum(get_subtree_size(v, visited) for v in node_data["children"].values())

        def format_tree(curr_tree, prefix="", visited=None):
            if visited is None:
                visited = set()
            lines = []
            sorted_children = sorted(
                curr_tree.items(),
                key=lambda x: get_subtree_size(x[1]),
                reverse=True
            )
            for idx, (num, data) in enumerate(sorted_children):
                if num in visited:
                    continue
                visited_next = set(visited)
                visited_next.add(num)
                child_is_last = (idx == len(sorted_children) - 1)
                marker = "└── " if child_is_last else "├── "

                with self.data_lock:
                    n = self.nodes.get(num)
                if n:
                    name = n.short_name or n.node_id or f"#{num}"
                else:
                    name = f"#{num}"

                snr_val = data["snr"]
                snr_str = f" ({snr_val:+.1f}dB)" if snr_val is not None else ""
                lines.append(f"{prefix}{marker}{name}{snr_str}")

                next_prefix = prefix + ("    " if child_is_last else "│   ")
                lines.extend(format_tree(data["children"], next_prefix, visited_next))
            return lines

        if not tree:
            tree_lines = ["ME (Local Node)", " └── (no active trace routes yet)"]
        else:
            tree_lines = ["ME (Local Node)"]
            tree_lines.extend(format_tree(tree, prefix=" "))

        # Clamp scroll offset
        max_scroll = max(0, len(tree_lines) - 1)
        if self.stats_scroll_offset > max_scroll:
            self.stats_scroll_offset = max_scroll
        if self.stats_scroll_offset < 0:
            self.stats_scroll_offset = 0

        visible_lines = tree_lines[self.stats_scroll_offset:]

        # Render tree dynamically depending on width
        if w >= 80:
            for y in range(2, h - 2):
                try:
                    stdscr.addch(y, 42, curses.ACS_VLINE)
                except curses.error:
                    pass

            stdscr.attron(curses.A_BOLD | curses.color_pair(3))
            stdscr.addnstr(2, 44, "■ NETWORK CONNECTIONS MAP", max(1, w - 46))
            stdscr.attroff(curses.A_BOLD | curses.color_pair(3))

            max_tree_height = max(1, h - 4 - 2)
            col_width = 35
            w_avail = w - 44
            num_cols = max(1, w_avail // col_width)

            for col_idx in range(num_cols):
                col_x = 44 + col_idx * col_width
                start = col_idx * max_tree_height
                end = (col_idx + 1) * max_tree_height
                col_lines = visible_lines[start:end]

                for idx, line in enumerate(col_lines):
                    if 4 + idx < h - 1:
                        stdscr.addnstr(4 + idx, col_x, line, col_width - 3)

            total_slots = num_cols * max_tree_height
            if len(visible_lines) > total_slots and h > 3:
                last_col_x = 44 + (num_cols - 1) * col_width
                stdscr.addnstr(h - 3, last_col_x, "[...↓ more]", col_width - 3, curses.color_pair(3) | curses.A_REVERSE)
        else:
            if h - row >= 6:
                row += 1
                stdscr.attron(curses.A_BOLD | curses.color_pair(3))
                stdscr.addnstr(row, 2, "■ NETWORK CONNECTIONS MAP", max(1, maxw - 4))
                stdscr.attroff(curses.A_BOLD | curses.color_pair(3))
                row += 1

                max_tree_height = max(1, h - row - 2)
                col_width = 35
                num_cols = max(1, w // col_width)

                for col_idx in range(num_cols):
                    col_x = 4 + col_idx * col_width
                    start = col_idx * max_tree_height
                    end = (col_idx + 1) * max_tree_height
                    col_lines = visible_lines[start:end]

                    for idx, line in enumerate(col_lines):
                        if row + idx < h - 1:
                            stdscr.addnstr(row + idx, col_x, line, col_width - 3)

                total_slots = num_cols * max_tree_height
                if len(visible_lines) > total_slots and (row + max_tree_height - 1 < h - 1):
                    last_col_x = 4 + (num_cols - 1) * col_width
                    stdscr.addnstr(row + max_tree_height - 1, last_col_x, "[...↓ more]", col_width - 3, curses.color_pair(3) | curses.A_REVERSE)

        footer = "[n] nodes  [m] messages  [s] stats  [↑/↓] scroll tree  [q] quit"
        stdscr.addnstr(h - 1, 0, footer.ljust(maxw), maxw)

    def _router_counts(self) -> Dict[int, int]:
        """
        Relay nodes -> how many traced nodes they connect ME to (counting themselves
        if traced). A node is a router when this is >= 2.
        """
        with self.data_lock:
            traced = [(num, n.last_route) for num, n in self.nodes.items()
                      if num != self.local_num and n.last_route is not None]
        connected: Dict[int, set] = {}
        traced_nums = {num for num, _ in traced}
        for num, route in traced:
            for hop in set(route):
                if hop not in (num, self.local_num, UNKNOWN_HOP):
                    connected.setdefault(hop, set()).add(num)
        return {hop: len(users) + (1 if hop in traced_nums else 0) for hop, users in connected.items()}

    def _router_nums(self) -> set:
        """Nodes that connect ME to >= 2 traced nodes. Computed once per frame."""
        return {hop for hop, count in self._router_counts().items() if count >= 2}

    def _is_router(self, node_num) -> bool:
        return node_num in self._router_nums()

    def _mermaid_node_id(self, node_num):
        if node_num is not None and node_num == self.local_num:
            return "ME"
        if node_num is None:
            return "ME"
        with self.data_lock:
            n = self.nodes.get(node_num)
        if n and n.node_id:
            return n.node_id.replace("!", "N")
        return f"Node_{node_num}"

    def node_definition(self, node_num):
        node_id = self._mermaid_node_id(node_num)
        if node_num is not None and node_num == self.local_num:
            return 'ME["ME (Local Node)"]'
        if node_num is None:
            return 'ME["ME (Local Node)"]'
        with self.data_lock:
            n = self.nodes.get(node_num)
        if n:
            label = (n.short_name or n.node_id or f"#{n.num}").replace('"', "'")
            if n.long_name:
                label += f" ({n.long_name.replace('\"', '\'')})"
            return f'{node_id}["{label}"]'
        else:
            return f'{node_id}["#{node_num}"]'

    def _export_topology(self):
        with self.data_lock:
            nodes_list = list(self.nodes.values())

        node_defs = set()
        edges = set()

        # Add local node
        node_defs.add(self.node_definition(self.local_num))

        for node in nodes_list:
            if node.is_me:
                continue

            route = getattr(node, "last_route", None)
            if route is not None:
                # Add definition for target node
                node_defs.add(self.node_definition(node.num))

                # Build hop path: ME -> Hop1 -> Hop2 -> ... -> Target
                path = [self.local_num if self.local_num is not None else 0] + [h for h in route if h is not None] + [node.num]

                # Add definitions for all intermediate hops
                for num in route:
                    if num is not None:
                        node_defs.add(self.node_definition(num))

                # Add edges
                for i in range(len(path) - 1):
                    id_from = self._mermaid_node_id(path[i])
                    id_to = self._mermaid_node_id(path[i+1])

                    # If it's the last hop, we can add SNR details if available
                    label = ""
                    if i == len(path) - 2:
                        snrs = getattr(node, "last_route_snr_towards", []) or []
                        if snrs and snrs[-1] is not None and snrs[-1] != -128:
                            label = f"{snrs[-1]/4.0:+.1f}dB"

                    edges.add((id_from, id_to, label))

        # Generate content
        lines = []
        lines.append("# Meshtastic Network Topology")
        lines.append(f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        lines.append("```mermaid")
        lines.append("graph TD")

        # Node definitions
        for d in sorted(node_defs):
            lines.append(f"  {d}")
        lines.append("")

        # Edges
        for n_from, n_to, label in sorted(edges):
            if label:
                lines.append(f"  {n_from} -->|{label}| {n_to}")
            else:
                lines.append(f"  {n_from} --> {n_to}")

        # Highlight local node in green
        lines.append("  style ME fill:#2e7d32,stroke:#1b5e20,stroke-width:3px,color:#fff")

        lines.append("```")
        content = "\n".join(lines)

        # Write to file
        try:
            try:
                base_dir = Path(__file__).resolve().parent
            except Exception:
                base_dir = Path(".").resolve()
            filepath = base_dir / "topology.md"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

            notification = MessageInfo(
                timestamp=time.time(),
                direction="IN",
                from_id="SYSTEM",
                from_short="SYS",
                to_id="ME",
                to_short="ME",
                text="Network topology exported to topology.md",
                portnum="TEXT_MESSAGE_APP",
                status="RECEIVED"
            )

            with self.data_lock:
                self.messages.append(notification)
        except Exception:
            pass

    def _auto_trace_one(self, num: int, node_name: str, last_attempt_times: dict) -> bool:
        """Trace one node, wait for the reply, then pause 30 s. Returns True if it answered."""
        with self.data_lock:
            node = self.nodes.get(num)
            if not node or not node.last_seen:
                return True  # nothing to do; don't queue a retry
        # wait for any manual trace (or a dropped connection) to clear
        while self.running and (self._trace_active() or self.iface is None):
            time.sleep(1)
        if not self.running:
            return True

        self.auto_trace_active_node = node_name
        last_attempt_times[num] = time.time()
        answered = False
        try:
            inflight = self._send_traceroute(num, "auto")
            while self.running and not inflight["done"].wait(0.2):
                if time.time() >= inflight["deadline"]:
                    self._finish_trace(inflight, ok=False)
            answered = inflight["ok"]
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            log.warning("auto-trace to %s failed: %s", node_name, e)
        finally:
            self.auto_trace_active_node = None

        # Keep 30 sec pause between scans to avoid congesting the channel
        for _ in range(300):
            if not self.running:
                break
            time.sleep(0.1)
        return answered

    def _auto_trace_loop(self):
        # Wait until the first non-local node is SEEN
        while self.running:
            has_seen_node = False
            with self.data_lock:
                for num, node in self.nodes.items():
                    if num != self.local_num and not node.is_me and getattr(node, "last_seen", 0) > 0:
                        has_seen_node = True
                        break
            if has_seen_node:
                break
            time.sleep(1)
        
        # Dictionary to track the timestamp of the last trace attempt for each node number
        last_attempt_times = {}
        
        while self.running:
            now = time.time()
            eligible_nodes = []
            with self.data_lock:
                for num, node in self.nodes.items():
                    if num == self.local_num or node.is_me:
                        continue
                    seen_t = getattr(node, "last_seen", 0) or 0
                    if seen_t > 0:
                        last_att = last_attempt_times.get(num, 0)
                        last_route_t = getattr(node, "last_route_time", 0) or 0
                        
                        needs_trace = False
                        if last_route_t > 0:
                            # We have trace info. Rescan every 24 hours.
                            if now - last_route_t >= 86400:
                                needs_trace = True
                        else:
                            # We do NOT have trace info. Try if never attempted,
                            # or if heard from again since our last attempt (with a 5m cooldown).
                            if last_att == 0:
                                needs_trace = True
                            elif seen_t > last_att and (now - last_att >= 300):
                                needs_trace = True
                                
                        if needs_trace:
                            eligible_nodes.append((num, node.short_name or node.long_name or f"#{node.num}"))
            
            if eligible_nodes:
                try:
                    self.auto_trace_in_progress = True
                    # Pass 1 traces every eligible node, pass 2 retries the silent ones once
                    no_answer_nodes = []
                    for num, node_name in eligible_nodes:
                        if not self.running:
                            break
                        if not self._auto_trace_one(num, node_name, last_attempt_times):
                            no_answer_nodes.append((num, node_name))
                    for num, node_name in no_answer_nodes:
                        if not self.running:
                            break
                        self._auto_trace_one(num, f"{node_name} (retry)", last_attempt_times)
                finally:
                    self.auto_trace_active_node = None
                    self.auto_trace_in_progress = False

            # Short sleep before checking for new eligible nodes
            for _ in range(5):
                if not self.running:
                    break
                time.sleep(1)

    def run(self):
        try:
            self.connect()
            # Start auto-trace and connection-watchdog daemon threads
            self.auto_trace_thread = threading.Thread(target=self._auto_trace_loop, daemon=True, name="auto-trace")
            self.auto_trace_thread.start()
            threading.Thread(target=self._connection_supervisor, daemon=True, name="conn-supervisor").start()
            # The meshtastic library print()s tracebacks and messages from its threads;
            # send them to the log file instead of scribbling over the curses screen.
            saved = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = _LogWriter()
            try:
                curses.wrapper(self.run_curses)
            finally:
                sys.stdout, sys.stderr = saved
        finally:
            self.running = False
            if getattr(self, "archiver", None):
                try:
                    self.archiver.close()
                except Exception:
                    pass
            if self.iface:
                try:
                    self.iface.close()
                except Exception:
                    pass


class _LogWriter:
    """File-like object that forwards writes to the meshtop log."""

    def write(self, data):
        data = data.rstrip()
        if data:
            logging.getLogger("meshtop.stdout").warning(data)
        return len(data)

    def flush(self):
        pass

    def isatty(self):
        return False


def setup_logging():
    base_dir = Path(__file__).resolve().parent
    handler = logging.FileHandler(base_dir / "meshtop.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]  # nothing goes to the terminal while curses runs
    root.setLevel(logging.INFO)


def get_local_subnet():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.254.254.254', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    
    if ip == '127.0.0.1':
        return None
    
    parts = ip.split('.')
    if len(parts) == 4:
        return '.'.join(parts[:3]) + '.'
    return None


def check_ip(ip, port=4403, timeout=0.8):
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            if sock.connect_ex((ip, port)) == 0:
                return ip
    except Exception:
        pass
    return None


def scan_network():
    from concurrent.futures import ThreadPoolExecutor
    subnet = get_local_subnet()
    if not subnet:
        return []
    
    ips = [f"{subnet}{i}" for i in range(1, 255)]
    found_ips = []
    
    print(f"Scanning local subnet {subnet}0/24 for Meshtastic devices on port 4403...")
    with ThreadPoolExecutor(max_workers=100) as executor:
        results = executor.map(lambda ip: check_ip(ip), ips)
        for r in results:
            if r:
                found_ips.append(r)
                
    return found_ips


def main():
    parser = argparse.ArgumentParser(description="Meshtastic live curses viewer")
    parser.add_argument("--host", required=False, help="Meshtastic device IP/hostname")
    parser.add_argument("--port", type=int, default=4403)
    args = parser.parse_args()
    setup_logging()

    host = args.host
    if not host:
        found_ips = scan_network()
        
        if len(found_ips) == 1:
            host = found_ips[0]
            print(f"Found Meshtastic device at {host}. Connecting...")
            time.sleep(1)
        elif len(found_ips) > 1:
            print("\nMultiple Meshtastic devices found:")
            for idx, ip in enumerate(found_ips, 1):
                print(f"[{idx}] {ip}")
            while True:
                try:
                    choice = input(f"Choose device (1-{len(found_ips)}): ").strip()
                    choice_idx = int(choice) - 1
                    if 0 <= choice_idx < len(found_ips):
                        host = found_ips[choice_idx]
                        break
                except ValueError:
                    pass
                print("Invalid choice. Please enter a number in range.")
        else:
            print("\nNo Meshtastic devices detected automatically on port 4403.")
            host = input("Please enter device IP address manually: ").strip()
            if not host:
                print("No IP entered. Exiting.")
                return

    app = MeshTopApp(host, args.port)
    app.run()


if __name__ == "__main__":
    main()
