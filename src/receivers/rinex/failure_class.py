"""Classify a per-file batch failure as transient or permanent.

The retrofit runners process tens of thousands of files unattended, and their
failures are not all the same kind of thing:

* **Permanent** — the input or the metadata is wrong, and running it again
  produces the identical error. A 3-byte "RINEX" file will never parse; a
  truncated ``.gz`` will never inflate; a date TOS has no session for needs a
  human to fix TOS. Retrying wastes time and, worse, buries the real signal.
* **Transient** — the operation failed for a reason unrelated to the data.
  The ELEY 1Hz retrofit (2026-07-14) finished with 18 errors, ~13 of them
  ``compress -f … exit 1``; the exact same files compressed fine on a manual
  re-run. Subprocess pressure or a staging write-race, not bad data.

Only the second class should be retried automatically. The first must surface
so an operator can act — ``archive-rm`` for corrupt stubs, a TOS fix for a
missing session, disk space for a failed ``rinex_org`` preservation.

Classification rule
-------------------
**Known-permanent signatures are enumerated; everything else is transient.**

The asymmetry is deliberate. A wrongly-retried permanent failure costs a few
seconds and reports the same error; a wrongly-surfaced transient failure means
a human re-runs a 35,000-file job by hand. When a new failure mode appears it
is safer to retry it a couple of times than to silently give up on it — and if
it turns out to be permanent, it shows up in the summary either way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TRANSIENT = "transient"
PERMANENT = "permanent"

#: Substrings that identify a failure as permanent. Matched case-insensitively
#: against the error text. Each entry maps to the operator action it implies.
_PERMANENT_SIGNATURES: tuple[tuple[str, str], ...] = (
    # --- bad or missing input -------------------------------------------
    ("could not read rinex header", "unreadable/corrupt RINEX — archive-rm"),
    ("file not found", "source file missing"),
    (
        "could not parse observation date",
        "filename does not carry a parseable date — archive-sort",
    ),
    # Truncated compressed input. gzip/zlib phrase this several ways; all mean
    # the bytes on disk are incomplete, so a retry reads the same short file.
    ("ended before the end-of-stream marker", "truncated raw — unrecoverable"),
    ("unexpected end of file", "truncated input"),
    ("not in gzip format", "not the compressed format the name claims"),
    ("invalid block type", "corrupt compressed stream"),
    ("crc check failed", "corrupt compressed stream"),
    # --- unconvertable raw (converter exits non-zero on bad data) ----------
    # runpkr00 reads a Trimble .T02/.T00 and extracts a .dat intermediate; a
    # non-zero exit is a data problem (corrupt/incompatible raw), not the
    # subprocess pressure that makes `compress` flaky, so a retry reads the same
    # bad file. exit 30 is the observed fleet-wide case (1367 files / 83
    # stations on the 2026-08-09 backfill run). Other runpkr00 codes are folded
    # in as they surface — they are data failures, not transient load.
    (
        "runpkr00 failed with exit code 30",
        "Trimble raw unconvertable (runpkr00 exit 30) — archive-rm",
    ),
    # --- metadata gaps, need a human ------------------------------------
    ("no tos session covers", "TOS has no session for this date — fix TOS"),
    # --- deliberate fail-safes, must never be retried away ---------------
    (
        "rinex_org preservation",
        "refused to overwrite an un-regenerable file — needs space/permissions",
    ),
    ("un-regenerable", "un-regenerable and preservation failed"),
)


@dataclass(frozen=True)
class FailureClass:
    """How a failure should be treated by the batch runner."""

    kind: str  # TRANSIENT | PERMANENT
    reason: str

    @property
    def retryable(self) -> bool:
        return self.kind == TRANSIENT


def classify_failure(error: object) -> FailureClass:
    """Classify one failure from its message.

    Accepts an exception or any object whose ``str()`` carries the message, so
    callers can pass either the raw exception or the error string already stored
    on a result dict.
    """
    text = ("" if error is None else str(error)).lower()
    if not text.strip():
        # No message at all: nothing identifies it as permanent, so it falls to
        # the default. A retry costs one attempt and usually produces a message.
        return FailureClass(TRANSIENT, "no error text")
    for signature, reason in _PERMANENT_SIGNATURES:
        if signature in text:
            return FailureClass(PERMANENT, reason)
    return FailureClass(TRANSIENT, "not a known-permanent failure")


def partition_failures(details: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split result dicts carrying an ``error`` into (retryable, permanent).

    Entries without an ``error`` are ignored, so a whole batch's ``details``
    list can be passed straight in.
    """
    retryable: list[dict] = []
    permanent: list[dict] = []
    for detail in details:
        err = detail.get("error")
        if not err:
            continue
        (retryable if classify_failure(err).retryable else permanent).append(detail)
    return retryable, permanent
