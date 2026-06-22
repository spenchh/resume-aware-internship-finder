"""SQLite cache (spec Section 5).

Keyed by the normalized (company + role + location) dedup key. Tracks:
  * first_seen  — the earliest run that observed a listing. Used as a freshness
                  fallback when a source exposes no explicit date (Section 6.2.b).
  * last_status — last live-check verdict.
  * per-run reported keys — to produce the "N new / M closed since last run" diff.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .models import Listing

log = logging.getLogger("internfinder.cache")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Cache:
    def __init__(self, db_path: str | Path = "cache.db"):
        self.path = str(db_path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS listings (
                key           TEXT PRIMARY KEY,
                company       TEXT,
                title         TEXT,
                location      TEXT,
                url           TEXT,
                source        TEXT,
                posted_date   TEXT,
                first_seen    TEXT NOT NULL,
                last_seen     TEXT NOT NULL,
                last_status   TEXT,
                last_verified TEXT,
                data_json     TEXT
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                n_listings  INTEGER DEFAULT 0,
                n_reported  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS run_listings (
                run_id      INTEGER NOT NULL,
                key         TEXT NOT NULL,
                reported    INTEGER NOT NULL DEFAULT 0,
                live_status TEXT,
                PRIMARY KEY (run_id, key)
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------- first-seen
    def get_first_seen(self, key: str) -> Optional[datetime]:
        row = self.conn.execute(
            "SELECT first_seen FROM listings WHERE key = ?", (key,)
        ).fetchone()
        if row and row["first_seen"]:
            try:
                return datetime.fromisoformat(row["first_seen"])
            except ValueError:
                return None
        return None

    def observe(self, listing: Listing) -> datetime:
        """Record that we saw this listing now. Returns its first_seen timestamp.

        Also back-fills ``listing.first_seen`` so the freshness validator can use
        it as a fallback date signal.
        """
        now = _utcnow()
        key = listing.cache_key()
        existing = self.get_first_seen(key)
        first_seen = existing or now
        self.conn.execute(
            """
            INSERT INTO listings (key, company, title, location, url, source,
                                  posted_date, first_seen, last_seen, data_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                last_seen=excluded.last_seen,
                url=excluded.url,
                source=excluded.source,
                posted_date=COALESCE(excluded.posted_date, listings.posted_date)
            """,
            (
                key, listing.company, listing.title, listing.location, listing.apply_url,
                listing.source, listing.posted_display if listing.posted_date else None,
                first_seen.isoformat(), now.isoformat(), json.dumps(listing.to_dict()),
            ),
        )
        self.conn.commit()
        listing.first_seen = first_seen
        return first_seen

    def record_verification(self, key: str, status: str, ts: Optional[datetime] = None) -> None:
        ts = ts or _utcnow()
        self.conn.execute(
            "UPDATE listings SET last_status=?, last_verified=? WHERE key=?",
            (status, ts.isoformat(), key),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- run diff
    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (_utcnow().isoformat(),)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, n_listings: int, n_reported: int) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, n_listings=?, n_reported=? WHERE run_id=?",
            (_utcnow().isoformat(), n_listings, n_reported, run_id),
        )
        self.conn.commit()

    def record_run_listing(self, run_id: int, listing: Listing, reported: bool) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO run_listings (run_id, key, reported, live_status)
               VALUES (?,?,?,?)""",
            (run_id, listing.cache_key(), int(reported), listing.live_status),
        )
        self.conn.commit()

    def previous_reported_keys(self, current_run_id: int) -> set[str]:
        """Keys reported in the most recent finished run before ``current_run_id``."""
        prev = self.conn.execute(
            "SELECT run_id FROM runs WHERE run_id < ? AND finished_at IS NOT NULL "
            "ORDER BY run_id DESC LIMIT 1",
            (current_run_id,),
        ).fetchone()
        if not prev:
            return set()
        rows = self.conn.execute(
            "SELECT key FROM run_listings WHERE run_id=? AND reported=1",
            (prev["run_id"],),
        ).fetchall()
        return {r["key"] for r in rows}

    def diff_since_last_run(
        self, current_run_id: int, current_reported: Iterable[Listing]
    ) -> tuple[list[str], list[str]]:
        """Return (new_keys, closed_keys) vs the previous run."""
        prev_keys = self.previous_reported_keys(current_run_id)
        cur_keys = {l.cache_key() for l in current_reported}
        new_keys = sorted(cur_keys - prev_keys)
        closed_keys = sorted(prev_keys - cur_keys)
        return new_keys, closed_keys

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
