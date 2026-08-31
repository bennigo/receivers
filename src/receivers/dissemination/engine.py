"""EposDisseminate — the T1 tracer: convert one (station, date) and push it.

For one station and date: resolve the archived RINEX (prefer existing RINEX;
rinex-from-raw is a later ticket), convert it to a RINEX 3.04 long-name file
(:mod:`receivers.dissemination.convert`), and rsync it to the dissemination
target's dest (a staging path in T1). Dry-run does everything but the rsync write.

This proves the riskiest end-to-end path with the fewest moving parts: no DB, no
TOS include-filter, no QC gate (those are T2-T5).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .config import DisseminationTarget

if TYPE_CHECKING:
    from .agencies import AgencyResolver
from .convert import (
    ConvertError,
    convert_for_dissemination,
    package,
    published_name,
)
from .qc_gate import qc_check
from .source_identity import check_source_identity
from .tos_access import session_fingerprint

# Supplies the TOS session (one device_history entry) for (station, observation
# datetime), or None if TOS has no coverage. T1/T2 leave it None (gate skipped);
# T3's TOS access layer injects the live provider.
SessionProvider = Callable[[str, datetime], Optional[dict[str, Any]]]

logger = logging.getLogger("receivers.dissemination")

# Month dir component matches the archive layout (lowercase 3-letter English).
_MONTHS = [
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
]

# Long-name R3 epoch ``_YYYYDDDHHMM_`` (e.g. RHOF00ISL_R_20261280000_01D_15S_MO.crx).
_LONG_EPOCH_RE = re.compile(r"_(\d{4})(\d{3})\d{4}_")
# Short-name stem ``SSSSDDD0.YY[dDoO]`` (DOY in cols 5-7; year taken from the path).
_SHORT_STEM_RE = re.compile(r"^[A-Za-z0-9]{4}(\d{3})[0a-x]\.\d{2}[dDoO]", re.ASCII)


def _date_from_rinex_name(name: str, year_hint: int) -> Optional[date]:
    """The observation date encoded in a RINEX filename.

    Long names carry a full ``YYYYDDD`` epoch; short names carry only DOY, so the
    year comes from the archive's ``/YYYY/`` path component (``year_hint``). Returns
    None for anything that isn't a recognisable RINEX obs name.
    """
    m = _LONG_EPOCH_RE.search(name)
    if m:
        year, doy = int(m.group(1)), int(m.group(2))
        try:
            return date(year, 1, 1) + timedelta(days=doy - 1)
        except ValueError:
            return None
    m = _SHORT_STEM_RE.match(name)
    if m:
        doy = int(m.group(1))
        if 1 <= doy <= 366:
            return date(year_hint, 1, 1) + timedelta(days=doy - 1)
    return None


def archive_date_bounds(
    source_root: str | Path,
    sessions: Optional[tuple[str, ...]],
    station: str,
) -> tuple[Optional[date], Optional[date]]:
    """``(earliest, latest)`` archived observation date for ``station``.

    Walks ``<source_root>/<YYYY>/<mon>/<SID>/<session>/rinex/`` across every year
    and month, deriving each file's date from its name (year from the path for
    short names). Returns ``(None, None)`` when the station has no archived RINEX.
    Backs the ``full`` / ``last`` date tokens.
    """
    root = Path(source_root).expanduser()
    sid = station.upper()
    session_names = tuple(sessions) if sessions else ("15s_24hr",)
    earliest: Optional[date] = None
    latest: Optional[date] = None
    if not root.is_dir():
        return None, None
    for ydir in root.iterdir():
        if not (ydir.is_dir() and ydir.name.isdigit()):
            continue
        year = int(ydir.name)
        for mdir in ydir.iterdir():
            if not mdir.is_dir():
                continue
            for session in session_names:
                rinex_dir = mdir / sid / session / "rinex"
                if not rinex_dir.is_dir():
                    continue
                for f in rinex_dir.iterdir():
                    d = _date_from_rinex_name(f.name, year)
                    if d is None:
                        continue
                    if earliest is None or d < earliest:
                        earliest = d
                    if latest is None or d > latest:
                        latest = d
    return earliest, latest


@dataclass
class DisseminateResult:
    """Outcome of one EposDisseminate.run_one()."""

    station: str
    file_date: date
    ok: bool = False
    dry_run: bool = False
    source_path: Optional[str] = None
    long_name: Optional[str] = None
    cached: bool = False
    pushed: bool = False
    artifact_path: Optional[str] = None  # local converted R3 file (for indexing)
    qc_passed: Optional[bool] = None  # None = gate not run (no session provider)
    qc_message: str = ""
    dest: Optional[str] = None
    relative_path: Optional[str] = None  # dest-relative path of the published file
    rinex_version: Optional[int] = None
    # Legacy short-name file this long-name product supersedes on the portal (same
    # day/dir), to be removed after a durable push+index. None when the name is
    # unchanged (R2-short overwrite) or the source was raw (no legacy RINEX).
    superseded_name: Optional[str] = None
    # Convert-cache directories this product occupies: the output-layer entry and
    # its decode-layer entry. Carried so the caller can drop them once the file is
    # DURABLE on the portal — see receivers/dissemination/job.py `_on_flush`.
    # Empty when the target keeps its cache (`prune_cache: false`) or on a dry run.
    cache_entries: list[str] = field(default_factory=list)
    message: str = ""
    errors: list[str] = field(default_factory=list)


#: Cache roots already reaped in this process (see EposDisseminate.__init__).
_REAPED_CACHE_ROOTS: set = set()


class EposDisseminate:
    """Run the dissemination pipeline for explicit (station, date) inputs."""

    def __init__(
        self,
        target: DisseminationTarget,
        *,
        dry_run: bool = False,
        dest_override: Optional[str] = None,
        session_provider: Optional[SessionProvider] = None,
        set_header: bool = True,
        agency_resolver: Optional[AgencyResolver] = None,
        tos_metadata_cache: Optional[dict] = None,
    ) -> None:
        self.target = target
        self.dry_run = dry_run
        # Reap orphaned convert-cache entries once per cache root per process.
        # Deliberately runs on a DRY RUN too: a dry run converts for real and
        # records no cache_entries, so it is one of the three things that orphan
        # them (see convert.reap_stale_cache). Doing it in __init__ rather than
        # per-file keeps it O(1) per run, and the guard keeps --parallel's
        # per-chunk engines from each re-walking the same directory.
        self._reap_stale_cache_once()
        self.dest_override = dest_override
        # When set, the header-QC gate runs before every push (T3 injects it).
        self.session_provider = session_provider
        # Caller-owned memo for the date-independent per-station TOS fetch inside
        # set_header (correct_rinex_from_tos). Shared across every date of a run so
        # a backfill makes 1 TOS call per station, not 1 per file. Own dict if none
        # passed (still dedupes within this engine's chunk).
        self.tos_metadata_cache: dict = (
            tos_metadata_cache if tos_metadata_cache is not None else {}
        )
        # Rewrite the converted header from TOS before caching/QC (needs a session
        # provider to fingerprint the TOS metadata for the cache key). Tests pass
        # set_header=False to stay offline.
        self.set_header = set_header
        # Per-station RINEX OBSERVER/AGENCY come from the station's TOS owner org via
        # agencies.yaml (falling back to the target's format defaults). Loaded once;
        # injectable for tests. A missing agencies.yaml → empty resolver → defaults.
        if agency_resolver is None:
            from .agencies import AgencyResolver

            agency_resolver = AgencyResolver.load()
        self.agency_resolver = agency_resolver
        # Optional utils.net_push.BatchPush: when set (by the range/sweep driver),
        # run_one STAGES the published file into the batcher instead of pushing it
        # per-file — the batcher flushes every N files in one rsync, and its
        # on_flush callback does the index+supersede AFTER the batch is durable.
        # Unset (single-date CLI, reactive) → immediate per-file push, unchanged.
        self.pusher: Optional[Any] = None

    def _resolve_observer_agency(self, session: Optional[dict]) -> tuple[str, str]:
        """(OBSERVER, AGENCY) for the RINEX header.

        The RINEX AGENCY is the *responsible* agency — the station **owner** (EPOS
        model) — rendered as the full ENGLISH institutional name (EPOS wants English
        agency names), via :attr:`AgencyInfo.rinex_agency` (falls back to the short
        form only when the English name overflows the 40-char field). observer/agency
        are NOT configured in sync.yaml — agencies.yaml owns them: per-station via the
        TOS owner org, and the IMO entity (``defaults.operator_agency``) as the
        fallback for an unknown/absent org. The format's code default is a last resort.
        """
        fmt = self.target.format
        owner_org = (session or {}).get("owner_org") if session else None
        info = (
            self.agency_resolver.resolve(owner_org)
            or self.agency_resolver.default_agency()
        )
        if info is not None:
            return (
                info.observer or getattr(fmt, "observer", ""),
                info.rinex_agency or getattr(fmt, "agency", ""),
            )
        return (getattr(fmt, "observer", ""), getattr(fmt, "agency", ""))

    # ---- source resolution -------------------------------------------------

    def _station_session_dir(self, station: str, d: date, session: str) -> Path:
        return (
            Path(self.target.source_root)
            / f"{d.year:04d}"
            / _MONTHS[d.month - 1]
            / station.upper()
            / session
        )

    def _rinex_dir(self, station: str, d: date, session: str) -> Path:
        return self._station_session_dir(station, d, session) / "rinex"

    def find_source(self, station: str, d: date) -> Optional[Path]:
        """Locate the archived RINEX for (station, date), any compression/case.

        Prefers an existing RINEX file. (rinex-from-raw fallback is a later
        ticket — logged, not implemented, in T1.)
        """
        doy = d.timetuple().tm_yday
        yy = d.year % 100
        for session in self.target.sessions or ("15s_24hr",):
            rinex_dir = self._rinex_dir(station, d, session)
            if not rinex_dir.is_dir():
                continue
            # RINEX-2 short stem: SSSSDDD0  (daily session char '0').
            stem = f"{station.upper()}{doy:03d}0"
            for pattern in (
                f"{stem}.{yy:02d}[dD]*",  # Hatanaka (.YYd/.YYD[.gz/.Z])
                f"{stem}.{yy:02d}[oO]*",  # plain obs
                f"{station.upper()}*{doy:03d}*_??_MO.???*",  # already long-name R3
            ):
                hits = sorted(rinex_dir.glob(pattern))
                if hits:
                    return hits[0]
        return None

    def find_raw_source(self, station: str, d: date) -> Optional[Path]:
        """Locate the archived raw file for (station, date) — the rinex fallback.

        Trimble ``.T02``/``.T00`` (any compression). Septentrio ``.sbf`` is found
        too, but the converter for it is not wired yet (handled at convert time).
        """
        # The raw dir holds a whole month (``%Y/#b/{station}/.../raw/``), so the
        # glob MUST be constrained to the requested day — the raw filename embeds
        # the observation date as ``YYYYMMDD`` (e.g. ``AKUR202606280000a.T02.gz``,
        # or underscore-padded ``AKUR______202606280000a.T02``). Without this
        # filter, ``sorted(...)[0]`` returns the month's earliest file and we would
        # publish the wrong day's data under the requested day's name.
        ymd = d.strftime("%Y%m%d")
        for session in self.target.sessions or ("15s_24hr",):
            raw_dir = self._station_session_dir(station, d, session) / "raw"
            if not raw_dir.is_dir():
                continue
            for pattern in ("*.T02*", "*.T00*", "*.t02*", "*.t00*", "*.sbf*"):
                hits = sorted(p for p in raw_dir.glob(pattern) if ymd in p.name)
                if hits:
                    return hits[0]
        return None

    # ---- push --------------------------------------------------------------

    @property
    def _dest_base(self) -> str:
        dest = (
            self.dest_override if self.dest_override is not None else self.target.dest
        )
        if not self.target.host:
            return dest
        return f"{self.target.user}@{self.target.host}:{dest}"

    def relative_dir(
        self, station: str, d: date, session_segment: Optional[str] = None
    ) -> str:
        """Render the format's ``dir_template`` (gtimes tokens + {station}) for d.

        ``{session}`` in the template resolves to ``session_segment`` — the
        per-product rate directory (``15s_24hr``/``30s_24hr``), derived from
        the product's sample else the obs INTERVAL. Never hardcode the rate.
        """
        import gtimes.timefunc as gt

        template = self.target.format.dir_template.replace("{station}", station.upper())
        if "{session}" in template:
            template = template.replace("{session}", session_segment or "15s_24hr")
        # gtimes resolves date tokens (%Y, #b, …); '1D' frequency for a daily file.
        rendered = gt.datepathlist(
            template, "1D", datelist=[datetime(d.year, d.month, d.day)]
        )
        return rendered[0].strip("/") if rendered else ""

    def _push(self, local_file: Path, rel_dir: str) -> bool:
        """rsync ``local_file`` into ``<dest>/<rel_dir>/``. Returns True if transferred.

        Creates the destination directory (local: os.makedirs; remote: rsync
        ``--mkpath``). The published filename is ``local_file.name``.
        """
        base = (
            self.dest_override if self.dest_override is not None else self.target.dest
        )
        rel_dir = rel_dir.strip("/")
        full_dir = (
            f"{base.rstrip('/')}/{rel_dir}/" if rel_dir else base.rstrip("/") + "/"
        )

        # --chmod=D755,F644 pins portal perms regardless of the source umask (see
        # net_push.rsync_tree); --no-owner/--no-group: the epos user can't chown.
        cmd = [
            "rsync",
            "-a",
            "--chmod=D755,F644",
            "--no-owner",
            "--no-group",
            "--itemize-changes",
            "--mkpath",
        ]
        if self.dry_run:
            cmd.append("--dry-run")
        if not self.target.host:
            if not self.dry_run:
                os.makedirs(full_dir, exist_ok=True)
            dest = full_dir
        else:
            dest = f"{self.target.user}@{self.target.host}:{full_dir}"
        cmd += [str(local_file), dest]
        logger.info(
            "rsync %s → %s%s",
            local_file.name,
            dest,
            " [dry-run]" if self.dry_run else "",
        )
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise ConvertError(f"rsync rc={proc.returncode}: {proc.stderr.strip()}")
        return any(
            ln[:1] in "<>" for ln in proc.stdout.splitlines() if len(ln.split()) == 2
        )

    # ---- orchestration -----------------------------------------------------

    def _session_segment(self, obs_path: Path, sample: Optional[int]) -> str:
        """``<rate>s_24hr`` for the {session} dir token (rate = sample else
        the obs INTERVAL — the same content-derived detection the long-name
        frequency token uses)."""
        from .convert import _resolve_data_frequency

        tok = _resolve_data_frequency(Path(obs_path), sample)
        if tok.endswith("S"):
            try:
                return f"{int(tok[:-1])}s_24hr"
            except ValueError:  # pragma: no cover - malformed token
                pass
        return "15s_24hr"

    def _reap_stale_cache_once(self) -> None:
        """Drop convert-cache entries no run will claim again. Best-effort."""
        try:
            max_age = int(getattr(self.target, "cache_max_age_days", 7) or 0)
            if max_age <= 0:
                return
            root = self.target.cache_path
            key = str(root)
            if key in _REAPED_CACHE_ROOTS:
                return
            _REAPED_CACHE_ROOTS.add(key)
            from .convert import reap_stale_cache

            reap_stale_cache(root, max_age_days=max_age, logger=logger)
        except Exception:  # noqa: BLE001 — housekeeping must never fail a run
            logger.debug("convert-cache reap skipped", exc_info=True)

    def run_one(
        self, station: str, d: date, product: Optional[Any] = None
    ) -> DisseminateResult:
        # Per-product dissemination (format.products): each product is one
        # published file per date. Default = the format's single product.
        if product is None:
            product = self.target.format.active_products()[0]
        sample = product.sample

        result = DisseminateResult(
            station=station.upper(), file_date=d, dry_run=self.dry_run
        )

        if station.upper() in self.target.exclude_stations:
            result.ok = True
            result.message = "station excluded from this target"
            return result

        # Source precedence: prefer archived RINEX, else fall back to archived raw.
        obs_dt = datetime(d.year, d.month, d.day)
        source = self.find_source(station, d)
        raw_source = None if source is not None else self.find_raw_source(station, d)
        if source is None and raw_source is None:
            result.message = "no archived RINEX or raw found"
            result.errors.append(result.message)
            return result
        result.source_path = str(source or raw_source)

        # Fetch the TOS session ONCE (before convert): it drives both the
        # set-header step's cache fingerprint and the QC gate. set-header only runs
        # when we have a session (so the fingerprint reflects the metadata used).
        session = (
            self.session_provider(station.upper(), obs_dt)
            if self.session_provider is not None
            else None
        )
        # Identity gate, on the SOURCE, BEFORE anything is converted or
        # rewritten. The QC gate below runs on the post-set-header product, so it
        # cannot answer "is this file actually this station's?" — set_header has
        # by then normalised the header towards TOS, and the check would be
        # validating the pipeline's own rewrite. See source_identity for the ISAK
        # Aug-2016 case this exists to stop: 14 days of a campaign survey up to
        # 245 km away, published as ISAK with ISAK's DOMES.
        if source is not None:
            identity = check_source_identity(Path(source), session)
            if not identity.ok:
                result.qc_passed = False
                result.qc_message = identity.message
                result.message = f"identity gate failed: {identity.message}"
                result.errors.append(result.message)
                return result

        do_set_header = self.set_header and session is not None
        fingerprint = session_fingerprint(session) if do_set_header else ""

        # Per-station RINEX OBSERVER/AGENCY from the station's TOS owner agency
        # (agencies.yaml), else the target's format defaults. owner_org is folded
        # into session_fingerprint, so a re-designation re-renders the cached header.
        observer, agency = self._resolve_observer_agency(session)

        # Convert to the cached canonical plain obs (Model B: version preserved).
        src = source if source is not None else raw_source
        assert src is not None
        try:
            conv = convert_for_dissemination(
                src,
                station.upper(),
                obs_dt,
                fmt=self.target.format,
                cache_dir=self.target.cache_path,
                tos_fingerprint=fingerprint,
                set_header=do_set_header,
                domes=(session or {}).get("domes", ""),
                observer=observer,
                agency=agency,
                sample=sample,
                tos_metadata_cache=self.tos_metadata_cache,
            )
        except ConvertError as exc:
            result.message = f"convert failed: {exc}"
            result.errors.append(str(exc))
            return result

        policy = self.target.format.policy_for(conv.rinex_version)
        # A decimated (sample) product IS emitted for RINEX2: the 30 s product
        # for genuinely-R2 data (e.g. pre-2012) is a real, wanted EPOS product —
        # decimated content, not a duplicate. Its short name carries no rate
        # token, so the session DIR (30s_24hr/ vs 15s_24hr/) distinguishes the
        # rate, as RINEX2 always does. A stale/premature R2 30 s (e.g. before a
        # station is re-rinexed to R3) is cleaned by the G2 same-slot purge when
        # the R3 long replaces it — so no up-front skip is needed here.
        pub_name = published_name(conv.obs_name, policy)
        session_segment = (
            self._session_segment(conv.output_path, sample)
            if "{session}" in self.target.format.dir_template
            else None
        )
        rel_dir = self.relative_dir(station, d, session_segment=session_segment)
        result.long_name = pub_name
        result.cached = conv.cached
        # Record both cache layers for post-push pruning. The output layer is the
        # converted file's own directory; the decode layer is keyed separately
        # (source content + sample) and lives under `decode/`, so pruning only the
        # output layer would leave the larger half behind.
        if getattr(self.target, "prune_cache", True) and not self.dry_run:
            from .convert import decode_cache_key

            entries = [str(Path(conv.output_path).parent)]
            try:
                entries.append(
                    str(
                        self.target.cache_path
                        / "decode"
                        / decode_cache_key(Path(conv.source_path), sample=sample)
                    )
                )
            except Exception:  # noqa: BLE001 — pruning is best-effort
                pass
            result.cache_entries = entries
        result.rinex_version = conv.rinex_version
        result.dest = self._dest_base
        result.relative_path = f"{rel_dir}/{pub_name}" if rel_dir else pub_name

        # Supersede target: when an R3 long-name product replaces the legacy
        # short-name file (the old container pushed archived RINEX under its
        # original short name), record that name so the caller can remove it from
        # the portal + DB after a durable push+index. Only when the source is an
        # archive RINEX (not raw) AND the published name actually changed — an
        # R2-short push keeps the same name (rsync overwrite, nothing to clean).
        # Only the NATIVE-rate product may supersede: a decimated product
        # (e.g. 30 s) is an ADDITIONAL file, not a replacement for the legacy
        # short-name day file.
        if source is not None and pub_name != Path(source).name and sample is None:
            result.superseded_name = Path(source).name

        # Header-QC gate: verify the plain obs header vs TOS before packaging/push.
        # Reuses the session fetched above (no second TOS round-trip).
        if self.session_provider is not None:
            verdict = qc_check(conv.output_path, session)
            result.qc_passed = verdict.passed
            result.qc_message = verdict.message
            if not verdict.passed:
                result.message = f"QC gate failed: {verdict.message}"
                result.errors.append(result.message)
                return result
        else:
            logger.debug("QC gate skipped for %s (no TOS session provider)", station)

        # Package (Hatanaka/compression per policy) into the cache dir (persists for
        # indexing + reuse), then push into the layout dir.
        try:
            published = package(conv.output_path, policy, conv.output_path.parent)
            result.artifact_path = str(published)
        except ConvertError as exc:
            result.message = f"package failed: {exc}"
            result.errors.append(str(exc))
            return result

        cache_note = " (cached)" if conv.cached else ""
        if self.pusher is not None:
            # Staged: the batcher rsyncs this file (with others) on its next flush,
            # and its on_flush does the index+supersede once durable. Mark ok so the
            # caller counts it; the actual transfer/index happen at flush time.
            self.pusher.add(published, rel_dir, pub_name, ref=result)
            result.ok = True
            result.message = f"staged {pub_name}{cache_note}"
            return result

        try:
            transferred = self._push(published, rel_dir)
        except ConvertError as exc:
            result.message = f"push failed: {exc}"
            result.errors.append(str(exc))
            return result
        result.pushed = transferred and not self.dry_run
        result.ok = True
        verb = (
            "would push"
            if self.dry_run
            else ("pushed" if transferred else "up-to-date")
        )
        result.message = f"{verb} {pub_name}{cache_note}"
        return result
