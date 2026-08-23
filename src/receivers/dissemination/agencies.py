"""Agency reference data — re-exported from tostools.

The implementation moved to :mod:`tostools.core.agencies` on 2026-08-23.

It had to: both site-log callers need it, and only one could reach it here.
``tosGPS sitelog`` could not import from receivers (receivers depends on
tostools, not the reverse), so it silently fell back to the renderer's legacy
TOS-contact path and produced a **different** §11/§12/§13 from the block
actually published to M3G — a single-line mailing address instead of the IGS
continuation lines, among others. Duplicating the resolver into tostools would
have created a second implementation to drift; moving it and re-exporting here
keeps one.

This module is kept as a shim so existing imports
(``from .agencies import AgencyResolver``, and the tests that import
``receivers.dissemination.agencies``) continue to work unchanged. Prefer
importing from ``tostools.core.agencies`` in new code.
"""

from __future__ import annotations

from tostools.core.agencies import (  # noqa: F401 — re-export
    AgencyInfo,
    AgencyResolver,
    agency_dict,
    default_agencies_path,
    resolve_sitelog_agencies,
    station_role_orgs,
)

__all__ = [
    "AgencyInfo",
    "AgencyResolver",
    "agency_dict",
    "default_agencies_path",
    "resolve_sitelog_agencies",
    "station_role_orgs",
]
