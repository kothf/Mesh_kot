
from __future__ import annotations
import json, os, sys, re
from pathlib import Path
from threading import Lock
from datetime import datetime, timezone

MiB = 1024 * 1024

def _app_dir() -> Path:
    try:
        return Path(sys.argv[0]).resolve().parent
    except Exception:
        return Path(".").resolve()

class MessageArchiver:
    """
    Writes to a stable active file:   <base_name>.jsonl  (e.g., messages.jsonl)
    On size > max, rotates to:        <base_name>.<seq:03d>.jsonl  (e.g., messages.001.jsonl)
    On restart, it continues appending to <base_name>.jsonl so history persists across runs.
    """
    def __init__(self, base_name: str = 'messages', max_size_mib: int = 5):
        self.base_dir = _app_dir()
        self.base_name = base_name
        self.max_size = max_size_mib * MiB
        self._lock = Lock()
        self._fh = None
        self._size = 0
        self.seq = 0  # last completed rotation number
        self._init_seq_and_open()

    # Paths
    def _active_path(self) -> Path:
        return self.base_dir / f"{self.base_name}.jsonl"

    def _rotated_path(self, seq: int) -> Path:
        return self.base_dir / f"{self.base_name}.{seq:03d}.jsonl"

    def _init_seq_and_open(self):
        # Determine highest existing rotation number
        pattern = re.compile(rf'{re.escape(self.base_name)}\.(\d{{3}})\.jsonl$')
        max_seq = 0
        try:
            for p in self.base_dir.glob(f"{self.base_name}.*.jsonl"):
                m = pattern.search(p.name)
                if m:
                    max_seq = max(max_seq, int(m.group(1)))
        except Exception:
            pass
        self.seq = max_seq
        # Open (or create) the active file for append and record its current size
        active = self._active_path()
        self._fh = open(active, 'a', encoding='utf-8')
        try:
            os.chmod(active, 0o600)
        except Exception:
            pass
        try:
            self._size = active.stat().st_size
        except Exception:
            self._size = 0

    def _rotate_if_needed(self, next_len: int):
        if self._size + next_len <= self.max_size:
            return
        # Close current file and rename to next rotation
        try:
            self._fh.flush(); os.fsync(self._fh.fileno())
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self.seq += 1
        src = self._active_path()
        dst = self._rotated_path(self.seq)
        try:
            os.replace(src, dst)  # atomic on same filesystem
        except Exception:
            # If replace fails (e.g., on Windows), try fallback copy/remove
            try:
                import shutil
                shutil.copyfile(src, dst)
                os.remove(src)
            except Exception:
                pass
        # Reopen fresh active file
        self._fh = open(self._active_path(), 'a', encoding='utf-8')
        try:
            os.chmod(self._active_path(), 0o600)
        except Exception:
            pass
        self._size = 0  # start counting fresh

    def write(self, record: dict):
        # copy dict to avoid mutating caller's data
        rec_copy = dict(record)
        # sanitize controls in 'text'
        txt = rec_copy.get('text')
        if isinstance(txt, str):
            rec_copy['text'] = ''.join(ch if (ch >= ' ' or ch in '\n\t') else '\uFFFD' for ch in txt)
        line = json.dumps(rec_copy, ensure_ascii=False, separators=(',', ':')) + '\n'
        data = line.encode('utf-8')
        with self._lock:
            if self._fh is None or getattr(self._fh, 'closed', False):
                self._init_seq_and_open()
            self._rotate_if_needed(len(data))
            self._fh.write(line)
            self._fh.flush()
            self._size += len(data)

    def close(self):
        with self._lock:
            if self._fh is not None and not getattr(self._fh, 'closed', False):
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

