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

Answers not used for ``MAX_AGE_DAYS`` are deleted, once per run, so the file
doesn't grow forever on a developer machine or in a CI cache.
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

#: Answers unused for this long are deleted.
MAX_AGE_DAYS = 90
_DAY = 86_400.0


class AICache:
    def __init__(self, path: Path = DEFAULT_PATH, max_age_days: float = MAX_AGE_DAYS) -> None:
        self.path = path
        self.max_age_days = max_age_days
        self._prepared = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        if not self._prepared:
            self._prepare(conn)
            self._prepared = True
        return conn

    def _prepare(self, conn: sqlite3.Connection) -> None:
        """Create or migrate the table, then delete answers unused for ``max_age_days``."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS responses "
            "(key TEXT PRIMARY KEY, response TEXT NOT NULL, saved_at REAL NOT NULL, used_at REAL)"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(responses)")}
        if "used_at" not in columns:  # caches written before 0.12.1
            conn.execute("ALTER TABLE responses ADD COLUMN used_at REAL")
        cutoff = time.time() - self.max_age_days * _DAY
        pruned = conn.execute("DELETE FROM responses WHERE COALESCE(used_at, saved_at) < ?", (cutoff,)).rowcount
        conn.commit()
        if pruned:
            logger.debug("AI cache: removed %d answers unused for %g days", pruned, self.max_age_days)

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT response FROM responses WHERE key = ?", (key,)).fetchone()
                if row is not None:
                    # Refresh at most once a day, so reads rarely write.
                    now = time.time()
                    conn.execute(
                        "UPDATE responses SET used_at = ? WHERE key = ? AND COALESCE(used_at, saved_at) < ?",
                        (now, key, now - _DAY),
                    )
                    conn.commit()
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
                    "INSERT OR REPLACE INTO responses (key, response, saved_at, used_at) VALUES (?, ?, ?, ?)",
                    (key, json.dumps(response, sort_keys=True), time.time(), time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.debug("AI cache write failed: %s", exc)


def cache_key(**parts: Any) -> str:
    """Stable hash of the request parts (order-independent, JSON-serialisable values)."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()
