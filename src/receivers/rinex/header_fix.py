"""In-place RINEX header correction driven by TOS (``receivers rinex --fix-headers``).

Walks the **archived RINEX** files (not raw) for a station/session/date-range,
finds the ones whose headers disagree with TOS, and rewrites **only the
discrepant header fields** in place — no SBF re-conversion. This is the
field-selective counterpart to ``--validate-only``: validate finds the
mismatches, ``--fix-headers`` fixes exactly those.

TOS efficiency: uses :class:`TOSSesionCache` — 1 API call per station, not
per-file. The device history is cached and reused for every observation date.

Reuses the legacy TOS/RINEX stack end-to-end — no header-editing logic is
duplicated here:

* :func:`tostools.rinex.reader.read_rinex_header` — reads a compressed
  (``.Z``/``.gz``) or plain RINEX header.
* :func:`tostools.rinex.reader.extract_header_info` — header → ``label → value``.
* :func:`tostools.gps_metadata_qc.gps_metadata` — TOS station metadata.
* :func:`receivers.dissemination.qc_gate.select_session` — merge the device
  sessions covering the observation date into one ``tos_session``.
* :func:`tostools.rinex.validator.compare_rinex_to_tos` — diff header vs TOS;
  its ``corrections`` dict is **only the discrepant fields** (label → value).
* :func:`tostools.rinex.correct_rinex_from_tos` — the in-place corrector
  (handles compression), now field-selective via ``only_fields``.

The only new code here is: archive-file discovery, the ``--archive-old``
parallel-directory move, and the orchestration that threads the legacy pieces.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("receivers.rinex.header_fix")


def _nominal_interval_seconds(session_type: Optional[str]) -> Optional[float]:
    """Expected RINEX sampling interval (seconds) for a session type, or None.

    Drives the validator's INTERVAL session-mixing guard: ``15s_24hr`` → 15.0,
    ``1Hz_1hr`` → 1.0. Unknown / non-rate session types (e.g. ``status_1hr``)
    return None so the check is skipped rather than guessed.
    """
    if not session_type:
        return None
    token = str(session_type).split("_")[0].strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)(s|hz)$", token)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2) == "hz":
        return 1.0 / val if val else None
    return val


def _rinex_obs_datetime(name: str, station: str) -> Optional[datetime]:
    """Observation datetime for ANY archived RINEX obs filename, or None.

    The general file-identity parser for fix-headers: delegates to
    :meth:`RinexNamer.parse_date_hour`, which already handles RINEX 2 daily
    (session ``0``) AND hourly (session ``a``–``x``) short names and RINEX 3
    long names (``01D``/``01H``). Daily → 00:00; hourly → the file's hour. This
    is what lets fix-headers work on 1Hz_1hr (hourly) files, not just daily.
    """
    from .rinex_namer import RinexNamer

    d, h = RinexNamer.parse_date_hour(name, station)
    if d is None:
        return None
    return datetime(d.year, d.month, d.day, h or 0)


def _fmt_value(v: Any) -> str:
    """Compact display of a header value for the change summary.

    Floats (and float-like strings) → 4 decimals; sequences → space-joined;
    everything else → stripped str.
    """
    if isinstance(v, float):
        return f"{v:.4f}"
    if isinstance(v, (list, tuple)):
        return " ".join(_fmt_value(x) for x in v)
    s = str(v).strip()
    try:
        return f"{float(s):.4f}"
    except (ValueError, TypeError):
        return s


def archive_old_file(
    rinex_file: Path,
    *,
    reason: str = "fix-headers",
    stamp: Optional[str] = None,
) -> Optional[Path]:
    """Move ``rinex_file`` to a parallel archive directory, filename unchanged.

    The destination is a sibling directory of the file's parent, named
    ``<parent>_archive/<reason>_<stamp>/``. For example::

        /data/2026/jun/RHOF/15s_24hr/rinex/RHOF1800.26D.Z
        → /data/2026/jun/RHOF/15s_24hr/rinex_archive/fix-headers_20260702/RHOF1800.26D.Z

    Returns the archived path, or None if the move failed (logged).
    """
    rinex_file = Path(rinex_file)
    if not rinex_file.is_file():
        return None

    stamp = stamp or datetime.now().strftime("%Y%m%d")
    parent = rinex_file.parent
    # parent is e.g. .../rinex → archive sibling is .../rinex_archive
    archive_root = parent.parent / f"{parent.name}_archive"
    archive_dir = archive_root / f"{reason}_{stamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    dest = archive_dir / rinex_file.name
    # If a same-named file already exists in the archive dir (re-run), don't
    # clobber — append a counter suffix so the operator can tell runs apart.
    if dest.exists():
        i = 1
        while True:
            cand = dest.with_name(f"{rinex_file.stem}_{i}{rinex_file.suffix}")
            if not cand.exists():
                dest = cand
                break
            i += 1
    try:
        shutil.move(str(rinex_file), str(dest))
        logger.debug("archived %s → %s", rinex_file.name, dest)
        return dest
    except OSError as exc:
        logger.warning("archive-old failed for %s: %s", rinex_file, exc)
        return None


def preserve_original_file(rinex_file: Path) -> Optional[Path]:
    """COPY an un-regenerable original to a permanent ``rinex_org/`` sibling.

    Unlike :func:`archive_old_file` (a dated, deletable backup), this is the
    durable preservation of a RINEX that CANNOT be regenerated (no convertible
    raw) — copied, not moved, and never auto-deleted. Pushed to the archive
    alongside the corrected file so the irreplaceable original survives on
    ananas before the in-place header rewrite overwrites ``rinex/``::

        …/rinex/RHOF1800.26D.Z  →  …/rinex_org/RHOF1800.26D.Z

    Idempotent: if the org copy already exists (a prior run preserved it), it is
    left as-is and returned. Returns the org path, or None on failure.
    """
    rinex_file = Path(rinex_file)
    if not rinex_file.is_file():
        return None
    parent = rinex_file.parent  # .../rinex
    org_dir = parent.parent / f"{parent.name}_org"
    dest = org_dir / rinex_file.name
    if dest.exists():
        logger.debug("rinex_org already holds %s — keeping", rinex_file.name)
        return dest
    try:
        org_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(rinex_file), str(dest))
        logger.debug("preserved un-regenerable original → %s", dest)
        return dest
    except OSError as exc:
        logger.error("rinex_org preservation FAILED for %s: %s", rinex_file, exc)
        return None


def _read_header_info(rinex_file: Path, loglevel: int) -> dict[str, str]:
    """Read a (possibly compressed) RINEX header → ``label → value`` dict.

    Reuses :func:`tostools.rinex.reader.read_rinex_header` (handles ``.Z``/``.gz``)
    + :func:`tostools.rinex.reader.extract_header_info`. Returns ``{}`` on any
    read failure — the caller treats that as "skip, can't validate".
    """
    try:
        from tostools.rinex.reader import extract_header_info, read_rinex_header

        rheader = read_rinex_header(rinex_file, loglevel=loglevel)
        if not rheader or "header" not in rheader:
            logger.debug("no header read from %s", rinex_file.name)
            return {}
        info = extract_header_info(rheader, loglevel=loglevel)
        return info or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("header read failed for %s: %s", rinex_file.name, exc)
        return {}

        return None


def fix_headers_in_file(
    rinex_file: Path,
    station_id: str,
    *,
    observation_date: Optional[datetime] = None,
    archive_old: bool = False,
    dry_run: bool = False,
    work_dir: Optional[Path] = None,
    source_base: Optional[Path] = None,
    session_type: Optional[str] = None,
    tos_cache: Any = None,
    tos_metadata_cache: Optional[dict] = None,
    correct_hardware: frozenset = frozenset(),
    skip_fields: frozenset = frozenset(),
    loglevel: int = logging.INFO,
) -> dict:
    """Fix the discrepant TOS header fields of one RINEX file.

    ``correct_hardware`` opts specific normally-FLAG_ONLY hardware fields into
    rewriting for this run — a subset of ``{"receiver", "antenna"}`` (see
    ``--correct-receiver`` / ``--correct-antenna``). Empty (default) preserves
    the safe behaviour: receiver/antenna are flagged, never rewritten.

    When ``work_dir`` is given, the file is COPIED to a mirror path under
    ``work_dir`` (preserving the archive-relative path from ``source_base``)
    and fixed there — the original source file is never touched. The copy
    only happens on a real write (not dry_run).

    ``tos_cache`` is a :class:`TOSSesionCache` (or compatible) providing
    ``.get_session(station, datetime)`` — avoids per-file TOS API calls.

    Returns a summary dict: ``{file, fixed, changed_labels, archived, error}``.
    """
    from tostools.rinex import correct_rinex_from_tos
    from tostools.rinex.corrector import render_correction, resolve_corrections
    from tostools.rinex.reader import _parse_daily_rinex_date
    from tostools.rinex.validator import compare_rinex_to_tos

    source_path = Path(rinex_file)  # the original (may be read-only)
    result: dict = {
        "file": str(source_path),
        "source": str(source_path),  # archive path (re-read post-push for cleanup)
        "station": station_id,
        "observation_date": None,  # set once parsed (below)
        "fixed": False,
        "changed_labels": [],
        "changes": {},
        "flagged": {},  # flag-only mismatches (receiver/antenna): reported, not written
        "archived": None,
        "preserved_org": None,
        "regenerable": None,
        "error": None,
    }
    if not source_path.is_file():
        result["error"] = "file not found"
        return result

    if observation_date is None:
        observation_date = _rinex_obs_datetime(source_path.name, station_id)
    if observation_date is None:
        result["error"] = "could not parse observation date from filename"
        return result
    result["observation_date"] = observation_date

    # 1. Read the file's header (handles .Z/.gz) and the TOS session for the date.
    rinex_info = _read_header_info(source_path, loglevel)
    if not rinex_info:
        result["error"] = "could not read RINEX header"
        return result
    if tos_cache is None:
        from ..dissemination.tos_access import TOSSesionCache

        tos_cache = TOSSesionCache()
    tos_session = tos_cache.get_session(station_id, observation_date)
    if tos_session is None:
        result["error"] = "no TOS session covers this date"
        return result

    # 2. compare_rinex_to_tos → discrepancies, classified into two groups.
    comparison = compare_rinex_to_tos(
        rinex_info,
        tos_session,
        loglevel=loglevel,
        observation_date=observation_date,
        expected_interval=_nominal_interval_seconds(session_type),
    )
    discrepancy_keys = set(comparison.get("discrepancies", {}).keys())
    _disc = comparison.get("discrepancies", {})

    # CORRECTABLE — authoritative station metadata, safe to rewrite from TOS
    # (marker, DOMES, antenna DELTA H/E/N, surveyed coordinates, observer/agency).
    # FLAG_ONLY — reported for review, NEVER auto-rewritten:
    #   receiver/antenna — the header records the ACTUAL hardware at acquisition;
    #     TOS device_history is a reconstruction (protect primary evidence).
    #   filename_marker / time_of_first_obs — a misnamed or misdated file is a
    #     data problem, not a header field to overwrite (legacy consistency check).
    _CORRECTABLE = {
        "marker": "MARKER NAME",
        "domes": "MARKER NUMBER",
        "antenna_height": "ANTENNA: DELTA H/E/N",
        "coordinates": "APPROX POSITION XYZ",
        "observer_agency": "OBSERVER / AGENCY",
    }
    _FLAG_ONLY = {
        "receiver",
        "antenna",
        "filename_marker",
        "time_of_first_obs",
        "interval",
    }

    # Opt-in promotion of normally FLAG_ONLY hardware fields. --correct-receiver
    # / --correct-antenna move receiver/antenna into the correctable set for THIS
    # run. The validator's normalized-identity gate still fires only on a REAL
    # change (a naming variant like PolaRx5 ≡ SEPT POLARX5 is never rewritten).
    # Use ONLY on a station whose header is known-stale — e.g. a BNC-stream 1Hz
    # whose receiver label was frozen across an instrument swap — NEVER on a
    # dual-instrument site, where the header correctly records a separate
    # streaming box and TOS would overwrite it with the wrong (daily) receiver.
    _HARDWARE_LABELS = {"receiver": "REC # / TYPE / VERS", "antenna": "ANT # / TYPE"}
    correctable_map = dict(_CORRECTABLE)
    flag_only = set(_FLAG_ONLY)
    for _hw in correct_hardware:
        _hw_label = _HARDWARE_LABELS.get(_hw)
        if _hw_label:
            correctable_map[_hw] = _hw_label
            flag_only.discard(_hw)

    # Narrow the run to a subset of fields. Dropping a key from correctable_map
    # (rather than filtering later) means the field is never previewed, never
    # written, and never counted as a fix — one gate, so a dry-run still
    # describes the action exactly.
    #
    # The case this exists for: APPROX POSITION XYZ. A header's approximate
    # position is the receiver's own autonomous solution and legitimately sits
    # tens of metres from the surveyed value in TOS, so it flags on essentially
    # every historical file (RHOF 2005: 347/347). Rewriting a station's entire
    # archive as a side effect of repairing MARKER NUMBER is not a decision this
    # tool should make silently — `--skip-fields coordinates` keeps the run to
    # what was actually asked for.
    for _skip in skip_fields:
        correctable_map.pop(_skip, None)

    correctable_keys = discrepancy_keys & correctable_map.keys()
    flag_keys = discrepancy_keys & flag_only

    # Record flag-only mismatches for the run summary (never written).
    for key in sorted(flag_keys):
        d = _disc.get(key) or {}
        result["flagged"][key] = (d.get("rinex"), d.get("tos") or d.get("expected"))

    if not correctable_keys:
        if flag_keys:
            logger.debug(
                "%s: flag-only discrepancies (%s) — reported, not fixed",
                source_path.name,
                ", ".join(sorted(flag_keys)),
            )
        else:
            logger.debug("%s: header agrees with TOS — no fix needed", source_path.name)
        return result

    # Labels to rewrite = correctable discrepancies the validator also produced a
    # correction for.
    corrections_labels = set(comparison.get("corrections", {}).keys())
    discrepant_labels = {
        correctable_map[key] for key in correctable_keys
    } & corrections_labels
    if not discrepant_labels:
        return result

    # OBSERVER / AGENCY value must be injected — the corrector cannot resolve
    # agencies.yaml, so pass the receivers-resolved value from the session.
    extra_corrections: dict = {}
    if "OBSERVER / AGENCY" in discrepant_labels:
        extra_corrections["OBSERVER / AGENCY"] = [
            str(tos_session.get("observer") or "").strip(),
            str(tos_session.get("agency") or "").strip(),
        ]

    # The validator decides WHICH fields differ; the corrector decides WHAT is
    # written. Ask the corrector's own builder so the preview is the write.
    # Previously the preview showed the validator's proposed value, and for a TOS
    # placeholder antenna serial the two disagree (blank vs "0000") — a dry-run
    # that does not describe the action is worthless on the one run it exists for.
    resolved = resolve_corrections(
        source_path,
        station_id,
        observation_date,
        loglevel=loglevel,
        only_fields=discrepant_labels,
        extra_corrections=extra_corrections or None,
        tos_metadata_cache=tos_metadata_cache,
    )

    # A label the corrector will not write is not a change — drop it rather than
    # report a fix that never happens (the position guard drops a km-scale
    # APPROX POSITION rewrite exactly this way).
    discrepant_labels = {lbl for lbl in discrepant_labels if lbl in resolved}
    if not discrepant_labels:
        logger.debug(
            "%s: corrector produced no value for %s — nothing to fix",
            source_path.name,
            ", ".join(sorted(result["changed_labels"] or [])) or "any flagged field",
        )
        return result
    result["changed_labels"] = sorted(discrepant_labels)

    # Record the transition as the header field text: old = what is in the file,
    # new = the exact 60-char field the corrector will emit (None = line removed).
    _label_to_key = {lbl: key for key, lbl in correctable_map.items()}
    for label in discrepant_labels:
        old = str(rinex_info.get(label) or "").rstrip()
        if not old:
            # The header has no such line (the corrector will INSERT one). Fall
            # back to what the validator saw so the summary still says what it
            # was, rather than an empty string that reads like a blank field.
            _d = _disc.get(_label_to_key.get(label, "")) or {}
            old = str(_d.get("rinex") or "").rstrip()
        rendered = render_correction(label, resolved[label])
        new = "<line removed>" if rendered is None else rendered.rstrip()
        result["changes"][label] = (old, new)

    if dry_run:
        logger.debug(
            "[DRY RUN] %s: would fix %d field(s): %s",
            source_path.name,
            len(discrepant_labels),
            ", ".join(result["changed_labels"]),
        )
        return result

    # 3. If staging to a work_dir, copy the file from the (possibly read-only)
    #    source archive into the writable work_dir before any other mutations.
    fix_target = source_path
    if work_dir is not None:
        try:
            rel = (
                source_path.relative_to(source_base)
                if source_base
                else Path(source_path.parent.name) / source_path.name
            )
        except ValueError:
            rel = Path(source_path.parent.name) / source_path.name
        fix_target = Path(work_dir) / rel
        fix_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, fix_target)
        result["file"] = str(fix_target)

    # 4. Archive the original (writable copy) to a parallel dir if requested.
    if archive_old:
        archived = archive_old_file(fix_target, reason="fix-headers")
        if archived is not None:
            shutil.copy2(archived, fix_target)
            result["archived"] = str(archived)

    # 4.5 SAFETY NET: a RINEX is only safe to overwrite in place if it is
    #     REGENERABLE — i.e. a convertible raw file still exists on the archive.
    #     Raw absent, OR raw present in an unrecognised format, means this RINEX
    #     is the sole surviving copy of the observation. Preserve the untouched
    #     original to a permanent rinex_org/ sibling (pushed to ananas) BEFORE
    #     the rewrite. Checked against the SOURCE archive's raw/ sibling
    #     (source_path), not the work_dir copy. If preservation fails we REFUSE
    #     to overwrite — never risk an irreplaceable file.
    from .raw_presence import check_regenerable

    regen = check_regenerable(
        source_path,
        observation_date,
        station_id=station_id,
        session_type=session_type,
    )
    result["regenerable"] = regen.regenerable
    if not regen.regenerable:
        preserved = preserve_original_file(fix_target)
        if preserved is None:
            result["error"] = (
                f"un-regenerable ({regen.reason}) and rinex_org preservation "
                f"failed — refusing to overwrite"
            )
            return result
        result["preserved_org"] = str(preserved)
        result["preserve_reason"] = regen.reason
        # Per-file detail at DEBUG only — the run summary reports the count.
        logger.debug(
            "%s: %s — preserved original → %s before header fix",
            source_path.name,
            regen.reason,
            preserved,
        )

    # 5. Rewrite only the discrepant fields in place (corrector handles
    #    compression + Hatanaka internally).
    try:
        out = correct_rinex_from_tos(
            fix_target,
            station_id,
            observation_date=observation_date,
            output_file=fix_target,  # overwrite in place
            loglevel=loglevel,
            only_fields=discrepant_labels,
            extra_corrections=extra_corrections or None,
            # Same cache the preview resolved against — so the write re-derives
            # the identical values without a second TOS fetch per file.
            tos_metadata_cache=tos_metadata_cache,
        )
        result["fixed"] = out is not None
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"in-place corrector failed: {exc}"
        return result

    logger.debug(
        "%s: fixed %d field(s): %s",
        fix_target.name,
        len(discrepant_labels),
        ", ".join(result["changed_labels"]),
    )
    return result


def discover_all_rinex_files(
    station_id: str,
    session: str,
    data_prepath: str,
) -> list[Path]:
    """Discover ALL archived RINEX files for a station/session — every year/month.

    Walks ``<data_prepath>/<YYYY>/<mon>/<STA>/<session>/rinex/`` for every
    year+month that exists on disk. Used by ``--fix-headers --all``.
    """
    from tostools.rinex.reader import _parse_daily_rinex_date

    root = Path(data_prepath)
    if not root.is_dir():
        return []

    files: list[Path] = []
    for year_dir in sorted(root.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            rinex_dir = month_dir / station_id / session / "rinex"
            if not rinex_dir.is_dir():
                continue
            for p in sorted(rinex_dir.iterdir()):
                if p.is_file() and (
                    p.name.endswith((".Z", ".gz")) or p.suffix in (".o", ".O")
                ):
                    files.append(p)
    return files


def discover_rinex_files(
    station_id: str,
    session: str,
    start_time: datetime,
    end_time: datetime,
    data_prepath: str,
) -> list[Path]:
    """Discover archived RINEX files for a station/session/date-range.

    Walks ``<data_prepath>/<YYYY>/<mon>/<STA>/<session>/rinex/`` and returns
    the RINEX files (``.YYD.Z`` / ``.crx.gz`` / ``.YYo`` etc.) whose observation
    date falls in ``[start_time, end_time)``. Daily session → one file per day
    (matched by DOY in the filename); hourly → one per hour.
    """
    # CLI dates from calculate_download_time_range are UTC-aware; TOS sessions
    # and parsed filenames are naive. Strip tzinfo for consistent comparisons.
    if start_time.tzinfo is not None:
        start_time = start_time.replace(tzinfo=None)
    if end_time.tzinfo is not None:
        end_time = end_time.replace(tzinfo=None)

    files: list[Path] = []
    cur = start_time
    step = timedelta(hours=1) if "1hr" in session.lower() else timedelta(days=1)
    while cur < end_time:
        year = cur.strftime("%Y")
        month = cur.strftime("%b").lower()
        rinex_dir = Path(data_prepath) / year / month / station_id / session / "rinex"
        if rinex_dir.is_dir():
            for p in sorted(rinex_dir.iterdir()):
                if not p.is_file():
                    continue
                # Filter by the file's own observation datetime (daily OR hourly,
                # short OR long name) so a dir listing the whole month doesn't
                # bleed into a shorter run.
                obs = _rinex_obs_datetime(p.name, station_id)
                if obs is None:
                    # Unrecognised name — include if it looks like RINEX; the
                    # caller will surface a parse error per file.
                    if p.name.endswith((".Z", ".gz")) or p.suffix in (".o", ".O"):
                        files.append(p)
                    continue
                if start_time <= obs < end_time:
                    files.append(p)
        cur += step
    # Dedup (a file matching two iteration steps shouldn't appear twice)
    seen: set[Path] = set()
    deduped: list[Path] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped


def _retry_transient_failures(
    summary: dict,
    station_id: str,
    *,
    attempts: int,
    backoff: float,
    pending: Optional[list],
    archive_old: bool,
    dry_run: bool,
    work_dir: Optional[Path],
    source_base: Path,
    session: str,
    tos_cache: Any,
    correct_hardware: frozenset,
    skip_fields: frozenset = frozenset(),
    loglevel: int,
) -> None:
    """Re-run the failures that a retry can plausibly fix, in place.

    Only the transient class — see :mod:`receivers.rinex.failure_class`. A
    corrupt stub or a missing TOS session produces the identical error on a
    second pass, and retrying it buries the signal an operator needs.

    Mutates ``summary`` so the reported counts reflect the final state rather
    than the first attempt: a file that succeeds on retry moves out of
    ``errors`` and into ``fixed``/``skipped``, and its ``error`` key is cleared.
    Retry outcomes are logged and summarised, because a run that only passes on
    the second attempt is itself worth noticing — it means the box is under
    pressure, which is the condition that produced the ELEY failures.
    """
    import time

    from .failure_class import partition_failures

    retryable, permanent = partition_failures(summary.get("details", []))
    # Recorded before the early return: a run whose failures are ALL permanent
    # is the common good case (ISAK's 6 corrupt stubs), and the count is what
    # the operator acts on.
    summary["permanent_failures"] = len(permanent)
    summary.setdefault("recovered_on_retry", 0)
    if permanent:
        logger.info(
            "%s: %d permanent failure(s) — not retried", station_id, len(permanent)
        )
    if not retryable:
        return

    print(
        f"   ↻ retrying {len(retryable)} transient failure(s), "
        f"up to {attempts} attempt(s)"
    )
    recovered = 0
    for detail in retryable:
        # `source` is the archive path the result was built from — the only
        # key guaranteed present, since it is set before any failure can occur.
        path = detail.get("source")
        if not path:
            continue
        for attempt in range(1, attempts + 1):
            if backoff:
                # Pressure is exactly what these failures are made of, so give
                # the box a widening moment rather than hammering it.
                time.sleep(backoff * attempt)
            retry = fix_headers_in_file(
                Path(path),
                station_id,
                archive_old=archive_old,
                dry_run=dry_run,
                work_dir=work_dir,
                source_base=source_base,
                session_type=session,
                tos_cache=tos_cache,
                correct_hardware=correct_hardware,
                skip_fields=skip_fields,
                loglevel=loglevel,
            )
            if retry.get("error"):
                logger.warning(
                    "%s: retry %d/%d still failing for %s: %s",
                    station_id,
                    attempt,
                    attempts,
                    Path(path).name,
                    retry["error"],
                )
                detail["error"] = retry["error"]
                detail["retry_attempts"] = attempt
                continue
            # Recovered. Fold the new result into the original entry so the
            # grouped field summary and the push batch both see it.
            detail.update(retry)
            detail["error"] = None
            detail["recovered_after"] = attempt
            summary["errors"] -= 1
            if retry.get("changed_labels"):
                if dry_run:
                    summary["would_fix"] = summary.get("would_fix", 0) + 1
                else:
                    summary["fixed"] += 1
            else:
                summary["skipped"] = summary.get("skipped", 0) + 1
            if pending is not None and (
                retry.get("fixed") or retry.get("preserved_org")
            ):
                pending.append(retry)
            recovered += 1
            logger.info(
                "%s: recovered %s on retry %d", station_id, Path(path).name, attempt
            )
            break

    summary["recovered_on_retry"] = recovered
    summary["permanent_failures"] = len(permanent)
    if recovered:
        print(f"   ✓ recovered {recovered}/{len(retryable)} on retry")
    else:
        print(f"   ✗ none of the {len(retryable)} transient failure(s) recovered")


def fix_headers_station(
    station_id: str,
    session: str,
    start_time: datetime,
    end_time: datetime,
    *,
    archive_old: bool = False,
    dry_run: bool = False,
    all_files: bool = False,
    work_dir: Optional[Path] = None,
    source_dir: Optional[Path] = None,
    tos_cache: Any = None,
    flush_fn: Any = None,
    flush_every: int = 0,
    push_dest: Optional[str] = None,
    retry_attempts: int = 2,
    retry_backoff: float = 3.0,
    correct_hardware: frozenset = frozenset(),
    skip_fields: frozenset = frozenset(),
    loglevel: int = logging.INFO,
    progress: Any = None,
) -> dict:
    """Run ``--fix-headers`` across a station's archived RINEX.

    ``correct_hardware`` (a subset of ``{"receiver", "antenna"}``) opts those
    normally-flag-only hardware fields into rewriting for this run — see
    ``fix_headers_in_file``. Empty preserves the safe flag-only default.

    ``skip_fields`` is the inverse: keys removed from the correctable set for
    this run (``marker``, ``domes``, ``antenna_height``, ``coordinates``,
    ``observer_agency``). Chiefly ``coordinates`` — see ``fix_headers_in_file``
    for why a position rewrite should not ride along with a marker repair.

    When ``work_dir`` is given, files are discovered from ``source_dir``
    (or ``data_prepath``) and COPIED to a mirror path under ``work_dir``
    before fixing — the source archive (read-only NFS) is never touched.

    ``tos_cache`` is a shared :class:`TOSSesionCache` (or compatible).
    When None a fresh one is created — but passing one from the fleet sweep
    (one call to ``fix_headers_station`` per station) means only 1 TOS call
    per station total, regardless of how many files are processed.

    Incremental durability: when ``flush_every > 0`` and ``flush_fn`` is given,
    ``flush_fn(batch_details)`` is called every ``flush_every`` **fixed** files
    (and once for the remainder at the end). The caller uses it to push+reindex
    that batch immediately, so an interruption loses at most one batch's work
    instead of the whole run (a re-run then skips already-pushed files, whose
    headers now match TOS). Never invoked on a dry-run.

    ``push_dest`` is a display-only string (the resolved archive gateway, e.g.
    ``gpsops@rawdata.vedur.is:~/gpsdata/``); when set it is echoed on the
    staging banner so the operator sees where fixed batches are pushed.

    Returns ``{station, scanned, fixed, skipped, errors, details: [...]}``.
    """
    from ..config.receivers_config import get_receivers_config

    cfg = get_receivers_config()
    data_prepath = source_dir or Path(cfg.get_data_prepath())
    source_base = Path(data_prepath) if isinstance(data_prepath, str) else data_prepath

    if all_files:
        files = discover_all_rinex_files(station_id, session, str(source_base))
    else:
        files = discover_rinex_files(
            station_id, session, start_time, end_time, str(source_base)
        )
    summary = {
        "station": station_id,
        "scanned": len(files),
        "fixed": 0,
        "skipped": 0,
        "errors": 0,
        "details": [],
    }
    if not files:
        logger.info("[%s] no archived RINEX files in range", station_id)
        return summary

    if work_dir is not None and not dry_run:
        print(
            f"   Staging fixed files into {work_dir} "
            f"(source archive NOT modified — push back with rsync)"
        )
        if push_dest:
            print(f"   → pushing fixed batches to {push_dest}")

    # One TOS call per station (cached device_history), not per file.
    if tos_cache is None:
        from ..dissemination.tos_access import TOSSesionCache

        tos_cache = TOSSesionCache()

    # Date-independent gps_metadata(station) payload, shared by every file in
    # this station run: the preview resolves against it and the write reuses it,
    # so the pair costs one TOS call for the station, not two per file.
    tos_metadata_cache: dict = {}

    # Progress: a ChunkProgress handle (parallel batch — tqdm bars from N
    # workers garble one terminal line, so the board reports instead), or a
    # tqdm bar for the interactive sequential run.
    if progress is not None:
        progress.set_total(len(files))
        pbar = files
    else:
        try:
            from tqdm import tqdm

            pbar = tqdm(
                files,
                desc=f"  {station_id}",
                unit="file",
                ncols=120,
                postfix={"disc": 0, "clean": 0, "err": 0},
            )
        except ImportError:
            pbar = files

    # Incremental push batching: flush every ``flush_every`` fixed files so an
    # interruption loses at most one batch (never on a dry-run).
    _do_flush = bool(not dry_run and flush_fn is not None and flush_every > 0)
    _pending: list[dict] = []
    _pending_fixed = 0

    for f in pbar:
        r = fix_headers_in_file(
            f,
            station_id,
            archive_old=archive_old,
            dry_run=dry_run,
            work_dir=work_dir,
            source_base=source_base,
            session_type=session,
            tos_cache=tos_cache,
            tos_metadata_cache=tos_metadata_cache,
            correct_hardware=correct_hardware,
            skip_fields=skip_fields,
            loglevel=loglevel,
        )
        summary["details"].append(r)
        if r.get("error"):
            summary["errors"] += 1
        elif r.get("changed_labels"):
            if dry_run:
                summary.setdefault("would_fix", 0)
                summary["would_fix"] += 1
            else:
                summary["fixed"] += 1
        else:
            summary["skipped"] += 1
        # Accumulate fixed/preserved files and flush a batch when it fills.
        if _do_flush and (r.get("fixed") or r.get("preserved_org")):
            _pending.append(r)
            if r.get("fixed"):
                _pending_fixed += 1
            if _pending_fixed >= flush_every:
                flush_fn(_pending)
                _pending = []
                _pending_fixed = 0
        # Update progress bar postfix every file (cheap — just dict assignment).
        if hasattr(pbar, "set_postfix"):
            pbar.set_postfix(
                disc=summary.get("would_fix", summary.get("fixed", 0)),
                clean=summary.get("clean", summary.get("skipped", 0)),
                err=summary["errors"],
                refresh=False,
            )
        if progress is not None:
            progress.advance()
    # Retry the transient failures before the final flush, so anything that
    # succeeds on a second attempt rides out with the last batch rather than
    # needing its own push.
    if retry_attempts > 0 and summary["errors"]:
        _retry_transient_failures(
            summary,
            station_id,
            attempts=retry_attempts,
            backoff=retry_backoff,
            pending=_pending if _do_flush else None,
            archive_old=archive_old,
            dry_run=dry_run,
            work_dir=work_dir,
            source_base=source_base,
            session=session,
            tos_cache=tos_cache,
            correct_hardware=correct_hardware,
            skip_fields=skip_fields,
            loglevel=loglevel,
        )

    # Flush the final partial batch.
    if _do_flush and _pending:
        flush_fn(_pending)
    # On dry_run, "skipped" in the CLI output means "no discrepancy", so
    # rename for clarity.
    if dry_run:
        summary.setdefault("would_fix", 0)
        summary["clean"] = summary.pop("skipped", 0)

    # Grouped summary — which fields were flagged, how many files, and the
    # actual value transitions (old header → TOS), e.g. "1.0070 → 1.0140".
    _by_field: dict[str, int] = {}
    _transitions: dict[str, dict[tuple, int]] = {}
    for d in summary.get("details", []):
        for lbl in d.get("changed_labels", []):
            _by_field[lbl] = _by_field.get(lbl, 0) + 1
        for lbl, (old, new) in d.get("changes", {}).items():
            key = (_fmt_value(old), _fmt_value(new))
            _transitions.setdefault(lbl, {})[key] = (
                _transitions.setdefault(lbl, {}).get(key, 0) + 1
            )
    if _by_field:
        print("   Fields flagged:")
        for lbl, cnt in sorted(_by_field.items(), key=lambda kv: -kv[1]):
            trans = _transitions.get(lbl, {})
            if len(trans) == 1:
                (old, new), _cnt = next(iter(trans.items()))
                print(f"      {lbl}: {cnt} file(s)   {old} → {new}")
            else:
                print(f"      {lbl}: {cnt} file(s)")
                for (old, new), n in sorted(trans.items(), key=lambda kv: -kv[1]):
                    print(f"         {old} → {new}: {n} file(s)")

    # Un-regenerable preservations — summarized (per-file logs are DEBUG only).
    _preserved = sum(1 for d in summary.get("details", []) if d.get("preserved_org"))
    if _preserved:
        _reasons: dict[str, int] = {}
        for d in summary.get("details", []):
            if d.get("preserved_org"):
                r = str(d.get("preserve_reason", "un-regenerable")).split(" (")[0]
                # collapse "raw absent for RHOF 20250901" → "raw absent"
                r = "raw absent" if r.startswith("raw absent") else r
                r = "no raw/ dir" if r.startswith("no raw/ dir") else r
                _reasons[r] = _reasons.get(r, 0) + 1
        detail = ", ".join(
            f"{n} {r}" for r, n in sorted(_reasons.items(), key=lambda kv: -kv[1])
        )
        print(
            f"   🔒 {_preserved} un-regenerable original(s) preserved to rinex_org ({detail})"
        )

    # Flag-only fields (receiver / antenna) — reported for review, never written.
    # These are genuine header↔TOS mismatches you may want to eyeball per station
    # before deciding whether TOS or the header is right (TOS device_history is a
    # reconstruction). Not counted as fixed.
    _flag_by_field: dict[str, int] = {}
    for d in summary.get("details", []):
        for key in d.get("flagged") or {}:
            _flag_by_field[key] = _flag_by_field.get(key, 0) + 1
    if _flag_by_field:
        summary["flagged"] = _flag_by_field
        detail = ", ".join(
            f"{n} {k}" for k, n in sorted(_flag_by_field.items(), key=lambda kv: -kv[1])
        )
        print(
            f"   ⚑ receiver/antenna mismatches flagged (NOT auto-fixed): {detail} "
            f"— review with --verbose or dry-run before deciding"
        )

    return summary


def archive_header_matches_tos(
    archive_file: Path,
    station_id: str,
    observation_date: datetime,
    *,
    tos_cache: Any = None,
    loglevel: int = logging.INFO,
) -> bool:
    """Re-read a (pushed) archive RINEX header and confirm it now equals TOS.

    The gate for deleting a rinex_archive pre-fix backup: only once the corrected
    file **on the archive** actually agrees with TOS (no real
    marker/antenna_height/coordinates discrepancy) is the backup safe to remove.
    Conservative — any read failure, stale NFS view, or missing TOS session
    returns False (keep the backup; the cleanup can be re-run later).
    """
    from tostools.rinex.validator import compare_rinex_to_tos

    info = _read_header_info(Path(archive_file), loglevel)
    if not info:
        return False
    if tos_cache is None:
        from ..dissemination.tos_access import TOSSesionCache

        tos_cache = TOSSesionCache()
    tos_session = tos_cache.get_session(station_id, observation_date)
    if tos_session is None:
        return False
    comparison = compare_rinex_to_tos(info, tos_session, loglevel=loglevel)
    # Only the CORRECTABLE fields gate backup deletion — receiver/antenna are
    # flag-only (never written), so they must not keep a backup alive forever.
    real = set(comparison.get("discrepancies", {}).keys()) & {
        "marker",
        "domes",
        "antenna_height",
        "coordinates",
        "observer_agency",
    }
    return not real


def cleanup_after_push(
    details: list[dict],
    *,
    work_dir: Optional[Path],
    tos_cache: Any = None,
    confirm_fn: Any = None,
    loglevel: int = logging.INFO,
) -> dict:
    """Post-push staging cleanup (opt-in via ``--cleanup``). Never touches the
    archive — only the local staging work-dir.

    For each successfully fixed file:
      * delete the staged ``rinex/`` obs (durably on the archive after push);
      * delete its ``rinex_archive/`` pre-fix backup ONLY once the re-read
        archive header matches TOS (:func:`archive_header_matches_tos`);
      * NEVER touch ``rinex_org/`` (permanent preservation of un-regenerable
        originals).

    Returns counts: staged_removed, backups_removed, backups_kept, org_kept,
    errors[].
    """
    stats = {
        "staged_removed": 0,
        "backups_removed": 0,
        "backups_kept": 0,
        "org_kept": 0,
        "errors": [],
    }
    work = Path(work_dir) if work_dir else None
    # Injectable for tests; defaults to the live archive-header-vs-TOS re-read.
    _confirm = confirm_fn
    if _confirm is None:

        def _confirm(src, st, od):
            return archive_header_matches_tos(
                Path(src), st, od, tos_cache=tos_cache, loglevel=loglevel
            )

    def _under(p: Path, root: Path) -> bool:
        try:
            p.relative_to(root)
            return True
        except ValueError:
            return False

    for d in details:
        if not d.get("fixed"):
            continue
        # 1. Staged rinex/ obs — safe to drop once it is on the archive.
        staged = Path(d["file"])
        if work is not None and _under(staged, work) and staged.is_file():
            try:
                staged.unlink()
                stats["staged_removed"] += 1
            except OSError as exc:
                stats["errors"].append(f"staged rm {staged}: {exc}")

        # 2. rinex_archive/ backup — gated on archive-header == TOS.
        archived = d.get("archived")
        if archived:
            ap = Path(archived)
            src = d.get("source")
            od = d.get("observation_date")
            st = d.get("station")
            confirmed = bool(src and od and st and _confirm(src, st, od))
            if confirmed and ap.is_file():
                try:
                    ap.unlink()
                    stats["backups_removed"] += 1
                except OSError as exc:
                    stats["errors"].append(f"backup rm {ap}: {exc}")
            else:
                stats["backups_kept"] += 1

        # 3. rinex_org/ — irreplaceable preservation, never auto-deleted.
        if d.get("preserved_org"):
            stats["org_kept"] += 1

    return stats
