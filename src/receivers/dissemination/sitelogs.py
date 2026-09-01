"""IGS/M3G site-log generation for EPOS dissemination (C6/T7).

EPOS §3.2 makes the station site log the canonical metadata record, maintained
in the M3G portal (https://gnss-metadata.eu) within one business day of any TOS
change. This module is the dissemination-side wiring around the existing tostools
generator: it reads the station's TOS metadata, renders the IGS site log, and
writes it to a target directory (a ``gps-sitelogs`` repo working tree in
production).

**Rendering goes through** :func:`tostools.core.site_log.build_site_log` —
the single entry point shared with ``tosGPS sitelog`` (2026-08-23). It in
turn calls :func:`tostools.legacy.gps_metadata_functions.site_log`, **NOT**
``core.site_log.generate_igs_site_log``, which this docstring named until
2026-08-22 and which has no production caller at all.

Both of those distinctions are load-bearing:

- A fix applied to ``generate_igs_site_log`` changes nothing that is
  published — the shape of the VMEY HTTP 422 incident (2026-08-20, empty
  antenna serial). ``tostools/tests/test_sitelog_unknown_antenna_serial.py``
  pins both modules for that reason.
- Before ``build_site_log``, this module and ``tosGPS sitelog`` called the
  renderer directly with *different* argument sets (this one passed
  ``monument_number``/``country_code``, tosGPS passed
  ``report_type``/``modified_sections``) and agreed only because the omitted
  arguments shared defaults. Route new callers through ``build_site_log``.

The repo-commit and M3G submission steps are deliberately split out (see
:func:`commit_site_log` / the M3G submitter stub) because they need the
``gps-sitelogs`` repo location and M3G credentials — open ops decision #3.
Generation itself is self-contained and testable offline with an injected client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .m3g_client import read_sitelog

logger = logging.getLogger("receivers.dissemination.sitelogs")

# Where the gps-sitelogs clone lives when [paths] sitelogs_repo is unset.
DEFAULT_SITELOGS_REPO = "~/git/gps-sitelogs"


def resolve_sitelogs_repo(override: Optional[str] = None) -> Path:
    """Return the gps-sitelogs working-tree directory.

    Precedence: explicit ``override`` → receivers.cfg ``[paths] sitelogs_repo``
    → :data:`DEFAULT_SITELOGS_REPO` (``~/git/gps-sitelogs``). Mirrors
    :func:`receivers.cfg.global_sync.resolve_global_repo`, but does not validate
    the tree (callers create it / commit into it). The path is expanduser'd.
    """
    raw = override
    if not raw:
        try:
            from ..config.receivers_config import ReceiversConfig

            raw = ReceiversConfig().get_sitelogs_repo()
        except Exception:  # noqa: BLE001 — config absent/unreadable → default
            raw = None
    return Path(raw or DEFAULT_SITELOGS_REPO).expanduser()


# _agency_dict / _station_role_orgs / resolve_sitelog_agencies moved to
# tostools.core.agencies on 2026-08-23 so `tosGPS sitelog` can reach them too —
# it could not import from receivers, so it fell back to the renderer's legacy
# TOS-contact path and produced a different §11/§12/§13 from what is published.
# Re-exported under the old names; this module's callers are unchanged.
from tostools.core.agencies import (  # noqa: E402 — after the module docstring
    agency_dict as _agency_dict,  # noqa: F401 — re-export
)
from tostools.core.agencies import (
    resolve_sitelog_agencies,  # noqa: F401 — re-export
)
from tostools.core.agencies import (
    station_role_orgs as _station_role_orgs,  # noqa: F401 — re-export
)

# find_previous_site_log moved to tostools.core.site_log 2026-08-23 so both
# site-log callers share one dated-series implementation. Re-exported: this
# module's own tests import it from here.
from tostools.core.site_log import (
    find_previous_site_log,  # noqa: F401 — re-export
)


def _normalize_sitelog(text: str) -> str:
    """Content for change-detection: drop the two volatile lines so an unchanged
    station doesn't look changed every render.

    - ``Date Prepared`` — set to the render date on every run.
    - ``Previous Site Log`` (§0) — a pointer to the prior dated file, not station
      content. (``Modified/Added Sections`` is derived from the previous log, so
      it too is dropped as chain-dependent, not station state.)
    """
    skip = ("Date Prepared", "Previous Site Log", "Modified/Added Sections")
    out = []
    for line in text.splitlines():
        label = line.split(":", 1)[0].strip()
        if any(label.startswith(s) for s in skip):
            continue
        out.append(line.rstrip())
    return "\n".join(out).strip()


def _render_sitelog(
    station: str,
    out_dir: Path,
    *,
    client: Any,
    country_code: str,
    monument_number: str,
    include_date: bool,
    custom_date: Optional[str],
    agency_resolver: Any,
    loglevel: int,
    station_metadata: Any = None,
    device_sessions: Any = None,
) -> Optional[tuple[str, Path]]:
    """Fetch TOS + render the site-log content and resolve its dated filename,
    WITHOUT writing. Returns ``(content, out_path)`` or None on any TOS/render
    failure (logged). Shared by :func:`generate_site_log` and the change-gate."""
    from datetime import datetime

    from tostools.core.site_log import build_site_log
    from tostools.tosGPS import generate_igs_sitelog_filename

    sid = station.upper()
    if client is None:
        from tostools.api.tos_client import TOSClient

        client = TOSClient()

    try:
        meta = client.get_complete_station_metadata(sid)
    except Exception as exc:  # noqa: BLE001 - any TOS failure ⇒ skip (caller decides)
        logger.warning("site log: TOS lookup failed for %s: %s", sid, exc)
        return None
    if not meta:
        logger.warning("site log: no TOS metadata for %s", sid)
        return None

    # Previous-log chaining (§0): the latest prior dated file in the archive dir.
    mon = str(monument_number)[:2].rjust(2, "0")
    nine_char = f"{sid}{mon}{country_code.upper()}"
    date_str = custom_date or datetime.now().strftime("%Y%m%d")
    previous = find_previous_site_log(Path(out_dir), nine_char, date_str)

    agencies = resolve_sitelog_agencies(client, meta, agency_resolver)
    try:
        # build_site_log is the ONE entry point both site-log callers use —
        # this and `tosGPS sitelog`. They previously called the renderer
        # directly with different argument sets and agreed only because the
        # omitted arguments shared defaults. Agencies are resolved here rather
        # than inside, because this path may be handed an injected resolver.
        content = build_site_log(
            sid,
            client=client,
            agencies=agencies,
            previous_log=previous,
            monument_number=mon,
            country_code=country_code,
            loglevel=loglevel,
            station_metadata=station_metadata,
            device_sessions=device_sessions,
        )
    except Exception as exc:  # noqa: BLE001 - renderer/TOS failure ⇒ skip (logged)
        logger.warning("site log: renderer failed for %s: %s", sid, exc)
        return None
    if not content:
        logger.warning("site log: generator produced nothing for %s", sid)
        return None

    _subdir, filename = generate_igs_sitelog_filename(
        sid,
        country_code=country_code,
        monument_number=monument_number,
        include_date=include_date,
        custom_date=custom_date,
        create_station_subdir=False,
    )
    return content, Path(out_dir) / filename


def _write_sitelog(
    content: str, out_path: Path, sid: str, loglevel: int
) -> Optional[Path]:
    from tostools.core.site_log import export_site_log_to_file

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not export_site_log_to_file(content, str(out_path), sid, loglevel=loglevel):
        logger.warning("site log: export failed for %s → %s", sid, out_path)
        return None
    logger.info("site log written: %s", out_path)
    return out_path


def generate_site_log(
    station: str,
    out_dir: Path,
    *,
    client: Any = None,
    country_code: str = "ISL",
    monument_number: str = "00",
    include_date: bool = True,
    custom_date: Optional[str] = None,
    agency_resolver: Any = None,
    loglevel: int = logging.WARNING,
    station_metadata: Any = None,
    device_sessions: Any = None,
) -> Optional[Path]:
    """Render the IGS site log for ``station`` from TOS into ``out_dir``.

    Returns the written path, or None when TOS has no usable metadata (logged).
    ``client`` is an injectable ``TOSClient`` (defaults to a fresh one) so tests
    run offline. The M3G dated filename form (``rhof00isl_20240827.log``) is the
    default — §0 "Previous Site Log" chains to the latest prior dated log found
    in ``out_dir``; pass ``include_date=False`` for the plain ``RHOF00ISL.log``.
    ``agency_resolver`` (default: load agencies.yaml) drives §11/§12/§13 via
    :func:`resolve_sitelog_agencies`.

    ``station_metadata`` / ``device_sessions`` are pre-fetched TOS metadata,
    threaded to the renderer. Both default to None, which keeps the live fetch.
    Injecting ``client`` alone was NOT enough to run offline: the renderer
    under ``build_site_log`` was called with the station id only and did its
    own fetch, so these tests passed solely because they reached production TOS.

    **Do not pass the ``meta`` this function already fetches.** It comes from
    ``TOSClient.get_complete_station_metadata``, whose device sessions are
    composed with the *narrow* attribute list; the renderer composes its own
    with ``SITELOG_GPS_ATTRIBUTE_CODES`` (the wide one — GAL/BDS/QZSS/SBAS/IRN
    plus azimuth). Substituting one for the other would silently drop
    constellation sub-periods from §3 of a log that gets PUBLISHED to M3G.
    Establishing that the two are interchangeable needs the site-log oracle,
    not an assumption.

    Always writes (no change-gate) — use :func:`generate_site_log_if_changed` to
    write only when the station content actually changed.
    """
    rendered = _render_sitelog(
        station,
        out_dir,
        client=client,
        country_code=country_code,
        monument_number=monument_number,
        include_date=include_date,
        custom_date=custom_date,
        agency_resolver=agency_resolver,
        loglevel=loglevel,
        station_metadata=station_metadata,
        device_sessions=device_sessions,
    )
    if rendered is None:
        return None
    content, out_path = rendered
    return _write_sitelog(content, out_path, station.upper(), loglevel)


@dataclass
class SitelogGateResult:
    """Outcome of a change-gated site-log generation."""

    station: str
    changed: bool
    path: Optional[Path] = None  # written file (changed) or existing (unchanged)
    previous: Optional[Path] = None  # the prior latest log compared against


def _latest_sitelog(out_dir: Path, nine_char: str) -> Optional[Path]:
    """The newest dated site log for ``nine_char`` in ``out_dir`` (lexicographic
    max == chronological for YYYYMMDD names), or None."""
    prefix = nine_char.lower()
    try:
        files = sorted(Path(out_dir).glob(f"{prefix}_*.log"))
    except OSError:
        return None
    return files[-1] if files else None


def generate_site_log_if_changed(
    station: str,
    out_dir: Path,
    *,
    client: Any = None,
    country_code: str = "ISL",
    monument_number: str = "00",
    custom_date: Optional[str] = None,
    agency_resolver: Any = None,
    loglevel: int = logging.WARNING,
    station_metadata: Any = None,
    device_sessions: Any = None,
) -> Optional[SitelogGateResult]:
    """Render the site log and write a new dated file ONLY when the station
    content changed vs the latest committed log.

    Content-hash gate: render current TOS → compare the NORMALIZED render (Date
    Prepared / §0 pointer stripped) against the normalized latest existing log.
    Unchanged ⇒ no write (``changed=False``, ``path`` = the existing log).
    Changed / no prior ⇒ write today's dated file (``changed=True``). Same-day
    regeneration overwrites that day's file (stable filename), so it's idempotent.
    Returns None only on a TOS/render failure (the caller skips). The commit /
    M3G submission are the caller's next steps, gated on ``changed``.
    """
    rendered = _render_sitelog(
        station,
        out_dir,
        client=client,
        country_code=country_code,
        monument_number=monument_number,
        include_date=True,
        custom_date=custom_date,
        agency_resolver=agency_resolver,
        loglevel=loglevel,
        station_metadata=station_metadata,
        device_sessions=device_sessions,
    )
    if rendered is None:
        return None
    content, out_path = rendered
    sid = station.upper()
    mon = str(monument_number)[:2].rjust(2, "0")
    nine_char = f"{sid}{mon}{country_code.upper()}"
    latest = _latest_sitelog(Path(out_dir), nine_char)

    if latest is not None:
        try:
            existing = read_sitelog(latest)
        except OSError:
            existing = ""
        if _normalize_sitelog(content) == _normalize_sitelog(existing):
            logger.info("site log unchanged for %s (vs %s) — no-op", sid, latest.name)
            return SitelogGateResult(sid, changed=False, path=latest, previous=latest)

    written = _write_sitelog(content, out_path, sid, loglevel)
    return SitelogGateResult(
        sid, changed=written is not None, path=written, previous=latest
    )


def commit_site_log(repo_dir: Path, site_log: Path, message: str) -> bool:
    """Stage + commit ``site_log`` in the ``gps-sitelogs`` repo working tree.

    Returns True on a real commit, False when there was nothing to commit. Raises
    on a genuine git error so callers see a misconfigured repo rather than silent
    loss. Pushing is :func:`push_site_logs`, which the ``--sitelog`` path calls
    right after a successful commit — see that function for why.
    """
    import subprocess

    repo_dir = Path(repo_dir)
    rel = site_log.relative_to(repo_dir)
    subprocess.run(["git", "-C", str(repo_dir), "add", str(rel)], check=True)
    status = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain", str(rel)],
        check=True,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        logger.info("site log unchanged, nothing to commit: %s", rel)
        return False
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", message, "--", str(rel)],
        check=True,
    )
    return True


def push_site_logs(repo_dir: Path) -> tuple[bool, str]:
    """Push the ``gps-sitelogs`` repo to origin. Best-effort; never raises.

    Committing without pushing leaves the clone ahead of origin, and nothing
    downstream ever notices: the M3G publish reads the LOCAL file, so a site log
    can be live on gnss-metadata.eu while the repo that is supposed to record it
    sits behind. That is not hypothetical — 67 unpushed commits had accumulated
    by 2026-09-01, some weeks old, from exactly this gap.

    Returns ``(ok, detail)``. A failure is REPORTED, never raised and never
    retried: the commit is already safe locally, and the common causes (no
    network, or origin moved ahead so the push is non-fast-forward) need an
    operator, not a retry. In particular this does not pull/rebase on rejection
    — that would reorder other people's commits to make our own push succeed.
    """
    import subprocess

    try:
        res = subprocess.run(
            ["git", "-C", str(Path(repo_dir)), "push", "origin", "HEAD"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        return (False, f"{type(exc).__name__}: {exc}")
    # git reports push progress on stderr, so prefer it; fall back to stdout.
    lines = (res.stderr or res.stdout).strip().splitlines()
    detail = lines[-1].strip() if lines else ""
    if res.returncode == 0:
        return (True, detail or "pushed")
    return (False, detail or f"git push exited {res.returncode}")


# M3G submission — see :func:`submit_to_m3g` (and :class:`M3GClient`). The
# upload-sitelog API publishes directly (no draft state); submit_to_m3g's
# dry_run default (validate-only) is the safety gate. Tracked as C6 in
# docs/architecture/epos-dissemination-plan.md.


@dataclass
class M3GSubmissionResult:
    """Outcome of :func:`submit_to_m3g` (validate + upload-as-draft)."""

    station: str
    validated: bool
    validation: Optional[object] = None  # ValidationResult
    uploaded: bool = False
    upload: Optional[object] = None  # UploadResult
    dry_run: bool = True
    skipped: Optional[str] = None  # reason when nothing was sent


def submit_to_m3g(
    station: str,
    *,
    site_log_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    client: Any = None,
    network: str = "EPOS",
    country_code: str = "ISL",
    monument_number: str = "00",
    dry_run: bool = True,
    endpoint: Optional[str] = None,
    skip_validation: bool = False,
) -> M3GSubmissionResult:
    """Submit a station's site log to M3G: validate, then publish.

    This is the local verb for the M3G submission step (EPOS §3.2). It renders
    the site log (unless ``site_log_path`` points at an existing file), validates
    it against the M3G network rules, and publishes it via the M3G API. **The
    M3G ``upload-sitelog`` API publishes directly — there is no draft state**,
    so this is the real publish trigger. The ``dry_run`` default (True) keeps it
    validate-only; pass ``dry_run=False`` to actually publish.

    Args:
        station: 4-char station id (e.g. ``RHOF``).
        site_log_path: An existing site log file. When given, rendering is
            skipped and this file is submitted as-is. When None, the log is
            generated into ``out_dir`` (default: the gps-sitelogs repo).
        out_dir: Where to render when ``site_log_path`` is None.
        client: Injected :class:`M3GClient` (tests). A fresh one is built
            otherwise, resolving endpoint/token from config/env.
        network: M3G network short name for validation (default ``EPOS``).
        country_code, monument_number: Render-time filename/form params.
        dry_run: When True (default), validate only — the publish PUT is **not**
            sent. Pass False to actually publish to M3G.
        endpoint: M3G endpoint URL or alias (``prod``/``test``). None → config.
        skip_validation: Skip the validate step (e.g. re-publishing a known-good
            log). Implies ``dry_run`` is the only gate on the publish.

    Returns an :class:`M3GSubmissionResult`. Raises :class:`M3GError` only on
    unrecoverable failures (no token, network down, 401).
    """
    from .m3g_client import M3GClient, M3GError  # noqa: F401 — re-exported

    sid = station.upper()
    mon = str(monument_number)[:2].rjust(2, "0")
    nine_char = (
        f"{sid}{mon}{country_code.upper()}"  # e.g. RHOF00ISL — M3G's station key
    )
    result = M3GSubmissionResult(station=sid, validated=False, dry_run=dry_run)

    # 1. Obtain the site log text — render or read.
    if site_log_path is not None:
        path = Path(site_log_path)
        if not path.is_file():
            result.skipped = f"site log not found: {path}"
            return result
    else:
        out_dir = out_dir or resolve_sitelogs_repo()
        path = generate_site_log(
            sid,
            Path(out_dir),
            country_code=country_code,
            monument_number=monument_number,
        )
        if path is None:
            result.skipped = f"site log generation failed for {sid} (see log)"
            return result
    content = read_sitelog(path)
    logger.info("m3g submit %s: site log = %s (%d bytes)", sid, path, len(content))

    if client is None:
        client = M3GClient(endpoint=endpoint)

    # 2. Validate against the network rules (auth-free; always run unless
    #    explicitly skipped — it's the gate that catches bad metadata).
    if not skip_validation:
        try:
            vr = client.validate_sitelog(content, network=network)
        except M3GError as exc:
            result.skipped = f"validate failed: {exc}"
            return result
        result.validation = vr
        result.validated = vr.ok
        if not vr.ok:
            result.skipped = (
                f"validation against {network} failed "
                f"({len(vr.errors)} error(s)) — not uploading"
            )
            return result

    # 3. Publish to M3G. In dry_run the PUT is not sent (default: safe).
    #    M3G's upload-sitelog ?id= requires the full 9-char station ID,
    #    and publishes directly (no draft state on the API path).
    ur = client.upload_sitelog(nine_char, content, dry_run=dry_run)
    result.upload = ur
    result.uploaded = ur.ok
    if not ur.ok:
        result.skipped = ur.error or f"upload HTTP {ur.status_code}"
    return result
