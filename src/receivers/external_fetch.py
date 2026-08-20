"""External data download — fetch files from third-party servers.

Replaces the legacy ``get*.sh`` cron scripts (rek2) with a general,
gtimes-templated fetcher inside ``receivers download``.

A station marked ``operational_status = external`` in stations.cfg carries::

    external_url_template = ftp://host/some/layout/%Y/%j/{station}%j0.%yO
    external_frequency   = 1D
    external_username    = anonymous      # optional
    external_password    =                # optional

The whole remote path is ONE template string — every ``strftime`` code plus
the GPS extensions ``#b`` / ``#Rin2`` / ``#hourl`` / ``#gpsw`` that
:func:`gtimes.timefunc.datepathlist` understands, and a ``{station}`` (or
``{station_lower}``) placeholder. Providers with different layouts (station
id in the directory vs filename, RINEX session letters, etc.) work from the
same code path.

See ``receivers/docs/architecture/external-download.md`` for the design.
"""

from __future__ import annotations

import ftplib
import logging
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: stations.cfg keys describing an external data source.
EXTERNAL_CONFIG_KEYS = (
    "external_url_template",
    "external_frequency",
    "external_username",
    "external_password",
    "external_data_type",
    "external_file_format",  # optional override: 'auto' (default) | crx_lzw | crx_gz | rnx3_plain | rnx2
)

#: Config-safe aliases for gtimes' ``#`` patterns. stations.cfg treats ``#``
#: as an inline comment (gps_parser strips it), so templates must not contain
#: literal ``#gpsw`` / ``#Rin2`` / … — use the ``{...}`` alias in the config
#: and this map restores the gtimes token before templating.
_GTIMES_ALIASES = {
    "{gpsw}": "#gpsw",
    "{rin2}": "#Rin2",
    "{rin3}": "#Rin3",
    "{hourl}": "#hourl",
    "{8hrin2}": "#8hRin2",
    "{b}": "#b",
}


def external_station_config(config: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Extract the external-source config from a station config dict.

    Returns ``None`` when the station has no ``external_url_template`` (i.e.
    it is not fetched externally). Values are strings (raw stations.cfg).
    """
    template = (config.get("external_url_template") or "").strip()
    if not template:
        return None
    return {
        "url_template": template,
        "frequency": (config.get("external_frequency") or "1D").strip(),
        "username": (config.get("external_username") or "").strip() or None,
        "password": config.get("external_password") or None,
        "data_type": (config.get("external_data_type") or "rinex").strip(),
        "file_format": (config.get("external_file_format") or "auto").strip(),
    }


def build_external_urls(
    station_id: str,
    template: str,
    frequency: str,
    start: datetime,
    end: Optional[datetime] = None,
) -> List[str]:
    """Expand an external URL template over a time range into remote URLs.

    Substitutes ``{station}`` / ``{station_lower}``, then delegates the date
    templating to :func:`gtimes.timefunc.datepathlist` (which understands
    ``%Y``/``%j``/``%m``/``%d``/``#b``/``#Rin2``/``#hourl``/``#gpsw`` …).
    Returns the list in chronological order.
    """
    import gtimes.timefunc as gt

    template = template.replace("{station_lower}", station_id.lower())
    template = template.replace("{station}", station_id.upper())
    for alias, pattern in _GTIMES_ALIASES.items():
        template = template.replace(alias, pattern)
    # gtimes defaults a missing endtime to an *aware* UTC now(), which breaks
    # the naive-vs-aware comparison against a naive starttime — default to
    # start (single-entry) instead, matching the docstring's "same as start"
    # semantics.
    end = end or start
    return gt.datepathlist(template, frequency, starttime=start, endtime=end)


def _fetch_one(
    url: str, dest: Path, *, username: Optional[str], password: Optional[str]
) -> Path:
    """Fetch a single URL into ``dest`` (filename derived from the URL path).

    Downloads to a ``.part`` temp file and atomically renames it into place on
    success, so a failed/partial download never leaves a 0-byte (or truncated)
    file at the final path — downstream archive tooling must not mistake junk
    for real data. Supports ``ftp://`` (ftplib) and ``http://``/``https://``
    (urllib). Returns the local path written.
    """
    parsed = urllib.parse.urlparse(url)
    filename = Path(urllib.parse.unquote(parsed.path)).name or "download"
    dest = dest / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    try:
        if parsed.scheme in ("http", "https"):
            # urllib handles http(s) transparently; no auth needed for anonymous
            # internal sources. (Credentials can be added later if a provider
            # requires basic auth.)
            urllib.request.urlretrieve(url, str(tmp))
        elif parsed.scheme == "ftp":
            host = parsed.hostname or ""
            port = parsed.port or 21
            ftp = ftplib.FTP()
            ftp.connect(host, port, timeout=30)
            ftp.login(username or "anonymous", password or "")
            try:
                remote_dir = str(Path(parsed.path).parent) or "/"
                if remote_dir != "/":
                    ftp.cwd(remote_dir)
                with open(tmp, "wb") as fh:
                    ftp.retrbinary(f"RETR {filename}", fh.write)
            finally:
                ftp.quit()
        else:
            raise ValueError(
                f"unsupported external URL scheme {parsed.scheme!r}: {url}"
            )
    except Exception:
        # Remove the partial temp file so a failed fetch leaves no junk.
        tmp.unlink(missing_ok=True)
        raise

    tmp.replace(dest)
    return dest


# ---------------------------------------------------------------------------
# Format detection + normalization (archive contract: Hatanaka RINEX + LZW)
# ---------------------------------------------------------------------------

_MAGIC_LZW = b"\x1f\x9d"  # Unix compress (.Z)
_MAGIC_GZIP = b"\x1f\x8b"  # gzip


def _decompressed_head(path: Path, nbytes: int = 512) -> bytes:
    """First ``nbytes`` of a file, transparently decompressing LZW/gzip."""
    magic = path.read_bytes()[:2]
    if magic in (_MAGIC_LZW, _MAGIC_GZIP):
        # GNU gzip -dc decompresses both gzip and Unix-compress (.Z)
        proc = subprocess.run(
            ["gzip", "-dc", str(path)], capture_output=True, timeout=60
        )
        if proc.returncode != 0:
            raise ValueError(
                f"cannot decompress {path.name}: "
                f"{proc.stderr.decode(errors='replace')[:200]}"
            )
        return proc.stdout[:nbytes]
    return path.read_bytes()[:nbytes]


def detect_file_format(path: Path) -> Dict[str, Any]:
    """Sniff a downloaded file's format from magic bytes + decompressed header.

    Returns ``{compression, compact, version}``:
      * ``compression`` — ``lzw`` / ``gzip`` / ``none``
      * ``compact``    — True when the file is Hatanaka (CRINEX)
      * ``version``    — RINEX major version ``'2'`` / ``'3'`` or None
    """
    magic = path.read_bytes()[:2]
    compression = (
        "lzw" if magic == _MAGIC_LZW else "gzip" if magic == _MAGIC_GZIP else "none"
    )
    head = _decompressed_head(path).decode("latin1", errors="replace")
    compact = "COMPACT RINEX" in head
    version = None
    for line in head.splitlines():
        if "RINEX VERSION" in line:
            m = re.search(r"(\d)\.\d+", line)
            if m:
                version = m.group(1)
            break
    return {"compression": compression, "compact": compact, "version": version}


def standard_archive_name(station_id: str, dt: datetime, frequency: str) -> str:
    """The archive-standard filename ``{station}{doy}{session}.{yy}D.Z``."""
    import gtimes.timefunc as gt

    return gt.datepathlist(
        f"{station_id.upper()}#Rin2D.Z", frequency, starttime=dt, endtime=dt
    )[0]


def normalize_external_file(
    path: Path, station_id: str, dt: datetime, dest_dir: Path, frequency: str
) -> Path:
    """Validate a downloaded external file and rename it to the archive standard.

    The archive contract is Hatanaka (CRINEX) RINEX 2/3 + Unix-compress
    (LZW), named ``{station}{doy}{session}.{yy}D.Z`` — exactly what the
    receivers converter produces. A source that already serves that format
    (e.g. LMI's ``.e``) is renamed + validated; any other format is REFUSED
    (never stored as-is) until a conversion step is wired.

    RINEX version is preserved (no R2→R3 conversion — lossy/ambiguous).
    """
    fmt = detect_file_format(path)
    ok = fmt["compression"] == "lzw" and fmt["compact"] and fmt["version"] in ("2", "3")
    if not ok:
        raise ValueError(
            f"{path.name}: unsupported external format {fmt} — expected "
            "Hatanaka RINEX 2/3 + Unix-compress (LZW); refusing to archive"
        )
    target = dest_dir / standard_archive_name(station_id, dt, frequency)
    path.replace(target)
    return target


def _datetimes(frequency: str, start: datetime, end: datetime) -> List[datetime]:
    """The per-file datetimes covered by ``[start, end]`` at ``frequency``."""
    import gtimes.timefunc as gt

    out = gt.datepathlist("#datelist", frequency, starttime=start, endtime=end)
    return list(out) if isinstance(out, (list, tuple)) else [out]


def fetch_external_station(
    station_id: str,
    config: Dict[str, Any],
    start: datetime,
    end: Optional[datetime],
    dest_dir: Path,
) -> List[Path]:
    """Download one external station's files over ``[start, end]`` into ``dest_dir``.

    Returns the list of local files written. A station without
    ``external_url_template`` yields an empty list (not an error).
    """
    ext = external_station_config(config)
    if ext is None:
        return []

    dts = _datetimes(ext["frequency"], start, end)
    logger.info("external %s: %d file(s) to fetch", station_id, len(dts))

    downloaded: List[Path] = []
    for dt in dts:
        url = build_external_urls(
            station_id, ext["url_template"], ext["frequency"], dt, dt
        )[0]
        try:
            tmp = _fetch_one(
                url, dest_dir, username=ext["username"], password=ext["password"]
            )
            final = normalize_external_file(
                tmp, station_id, dt, dest_dir, ext["frequency"]
            )
            downloaded.append(final)
            logger.info("external %s: fetched %s", station_id, final.name)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — one bad file must not abort the station
            logger.warning("external %s: failed %s: %s", station_id, url, exc)
    return downloaded


def external_stations(station_configs: Dict[str, Dict[str, Any]]) -> Sequence[str]:
    """Station ids (sorted) that carry an external URL template."""
    return sorted(
        sid
        for sid, cfg in station_configs.items()
        if external_station_config(cfg) is not None
    )


def raw_station_config(station_id: str) -> Optional[Dict[str, Any]]:
    """Raw stations.cfg section for one station, via gps_parser.

    Unlike receivers' :func:`~receivers.config_utils.get_station_config`,
    this does NOT require ``router_ip``/``receiver_type`` — external (and
    passive) stations legitimately lack those, so the typed accessor returns
    None for them. Returns the raw section dict (all stations.cfg keys,
    including ``external_url_template``), or None when the section is absent.
    """
    import gps_parser

    try:
        info = gps_parser.ConfigParser().getStationInfo(station_id)
    except Exception:  # noqa: BLE001 — treat as "no section"
        return None
    return (info or {}).get("station") or None


def external_station_configs() -> Dict[str, Dict[str, Any]]:
    """Map station_id → raw section for every station with an external template.

    Raw read (``interpolation=None``) so the ``%Y``/``%j``/``#gpsw`` tokens in
    ``external_url_template`` survive — default configparser interpolation
    would raise on the bare ``%``.
    """
    import configparser

    import gps_parser

    cp = configparser.ConfigParser(interpolation=None)
    cp.read(gps_parser.ConfigParser().get_stations_config_path())
    out: Dict[str, Dict[str, Any]] = {}
    for section in cp.sections():
        raw = dict(cp.items(section))
        if external_station_config(raw) is not None:
            out[section] = raw
    return out
