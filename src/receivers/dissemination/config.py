"""Dissemination target config — the ``tier: dissemination`` rows of sync.yaml.

Reuses the same ``sync.yaml`` file and parsing helpers as
:mod:`receivers.archive.config`, but a dissemination target carries a declarative
:class:`DisseminationFormat` (the source of truth for naming / compression /
layout — Model B per-version policy + dest templates) that an archive (as-is
rsync) target does not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..archive.config import _default_config_path, _parse_cutover

try:
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover - yaml is a hard dep in practice
    yaml = None  # type: ignore[assignment]
    HAS_YAML = False

logger = logging.getLogger(__name__)

DEFAULT_COUNTRY_CODE = "ISL"
DEFAULT_CACHE_DIR = "~/.cache/gps_receivers/epos_convert"
# Mirror the legacy EPOS archive tree by default (gtimes datepathlist tokens +
# {station}; %Y=year, #b=lowercase 3-letter month).
DEFAULT_DIR_TEMPLATE = "%Y/#b/{station}/15s_24hr/rinex/"
DEFAULT_FILENAME_TEMPLATE = "{name}"


@dataclass(frozen=True)
class VersionPolicy:
    """Per-RINEX-version naming + packaging policy for a dissemination target."""

    naming: str  # 'short' (RINEX2 SSSS0DDF.YYt) or 'long' (RINEX3 IGS)
    hatanaka: bool  # True → CRINEX (.crx / R2 .YYd); False → obs (.rnx / R2 .YYo)
    compression: str  # 'gz' | 'Z' | 'none'

    @staticmethod
    def from_dict(raw: dict, *, default_naming: str, default_compression: str):
        raw = raw or {}
        return VersionPolicy(
            naming=raw.get("naming", default_naming),
            hatanaka=bool(raw.get("hatanaka", True)),
            compression=raw.get("compression", default_compression),
        )


@dataclass(frozen=True)
class ProductSpec:
    """One published product per observation date (a ``format.products:`` entry).

    ``sample=None`` ships the source rate unchanged (the native product —
    the only one that may supersede a legacy short-name file on the portal);
    ``sample=30`` decimates to 30 s at the dissemination boundary. Each
    product gets its own convert-cache slot and its own ``{session}``
    directory segment (``<rate>s_24hr``).
    """

    sample: Optional[int] = None
    enabled: bool = True

    @staticmethod
    def from_dict(raw: dict) -> ProductSpec:
        raw = raw or {}
        sample_raw = raw.get("sample")
        return ProductSpec(
            sample=int(sample_raw) if sample_raw is not None else None,
            enabled=bool(raw.get("enabled", True)),
        )


@dataclass(frozen=True)
class DisseminationFormat:
    """The declarative format policy for a dissemination target (from sync.yaml).

    Model B: the source's RINEX version is preserved (never R2↔R3 converted); each
    version gets its own naming + packaging (``rinex2``/``rinex3``). The dest layout
    is two gtimes-templated strings.
    """

    preserve_source_version: bool = True
    country_code: str = DEFAULT_COUNTRY_CODE
    set_header_from_tos: bool = True
    rinex2: VersionPolicy = field(
        default_factory=lambda: VersionPolicy("short", True, "Z")
    )
    rinex3: VersionPolicy = field(
        default_factory=lambda: VersionPolicy("long", True, "gz")
    )
    dir_template: str = DEFAULT_DIR_TEMPLATE
    filename_template: str = DEFAULT_FILENAME_TEMPLATE

    sample: Optional[int] = None
    """Decimate the disseminated obs to this sampling interval (seconds) before
    packaging — e.g. ``30`` for the conventional EPOS 30s daily product. ``None``
    (default) ships the source rate unchanged. Dissemination-boundary only; the
    archive is never touched. The RINEX 3 long-name frequency token follows this
    when set, otherwise it is derived from the file's actual ``INTERVAL``."""

    file_period: str = "01D"
    """RINEX 3 long-name file-period token (``01D`` daily). Config knob so the
    naming is not a hardcoded assumption in the convert code."""

    monument_number: str = "00"
    """2-digit monument number in the 9-char station ID (``RHOF``**00**``ISL``),
    used for both the long filename and the MARKER NAME header. In config for now;
    becomes a per-station TOS attribute later (do NOT hardcode it in the code)."""

    observer: str = "GNSSatIMO"
    """OBSERVER value written into disseminated headers (EPOS 4.1.7 requires a
    generic team name / email, not personal initials). ``@``→``at`` already
    applied by convention."""

    agency: str = "Icelandic Meteorological Office"
    """AGENCY value written into disseminated headers (paired with OBSERVER)."""

    products: tuple = ()
    """Optional multi-product list (``format.products:``): one published product
    per entry per date (e.g. native 15 s + decimated 30 s). Empty = single
    product at the top-level ``sample`` (back-compat). When ``products`` is
    given, the top-level ``sample`` is ignored (a warning is logged at parse)."""

    def policy_for(self, rinex_version: int) -> VersionPolicy:
        return self.rinex2 if rinex_version == 2 else self.rinex3

    def active_products(self) -> list[ProductSpec]:
        """The products to publish per date, in config order; never empty."""
        prods = [p for p in self.products if p.enabled]
        return prods or [ProductSpec(sample=self.sample)]

    @staticmethod
    def from_dict(raw: dict) -> DisseminationFormat:
        raw = raw or {}
        sample_raw = raw.get("sample")
        products_raw = raw.get("products") or []
        if products_raw and sample_raw is not None:
            logger.warning(
                "format: both 'products:' and top-level 'sample:' set — "
                "'sample' is ignored (products define the per-product rates)"
            )
            sample_raw = None
        return DisseminationFormat(
            preserve_source_version=bool(raw.get("preserve_source_version", True)),
            country_code=raw.get("country_code", DEFAULT_COUNTRY_CODE),
            set_header_from_tos=bool(raw.get("set_header_from_tos", True)),
            rinex2=VersionPolicy.from_dict(
                raw.get("rinex2", {}), default_naming="short", default_compression="Z"
            ),
            rinex3=VersionPolicy.from_dict(
                raw.get("rinex3", {}), default_naming="long", default_compression="gz"
            ),
            dir_template=raw.get("dir_template", DEFAULT_DIR_TEMPLATE),
            filename_template=raw.get("filename_template", DEFAULT_FILENAME_TEMPLATE),
            sample=int(sample_raw) if sample_raw is not None else None,
            file_period=raw.get("file_period", "01D"),
            monument_number=str(raw.get("monument_number", "00")),
            observer=raw.get("observer", "GNSSatIMO"),
            agency=raw.get("agency", "Icelandic Meteorological Office"),
            products=tuple(ProductSpec.from_dict(p) for p in products_raw),
        )


@dataclass(frozen=True)
class DisseminationTarget:
    """One ``tier: dissemination`` destination (e.g. the EPOS files server).

    Mirrors the shape of :class:`receivers.archive.config.SyncTarget` for the
    transport fields, and carries the declarative :class:`DisseminationFormat`.
    """

    name: str
    active: bool
    """Master on/off switch (the ``active: true/false`` gate)."""

    host: str
    user: str
    dest: str
    """Destination base path on the remote (bare local path when ``host`` empty)."""

    source_root: str
    """Local archive root to read RINEX/raw from (e.g. '/mnt/data/gpsdata').

    sync.yaml may give a LIST of candidates — the first existing directory
    wins, so ONE shared config serves hosts with different mounts (rek-d01
    reads its live /mnt/data/gpsdata, the laptop its /mnt_data/rawgpsdata
    NFS view). See _resolve_source_root."""

    sessions: tuple[str, ...]
    exclude_stations: frozenset[str]
    stations: frozenset[str] = frozenset()
    """Optional rollout allowlist (upper-cased). When non-empty, the automated
    sweep disseminates ONLY these markers (intersected with the in_epos set and
    minus ``exclude_stations``). Empty or absent = all in_epos stations — lets you
    onboard EPOS one station at a time. The explicit ``--station`` path ignores it."""
    format: DisseminationFormat = field(default_factory=DisseminationFormat)
    convert_cache_dir: str = DEFAULT_CACHE_DIR
    #: Drop each product's convert-cache entries once the push is DURABLE on the
    #: portal. Default ON: a full-history run is ~120 GB of intermediates for a
    #: single station (measured on ELDC 2020-2026, 4,441 products), and an
    #: unreaped cache has filled /mnt/data before and taken Postgres down with it.
    #: Set false to keep the cache when a cheap `--force` header re-push is
    #: planned — that is the only thing it buys.
    prune_cache: bool = True
    """Where converted outputs are cached, keyed on
    ``hash(source content_sha256 + TOS-metadata fingerprint)``."""

    cutover: Optional[datetime] = None
    """Optional watermark floor (unused by the single-file path)."""

    tier: str = "dissemination"

    @property
    def remote(self) -> str:
        """rsync destination: ``user@host:dest`` (or bare ``dest`` when local)."""
        if not self.host:
            return self.dest
        return f"{self.user}@{self.host}:{self.dest}"

    def select_markers(self, markers) -> list[str]:
        """Filter a discovered in_epos marker list by the rollout allowlist and
        exclude list (both case-insensitive, order preserved). An empty allowlist
        means "all" — so this only ever narrows when ``stations`` is set. Applied
        to the automated sweep only; the explicit ``--station`` path bypasses it.
        """
        out = []
        for m in markers:
            u = str(m).upper()
            if self.stations and u not in self.stations:
                continue
            if u in self.exclude_stations:
                continue
            out.append(m)
        return out

    @property
    def cache_path(self) -> Path:
        return Path(self.convert_cache_dir).expanduser()


def _resolve_source_root(value, name: str) -> str:
    """First existing directory from a str-or-list ``source_root``.

    Found live on rek-d01 (2026-07-07): the shared sync.yaml said
    /mnt_data/rawgpsdata (the laptop's NFS view), which does not exist there
    — every dissemination date was silently 'skipped'. A candidate list makes
    the same config correct per host; when nothing exists we keep the first
    entry and warn loudly (the sweep will skip, but the log says WHY).
    """
    candidates = [value] if isinstance(value, str) else list(value)
    for c in candidates:
        if Path(c).expanduser().is_dir():
            return str(c)
    logger.warning(
        "dissemination target %s: NO source_root candidate exists on this "
        "host (%s) — dates will be skipped as no-data",
        name,
        ", ".join(map(str, candidates)),
    )
    return str(candidates[0])


def _build_target(raw: dict) -> DisseminationTarget:
    name = raw["name"]
    cutover = None
    if raw.get("cutover") is not None:
        cutover = _parse_cutover(raw["cutover"], name)
    return DisseminationTarget(
        name=name,
        active=bool(raw.get("active", False)),
        host=raw.get("host", ""),
        user=raw.get("user", ""),
        dest=raw["dest"],
        source_root=_resolve_source_root(raw["source_root"], name),
        sessions=tuple(raw.get("sessions", ())),
        exclude_stations=frozenset(s.upper() for s in raw.get("exclude_stations", ())),
        stations=frozenset(s.upper() for s in raw.get("stations", ())),
        format=DisseminationFormat.from_dict(raw.get("format", {})),
        convert_cache_dir=raw.get("convert_cache_dir", DEFAULT_CACHE_DIR),
        prune_cache=bool(raw.get("prune_cache", True)),
        cutover=cutover,
    )


def load_dissemination_config(
    config_path: Path | None = None,
) -> list[DisseminationTarget]:
    """Load all ``tier: dissemination`` targets from ``sync.yaml``.

    Returns an empty list when the file is absent. Archive targets (the default
    ``tier: archive``) are ignored here — they belong to
    :func:`receivers.archive.config.load_sync_config`.
    """
    path = config_path or _default_config_path()
    if not path.is_file():
        logger.info("No sync config at %s — dissemination disabled", path)
        return []
    if not HAS_YAML:
        raise RuntimeError("PyYAML is required to read sync.yaml")
    assert yaml is not None
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    return [
        _build_target(t)
        for t in doc.get("targets", [])
        if t.get("tier") == "dissemination"
    ]
