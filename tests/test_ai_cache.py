"""
The AI answer cache keeps answers that are used and deletes ones unused for
``MAX_AGE_DAYS``, including in cache files written by older versions.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from archilens.ai.cache import MAX_AGE_DAYS, AICache

DAY = 86_400.0


def _age(path: Path, key: str, days: float, column: str = "used_at") -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(f"UPDATE responses SET {column} = ? WHERE key = ?", (time.time() - days * DAY, key))
    conn.commit()
    conn.close()


def _keys(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    keys = {row[0] for row in conn.execute("SELECT key FROM responses")}
    conn.close()
    return keys


def test_default_keeps_answers_for_90_days():
    assert MAX_AGE_DAYS == 90


def test_round_trip(tmp_path: Path):
    cache = AICache(tmp_path / "c.db")
    cache.put("k", {"summary": "s"})
    assert cache.get("k") == {"summary": "s"}
    assert cache.get("missing") is None


def test_answers_unused_for_too_long_are_pruned_on_the_next_run(tmp_path: Path):
    path = tmp_path / "c.db"
    first_run = AICache(path, max_age_days=30)
    first_run.put("old", {"a": 1})
    first_run.put("recent", {"a": 2})
    _age(path, "old", 31)
    _age(path, "recent", 29)

    next_run = AICache(path, max_age_days=30)
    assert next_run.get("recent") == {"a": 2}
    assert _keys(path) == {"recent"}


def test_reading_an_answer_keeps_it(tmp_path: Path):
    path = tmp_path / "c.db"
    cache = AICache(path, max_age_days=30)
    cache.put("k", {"a": 1})
    _age(path, "k", 29, column="used_at")
    _age(path, "k", 400, column="saved_at")  # saved long ago, but used recently
    assert cache.get("k") == {"a": 1}  # refreshes used_at

    _age(path, "k", 400, column="saved_at")
    assert AICache(path, max_age_days=30).get("k") == {"a": 1}


def test_cache_from_before_used_at_is_migrated(tmp_path: Path):
    path = tmp_path / "c.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE responses (key TEXT PRIMARY KEY, response TEXT NOT NULL, saved_at REAL NOT NULL)")
    conn.executemany(
        "INSERT INTO responses VALUES (?, ?, ?)",
        [("fresh", '{"a": 1}', time.time()), ("stale", '{"a": 2}', time.time() - 200 * DAY)],
    )
    conn.commit()
    conn.close()

    cache = AICache(path)
    assert cache.get("fresh") == {"a": 1}
    assert cache.get("stale") is None  # pruned by its saved_at, the only date it has
    cache.put("new", {"a": 3})
    assert _keys(path) == {"fresh", "new"}


def test_prunes_once_per_run_not_per_request(tmp_path: Path):
    path = tmp_path / "c.db"
    cache = AICache(path, max_age_days=30)
    cache.put("k", {"a": 1})
    _age(path, "k", 31)
    # Already prepared this run: an answer that ages out mid-run is still served.
    assert cache.get("k") == {"a": 1}
