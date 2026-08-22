"""IGS/M3G site-log generation for EPOS dissemination (C6/T7).

EPOS §3.2 makes the station site log the canonical metadata record, maintained
in the M3G portal (https://gnss-metadata.eu) within one business day of any TOS
change. This module is the dissemination-side wiring around the existing tostools
generator: it reads the station's TOS metadata, renders the IGS site log, and
writes it to a target directory (a ``gps-sitelogs`` repo working tree in
production).

**The renderer is** :func:`tostools.legacy.gps_metadata_functions.site_log`
(imported at the call site below), **NOT**
``tostools.core.site_log.generate_igs_site_log``, which this docstring named
until 2026-08-22 and which has no production caller at all. Only the file
writer, ``core.site_log.export_site_log_to_file``, comes from that module.

That distinction is load-bearing: what M3G receives is produced by the legacy
renderer, so a fix applied to ``core/site_log.py`` changes nothing that is
published. This is the shape of the VMEY HTTP 422 incident (2026-08-20, empty
antenna serial), and ``tostools/tests/test_sitelog_unknown_antenna_serial.py``
pins both modules for exactly that reason.

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


def _agency_dict(info: Any) -> dict[str, Any]:
    """AgencyInfo → the plain dict :func:`generate_igs_site_log` renders (§11/§12)."""
    return {
        "name_lines": [ln for ln in (info.english_name, info.department_en) if ln],
        "abbrev": info.abbrev,
        "address": list(info.address),
        "contact_name": info.contact_name,
        "phone": info.phone,
        "email": info.email,
    }


def _station_role_orgs(client: Any, meta: dict[str, Any]) -> dict[str, str]:
    """The station's contact-role → organization map from raw TOS contacts.

    Reads ``get_contacts(id_entity)`` (raw rows) rather than the processed
    ``meta['contact']`` dict — the processed view keeps one contact per bucket and
    its 'eigandi' substring match cannot distinguish *Eigandi stöðvar* (station
    owner) from *Eigandi gagna* (data owner). Best-effort: any failure → empty map
    (the IMO defaults then apply).
    """
    roles: dict[str, str] = {}
    try:
        rows = client.get_contacts(meta.get("id_entity")) or []
    except Exception as exc:  # noqa: BLE001 - roles are enrichment, not required
        logger.warning("site log: contact-role lookup failed: %s", exc)
        return roles
    for row in rows:
        role = f"{row.get('role_is') or ''} {row.get('role') or ''}".lower()
        org = (row.get("organization") or row.get("name") or "").strip()
        if not org:
            continue
        if "eigandi gagna" in role or "data_owner" in role or "data owner" in role:
            roles.setdefault("data_owner", org)
        elif "eigandi" in role or "owner" in role:
            roles.setdefault("owner", org)
        elif "rekstrar" in role or "operator" in role or "tengili" in role:
            roles.setdefault("operator", org)
    return roles


def resolve_sitelog_agencies(
    client: Any, meta: dict[str, Any], resolver: Any = None
) -> dict[str, Any]:
    """Role-guided §11/§12/§13 agency data (TOS roles = who, agencies.yaml = render).

    - §11 On-Site POC        ← always the IMO default: IMO runs the network and
      disseminates the data, so it is the on-site/data point of contact even when
      TOS records another org as Rekstraraðili (that upkeep role belongs in §12).
    - §12 Responsible Agency ← owner role — only when it differs from §11.
    - §13 Primary DC         ← data-owner role, else the IMO default;
      Secondary DC           ← owner (when ≠ primary); URL ← agencies.yaml default.

    Unknown-org fallbacks keep the log renderable: an owner org missing from
    agencies.yaml is emitted by its raw TOS name (never dropped silently).
    """
    from .agencies import AgencyResolver

    if resolver is None:
        resolver = AgencyResolver.load()
    roles = _station_role_orgs(client, meta)

    poc_info = resolver.operator_default()
    dc_info = (
        resolver.resolve(roles.get("data_owner")) or resolver.data_center_default()
    )
    owner_org = roles.get("owner") or ""
    owner_info = resolver.resolve(owner_org)

    agencies: dict[str, Any] = {
        "poc": _agency_dict(poc_info) if poc_info else None,
        "responsible": None,
        "data_center": {
            "primary": dc_info.dc_label if dc_info else "",
            "secondary": "",
            "url": resolver.url_default(),
        },
    }
    # §12 only when the responsible (owner) agency differs from the §11 contact.
    poc_org = poc_info.org if poc_info else ""
    if owner_org and owner_org != poc_org:
        agencies["responsible"] = (
            _agency_dict(owner_info) if owner_info else {"name_lines": [owner_org]}
        )
    # §13 secondary = the owner, when it isn't already the primary data center.
    dc_org = dc_info.org if dc_info else ""
    if owner_org and owner_org != dc_org:
        agencies["data_center"]["secondary"] = (
            owner_info.dc_label if owner_info else owner_org
        )
    return agencies


def find_previous_site_log(out_dir: Path, nine_char: str, current_date: str) -> str:
    """The latest dated site log for ``nine_char`` older than ``current_date``.

    The M3G convention is a dated series (``rhof00isl_20240827.log``); §0
    "Previous Site Log" chains each log to its predecessor. Lexicographic sort ==
    chronological for ``YYYYMMDD`` names; the current date's own file is excluded
    so a same-day regeneration doesn't reference itself. Empty string when no
    prior log exists (first log in the series).
    """
    prefix = nine_char.lower()
    current_name = f"{prefix}_{current_date}.log"
    try:
        names = sorted(
            p.name
            for p in Path(out_dir).glob(f"{prefix}_*.log")
            if p.name < current_name
        )
    except OSError:
        return ""
    return names[-1] if names else ""


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
) -> Optional[tuple[str, Path]]:
    """Fetch TOS + render the site-log content and resolve its dated filename,
    WITHOUT writing. Returns ``(content, out_path)`` or None on any TOS/render
    failure (logged). Shared by :func:`generate_site_log` and the change-gate."""
    from datetime import datetime

    from tostools.legacy.gps_metadata_functions import site_log as render_site_log
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
        content = render_site_log(
            sid,
            loglevel=loglevel,
            previous_log=previous,
            agencies=agencies,
            monument_number=mon,
            country_code=country_code.upper(),
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
) -> Optional[Path]:
    """Render the IGS site log for ``station`` from TOS into ``out_dir``.

    Returns the written path, or None when TOS has no usable metadata (logged).
    ``client`` is an injectable ``TOSClient`` (defaults to a fresh one) so tests
    run offline. The M3G dated filename form (``rhof00isl_20240827.log``) is the
    default — §0 "Previous Site Log" chains to the latest prior dated log found
    in ``out_dir``; pass ``include_date=False`` for the plain ``RHOF00ISL.log``.
    ``agency_resolver`` (default: load agencies.yaml) drives §11/§12/§13 via
    :func:`resolve_sitelog_agencies`.

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

    Returns True on a real commit, False when there was nothing to commit. Does
    NOT push (the sync/submission policy is decided per decision #3). Raises on a
    genuine git error so callers see a misconfigured repo rather than silent loss.
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
