"""Convert an archived RINEX file to RINEX 3.04 with a long IGS name.

The riskiest link in the dissemination chain — it is what the legacy library
*cannot* do. The chain, for a 15s_24hr RINEX-2 Hatanaka source
(``SSSSDDD0.YYd.gz`` / ``.YYD.Z``):

    1. decompress  (.gz/.Z → CRINEX)            via ``gzip -dc``
    2. un-Hatanaka (CRINEX → RINEX-2 obs)       via ``CRX2RNX``
    3. R2 → R3 + long name                      via ``gfzrnx -vo 3``
    (4. set header from TOS — DEFERRED to T2/T3; ``set_header`` is a stub here)

The output is a two-layer cache: the expensive *header-free* decode is cached on
source content alone (:func:`decode_cache_key`), and the header-applied product on
``sha256(content_sha256 + ":" + tos_fingerprint + ":h" + schema)``
(:func:`cache_key`). A later TOS header correction (which changes ``tos_fingerprint``)
or a header-logic bump invalidates exactly the affected output files but reuses the
decode cache — so the re-push re-heads the cached obs with no raw re-decode. T1 passes
an empty fingerprint; the reactive ticket (T6) supplies the real one.

Tools resolve from ``$GPS_TOOLS_BIN`` → ``PATH`` (incl. uppercase) → the sibling
``gps-tools/bin/`` checkout, so it works both deployed (symlinked into
/usr/local/bin) and on a dev laptop.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..rinex.converter_base import ConversionError, NamingConvention, RinexVersion
from ..rinex.rinex_namer import RinexNamer
from ..utils.content_hash import content_sha256

logger = logging.getLogger(__name__)

# Repo-relative fallback: convert.py is receivers/src/receivers/dissemination/ →
# the gpslibrary_new root is parents[4], and gps-tools/bin is its sibling.
_GPS_TOOLS_BIN_FALLBACK = Path(__file__).resolve().parents[4] / "gps-tools" / "bin"


class ConvertError(RuntimeError):
    """A convert step (decompress / CRX2RNX / gfzrnx / packaging) failed."""


# Raw extensions handled by the raw→rinex fallback (Trimble only in this build).
_TRIMBLE_RAW = (".t02", ".t00")


def resolve_tool(name: str) -> str:
    """Locate an external tool: ``$GPS_TOOLS_BIN`` → PATH → sibling gps-tools/bin.

    Also tries the uppercase variant (CRX2RNX/RNX2CRX ship uppercase).
    """
    env_bin = os.getenv("GPS_TOOLS_BIN")
    candidates = []
    for base in (env_bin, str(_GPS_TOOLS_BIN_FALLBACK)):
        if not base:
            continue
        candidates += [Path(base) / name, Path(base) / name.upper()]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    found = shutil.which(name) or shutil.which(name.upper())
    if found:
        return found
    raise ConvertError(
        f"tool {name!r} not found (set GPS_TOOLS_BIN, add to PATH, or check "
        f"{_GPS_TOOLS_BIN_FALLBACK})"
    )


def _run(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    logger.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ConvertError(
            f"{cmd[0]} rc={proc.returncode}: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc


@dataclass
class ConvertResult:
    """Outcome of one convert call — the cached canonical *plain obs* file.

    Packaging (Hatanaka / compression) and the published filename are applied
    separately by :func:`package` / :func:`published_name`, per the version policy.
    """

    output_path: Path  # cached plain RINEX obs (.rnx for R3, .YYo for R2)
    obs_name: str
    rinex_version: int  # the SOURCE's version, preserved (Model B)
    cached: bool  # True if served from cache (no work done)
    source_path: Path


def _strip_compression(name: str) -> tuple[str, bool]:
    """Return (name-without-compression-suffix, was_compressed)."""
    for suffix in (".gz", ".Z", ".bz2"):
        if name.endswith(suffix):
            return name[: -len(suffix)], True
    return name, False


def _crinex_to_obs_name(crinex_name: str) -> str:
    """Expected CRX2RNX output name for a CRINEX input.

    ``*.YYd`` → ``*.YYo``, ``*.YYD`` → ``*.YYO``, ``*.crx`` → ``*.rnx``.
    """
    if crinex_name.endswith(".crx"):
        return crinex_name[:-4] + ".rnx"
    if crinex_name.endswith(".CRX"):
        return crinex_name[:-4] + ".RNX"
    last = crinex_name[-1]
    if last == "d":
        return crinex_name[:-1] + "o"
    if last == "D":
        return crinex_name[:-1] + "O"
    raise ConvertError(f"cannot derive obs name from CRINEX {crinex_name!r}")


def _is_hatanaka(name: str) -> bool:
    """True if a decompressed RINEX name is Hatanaka-compressed (CRINEX)."""
    return bool(name) and (name.endswith((".crx", ".CRX")) or name[-1] in "dD")


# Bump on any CODE-level change to what set_header_from_tos / finalize_epos_header
# write into the header. Folded into cache_key so a logic fix invalidates cached
# converted products even though the source bytes AND the TOS fingerprint are
# unchanged — a plain `epos-disseminate --force` re-push then re-heads the cached
# decode obs (no raw re-decode) instead of serving a stale cache hit. (A TOS-driven
# header fix already moves the fingerprint, so it does not need a bump.)
#   v1: original EPOS header finalization
#   v2: MARKER NUMBER no-DOMES fallback → version-aware (9-char R3 / 4-char R2)
#   v3: MARKER NUMBER = IERS DOMES only; no DOMES → strip the line (no id fallback)
#   v4: ANTENNA: DELTA H/E/N east/north are a monument+antenna composite. The
#       TOS path (which is the ONLY path dissemination uses — set_header_from_tos
#       passes station_config=None) read the eccentricity off the antenna entity
#       alone, so a station whose offset lives on the MONUMENT record published a
#       zeroed E/N against a site log carrying the real value. Fleet-wide this is
#       ISAK (0.0001/0.0002) and AUST (-0.0001/0.0001) and nobody else — but the
#       bump must be fleet-wide anyway: the fix is a code change, so neither the
#       source bytes nor the TOS fingerprint move, and without a new schema
#       version a re-push of those two would silently re-serve the cached stale
#       header. Re-heads for the other 342 produce identical bytes.
HEADER_SCHEMA_VERSION = 4


def cache_key(
    source_path: Path, tos_fingerprint: str = "", sample: Optional[int] = None
) -> str:
    """``sha256(content_sha256 + ':' + tos_fingerprint + ':' + header schema)``.

    Three things invalidate the cached converted product:

    - **Source content** — the raw/RINEX bytes changed.
    - **TOS fingerprint** — a header correction driven by a TOS metadata change
      (the source bytes are unchanged, but the applied header differs).
    - **Header schema version** (:data:`HEADER_SCHEMA_VERSION`) — a *code-level*
      change to what ``finalize_epos_header`` / ``set_header_from_tos`` write.
      A TOS fix moves the fingerprint; a logic fix does not, so without this a
      ``--force`` re-push would serve a stale header. Bump the constant on any
      such change.

    A decimated product (``sample`` set) gets its own ``-s<rate>`` key so the
    native and 30 s obs for the same date never share a cache slot.
    """
    src_hash = content_sha256(source_path)
    key = hashlib.sha256(
        f"{src_hash}:{tos_fingerprint}:h{HEADER_SCHEMA_VERSION}".encode()
    ).hexdigest()
    return f"{key}-s{sample}" if sample is not None else key


#: Convert-cache entry names we are willing to delete: a 64-hex cache key with an
#: optional ``-s<rate>`` suffix, the ``decode`` sub-cache, or one of the
#: ``.epos_<phase>_<suffix>`` TemporaryDirectory leftovers. Anything else in the
#: cache root is not ours and is never touched -- the reaper runs unattended
#: against a configurable path, so it refuses to be pointed at a real directory.
_CACHE_ENTRY_RE = re.compile(
    r"^(?:[0-9a-f]{64}(?:-s\d+)?|decode|\.epos_[a-z]+_[A-Za-z0-9_]+)$"
)


def reap_stale_cache(
    cache_root: Path, max_age_days: int = 7, logger: Any = None
) -> tuple[int, int]:
    """Delete convert-cache entries no run will ever claim again.

    ``prune_cache`` reaps an entry when its push goes DURABLE, which covers the
    success path and only the success path. Three things bypass it entirely:

    * a **dry run** -- it converts for real (that is the point) but
      :class:`EposDisseminate` skips recording ``cache_entries`` when
      ``dry_run``, and nothing ever pushes, so every intermediate it writes is
      orphaned on exit;
    * a run **killed** mid-range -- ^C, SIGTERM, a systemd stop, an OOM;
    * a run whose **push failed** -- the entry is deliberately kept so a retry
      is cheap, and then nobody retries.

    Measured 2026-08-31 on rek-d01: 209 GB across 18,097 entries dating back
    three weeks, against a 2 TB volume that an unreaped cache has filled before
    and taken Postgres down with it. Nothing was reaping them because nothing
    was responsible for them.

    Age is the right discriminator precisely BECAUSE ``prune_cache`` works: an
    entry from a healthy run is deleted seconds after its push, so anything
    still present after ``max_age_days`` is by construction an orphan. Entries
    are matched against :data:`_CACHE_ENTRY_RE` before deletion, so a
    mis-pointed ``convert_cache_dir`` reaps nothing rather than eating a real
    directory. Best-effort throughout: a delete that loses a race with a
    concurrent run is skipped, never raised -- reaping must not fail a push.

    Returns ``(entries_removed, bytes_reclaimed)``.
    """
    cache_root = Path(cache_root).expanduser()
    # 0 (and anything negative) means OFF, matching the config knob's
    # documented semantics. Treating 0 as "older than zero days" would
    # make the disable value reap the entire cache instead.
    if not cache_root.is_dir() or max_age_days <= 0:
        return (0, 0)
    cutoff = time.time() - (max_age_days * 86400)
    removed = freed = 0
    try:
        entries = list(cache_root.iterdir())
    except OSError:
        return (0, 0)
    for entry in entries:
        if not _CACHE_ENTRY_RE.match(entry.name):
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            size = (
                sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                if entry.is_dir()
                else entry.stat().st_size
            )
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue  # lost a race with a live run, or vanished — fine
        removed += 1
        freed += size
    if removed and logger is not None:
        logger.info(
            "reaped %d orphaned convert-cache entries (%.1f GB) older than %d "
            "days from %s",
            removed,
            freed / 1073741824,
            max_age_days,
            cache_root,
        )
    return (removed, freed)


def decode_cache_key(source_path: Path, sample: Optional[int] = None) -> str:
    """``sha256(content_sha256 + ':decode')`` — the *header-free* decode key.

    The expensive raw→canonical decode (sbf2rin / CRX2RNX / gfzrnx) depends only on
    the **source bytes** and the sampling rate — NOT on TOS metadata or the header
    schema. Keying the decoded canonical obs on content alone lets a TOS-driven
    header correction (which moves :func:`cache_key`'s fingerprint) or a header-logic
    bump (:data:`HEADER_SCHEMA_VERSION`) re-push a corrected header by re-copying and
    re-heading this cached obs — with no raw re-decode. This is what makes an
    ``epos-disseminate --force`` header re-push cheap.
    """
    src_hash = content_sha256(source_path)
    key = hashlib.sha256(f"{src_hash}:decode".encode()).hexdigest()
    return f"{key}-s{sample}" if sample is not None else key


def long_rinex3_name(
    station: str,
    observation_dt: datetime,
    *,
    country_code: str = "ISL",
    data_frequency: str = "15S",
    file_period: str = "01D",
    monument_number: str = "00",
) -> str:
    """The RINEX 3 long IGS filename for this station/epoch (e.g. ``...MO.rnx``)."""
    namer = RinexNamer(
        station,
        RinexVersion.RINEX_3,
        country_code=country_code,
        monument_number=monument_number,
    )
    return namer.generate_filename(
        observation_dt,
        convention=NamingConvention.LONG,
        data_source="R",
        file_period=file_period,
        data_frequency=data_frequency,
        file_type="MO",
    )


def _obs_to_crinex_name(name: str) -> str:
    """Inverse of :func:`_crinex_to_obs_name`: obs → CRINEX (.rnx→.crx, .YYo→.YYd)."""
    if name.endswith(".rnx"):
        return name[:-4] + ".crx"
    if name.endswith(".RNX"):
        return name[:-4] + ".CRX"
    last = name[-1]
    if last == "o":
        return name[:-1] + "d"
    if last == "O":
        return name[:-1] + "D"
    raise ConvertError(f"cannot derive CRINEX name from obs {name!r}")


def detect_rinex_version(obs_path: Path) -> int:
    """The major RINEX version (2 or 3) from a plain obs file's first header line."""
    with open(obs_path, encoding="utf-8", errors="ignore") as fh:
        first = fh.readline()
    try:
        return int(float(first[:9].strip()))
    except ValueError as exc:
        raise ConvertError(f"no RINEX version in {obs_path.name}: {first!r}") from exc


def _obs_complete(obs_path: Path) -> bool:
    """Cheap completeness check for a cached PLAIN obs.

    A killed convert (before atomic writes were introduced) could leave a
    truncated obs in the cache dir; cache-hits then served it forever, causing
    false QC blocks / RNX2CRX "truncated" package failures until a manual clear.
    This heals such pre-existing poison (and bit-rot) on the hit path.

    Streams to ``END OF HEADER`` with an early exit (never slurps the whole file
    — obs can be 20 MB and this runs per cache-hit). END OF HEADER is mandatory in
    every RINEX version, so its presence + a size floor is a false-positive-free
    signal that the header write completed. It intentionally does NOT try to catch
    mid-body truncation — that class is prevented by atomic cache writes, not by a
    cheap scan.
    """
    try:
        # Floor only rejects trivially-tiny garbage; END OF HEADER is the real
        # signal (a valid header-only obs can be ~150 B).
        if obs_path.stat().st_size < 100:
            return False
        with open(obs_path, encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line[60:].strip().startswith("END OF HEADER"):
                    return True
    except OSError:
        return False
    return False


def _find_complete_plain_obs(out_dir: Path) -> Optional[Path]:
    """Return a complete PLAIN obs in ``out_dir``, or None (evicting poison).

    The cache dir holds the canonical PLAIN obs (.rnx / .YYo). The engine also
    packages the published file (.crx.gz / .YYd.Z) into this same dir for reuse, so
    a hit must select the plain obs — never a compressed/Hatanaka packaged artifact
    (which :func:`detect_rinex_version` can't read). A truncated obs (pre-atomic-write
    poison or bit-rot) evicts the whole key dir and returns None so the caller
    re-renders rather than serving it forever.
    """
    if not out_dir.is_dir():
        return None
    existing = [
        p
        for p in out_dir.iterdir()
        if p.is_file()
        and not _strip_compression(p.name)[1]
        and not _is_hatanaka(p.name)
    ]
    if not existing:
        return None
    obs = existing[0]
    if _obs_complete(obs):
        return obs
    logger.warning(
        "convert cache POISONED (incomplete obs %s) — evicting %s and re-rendering",
        obs.name,
        out_dir.name,
    )
    shutil.rmtree(out_dir, ignore_errors=True)
    return None


def _packaged_valid(pkg_path: Path) -> bool:
    """Cheap validity check for a cached PACKAGED artifact (.crx.gz / .YYd.Z).

    Analogue of :func:`_obs_complete` for the reused published file: a size floor,
    and for gzip a CRC test (``gzip -t``, cheap — checks the trailer without a full
    decompress to disk). ``.Z`` (Unix compress) has no portable test flag, so it
    gets the size floor only; the primary defence there is the atomic write.
    """
    try:
        if pkg_path.stat().st_size < 100:
            return False
    except OSError:
        return False
    if pkg_path.name.lower().endswith(".gz"):
        try:
            return (
                subprocess.run(
                    ["gzip", "-t", str(pkg_path)],
                    capture_output=True,
                    timeout=60,
                ).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError):
            return False
    return True


def detect_interval(obs_path: Path) -> Optional[float]:
    """The observation sampling interval (seconds) from the ``INTERVAL`` header.

    Returns ``None`` when the (optional) INTERVAL record is absent — callers fall
    back to the configured ``sample`` or a logged default rather than guessing.
    """
    with open(obs_path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            label = line[60:].strip()
            if label.startswith("INTERVAL"):
                try:
                    return float(line[:10])
                except ValueError:
                    return None
            if label.startswith("END OF HEADER"):
                break
    return None


def _frequency_token(interval_seconds: float) -> str:
    """RINEX 3 long-name data-frequency token, e.g. 15 → ``15S``, 1 → ``01S``."""
    return f"{int(round(interval_seconds)):02d}S"


# Stations already warned (this process) about a missing INTERVAL, so a
# full-history backfill logs the fallback ONCE per station at WARNING instead of
# ~2×/date (old RINEX-2 obs have no INTERVAL record — thousands of identical lines).
_NO_INTERVAL_WARNED: set[str] = set()


def _resolve_data_frequency(plain_obs: Path, sample: Optional[int]) -> str:
    """Frequency token: the config ``sample`` override, else the file's INTERVAL.

    Never invents a rate silently — if neither is known we fall back to ``15S``
    (the legacy default) so the filename is at least deterministic. The fallback
    is flagged once per station at WARNING (further occurrences at DEBUG) so a
    20-year backfill of INTERVAL-less RINEX-2 doesn't drown the log.
    """
    if sample is not None:
        return _frequency_token(sample)
    interval = detect_interval(plain_obs)
    if interval is not None:
        return _frequency_token(interval)
    station = plain_obs.name[:4].upper()
    if station not in _NO_INTERVAL_WARNED:
        _NO_INTERVAL_WARNED.add(station)
        logger.warning(
            "no INTERVAL in %s and no configured sample — naming as 15S "
            "(further %s files logged at DEBUG)",
            plain_obs.name,
            station,
        )
    else:
        logger.debug("no INTERVAL in %s — naming as 15S", plain_obs.name)
    return "15S"


def _to_plain_obs(src: Path, tmpdir: Path) -> Path:
    """Decompress (.gz/.Z) and un-Hatanaka ``src`` into a plain obs in ``tmpdir``."""
    stripped, was_compressed = _strip_compression(src.name)
    if was_compressed:
        dec = tmpdir / stripped
        with open(dec, "wb") as fh:
            proc = subprocess.run(
                ["gzip", "-dc", str(src)], stdout=fh, stderr=subprocess.PIPE
            )
        if proc.returncode != 0:
            raise ConvertError(f"decompress {src.name}: {proc.stderr.decode().strip()}")
    else:
        dec = tmpdir / src.name
        if dec != src:
            shutil.copy2(src, dec)

    if _is_hatanaka(dec.name):
        crx2rnx = resolve_tool("CRX2RNX")
        _run([crx2rnx, "-f", str(dec)])
        obs = tmpdir / _crinex_to_obs_name(dec.name)
        if not obs.is_file():
            raise ConvertError(f"CRX2RNX produced no {obs.name}")
        return obs
    return dec


def _short_obs_name(station: str, observation_dt: datetime, country_code: str) -> str:
    namer = RinexNamer(station, RinexVersion.RINEX_2, country_code=country_code)
    return namer.generate_filename(
        observation_dt, convention=NamingConvention.SHORT, file_type="o"
    )


def _canonical_obs(
    plain_obs: Path,
    station: str,
    observation_dt: datetime,
    version: int,
    naming: str,
    country_code: str,
    tmpdir: Path,
    *,
    sample: Optional[int] = None,
    file_period: str = "01D",
    monument_number: str = "00",
) -> tuple[Path, str]:
    """Rename/normalise ``plain_obs`` to the policy's canonical obs name.

    R3 long → gfzrnx ``-vo {version}`` (version PRESERVED — Model B never up/down-
    converts) writing the long IGS name, with the data-frequency token derived from
    the obs (config ``sample`` override else the file's INTERVAL). R2 short → plain
    rename, no gfzrnx, so the R2 product is byte-for-byte the source obs.

    When ``sample`` is set the obs is decimated to that interval via ``gfzrnx -smp``
    (both versions) — this is the only case the R2 product is rewritten, and it is
    the dissemination-boundary 30s-product path, never the archive.
    """
    data_frequency = _resolve_data_frequency(plain_obs, sample)
    gfzrnx = resolve_tool("gfzrnx")

    if naming == "long":
        obs_name = long_rinex3_name(
            station,
            observation_dt,
            country_code=country_code,
            data_frequency=data_frequency,
            file_period=file_period,
            monument_number=monument_number,
        )
        out = tmpdir / obs_name
        cmd = [gfzrnx, "-finp", str(plain_obs), "-fout", str(out), "-vo", str(version)]
        if sample is not None:
            cmd += ["-smp", str(sample)]
        cmd.append("-f")
        _run(cmd)
        if not out.is_file():
            raise ConvertError(f"gfzrnx produced no {obs_name}")
        return out, obs_name

    obs_name = _short_obs_name(station, observation_dt, country_code)
    out = tmpdir / obs_name
    if sample is not None:
        # Decimate the R2 product too (the only case it is not byte-for-byte).
        _run(
            [
                gfzrnx,
                "-finp",
                str(plain_obs),
                "-fout",
                str(out),
                "-vo",
                str(version),
                "-smp",
                str(sample),
                "-f",
            ]
        )
        if not out.is_file():
            raise ConvertError(f"gfzrnx produced no {obs_name}")
        return out, obs_name
    if out != plain_obs:
        shutil.move(str(plain_obs), str(out))
    return out, obs_name


def convert_for_dissemination(
    source_path: Path,
    station: str,
    observation_dt: datetime,
    *,
    fmt,
    cache_dir: Path,
    tos_fingerprint: str = "",
    set_header: bool = False,
    domes: str = "",
    observer: str = "",
    agency: str = "",
    sample: Optional[int] = None,
    tos_metadata_cache: Optional[dict] = None,
) -> ConvertResult:
    """Produce the cached canonical plain obs for dissemination (Model B).

    Two-layer cache (the split is what makes a header re-push cheap):

    1. **Decode layer** (:func:`decode_cache_key`, content + sample only) — the
       expensive raw→canonical obs, *header-free*. Re-run only when the source
       bytes change.
    2. **Output layer** (:func:`cache_key`, content + TOS fingerprint + header
       schema) — the header-applied plain obs the engine packages/pushes.

    A TOS metadata change (moves the fingerprint) or a header-logic bump
    (:data:`HEADER_SCHEMA_VERSION`) misses the output layer but HITS the decode
    layer, so ``set_header``'s corrected header is re-applied by copying + re-heading
    the cached decode obs — with **no** sbf2rin/CRX2RNX/gfzrnx re-decode. That is the
    cheap ``epos-disseminate --force`` header re-push. The source's RINEX version is
    preserved; naming follows ``fmt`` (R2→short, R3→long). Packaging is applied later
    by :func:`package`. ``set_header`` rewrites the header from TOS + EPOS rules.
    """
    import tempfile

    source_path = Path(source_path)
    is_raw = any(e in source_path.name.lower() for e in _TRIMBLE_RAW + (".sbf",))

    # Per-call sample (a ProductSpec rate) wins; fall back to the format-level
    # knob for the single-product config shape.
    if sample is None:
        sample = getattr(fmt, "sample", None)

    cache_root = cache_dir.expanduser()
    key = cache_key(source_path, tos_fingerprint, sample=sample)
    out_dir = cache_root / key

    # Output-layer hit: header already applied under this exact fingerprint+schema.
    hit = _find_complete_plain_obs(out_dir)
    if hit:
        logger.info("convert cache hit %s → %s", source_path.name, hit.name)
        return ConvertResult(
            hit, hit.name, detect_rinex_version(hit), True, source_path
        )

    # Decode layer: the expensive header-free canonical obs (reused across TOS /
    # header-schema changes). This is the only step that may run sbf2rin / gfzrnx.
    canon_src, version = _decode_cached_canonical(
        source_path,
        station,
        observation_dt,
        fmt=fmt,
        cache_root=cache_root,
        sample=sample,
        is_raw=is_raw,
    )
    obs_name = canon_src.name

    out_dir.mkdir(parents=True, exist_ok=True)
    # Stage in a temp dir on the SAME filesystem as the cache (a sibling of out_dir,
    # invisible to the hit scanner) so the final placement is an atomic os.replace.
    # Header mutation happens on the temp copy BEFORE placement, so a kill mid-rewrite
    # dies in the temp dir and never leaves a partial in the cache — and the decode
    # obs is never touched.
    with tempfile.TemporaryDirectory(dir=cache_root, prefix=".epos_hdr_") as tmp:
        staged = Path(tmp) / obs_name
        shutil.copy2(canon_src, staged)

        if set_header:
            set_header_from_tos(
                staged, station, observation_dt, tos_metadata_cache=tos_metadata_cache
            )
            # EPOS-specific header finalization (4.1.7) that the general TOS corrector
            # does not do: 9-char MARKER NAME (R3), DOMES in MARKER NUMBER, generic
            # OBSERVER/AGENCY. Done on the temp obs so the placed product is complete.
            finalize_epos_header(
                staged,
                station,
                version,
                country_code=fmt.country_code,
                monument_number=getattr(fmt, "monument_number", "00"),
                domes=domes,
                # Per-station agency (resolved from TOS owner org) overrides the
                # format default; empty ⇒ fall back to the format's observer/agency.
                observer=observer or getattr(fmt, "observer", ""),
                agency=agency or getattr(fmt, "agency", ""),
            )

        final_obs = out_dir / obs_name
        os.replace(str(staged), str(final_obs))

    logger.info(
        "re-headed %s → %s (RINEX %d)", source_path.name, final_obs.name, version
    )
    return ConvertResult(final_obs, obs_name, version, False, source_path)


def _decode_cached_canonical(
    source_path: Path,
    station: str,
    observation_dt: datetime,
    *,
    fmt,
    cache_root: Path,
    sample: Optional[int],
    is_raw: bool,
) -> tuple[Path, int]:
    """Return ``(header-free canonical obs, version)`` from the decode cache.

    Keyed on :func:`decode_cache_key` (source content + sample) under
    ``cache_root/decode/<key>/`` — independent of TOS metadata and the header
    schema, so it survives header re-pushes. On a miss it runs the expensive
    decode (sbf2rin / CRX2RNX / gfzrnx) and places the result atomically. The
    returned obs carries NO header corrections; the caller applies those on a copy.
    """
    import tempfile

    dkey = decode_cache_key(source_path, sample=sample)
    decode_dir = cache_root / "decode" / dkey
    hit = _find_complete_plain_obs(decode_dir)
    if hit:
        logger.info("decode cache hit %s → %s", source_path.name, hit.name)
        return hit, detect_rinex_version(hit)

    decode_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_root, prefix=".epos_decode_") as tmp:
        tmpdir = Path(tmp)
        if is_raw:
            decoded = _decode_raw(source_path, station, observation_dt, tmpdir)
            plain = _to_plain_obs(decoded, tmpdir)
        else:
            plain = _to_plain_obs(source_path, tmpdir)
        version = detect_rinex_version(plain)
        naming = fmt.policy_for(version).naming
        canon, obs_name = _canonical_obs(
            plain,
            station,
            observation_dt,
            version,
            naming,
            fmt.country_code,
            tmpdir,
            sample=sample,
            file_period=getattr(fmt, "file_period", "01D"),
            monument_number=getattr(fmt, "monument_number", "00"),
        )
        final = decode_dir / obs_name
        os.replace(str(canon), str(final))

    logger.info("decoded %s → %s (RINEX %d)", source_path.name, final.name, version)
    return final, version


def epos_marker_name(
    station: str, version: int, country_code: str, monument_number: str = "00"
) -> str:
    """EPOS MARKER NAME: 9-char ID for RINEX 3 (``RHOF00ISL``), 4-char for R2.

    Per EPOS 4.1.7 — "the 9-character station ID (4-character for RINEX 2 data)
    must be found in the MARKER NAME field". ``monument_number`` and
    ``country_code`` come from config (will become per-station TOS attributes);
    the 9-char ID here MUST match the long filename's, so both read the same knobs.
    """
    sid = station.upper()
    mon = str(monument_number)[:2].rjust(2, "0")
    return f"{sid}{mon}{country_code.upper()}" if version >= 3 else sid


def _set_header_records(rinex_file: Path, records: dict[str, str | None]) -> None:
    """Update-or-insert fixed-column header records ``{label: value(cols 1-60)}``.

    RINEX header lines are ``value[1:60] + label[61:80]``. Existing records with a
    matching label are rewritten; any not present are inserted just before
    ``END OF HEADER`` (in dict order). A value of ``None`` means **strip**: an
    existing line with that label is removed and none is inserted (used for
    MARKER NUMBER on a station with no IERS DOMES). Idempotent.

    Uses latin-1 for the whole-file round-trip: it is byte-preserving (0-255 map
    bijectively to U+0000-00FF), so a stray non-ASCII byte in the OBSERVATION data
    survives untouched instead of being mangled by the platform default codec —
    which would otherwise corrupt a data line and make the downstream RNX2CRX
    Hatanaka packaging fail ("ERROR when reading line N"). Header edits are ASCII,
    a latin-1 subset, so they round-trip identically.
    """
    lines = rinex_file.read_text(encoding="latin-1").splitlines(keepends=True)
    out: list[str] = []
    applied: set[str] = set()
    for line in lines:
        label = line[60:80].strip() if len(line) >= 61 else ""
        if label == "END OF HEADER":
            for lbl, val in records.items():
                if lbl not in applied and val is not None:
                    out.append(f"{val:<60}{lbl:<20}\n")
                    applied.add(lbl)
            out.append(line)
            continue
        if label in records and label not in applied:
            val = records[label]
            applied.add(label)
            if val is None:  # strip: drop this line, insert nothing
                continue
            out.append(f"{val:<60}{label:<20}\n")
            continue
        out.append(line)
    rinex_file.write_text("".join(out), encoding="latin-1")


def finalize_epos_header(
    rinex_file: Path,
    station: str,
    version: int,
    *,
    country_code: str,
    monument_number: str = "00",
    domes: str = "",
    observer: str = "",
    agency: str = "",
) -> None:
    """Write the EPOS-mandated marker / observer header records (4.1.7).

    - MARKER NAME  ← 9-char ID (R3) / 4-char (R2)
    - MARKER NUMBER ← IERS DOMES only. A station with no DOMES (e.g. ELEY) gets
      **no** MARKER NUMBER line (the ``None`` = strip signal to
      :func:`_set_header_records`) — never the station id, which MARKER NAME
      already carries.
    - OBSERVER / AGENCY ← generic team name + agency (never personal initials)

    Best-effort like :func:`set_header_from_tos`; the QC gate is the safety net.
    """
    try:
        from tostools.rinex import domes_or_skip

        sid = station.upper()
        records: dict[str, str | None] = {
            "MARKER NAME": epos_marker_name(
                sid, version, country_code, monument_number
            ),
            # DOMES when real, else None → strip the line (no id fallback).
            "MARKER NUMBER": domes_or_skip(domes) or None,
        }
        if observer or agency:
            records["OBSERVER / AGENCY"] = f"{observer:<20}{agency:<40}"
        _set_header_records(rinex_file, records)
    except Exception as exc:  # noqa: BLE001 - never fail convert on a header write
        logger.warning("EPOS header finalize failed for %s: %s", station, exc)


def published_name(obs_name: str, policy) -> str:
    """The published filename for an obs name under a :class:`VersionPolicy`."""
    name = _obs_to_crinex_name(obs_name) if policy.hatanaka else obs_name
    # RINEX2 Hatanaka short names use uppercase ``.D`` on the IMO archive (the
    # fleet-wide .d->.D standardization) AND on the legacy EPOS portal
    # (RHOF0010.00D.Z). _obs_to_crinex_name follows the obs ``.YYo`` case, which
    # yields lowercase ``.YYd`` — force uppercase so a disseminated R2 short lines
    # up with the archive + supersede-by-name (a lowercase straggler is exactly
    # what slipped past supersede before). R3 Hatanaka is ``.crx`` (unaffected).
    if (
        policy.hatanaka
        and len(name) >= 4
        and name[-4] == "."
        and name[-3:-1].isdigit()  # 2-digit year
        and name[-1] == "d"
    ):
        name = name[:-1] + "D"
    ext = {"gz": ".gz", "Z": ".Z", "none": ""}.get(policy.compression, ".gz")
    return name + ext


def package(obs_path: Path, policy, out_dir: Path) -> Path:
    """Hatanaka-compress (optional) + compress an obs into the published file.

    Returns the published path in ``out_dir``, leaving the cached plain obs
    untouched. Idempotent: if the published file already exists it is returned
    as-is (so a cache-hit convert doesn't re-package). ``Z`` uses the system
    ``compress`` (available on rek_new). Works in an internal temp dir so it never
    clobbers the source obs even when ``out_dir`` is the obs's own directory.
    """
    import tempfile

    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / published_name(obs_path.name, policy)
    if final.is_file():
        if _packaged_valid(final):
            return final
        # A partial published artifact from an interrupted package (pre-atomic)
        # would be reused forever — evict and re-package.
        logger.warning("packaged cache artifact %s invalid — re-packaging", final.name)
        final.unlink(missing_ok=True)

    # Temp dir INSIDE out_dir (same filesystem, and a subdir is invisible to the
    # obs cache-hit scanner) so the final placement is an atomic os.replace, never
    # a cross-fs copy that a kill could truncate into a reusable partial.
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".epos_pkg_") as tmp:
        work = Path(tmp) / obs_path.name
        shutil.copy2(obs_path, work)

        if policy.hatanaka:
            rnx2crx = resolve_tool("RNX2CRX")
            _run([rnx2crx, "-f", str(work)])
            crx = work.parent / _obs_to_crinex_name(work.name)
            if not crx.is_file():
                raise ConvertError(f"RNX2CRX produced no {crx.name}")
            work = crx

        if policy.compression == "gz":
            _run(["gzip", "-f", str(work)])
            work = Path(str(work) + ".gz")
        elif policy.compression == "Z":
            compress = resolve_tool("compress")
            _run([compress, "-f", str(work)])
            work = Path(str(work) + ".Z")

        if not work.is_file():
            raise ConvertError(f"packaging produced no {work.name}")
        os.replace(str(work), str(final))
    return final


def set_header_from_tos(
    rinex_file: Path,
    station: str,
    observation_dt: datetime,
    tos_metadata_cache: Optional[dict] = None,
) -> bool:
    """Rewrite ``rinex_file``'s header from TOS metadata (in place). Best-effort.

    Delegates to ``tostools.rinex.correct_rinex_from_tos`` with ``station_config``
    unset, so TOS is the authority for every epoch (EPOS wants the canonical TOS
    metadata, and historical re-pushes must reflect TOS as of that date). Returns
    True if a correction was applied; False (logged) on any failure — the QC gate
    is the safety net that blocks a still-wrong header from being pushed.

    ``tos_metadata_cache`` (a caller-owned dict) memoizes the date-independent
    per-station TOS fetch: a range/backfill that corrects every day of a station
    makes 1 TOS call per station instead of 1 per file (and is far less exposed to
    a transient TOS/VPN blip). Pass the same dict across every date of a run.
    """
    try:
        from tostools.rinex import correct_rinex_from_tos

        result = correct_rinex_from_tos(
            rinex_file=rinex_file,
            station_id=station.upper(),
            observation_date=observation_dt,
            output_file=rinex_file,
            station_config=None,  # force TOS (canonical for EPOS), not station.cfg
            loglevel=logging.WARNING,
            tos_metadata_cache=tos_metadata_cache,
        )
        return result is not None
    except Exception as exc:  # noqa: BLE001 - never fail the convert on a TOS glitch
        logger.warning("set-header-from-TOS failed for %s: %s", station, exc)
        return False


def _decode_raw(
    raw_path: Path, station: str, observation_dt: datetime, out_dir: Path
) -> Path:
    """Decode an archived raw file to RINEX, dispatching by receiver type.

    Dispatch is by CONTENT (magic bytes) with extension as fallback — the
    archive holds mislabeled files (e.g. KOSK ``.atc`` that is PolaRx2 SBF),
    and the wrong decoder either dies or silently emits nothing. Septentrio
    SBF → sbf2rin; Trimble ``.T02``/``.T00`` → the Trimble native converter;
    a magic↔extension disagreement follows the magic, loudly. Ashtech U/R
    files classify cleanly but have no dissemination decoder yet (teqc chain
    — vault todo #56), so they fail loud instead of hitting the wrong tool.
    """
    from ..archive.raw_format import SBF, TRIMBLE, UNKNOWN, classify_raw

    fmt = classify_raw(raw_path)
    ext_says_sbf = ".sbf" in raw_path.name.lower()
    if fmt == SBF or (fmt == UNKNOWN and ext_says_sbf):
        if fmt == SBF and not ext_says_sbf:
            logger.warning(
                "%s: extension says non-SBF but content IS SBF — decoding as SBF",
                raw_path.name,
            )
        return _decode_sbf_raw(raw_path, station, observation_dt, out_dir)
    if fmt not in (TRIMBLE, UNKNOWN):
        raise ConversionError(
            f"{raw_path.name}: raw format '{fmt}' has no dissemination decoder yet"
        )
    if ext_says_sbf:
        logger.warning(
            "%s: extension says SBF but content is not — trying Trimble decoder",
            raw_path.name,
        )
    return _decode_trimble_raw(raw_path, station, observation_dt, out_dir)


def _decode_trimble_raw(
    raw_path: Path, station: str, observation_dt: datetime, out_dir: Path
) -> Path:
    """Decode a Trimble .T02/.T00 to a RINEX file in ``out_dir`` and return it.

    Prefers the production native converter (official Trimble Convert-to-RINEX via
    the ``trm2rinex`` Docker image — the path the live download pipeline uses);
    falls back to the legacy runpkr00+teqc converter when Docker is unavailable.
    No header corrections (the QC gate verifies; set-header is a later ticket).
    """
    from ..rinex.trimble_native_converter import TrimbleNativeConverter

    if TrimbleNativeConverter.is_available():
        conv: Any = TrimbleNativeConverter(
            station,
            rinex_version=RinexVersion.RINEX_3,
            apply_header_corrections=False,
            loglevel=logging.WARNING,
        )
    else:
        # Legacy fallback. NOTE: runpkr00+teqc does NOT handle some T02/T00 files
        # (returns rc=30 / no output) — the native Docker converter is the
        # production path. This branch exists only for Docker-less environments.
        from ..rinex.trimble_converter import TrimbleConverter

        logger.warning(
            "trm2rinex Docker image unavailable — falling back to legacy "
            "runpkr00+teqc, which fails on some Trimble files"
        )
        conv = TrimbleConverter(
            station,
            rinex_version=RinexVersion.RINEX_2,
            apply_header_corrections=False,
            loglevel=logging.WARNING,
        )

    result = conv.convert_file(
        raw_path, output_dir=out_dir, observation_date=observation_dt
    )
    if not result.success or not result.rinex_file:
        raise ConvertError(
            f"Trimble decode failed for {raw_path.name}: {result.message}"
        )
    return Path(result.rinex_file)


def _decode_sbf_raw(
    raw_path: Path, station: str, observation_dt: datetime, out_dir: Path
) -> Path:
    """Decode a Septentrio ``.sbf``/``.sbf.gz`` to a RINEX 3 file in ``out_dir``.

    Uses ``sbf2rin`` (RxTools) via :class:`receivers.rinex.sbf_converter.SBFConverter`
    — Septentrio is the dominant receiver type in the fleet, so the raw fallback
    must cover it (the Trimble decoder rejects ``.sbf``). Emits a plain RINEX obs
    (no Hatanaka); ``_to_plain_obs`` normalises it. No header corrections here —
    the QC gate verifies and set-header runs downstream.
    """
    from ..rinex.sbf_converter import SBFConverter

    conv = SBFConverter(
        station,
        rinex_version=RinexVersion.RINEX_3,
        apply_header_corrections=False,
        apply_hatanaka=False,
        loglevel=logging.WARNING,
    )
    result = conv.convert_file(
        raw_path, output_dir=out_dir, observation_date=observation_dt
    )
    if not result.success or not result.rinex_file:
        raise ConvertError(f"SBF decode failed for {raw_path.name}: {result.message}")
    return Path(result.rinex_file)
