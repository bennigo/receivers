"""Guarded deletion of files from the long-term archive (rawdata gateway).

Deleting from the production archive is irreversible, so this is built to make a
*wrong* invocation safe rather than to trust the caller:

  * **Enumerated explicit paths only** — no globs, no directories, no recursion.
    Each path must match the strict archive layout (:func:`validate_archive_relpath`);
    absolute paths, ``..`` and shell metacharacters are rejected up front.
  * **Argv boundary (the spine):** paths are handed to the remote shell as ARGV
    (``ssh host bash -s -- "$@"`` with a QUOTED heredoc), never interpolated into
    a command string, so a filename can never be parsed as code. ``rm --`` blocks
    option-injection (a name starting with ``-``).
  * **Server-side size guard:** the file size is re-checked ON THE ARCHIVE at
    the moment of deletion. Only files with ``size <= max_size`` are eligible;
    ``max_size`` defaults to **0** (empty only). A file over the cap is SKIPPED,
    never deleted. Raising the cap (e.g. to remove a known 3-byte truncated
    file) is a second deliberate act on top of ``execute`` — and it is still
    BOUNDED, so a large file can never be removed by accident.
  * **Dry-run is the default** — nothing is deleted unless ``execute=True``.

DO NOT refactor the remote command to f-string a path into it — that
re-introduces catastrophic shell-injection / wrong-delete risk. The argv
boundary and the server-side size re-check are the whole point.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("receivers.archive.remove")
audit = logging.getLogger("receivers.audit")

# Strict archive layout: YYYY/mon/STA/session/category/FILE — anchors the delete
# to a real archive path and (as a secondary defense) admits no shell metachars.
# The category dir may be followed by ONE known sidecar level: the dated
# supersede/backup dirs the pipeline itself creates beside the products —
# `superseded_rt_<YYYYMMDD>` (RT stream files replaced by a raw-derived
# conversion), `rinex_bak/` (--backup-old) and `fix-headers_<YYYYMMDD>` under
# rinex_archive/. Without this, cleaning up the pipeline's own leftovers had to
# bypass the gateway and hand-run rm on the server, which is precisely what this
# verb exists to prevent. Named exactly, so an arbitrary subdirectory is still
# refused.
_SIDECAR = r"(?:(?:superseded_rt_[0-9]{8}|rinex_bak|fix-headers_[0-9]{8})/)?"
# `rinex_bak` appears in BOTH positions because --backup-old creates it as a
# SIBLING of the category dir, not a child: `bak = output_dir.parent /
# "rinex_bak"` (cli/main.py) and `bakdir="$(dirname "$dir")/rinex_bak"` (the
# remote move below). Listing it only in _SIDECAR meant every real backup path
# —`YYYY/mon/STA/session/rinex_bak/FILE`— was refused as "invalid/unsafe", so
# archive-rm could not delete the very leftovers it exists to clean up. That,
# together with --del-backup walking only data_prepath and /mnt/rawgpsdata
# being read-only on rek-d01, left NO working path to reclaim them: ~265 GB had
# accumulated across ELDC and ISAK by 2026-08-10. superseded_rt_* and
# fix-headers_* really are nested under the category dir, so they stay in
# _SIDECAR only.
_RELPATH_RE = re.compile(
    r"^[0-9]{4}/[a-z]{3}/[A-Z0-9]{2,9}/[0-9A-Za-z_]+/"
    r"(rinex|raw|rinex_org|rinex_archive|rinex_bak)/"
    + _SIDECAR
    + r"[A-Za-z0-9][A-Za-z0-9._-]*$"
)

# Characters never allowed in a target path (defense in depth over the regex).
_FORBIDDEN = set("*?[]{}~ \t\n\r\\'\"$`;|&<>()!")

# Known-harmless OpenSSH stderr noise. OpenSSH 10 prints a 3-line post-quantum
# key-exchange warning on every connection to an older server; left in place it
# fills the truncated error text and hides the REAL ssh/rsync error.
_SSH_NOISE_MARKERS = (
    "post-quantum key exchange",
    "store now, decrypt later",
    "openssh.com/pq.html",
)


def clean_ssh_stderr(stderr: str, limit: int = 400) -> str:
    """Strip known-harmless OpenSSH warning lines so the real error survives
    truncation; collapse the rest to one ``|``-separated line of ≤limit chars."""
    lines = [
        ln.strip()
        for ln in stderr.splitlines()
        if ln.strip() and not any(m in ln for m in _SSH_NOISE_MARKERS)
    ]
    if not lines and stderr.strip():
        return "(stderr was only OpenSSH warning noise — no error text)"
    return " | ".join(lines)[:limit]


def validate_archive_relpath(rel: str) -> bool:
    """True only for a safe, archive-layout-shaped relative path."""
    if not rel or rel.startswith("/") or ".." in rel:
        return False
    if any(c in _FORBIDDEN for c in rel):
        return False
    return bool(_RELPATH_RE.match(rel))


# Remote script: reads root/empty_only/execute as $1..$3, then the paths as $@.
# Paths arrive as ARGV — the shell never parses them as code. Quoted heredoc.
_REMOTE_SCRIPT = r"""
set -u
root="$1"; shift
maxsize="$1"; shift
execute="$1"; shift
case "$root" in "~/"*) root="$HOME/${root#\~/}";; "~") root="$HOME";; esac
for rel in "$@"; do
  f="$root/$rel"
  if [ ! -e "$f" ]; then echo "MISSING|$rel|0"; continue; fi
  if [ ! -f "$f" ]; then echo "SKIP_NOTFILE|$rel|0"; continue; fi
  sz=$(stat -c %s "$f" 2>/dev/null || echo -1)
  if [ "$sz" -lt 0 ] || [ "$sz" -gt "$maxsize" ]; then
    echo "SKIP_TOOBIG|$rel|$sz"; continue
  fi
  if [ "$execute" = "1" ]; then
    if rm -- "$f"; then echo "DELETED|$rel|$sz"; else echo "FAIL|$rel|$sz"; fi
  else
    echo "WOULD_DELETE|$rel|$sz"
  fi
done
"""


@dataclass
class RemoveResult:
    deleted: list = field(default_factory=list)  # (rel, size)
    would_delete: list = field(default_factory=list)  # (rel, size)
    skipped_toobig: list = field(default_factory=list)  # (rel, size) over cap
    missing: list = field(default_factory=list)  # rel
    not_file: list = field(default_factory=list)  # rel
    failed: list = field(default_factory=list)  # (rel, size)
    invalid: list = field(default_factory=list)  # rel

    @property
    def ok(self) -> bool:
        return not self.failed and not self.invalid


def remove_archive_files(
    rel_paths: list[str],
    *,
    ssh_target: str,
    dest_root: str,
    max_size: int = 0,
    execute: bool = False,
    timeout: int = 300,
) -> RemoveResult:
    """Delete (or dry-run) archive files via the rawdata SSH gateway.

    Args:
        rel_paths: archive-relative paths (``YYYY/mon/STA/session/cat/FILE``).
        ssh_target: ``user@host`` for the gateway (from the archive sync target).
        dest_root: archive root on the gateway (e.g. ``~/gpsdata``).
        max_size: only delete files with ``size <= max_size`` bytes (server-side
            re-check). Default 0 = empty only; bounded even when raised.
        execute: actually delete; False (default) = dry-run.
    """
    res = RemoveResult()
    valid: list[str] = []
    for rel in rel_paths:
        if validate_archive_relpath(rel):
            valid.append(rel)
        else:
            res.invalid.append(rel)
            logger.error("refusing invalid archive path: %r", rel)
    if not valid:
        return res

    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        ssh_target,
        "bash",
        "-s",
        "--",
        dest_root,
        str(int(max_size)),
        "1" if execute else "0",
        *valid,
    ]
    proc = subprocess.run(
        cmd, input=_REMOTE_SCRIPT, capture_output=True, text=True, timeout=timeout
    )
    for line in proc.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        status, rel, sz_s = parts
        try:
            sz = int(sz_s)
        except ValueError:
            sz = -1
        if status == "DELETED":
            res.deleted.append((rel, sz))
            audit.info("archive-rm DELETED %s (%d bytes)", rel, sz)
        elif status == "WOULD_DELETE":
            res.would_delete.append((rel, sz))
        elif status == "SKIP_TOOBIG":
            res.skipped_toobig.append((rel, sz))
        elif status == "MISSING":
            res.missing.append(rel)
        elif status == "SKIP_NOTFILE":
            res.not_file.append(rel)
        elif status == "FAIL":
            res.failed.append((rel, sz))
            logger.error("archive-rm FAILED to delete %s", rel)
    if proc.returncode != 0 and not (res.deleted or res.would_delete):
        logger.error(
            "ssh gateway error (rc=%s): %s",
            proc.returncode,
            clean_ssh_stderr(proc.stderr),
        )
    return res


def remove_catalog_rows(conn, storage_location: str, rel_paths: list[str]) -> int:
    """Delete archive_catalog rows for the given archive-relative paths.

    Called AFTER the files are removed (file first, then catalog row — a
    missing-file flag is a louder, recoverable failure than a silently
    uncataloged file). Returns the number of rows deleted.
    """
    if conn is None or not rel_paths:
        return 0
    from ..utils.canonical_key import canonical_key
    from .path_parse import parse_archive_path

    removed = 0
    for rel in rel_paths:
        parsed = parse_archive_path(rel, "")  # root='' → rel is already relative
        if parsed is None:
            continue
        key = canonical_key(rel.rsplit("/", 1)[-1])
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM archive_catalog
                   WHERE storage_location = %s AND session_type = %s
                     AND file_category = %s AND canonical_key = %s""",
                (storage_location, parsed.session_type, parsed.file_category, key),
            )
            removed += cur.rowcount
        conn.commit()
    return removed


#: Paths per SSH invocation. Each path is an argv entry, so an unbounded call
#: overruns the remote shell — see :func:`backup_old_archive_files`.
_BACKUP_CHUNK = 500

# Remote move: rinex/FILE -> sibling rinex_bak/FILE. Same argv-safe pattern as
# the delete script — paths arrive as $@, never interpolated. Quoted heredoc.
_BACKUP_REMOTE_SCRIPT = r"""
set -u
root="$1"; shift
execute="$1"; shift
case "$root" in "~/"*) root="$HOME/${root#\~/}";; "~") root="$HOME";; esac
for rel in "$@"; do
  f="$root/$rel"
  if [ ! -e "$f" ]; then echo "MISSING|$rel"; continue; fi
  if [ ! -f "$f" ]; then echo "SKIP_NOTFILE|$rel"; continue; fi
  dir=$(dirname "$f"); base=$(basename "$f")
  bakdir="$(dirname "$dir")/rinex_bak"
  if [ "$execute" = "1" ]; then
    mkdir -p "$bakdir" || { echo "FAIL|$rel"; continue; }
    dest="$bakdir/$base"; i=1
    while [ -e "$dest" ]; do dest="$bakdir/${base}.$i"; i=$((i+1)); done
    if mv -- "$f" "$dest"; then echo "BACKED_UP|$rel"; else echo "FAIL|$rel"; fi
  else
    echo "WOULD_BACKUP|$rel"
  fi
done
"""


@dataclass
class BackupResult:
    backed_up: list = field(default_factory=list)
    would_backup: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    not_file: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    invalid: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.invalid


def backup_old_archive_files(
    rel_paths: list[str],
    *,
    ssh_target: str,
    dest_root: str,
    execute: bool = False,
    timeout: int = 600,
) -> BackupResult:
    """Move existing archive ``rinex/FILE`` to a sibling ``rinex_bak/FILE`` via the
    rawdata SSH gateway — the archive-side backup for ``--backup-old`` at push
    time (before a re-rinexed file overwrites it). Dry-run by default; same
    enumerated-argv, layout-validated safety as :func:`remove_archive_files`.
    """
    res = BackupResult()
    valid: list[str] = []
    for rel in rel_paths:
        if validate_archive_relpath(rel):
            valid.append(rel)
        else:
            res.invalid.append(rel)
            logger.error("refusing invalid archive path: %r", rel)
    if not valid:
        return res

    # Chunked: every path is an argv entry, so one call for a whole staging
    # tree blows the remote shell's limit — `/bin/bash: Argument list too long`,
    # rc=1, ZERO files backed up. Observed on the ISAK R3 run at ~3,100 paths,
    # 11 times, each silently leaving that batch unprotected before the rsync
    # overwrote it. 500 keeps the command well inside ARG_MAX on any host while
    # still amortising the SSH handshake.
    for start in range(0, len(valid), _BACKUP_CHUNK):
        batch = valid[start : start + _BACKUP_CHUNK]
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            ssh_target,
            "bash",
            "-s",
            "--",
            dest_root,
            "1" if execute else "0",
            *batch,
        ]
        proc = subprocess.run(
            cmd,
            input=_BACKUP_REMOTE_SCRIPT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        reported = 0
        for line in proc.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 2:
                continue
            status, rel = parts
            reported += 1
            if status == "BACKED_UP":
                res.backed_up.append(rel)
                audit.info("re-rinex archive backup %s -> rinex_bak/", rel)
            elif status == "WOULD_BACKUP":
                res.would_backup.append(rel)
            elif status == "MISSING":
                res.missing.append(rel)
            elif status == "SKIP_NOTFILE":
                res.not_file.append(rel)
            elif status == "FAIL":
                res.failed.append(rel)
                logger.error("re-rinex backup FAILED for %s", rel)
        if proc.returncode != 0 and reported == 0:
            # This batch produced nothing — the caller must not read that as
            # "nothing needed backing up", so name the size that failed.
            logger.error(
                "ssh gateway error (rc=%s, %d path(s) unbacked): %s",
                proc.returncode,
                len(batch),
                clean_ssh_stderr(proc.stderr),
            )
    return res
