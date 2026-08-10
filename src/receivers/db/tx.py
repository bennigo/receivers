"""Transaction hygiene for connections whose lifetime this code does not own.

psycopg2 opens a transaction on the FIRST statement — a bare ``SELECT`` counts.
A function that reads through a long-lived or caller-owned connection and then
neither commits nor rolls back leaves that connection parked in
``idle in transaction`` for as long as its owner keeps it.

Measured on rek-d01 2026-08-10, twice:

  * ``FormatResolver`` caching ``storage_location`` — 68 and 59 minutes;
  * ``archive.state.get_last_success`` reading the sync watermark — 25 minutes,
    found only because a monitor was watching while the first fix was already
    deployed.

Why it is worse than a wasted session:

  * an open transaction pins the vacuum ``xmin`` horizon, so VACUUM cannot
    reclaim dead tuples on a table taking tens of millions of updates; and
  * ``CREATE``/``DROP INDEX CONCURRENTLY`` can never finish — it waits for
    every transaction that could see the index, and there was always one open.
    Migration 065 died on ``lock_timeout`` six times before the sessions were
    terminated by hand.

**When NOT to use this.** Only for a read that cannot follow a write on the
same connection. A read interleaved into a caller's write transaction (see
``archive.reindex._existing_sha``, called inside the per-file upsert loop) must
be left alone: rolling back there would discard the caller's pending writes.
The rule is "safe iff the read is the first statement", not "reads always roll
back". ``tests/test_transaction_audit.py`` enforces the distinction with an
explicit allowlist.
"""

from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def read_only_cursor(conn):
    """Cursor for a read that ALWAYS ends its transaction.

    ``rollback()`` rather than ``commit()`` on purpose: if a caller ever leaves
    a write pending on this connection, discarding it is the safer of the two
    failures. Cleanup is best-effort — a connection that cannot roll back must
    not turn a successful read into an exception.
    """
    with conn.cursor() as cur:
        try:
            yield cur
        finally:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 — cleanup must never break the read
                pass
