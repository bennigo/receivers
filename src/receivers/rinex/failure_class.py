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
    # --- wrong converter, NOT bad data -------------------------------------
    # CORRECTED 2026-08-16. This used to read "Trimble raw unconvertable
    # (runpkr00 exit 30) — archive-rm", i.e. it advised DELETING the raw. That
    # was wrong and would have destroyed good data.
    #
    # exit 30 means runpkr00 could not read the .T02, not that the file is bad.
    # These .T02 carry a bzip2-compressed payload (magic "BZh" a few bytes in)
    # that this runpkr00 build cannot decode — it writes a 26-byte stub .dat and
    # exits 30. The SAME files convert cleanly through the native Docker/Wine
    # converter, which is why `use_native_trimble` exists and is the only path
    # that produces RINEX 3 for these receivers.
    #
    # It surfaced fleet-wide (1367 files / 83 stations on 2026-08-09; 566 files
    # and 1,132 failed spawns per 3 h on 2026-08-16) because the BACKFILL path
    # hardcoded TrimbleConverter while live downloads used the native converter.
    # Fixed in RINEXTask._resolve_trimble_converter. If this fires again, the
    # native converter is unavailable — check Docker and the trm2rinex image,
    # do NOT delete the raw.
    (
        "runpkr00 failed with exit code 30",
        "runpkr00 cannot read this .T02 (compressed payload) — the native "
        "Docker converter can; check use_native_trimble + the trm2rinex image. "
        "DO NOT archive-rm: the raw is fine.",
    ),
    # --- receiver logged, but observed nothing ---------------------------
    # sbf2rin exit 2. NOT a conversion fault: the raw is intact and the
    # converter is right to refuse — there are no measurements to write.
    # Measured on VMEY 2022-11-01: 5760 MeasEpoch + 5760 PVTGeodetic (a full
    # 24 h at 15 s) with N1 == 0 in every single epoch. Receiver powered and
    # logging on schedule, antenna dead. ~238 VMEY days across 2021-2023 in
    # long contiguous runs, each ~88 KB gzipped against a normal day's 9.7 MB.
    #
    # Permanent because no retry can produce data that was never observed. The
    # suggested action names the VERIFIER, not archive-rm directly: the
    # runpkr00 entry below is the standing reminder that a converter error read
    # as "bad data" once advised deleting 1,367 perfectly good files. Prove it
    # with receivers.rinex.sbf_inspect first — is_provably_empty is true only
    # when the file parsed, HAS epochs, and none carry a satellite.
    (
        "no relevant data available",
        "receiver logged epochs but observed ZERO satellites (antenna fault) — "
        "confirm with sbf_inspect.is_provably_empty, then archive-rm",
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
