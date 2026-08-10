"""Per-target sync watermark — the moving floor that bounds each run's work.

floor = max(last_success_ts - overlap, cutover). The watermark advances ONLY on
a fully-successful run, so a partial failure re-tries its frontier next run
instead of silently skipping it. Backed by the ``sync_state`` table (migration
051). See design 1781867391 decision 2.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from ..db.tx import read_only_cursor

logger = logging.getLogger(__name__)


def get_last_success(conn, target_name: str) -> Optional[datetime]:
    """Return the target's last fully-synced frontier, or ``None`` if never."""
    with read_only_cursor(conn) as cur:
        cur.execute(
            "SELECT last_success_ts FROM sync_state WHERE target = %s",
            (target_name,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def compute_floor(
    last_success: Optional[datetime], cutover: datetime, overlap_minutes: int
) -> datetime:
    """The mtime floor for the next delta scan.

    First run (no watermark) starts at ``cutover`` so legacy-era files never
    enter the delta. Subsequent runs back off by ``overlap_minutes`` to cover the
    mtime-boundary / clock-skew race, but never below ``cutover``.
    """
    if last_success is None:
        return cutover
    backed_off = last_success - timedelta(minutes=overlap_minutes)
    return max(backed_off, cutover)


def record_run(
    conn,
    target_name: str,
    *,
    ran_at: datetime,
    files: int,
    ok: bool,
    advance_to: Optional[datetime],
) -> None:
    """Persist a run's outcome; advance ``last_success_ts`` only when ``ok``.

    ``advance_to`` is the new frontier (the scan-start time captured BEFORE the
    delta find) and is written to ``last_success_ts`` only on a fully-successful
    run. On failure the previous watermark is preserved (COALESCE keeps it).
    """
    new_success = advance_to if ok else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_state
                (target, last_success_ts, last_run_at, last_run_files,
                 last_run_ok, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (target) DO UPDATE SET
                last_success_ts = COALESCE(EXCLUDED.last_success_ts,
                                           sync_state.last_success_ts),
                last_run_at     = EXCLUDED.last_run_at,
                last_run_files  = EXCLUDED.last_run_files,
                last_run_ok     = EXCLUDED.last_run_ok,
                updated_at      = now()
            """,
            (target_name, new_success, ran_at, files, ok),
        )
    conn.commit()
