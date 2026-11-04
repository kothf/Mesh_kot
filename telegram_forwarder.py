"""
Forward incoming Meshtastic text messages to a Telegram chat via the Bot API.

Configuration (environment variables win over the config file):
  TELEGRAM_BOT_TOKEN   bot token from @BotFather
  TELEGRAM_CHAT_ID     destination chat/channel id (optional, see below)

or a JSON file `telegram.json` next to meshtop_live.py (git-ignored):
  {"bot_token": "123456:ABC...", "chat_id": 123456789}

If no chat id is configured, the forwarder looks at the bot's recent updates
(getUpdates) and uses the chat that last messaged the bot - so just send
/start to your bot once. The detected id is saved into telegram.json.

Only the standard library is used. Sending happens on a background thread so
the curses UI and the radio callbacks never block on the network.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("meshtop.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4096  # Telegram message length limit


def _config_path() -> Path:
    return Path(__file__).resolve().parent / "telegram.json"


def _load_file_config() -> dict:
    p = _config_path()
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("cannot read %s: %s", p, e)
        return {}


class TelegramForwarder:
    def __init__(self, token: str, chat_id: Optional[str] = None):
        self.token = token
        self.chat_id = str(chat_id) if chat_id not in (None, "") else None
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=500)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="telegram")
        self._thread.start()

    # ---------- construction ----------

    @classmethod
    def from_config(cls) -> Optional["TelegramForwarder"]:
        """Build a forwarder from env/config, or return None if not configured."""
        cfg = _load_file_config()
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("bot_token")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("chat_id")
        if not token:
            return None
        log.info("Telegram forwarding enabled (chat_id=%s)", chat_id or "auto-detect")
        return cls(str(token).strip(), chat_id)

    # ---------- public API ----------

    def forward(self, text: str):
        """Queue a message for delivery; never blocks, drops if the queue is full."""
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "…"
        try:
            self._q.put_nowait(text)
        except queue.Full:
            log.warning("Telegram queue full, dropping message")

    # ---------- internals ----------

    def _call(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        url = API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error_code": e.code, "description": str(e)}

    def _detect_chat_id(self) -> Optional[str]:
        """Use the most recent chat that messaged the bot."""
        try:
            r = self._call("getUpdates", {"limit": 100, "timeout": 0})
        except Exception as e:
            log.warning("Telegram getUpdates failed: %s", e)
            return None
        if not r.get("ok"):
            log.warning("Telegram getUpdates error: %s", r.get("description"))
            return None
        for upd in reversed(r.get("result") or []):
            for key in ("message", "channel_post", "my_chat_member", "edited_message"):
                chat = (upd.get(key) or {}).get("chat")
                if chat and "id" in chat:
                    cid = str(chat["id"])
                    self._save_chat_id(cid)
                    log.info("Telegram chat id auto-detected: %s", cid)
                    return cid
        return None

    def _save_chat_id(self, cid: str):
        p = _config_path()
        cfg = _load_file_config()
        if str(cfg.get("chat_id", "")) == cid:
            return
        cfg["chat_id"] = int(cid) if cid.lstrip("-").isdigit() else cid
        try:
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except Exception as e:
            log.warning("cannot save chat id to %s: %s", p, e)

    def _send(self, text: str) -> bool:
        """Send one message, retrying on rate limits / network errors."""
        for attempt in range(5):
            if not self.chat_id:
                self.chat_id = self._detect_chat_id()
                if not self.chat_id:
                    time.sleep(30)  # wait for the user to /start the bot
                    continue
            try:
                r = self._call("sendMessage", {
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": "true",
                })
            except Exception as e:
                log.warning("Telegram send failed (attempt %d): %s", attempt + 1, e)
                time.sleep(min(60, 2 ** attempt * 2))
                continue
            if r.get("ok"):
                return True
            code = r.get("error_code")
            if code == 429:
                wait = (r.get("parameters") or {}).get("retry_after", 5)
                time.sleep(float(wait) + 0.5)
                continue
            log.warning("Telegram sendMessage error %s: %s", code, r.get("description"))
            if code in (400, 403) and "chat" in str(r.get("description", "")).lower():
                self.chat_id = None  # wrong/blocked chat: try to re-detect next time
            return False
        return False

    def _worker(self):
        while True:
            text = self._q.get()
            try:
                self._send(text)
            except Exception:
                log.exception("Telegram worker error")
            time.sleep(1.0)  # stay under Telegram's ~1 msg/s per chat limit
