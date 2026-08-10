"""Path-validation tests for ``receivers archive-rm``.

The validator is the only thing standing between a typo and an rm on the
long-term archive, so it is deliberately strict. These tests pin both halves of
that contract: the layouts it must accept, and the ones it must keep refusing.

Regression guarded here (found 2026-08-10): ``rinex_bak`` was listed only as a
_SIDECAR — i.e. as a child of the category dir — but ``--backup-old`` creates it
as a SIBLING of ``rinex/``. Every real backup path was therefore refused as
"invalid/unsafe", and since ``--del-backup`` only walks ``data_prepath`` and
``/mnt/rawgpsdata`` is read-only on rek-d01, there was no working way to reclaim
them at all. ~265 GB had piled up across ELDC and ISAK before anyone noticed.
"""

import pytest

from receivers.archive.remove import _FORBIDDEN, _RELPATH_RE

# --- layouts that MUST be accepted ----------------------------------------

ACCEPT = [
    # the plain product
    "2012/aug/RHOF/15s_24hr/rinex/RHOF2430.12D.Z",
    "2026/aug/ELDC/1Hz_1hr/raw/ELDC202608091900b.sbf.gz",
    # backup dir as a SIBLING of the category — the actual --backup-old layout
    "2012/aug/RHOF/15s_24hr/rinex_bak/RHOF2430.12D.Z",
    "2020/apr/ELDC/15s_24hr/rinex_bak/ELDC0920.20D.Z",
    # ...including the .N generations that stack when it is re-created
    "2020/apr/ELDC/15s_24hr/rinex_bak/ELDC0920.20D.Z.17",
    # genuinely nested sidecars stay valid
    "2026/jul/ISAK/15s_24hr/rinex/superseded_rt_20260705/ISAK1860.26D.Z",
    "2026/jul/ISAK/15s_24hr/rinex_archive/fix-headers_20260705/ISAK1860.26D.Z",
    # preservation copies
    "2019/jan/KOSK/15s_24hr/rinex_org/KOSK0010.19D.Z",
]

# --- layouts that MUST still be refused ------------------------------------

REFUSE = [
    # arbitrary subdirectory is not a known sidecar
    "2012/aug/RHOF/15s_24hr/rinex/whatever/RHOF2430.12D.Z",
    # unknown category
    "2012/aug/RHOF/15s_24hr/scratch/RHOF2430.12D.Z",
    # traversal
    "2012/aug/RHOF/15s_24hr/rinex/../../../etc/passwd",
    "../2012/aug/RHOF/15s_24hr/rinex/RHOF2430.12D.Z",
    # absolute path
    "/mnt/rawgpsdata/2012/aug/RHOF/15s_24hr/rinex/RHOF2430.12D.Z",
    # malformed date parts
    "12/aug/RHOF/15s_24hr/rinex/RHOF2430.12D.Z",
    "2012/august/RHOF/15s_24hr/rinex/RHOF2430.12D.Z",
    # a directory, not a file
    "2012/aug/RHOF/15s_24hr/rinex_bak/",
    # two sidecar levels
    "2012/aug/RHOF/15s_24hr/rinex/rinex_bak/superseded_rt_20260705/X.Z",
]


@pytest.mark.parametrize("path", ACCEPT)
def test_accepts_real_archive_layouts(path):
    assert _RELPATH_RE.match(path), f"should be accepted: {path}"


@pytest.mark.parametrize("path", REFUSE)
def test_refuses_unsafe_or_unknown_layouts(path):
    assert not _RELPATH_RE.match(path), f"should be REFUSED: {path}"


def test_backup_sibling_layout_is_the_one_that_regressed():
    """The exact path archive-rm refused on 2026-08-10."""
    assert _RELPATH_RE.match("2012/aug/RHOF/15s_24hr/rinex_bak/RHOF2430.12D.Z")


def test_shell_metacharacters_are_forbidden_independently():
    """Defense in depth: the regex excludes them, and so does _FORBIDDEN."""
    for ch in "*?[]{}~ ;|&$`'\"<>()!":
        assert ch in _FORBIDDEN, f"{ch!r} should be forbidden"
