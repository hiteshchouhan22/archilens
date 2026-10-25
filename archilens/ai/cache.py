"""
Content-addressed cache for AI responses.

A response is stored under a hash of everything that determines it: provider,
model, effort, output schema, prompt text and a prompt-format version. If any
of those change (including the code or outline inside the prompt), the key
changes and the model is asked again; otherwise the stored answer is reused.
That makes repeated runs cheaper and keeps summaries stable, so committed
diagrams don't change wording between runs on unchanged code.

Lives at ``~/.archilens/ai_cache.db`` (SQLite, safe for the parallel summary
threads). Disable with ``ai.cache: false`` in .archilens.yml.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path.home() / ".archilens" / "ai_cache.db"


class AICache:
    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, response TEXT NOT NULL, saved_at REAL NOT NULL)"
        )
        return conn

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT response FROM responses WHERE key = ?", (key,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.debug("AI cache read failed: %s", exc)
            return None
        if row is None:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None

    def put(self, key: str, response: dict[str, Any]) -> None:
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO responses VALUES (?, ?, ?)",
                    (key, json.dumps(response, sort_keys=True), time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.debug("AI cache write failed: %s", exc)


def cache_key(**parts: Any) -> str:
    """Stable hash of the request parts (order-independent, JSON-serialisable values)."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()
