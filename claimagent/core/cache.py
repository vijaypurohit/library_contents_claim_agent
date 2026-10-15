"""DiskCache: one small SQLite file that remembers model answers and fetched web pages.

Why: rerunning a stage after a code fix should not pay for, or wait for, the same model call again,
and tests can replay real responses without network access.

Entries are grouped by namespace ("llm", "web", ...) and looked up by a hash of everything that
defines the request (model name, prompt, image bytes). Identical request -> identical key -> cached answer.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path


class DiskCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # stages may call models from several threads
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS entries ("
            " namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
            " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " PRIMARY KEY (namespace, key))"
        )
        self._db.commit()

    @staticmethod
    def key(*parts: str | bytes) -> str:
        """SHA-256 over all parts. Each part is length-prefixed so ("ab", "c") and ("a", "bc") differ."""
        digest = hashlib.sha256()
        for part in parts:
            data = part.encode() if isinstance(part, str) else part
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        return digest.hexdigest()

    def get(self, namespace: str, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM entries WHERE namespace = ? AND key = ?", (namespace, key)
            ).fetchone()
        return row[0] if row else None

    def set(self, namespace: str, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO entries (namespace, key, value) VALUES (?, ?, ?)", (namespace, key, value)
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
