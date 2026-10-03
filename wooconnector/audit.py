"""Thread-safe audit trail: ring buffer for live UIs + optional JSONL file for compliance."""
from __future__ import annotations
import json, threading, time, os
from collections import deque


class Audit:
    def __init__(self, maxlen: int = 500, path: str | None = None):
        self._buf: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self._path = path or os.environ.get("WOO_AUDIT_FILE")

    def record(self, kind: str, **fields) -> dict:
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "ts": round(time.time(), 3), "kind": kind, **fields}
            self._buf.append(ev)
            if self._path:
                try:
                    with open(self._path, "a") as f:
                        f.write(json.dumps(ev, default=str) + "\n")
                except OSError:
                    pass
        return ev

    def since(self, seq: int = 0) -> list[dict]:
        with self._lock:
            return [e for e in self._buf if e["seq"] > seq]
