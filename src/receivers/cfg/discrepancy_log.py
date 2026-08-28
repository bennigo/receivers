"""Persistence layer for cfg/receiver/TOS discrepancies.

Backed by the ``cfg_discrepancy`` table (migration 038). One open row per
``(station_id, cfg_key)`` at a time; subsequent detections of the same
drift reuse the open row, so the log does not grow on every health probe.

Detection callers should pass a ``detected_by`` tag describing the origin
(``health_probe``, ``cfg_reconcile``, ``scheduler``) so the audit trail
can distinguish ad-hoc reconcile runs from automated background sweeps.

All writers swallow database errors after logging them — a missing or
unreachable ``cfg_discrepancy`` table must not break the surrounding
health/reconcile flow. Readers (:func:`list_open`, :func:`get_history`)
propagate errors so operator commands fail loudly instead of silently
returning empty lists.
"""

from __future__ import annotations

import getpass
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# detected_by tags
DETECTED_BY_HEALTH = "health_probe"
DETECTED_BY_RECONCILE = "cfg_reconcile"
DETECTED_BY_SCHEDULER = "scheduler"
# Recorded when the download path observes that the FTP mode in stations.cfg
# differs from the mode that actually works (typically passive→active fallback
# succeeds where the cfg-declared mode failed). receiver_value carries the
# observed working mode; cfg_value carries the originally-configured mode.
DETECTED_BY_FTP_HANDSHAKE = "ftp_handshake"

# resolved_action values
ACTION_CFG_UPDATED = "cfg_updated"
ACTION_TOS_UPDATED = "tos_updated"
ACTION_AUTO_RESOLVED = "auto-resolved"
ACTION_SUPERSEDED = "superseded"
ACTION_IGNORED = "ignored"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DiscrepancyRecord:
    id: int
    station_id: str
    cfg_key: str
    cfg_value: Optional[str]
    receiver_value: Optional[str]
    tos_value: Optional[str]
    verdict: str
    detected_at: datetime
    detected_by: str
    resolved_at: Optional[datetime]
    resolved_by: Optional[str]
    resolved_action: Optional[str]
    resolved_value: Optional[str]
    resolution_note: Optional[str]

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "station_id": self.station_id,
            "cfg_key": self.cfg_key,
            "cfg_value": self.cfg_value,
            "receiver_value": self.receiver_value,
            "tos_value": self.tos_value,
            "verdict": self.verdict,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "detected_by": self.detected_by,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "resolved_action": self.resolved_action,
            "resolved_value": self.resolved_value,
            "resolution_note": self.resolution_note,
        }


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def _current_user() -> str:
    """Best-effort identifier for ``resolved_by`` / ``detected_by`` audit fields."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def _row_to_record(row: Sequence[Any]) -> DiscrepancyRecord:
    return DiscrepancyRecord(
        id=row[0],
        station_id=row[1],
        cfg_key=row[2],
        cfg_value=row[3],
        receiver_value=row[4],
        tos_value=row[5],
        verdict=row[6],
        detected_at=row[7],
        detected_by=row[8],
        resolved_at=row[9],
        resolved_by=row[10],
        resolved_action=row[11],
        resolved_value=row[12],
        resolution_note=row[13],
    )


_ALL_COLS = (
    "id, station_id, cfg_key, cfg_value, receiver_value, tos_value, "
    "verdict, detected_at, detected_by, resolved_at, resolved_by, "
    "resolved_action, resolved_value, resolution_note"
)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _detect_on_cursor(
    cur: Any,
    station_id: str,
    cfg_key: str,
    *,
    cfg_value: Optional[str],
    receiver_value: Optional[str],
    tos_value: Optional[str],
    verdict: str,
    detected_by: str,
) -> Optional[int]:
    """The record-a-detection SQL, on a caller-supplied cursor.

    Split out so a single-field caller and a whole-station batch run the exact
    same statements in the same order — the alternative is two copies of a
    supersede-then-insert sequence, which is how audit logic drifts.

    The caller owns the transaction AND the advisory lock.
    """
    cur.execute(
        """
        SELECT id, cfg_value, receiver_value, tos_value, verdict
        FROM cfg_discrepancy
        WHERE station_id = %s AND cfg_key = %s AND resolved_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (station_id, cfg_key),
    )
    existing = cur.fetchone()

    if existing is not None:
        existing_id, e_cfg, e_rx, e_tos, e_verdict = existing
        if (e_cfg, e_rx, e_tos, e_verdict) == (
            cfg_value,
            receiver_value,
            tos_value,
            verdict,
        ):
            return int(existing_id)

        # Keyed on the natural key (station_id, cfg_key, still-open), NOT
        # ``existing_id``: this connection may fan out to the pgdev mirror,
        # whose serial ids are assigned independently — an id-keyed UPDATE
        # would resolve an unrelated discrepancy there. Same predicate as
        # ``record_resolution``.
        cur.execute(
            """
            UPDATE cfg_discrepancy
            SET resolved_at = NOW(),
                resolved_by = %s,
                resolved_action = %s,
                resolution_note = 'Detection values changed'
            WHERE station_id = %s
              AND cfg_key = %s
              AND resolved_at IS NULL
            """,
            (detected_by, ACTION_SUPERSEDED, station_id, cfg_key),
        )

    cur.execute(
        """
        INSERT INTO cfg_discrepancy
          (station_id, cfg_key, cfg_value, receiver_value,
           tos_value, verdict, detected_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            station_id,
            cfg_key,
            cfg_value,
            receiver_value,
            tos_value,
            verdict,
            detected_by,
        ),
    )
    return int(cur.fetchone()[0])


def _resolve_on_cursor(
    cur: Any,
    station_id: str,
    cfg_key: str,
    *,
    action: str,
    resolved_value: Optional[str],
    resolved_by: str,
    note: Optional[str],
) -> bool:
    """The mark-resolved SQL, on a caller-supplied cursor."""
    cur.execute(
        """
        UPDATE cfg_discrepancy
        SET resolved_at = NOW(),
            resolved_by = %s,
            resolved_action = %s,
            resolved_value = %s,
            resolution_note = COALESCE(%s, resolution_note)
        WHERE station_id = %s
          AND cfg_key = %s
          AND resolved_at IS NULL
        """,
        (resolved_by, action, resolved_value, note, station_id, cfg_key),
    )
    return cur.rowcount > 0


def record_detection(
    station_id: str,
    cfg_key: str,
    *,
    cfg_value: Optional[str],
    receiver_value: Optional[str],
    tos_value: Optional[str],
    verdict: str,
    detected_by: str,
) -> Optional[int]:
    """Record a detected discrepancy.

    Idempotent: if an open row already exists for this ``(station_id,
    cfg_key)`` with the same values and verdict, returns its id without
    inserting. If an open row exists with different values, the old row
    is marked superseded and a fresh row is inserted (preserving history
    of how the drift evolved).

    Returns the row id, or ``None`` if the database is unavailable.

    Concurrency: serialized per (station_id, cfg_key) by a transaction-
    scoped advisory lock. Two callers (e.g. health probe + interactive
    CLI) racing on the same key will queue rather than collide on the
    partial unique index. Lock is released automatically at COMMIT or
    ROLLBACK.
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory
    except Exception as exc:  # noqa: BLE001
        logger.debug("database_factory unavailable: %s", exc)
        return None

    try:
        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                # Serialize concurrent writers for the same (station, key).
                # hashtext() can collide across distinct strings, but that
                # only causes spurious serialization — never wrong rows.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (station_id, cfg_key),
                )
                return _detect_on_cursor(
                    cur,
                    station_id,
                    cfg_key,
                    cfg_value=cfg_value,
                    receiver_value=receiver_value,
                    tos_value=tos_value,
                    verdict=verdict,
                    detected_by=detected_by,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[%s] failed to record cfg discrepancy for %s: %s",
            station_id,
            cfg_key,
            exc,
        )
        return None


def record_resolution(
    station_id: str,
    cfg_key: str,
    *,
    action: str,
    resolved_value: Optional[str],
    resolved_by: Optional[str] = None,
    note: Optional[str] = None,
) -> bool:
    """Mark the open row for ``(station_id, cfg_key)`` as resolved.

    Returns True if a row was updated, False if no open row existed (or
    on database failure).
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory
    except Exception as exc:  # noqa: BLE001
        logger.debug("database_factory unavailable: %s", exc)
        return False

    by = resolved_by or _current_user()
    try:
        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                return _resolve_on_cursor(
                    cur,
                    station_id,
                    cfg_key,
                    action=action,
                    resolved_value=resolved_value,
                    resolved_by=by,
                    note=note,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[%s] failed to record cfg resolution for %s: %s",
            station_id,
            cfg_key,
            exc,
        )
        return False


def auto_resolve_if_open(
    station_id: str,
    cfg_key: str,
    *,
    note: str = "Values converged without operator action",
) -> bool:
    """Close any open row because cfg now agrees with the queried sources.

    Called from :func:`receivers.cfg.reconciler.compare_station` when a
    field comes back ``OK`` — i.e. a previously-flagged drift has
    self-healed (cfg edited by hand, receiver re-flashed, etc.). Marks
    ``resolved_action = 'auto-resolved'`` so the audit trail captures
    that no operator did this through ``cfg reconcile``.
    """
    return record_resolution(
        station_id,
        cfg_key,
        action=ACTION_AUTO_RESOLVED,
        resolved_value=None,
        resolved_by="auto",
        note=note,
    )


@dataclass(frozen=True)
class LogSyncEntry:
    """One field's outcome, ready to be written to the log.

    ``verdict is None`` means "close any open row" (the field came back OK and
    every source that could speak was asked); otherwise it is a detection.
    """

    cfg_key: str
    verdict: Optional[str] = None
    cfg_value: Optional[str] = None
    receiver_value: Optional[str] = None
    tos_value: Optional[str] = None


def sync_station(
    station_id: str,
    entries: Sequence[LogSyncEntry],
    *,
    detected_by: str,
    auto_resolve_note: str = "Values converged without operator action",
) -> int:
    """Write a whole station's log outcomes over ONE connection.

    Architecture review §4.7: this used to run inside ``compare_station``'s
    per-field loop, so every field opened its own connection and took its own
    advisory lock. ``reconcile --all`` is ~173 stations x ~15 fields — roughly
    2,600 sequential checkouts against a host with documented
    ``max_connections=100`` exhaustion.

    **Locking.** Field locks are still taken per ``(station, cfg_key)`` — NOT
    replaced by one station-wide lock — because a station lock uses a different
    key and would therefore no longer serialise against a concurrent per-field
    :func:`record_detection` from the health probe, which is the exact race the
    single-field docstring promises to handle. They are acquired in **sorted
    key order** so two batches can never deadlock; a single-field caller holds
    only one lock at a time and so can never be half of a cycle either.

    **Error isolation is per field**, matching the loop this replaces: one bad
    field loses its own row and nothing else. A failure to connect at all loses
    the station, which is what the old code did too (every field failed
    identically).

    Returns the number of entries written or closed.
    """
    if not entries:
        return 0

    try:
        from ..health.database_factory import DatabaseConnectionFactory
    except Exception as exc:  # noqa: BLE001
        logger.debug("database_factory unavailable: %s", exc)
        return 0

    # NB: resolved_by is the literal "auto", matching auto_resolve_if_open —
    # the point of that value is that no operator did this.
    done = 0
    try:
        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                for entry in sorted(entries, key=lambda e: e.cfg_key):
                    try:
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                            (station_id, entry.cfg_key),
                        )
                        if entry.verdict is None:
                            if _resolve_on_cursor(
                                cur,
                                station_id,
                                entry.cfg_key,
                                action=ACTION_AUTO_RESOLVED,
                                resolved_value=None,
                                resolved_by="auto",
                                note=auto_resolve_note,
                            ):
                                done += 1
                        else:
                            _detect_on_cursor(
                                cur,
                                station_id,
                                entry.cfg_key,
                                cfg_value=entry.cfg_value,
                                receiver_value=entry.receiver_value,
                                tos_value=entry.tos_value,
                                verdict=entry.verdict,
                                detected_by=detected_by,
                            )
                            done += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "[%s] discrepancy_log sync failed for %s: %s",
                            station_id,
                            entry.cfg_key,
                            exc,
                        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] discrepancy_log batch sync failed: %s", station_id, exc)
    return done


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def list_open(
    station_ids: Optional[Sequence[str]] = None,
    cfg_keys: Optional[Sequence[str]] = None,
    verdicts: Optional[Sequence[str]] = None,
) -> List[DiscrepancyRecord]:
    """Return all open discrepancies, optionally filtered.

    Raises whatever the database driver raises on failure — readers must
    fail loudly so an operator running ``cfg list`` sees the real error
    instead of an empty list.
    """
    from ..health.database_factory import DatabaseConnectionFactory

    sql = f"SELECT {_ALL_COLS} FROM cfg_discrepancy WHERE resolved_at IS NULL"
    params: List[Any] = []
    if station_ids:
        sql += " AND station_id = ANY(%s)"
        params.append(list(station_ids))
    if cfg_keys:
        sql += " AND cfg_key = ANY(%s)"
        params.append(list(cfg_keys))
    if verdicts:
        sql += " AND verdict = ANY(%s)"
        params.append(list(verdicts))
    sql += " ORDER BY station_id, cfg_key"

    with DatabaseConnectionFactory.connection(single_host=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row_to_record(row) for row in cur.fetchall()]


def get_history(
    station_id: Optional[str] = None,
    cfg_key: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 500,
) -> List[DiscrepancyRecord]:
    """Return historical rows for a station or field.

    At least one of ``station_id`` or ``cfg_key`` must be provided; an
    unbounded ``cfg history`` would dump the full table.
    """
    if station_id is None and cfg_key is None:
        raise ValueError("get_history requires station_id or cfg_key")

    from ..health.database_factory import DatabaseConnectionFactory

    sql = f"SELECT {_ALL_COLS} FROM cfg_discrepancy WHERE 1=1"
    params: List[Any] = []
    if station_id is not None:
        sql += " AND station_id = %s"
        params.append(station_id)
    if cfg_key is not None:
        sql += " AND cfg_key = %s"
        params.append(cfg_key)
    if since is not None:
        sql += " AND detected_at >= %s"
        params.append(since)
    sql += " ORDER BY detected_at DESC LIMIT %s"
    params.append(limit)

    with DatabaseConnectionFactory.connection(single_host=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row_to_record(row) for row in cur.fetchall()]


__all__ = [
    "ACTION_AUTO_RESOLVED",
    "ACTION_CFG_UPDATED",
    "ACTION_IGNORED",
    "ACTION_SUPERSEDED",
    "ACTION_TOS_UPDATED",
    "DETECTED_BY_HEALTH",
    "DETECTED_BY_RECONCILE",
    "DETECTED_BY_SCHEDULER",
    "DiscrepancyRecord",
    "auto_resolve_if_open",
    "get_history",
    "list_open",
    "open_station_ids",
    "open_field_keys",
    "record_detection",
    "record_resolution",
]


def open_station_ids(
    verdicts: Optional[Sequence[str]] = None,
    cfg_keys: Optional[Sequence[str]] = None,
) -> List[str]:
    """Unique station IDs that have at least one open discrepancy."""
    records = list_open(cfg_keys=cfg_keys, verdicts=verdicts)
    seen: set = set()
    return [
        r.station_id
        for r in records
        if not (r.station_id in seen or seen.add(r.station_id))
    ]


def open_field_keys(
    verdicts: Optional[Sequence[str]] = None,
    station_ids: Optional[Sequence[str]] = None,
) -> List[str]:
    """Unique cfg_key values that have at least one open discrepancy."""
    records = list_open(station_ids=station_ids, verdicts=verdicts)
    seen: set = set()
    return [
        r.cfg_key for r in records if not (r.cfg_key in seen or seen.add(r.cfg_key))
    ]
