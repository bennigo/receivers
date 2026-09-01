#!/usr/bin/env bash
# Run a command once an already-running job has finished.
#
# WHY THIS EXISTS. The obvious hand-rolled version deadlocks forever:
#
#     bash -c 'while pgrep -f "receivers rinex OFEL --from-archive" >/dev/null; \
#              do sleep 300; done; receivers rinex OFEL --fix-headers … --push'
#
# `pgrep -f` matches against the FULL command line of every process — including
# the watcher's own `bash -c …`, which contains the pattern as a literal. So the
# watcher matches itself, the loop never exits, and the queued job never runs.
# Measured 2026-09-01: two of these had been spinning for 4 days 16 hours (OFEL
# here, DYNC on rek-d01) with their fix-headers pushes silently never firing.
#
# THE FIX: resolve the pattern to PIDs exactly ONCE, up front, and then wait on
# those specific PIDs. Our own PID cannot be in that set — we exclude it and our
# parent explicitly — so self-matching is structurally impossible rather than
# merely avoided. It also means a LATER process that happens to match the same
# pattern does not extend the wait, which the naive loop got wrong too.
#
# Usage:
#   run-after.sh --pattern "receivers rinex OFEL --from-archive" -- <cmd> [args…]
#   run-after.sh --pid 12345 [--pid 23456] -- <cmd> [args…]
#   run-after.sh --pattern "…" --poll 60 -- <cmd> [args…]
#
# Exits with the command's status. If nothing matches, the command runs at once.
set -uo pipefail

POLL=60
PIDS=()
PATTERN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pattern) PATTERN="${2:?--pattern needs a value}"; shift 2 ;;
    --pid)     PIDS+=("${2:?--pid needs a value}"); shift 2 ;;
    --poll)    POLL="${2:?--poll needs a value}"; shift 2 ;;
    --)        shift; break ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "run-after.sh: unknown option '$1'" >&2; exit 2 ;;
  esac
done

if [[ $# -eq 0 ]]; then
  echo "run-after.sh: no command given (did you forget '--'?)" >&2
  exit 2
fi

_still_running() {
  # kill -0 is TRUE for a zombie: the process has exited but its parent has not
  # reaped it yet, so the PID still exists. Waiting on that never finishes — and
  # it is not hypothetical, since a job launched by a script that is itself busy
  # waiting is exactly how zombies linger. /proc/<pid>/stat field 3 is the state
  # letter; 'Z' means already dead.
  local st rest state
  kill -0 "$1" 2>/dev/null || return 1
  [[ -r "/proc/$1/stat" ]] || return 1
  st=$(< "/proc/$1/stat")
  rest="${st##*) }"
  state=$(printf '%s' "$rest" | cut -d' ' -f1)
  [[ "$state" == "Z" ]] && return 1
  return 0
}

_ppid_of() {  # /proc/<pid>/stat field 4; comm may hold spaces/parens, so cut past the last ')'
  local st rest
  [[ -r "/proc/$1/stat" ]] || return 1
  st=$(< "/proc/$1/stat")
  rest="${st##*) }"
  printf '%s' "$rest" | cut -d' ' -f2
}

# Every PID in OUR OWN LINEAGE — ancestors and self. Anything here is excluded
# from a --pattern match, and so is anything descended from us.
#
# Self-exclusion cannot be done with a regex trick: this script receives the
# pattern as an argument, so its argv always contains the text the pattern
# matches. Nor is excluding self+children enough — `pgrep -f` matches the FULL
# command line of EVERY process, and the pattern text is equally present in
# whoever invoked us: the interactive shell, a `timeout …` wrapper, an
# `ssh host '…'` command string, a CI runner. Each of those is an ANCESTOR.
# Measured while writing this: `python3 -c "…definitely-no-such-xyzzy…"` and its
# `timeout` wrapper both matched a pattern that matched no real process at all.
#
# A genuine target job — started separately, earlier, by someone else — is
# neither an ancestor nor a descendant of this script. That is the whole
# discriminator, and it is structural rather than a list of special cases.
_LINEAGE=" "
_walk=$$
_hops=0
while [[ -n "$_walk" && "$_walk" != "0" && "$_walk" != "1" && $_hops -lt 64 ]]; do
  _LINEAGE+="$_walk "
  _walk=$(_ppid_of "$_walk") || break
  _hops=$((_hops + 1))
done

is_own_lineage() {           # ancestor, self, or descendant of this script
  local pid="$1" hops=0
  [[ "$_LINEAGE" == *" $pid "* ]] && return 0
  while [[ -n "$pid" && "$pid" != "0" && "$pid" != "1" && $hops -lt 64 ]]; do
    [[ "$pid" == "$$" ]] && return 0
    pid=$(_ppid_of "$pid") || return 1
    hops=$((hops + 1))
  done
  return 1
}

if [[ -n "$PATTERN" ]]; then
  # Snapshot ONCE. Waiting on a snapshot rather than re-running pgrep also means
  # a LATER process matching the same pattern cannot silently extend the wait.
  mapfile -t _candidates < <(pgrep -f -- "$PATTERN" 2>/dev/null || true)
  for p in "${_candidates[@]}"; do
    [[ -z "$p" ]] && continue
    _still_running "$p" || continue   # already gone (typically the pgrep fork)
    is_own_lineage "$p" && continue
    PIDS+=("$p")
  done
  if [[ ${#PIDS[@]} -eq 0 && ${#_candidates[@]} -gt 0 ]]; then
    echo "run-after.sh: ${#_candidates[@]} pgrep match(es) were all this script's own lineage — ignoring" >&2
  fi
fi

if [[ ${#PIDS[@]} -eq 0 ]]; then
  echo "run-after.sh: nothing to wait for — running now" >&2
else
  echo "run-after.sh: waiting for PID(s): ${PIDS[*]} (poll ${POLL}s)" >&2
  while :; do
    alive=0
    for p in "${PIDS[@]}"; do
      # Existence test without signalling — cheaper and more precise than
      # re-running pgrep, and immune to a new match appearing later. Treats a
      # zombie as exited; see _still_running.
      if _still_running "$p"; then alive=1; break; fi
    done
    [[ $alive -eq 0 ]] && break
    sleep "$POLL"
  done
  echo "run-after.sh: all waited-for PIDs have exited — running now" >&2
fi

exec "$@"
