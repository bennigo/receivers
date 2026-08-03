# Design Proposal — Operational Log Routing + Loki/Grafana Ops-Follow for `receivers`

**Status**: Proposal (no code changed)
**Date**: 2026-07-14
**Scope**: `receivers` package on rek-d01 (gpsops user); laptop dev unaffected except where noted
**Related todos**: #60 (log rotation — resolved, `make_log_file_handler`), #61 (verbosity audit — receivers.log ~19 MB/h)

---

## 0. TL;DR recommendation

1. **Formal per-run log dir**: `~/.cache/gps_receivers/logs/runs/` — every CLI invocation gets an
   auto-named JSON run log via a new `run_log` mode in `setup_logging()`; detached runs capture
   stdout too (isatty check), so `nohup ... > ~/rhof_1hz_fix.log` becomes simply `nohup receivers rinex ... &`.
   Nothing lands in `$HOME`, ever.
2. **Schema**: extend the existing `JSONFormatter` — explicit-UTC `ts`, plus `station`, `component`
   (module family), `run_id`, `command`, `event`, `host`, `app`. The logger hierarchy already encodes
   module+station; this just surfaces it as first-class JSON fields.
3. **Ingestion**: **no Loki instance needs standing up.** Verified 2026-07-14: grafana.vedur.is already
   has a central Loki datasource (uid `c72827c2-7aa4-45fb-a07f-06ffd1267d9c`) fed by Promtail-style
   agents on the swarm hosts, with label conventions `host`, `job`, `level`, `filename`. The lightest
   path is a **Grafana Alloy (or Promtail) file scrape on rek-d01** tailing the JSON logs and pushing
   to that existing Loki — an IT ask, not an infrastructure build. No Python push handler.
4. **Labels**: low-cardinality Loki labels only — `job="gps-receivers"`, `host`, `level`, `component`.
   `station` and `run_id` stay JSON fields (or Loki structured metadata if the central Loki is ≥2.9)
   and are filtered with `| json | station="RHOF"` — fast enough, and avoids a 173-station × level ×
   component stream explosion.
5. **Dashboard**: one new "GPS Receivers — Log Follow" Grafana dashboard (Loki datasource) in
   `docs/grafana/`, pushed with the existing `scripts/grafana_sync.py`; the Postgres `gps_health`
   dashboards stay as-is and gain a per-station deep-link into the log dashboard.

---

## 1. Current state (code-verified)

### 1.1 What already works

`src/receivers/logging_config.py :: setup_logging()` is the unified entry point. `_configure()` wires,
on the `receivers` root logger (propagate=False):

| Handler | Sink | Format | Level |
|---|---|---|---|
| `StreamHandler(sys.stderr)` | console | `ProductionFormatter` (emoji) or `JSONFormatter` with `--json-log` | CLI `--loglevel` |
| `make_log_file_handler()` | `~/.cache/gps_receivers/logs/receivers.log` | `JSONFormatter` | DEBUG |
| `StationLogDispatcher` | `logs/stations/{SID}.log` (daily rotation, 30 d) | `JSONFormatter` | INFO |

Plus `AuditLogger` (`production_logging.py`) → `logs/download_audit.jsonl`, third-party suppression,
and per-component level overrides from `database.cfg [logging]`.

Rotation (#60, resolved): `make_log_file_handler()` returns `WatchedFileHandler` when
`/etc/logrotate.d/gps-receivers` exists (server; logrotate owns rotation: `logs/*.log` daily×30,
`logs/*.jsonl` daily×90 max 100M), else self-rotating `RotatingFileHandler` (dev).

Every CLI subcommand (`cmd_download`, `cmd_rinex` at main.py:5459, `cmd_health`, …) calls
`setup_logging(args.loglevel)`, so **logger records from detached runs already reach
`receivers.log`** — that part is not the gap.

### 1.2 The gaps

1. **stdout is outside the system.** `cli/main.py` has ~400 `print()` calls (progress, tables,
   summaries). A detached run's useful narrative goes to stdout, which is why operators do
   `nohup receivers rinex RHOF --fix-headers ... > ~/rhof_1hz_fix.log 2>&1 &` — ad-hoc files
   littering gpsops' `$HOME`, unrotated, unstructured, invisible to monitoring.
2. **No per-run identity.** Records from a long `rinex --fix-headers` run interleave with the
   scheduler's firehose in `receivers.log` with no way to say "show me *that run*". No `run_id`,
   no `command` field.
3. **JSON schema is not Loki-optimized.** `JSONFormatter.format()` (`production_logging.py:67`):
   - `timestamp` is **local-naive** ISO (`datetime.fromtimestamp(...).isoformat()`) — no offset.
     (Iceland is UTC year-round so it happens to be right on rek-d01, but it's ambiguous by
     construction and wrong on any non-UTC dev box.)
   - `station_id` is derived by a fragile heuristic (`name.count(".") >= 2` → last component), which
     also mislabels non-station leaves like `receivers.scheduler.reconciler` → `station_id: "reconciler"`.
   - No `host`, no module-family field, no event slug.
4. **Nothing ships to Loki.** rek-d01 does not appear in the central Loki's `host` label values
   (only `swarm-*` hosts and pollers ship today).

---

## 2. Design

### 2.1 Formal log directory layout

**Option A (recommended): stay under `~/.cache/gps_receivers/logs/`, add `runs/`.**

```
~gpsops/.cache/gps_receivers/logs/
├── receivers.log            # firehose (existing; DEBUG; logrotate daily×30)
├── download_audit.jsonl     # audit trail (existing; logrotate daily×90)
├── stations/                # per-station daily logs (existing; self-rotating)
│   └── {SID}.log
└── runs/                    # NEW — one JSON log per CLI invocation
    └── 20260714T091233Z-rinex-RHOF-a1b2c3.log
```

Run-log filename: `{utc_stamp}-{command}-{scope}-{shortid}.log` where `scope` is the first station
(or `all`/`multi`) and `shortid` is 6 hex chars of a uuid — same string as the `run_id` field in
every record, so a filename ↔ Loki `run_id` round-trip is trivial.

Why A: gpsops-writable with zero install.sh changes; already the documented log root in
CLAUDE.md; the existing logrotate config and Alloy scrape only need one more glob; dev and prod
stay symmetric.

**Option B: `/var/log/gps-receivers/`.** More conventional for an ops host and easier for IT to
find, but needs a root-created, gpsops-writable dir (install.sh Phase), diverges dev/prod paths,
and buys nothing functionally. Only worth it if IT policy requires `/var/log` for scraped logs —
**ask IT** (open question Q3), default to A.

**Cleanup of run logs.** Run logs are bounded by the run (no rotation needed) but accumulate.
Two mechanisms, both cheap:
- Opportunistic janitor in the new run-log setup: on each CLI start, delete `runs/*.log` older
  than `run_log_days` (default 30; `database.cfg [logging] run_log_days`). One `glob` + `stat`
  pass, same pattern as `StationLogDispatcher` retention.
- Belt-and-braces logrotate stanza in `deployment/logrotate.d/gps-receivers` — note the existing
  `logs/*.log` glob does **not** recurse, so `runs/` (and incidentally `stations/`) is currently
  uncovered; add `logs/runs/*.log { maxage 30 missingok ... }` or rely on the janitor alone.
  Recommend janitor as primary (works on dev too), logrotate stanza as backstop.

### 2.2 Routing detached CLI runs into the formal dir

Changes concentrated in `logging_config.py`; the per-command `setup_logging(args.loglevel)`
call sites don't multiply.

1. **Hoist logging setup into `main()`** (cli/main.py) so it runs once after argparse, before
   command dispatch — today each `cmd_*` calls it (idempotent, so hoisting is safe) but only
   `main()` knows the full argv/command needed for the run context. The thin
   `cli/main.py::setup_logging()` wrapper stays for backward compat.

2. **New per-run file handler** in `_configure()` (or a small `RunLogManager`), gated by a new
   `run_log: bool = True` param used by the CLI path (scheduler service passes `run_log=False` —
   its "run" is the service lifetime and `receivers.log` + journald already cover it):
   - `FileHandler(logs/runs/<run_name>.log)`, `JSONFormatter`, level = INFO by default,
     DEBUG when `--loglevel DEBUG`.
   - Attach a `RunContextFilter` to the `receivers` root logger stamping `record.run_id`,
     `record.command`, and (first record only) `record.argv` onto every record — so the run
     context reaches **all** sinks (receivers.log, station files, console, run log), not just
     the run file. That is what makes "filter receivers.log/Loki by run" work.

3. **CLI flags** (in `cli/arguments.py`, global to all subcommands):
   - `--log-file PATH` — override the auto path (escape hatch; still JSON).
   - `--no-run-log` — suppress (for trivial interactive `receivers status` one-offs, and to
     keep test suites clean).
   - Keep `--json-log` meaning "JSON on console" as today.

4. **Capture stdout when detached.** The pragmatic fix for the 400 `print()`s without a mass
   rewrite: when `not sys.stdout.isatty()` (i.e. nohup/pipe/cron) — or with an explicit
   `--capture-stdout` — wrap `sys.stdout` in a `StreamToLogger` that emits each line as
   `receivers.cli.stdout` INFO with `extra={"batch_quiet": True}` (reusing the existing console
   filter so nothing echoes back to the console handler). Result:

   ```bash
   # before
   nohup receivers rinex RHOF --fix-headers ... > ~/rhof_1hz_fix.log 2>&1 &
   # after — no redirect at all; everything lands in logs/runs/...RHOF....log
   nohup receivers rinex RHOF --fix-headers ... >/dev/null 2>&1 &
   ```

   Long-term (#61 synergy) the chatty prints should migrate to `logger.info(..., extra={"event": ...})`,
   but the isatty capture makes the formal dir complete from day one.

5. **Discoverability helper** (nice-to-have): `receivers logs [--follow] [--last N] [STATION]` —
   lists/tails recent run logs, prints the matching Grafana deep-link. Cheap to build once run
   logs are structured; keeps operators out of `ls ~/.cache/...`.

6. **Alternative considered — `systemd-run --user` for detached runs** (journald capture, Alloy
   journal source). Clean, but changes operator muscle memory and adds a journald→JSON parsing
   step; files stay the canonical sink. Rejected as primary, compatible as a later addition.

### 2.3 Structured line schema

Extend `JSONFormatter` (single place; every sink inherits). Proposed record:

```json
{
  "ts": "2026-07-14T09:12:33.123456+00:00",
  "level": "ERROR",
  "app": "receivers",
  "host": "rek-d01",
  "logger": "receivers.rinex.corrector",
  "component": "rinex",
  "station": "RHOF",
  "run_id": "20260714T091233Z-rinex-RHOF-a1b2c3",
  "command": "rinex",
  "event": "header_fix_failed",
  "msg": "MARKER NUMBER strip failed: ...",
  "src": "corrector.py:412",
  "exc_info": "Traceback (most recent call last): ...",
  "duration_seconds": 12.4,
  "bytes_downloaded": 1048576
}
```

Field rules:

| Field | Source | Notes |
|---|---|---|
| `ts` | `datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()` | **Fix**: explicit UTC offset (RFC3339). Current local-naive form is ambiguous. |
| `app` | constant `"receivers"` | Lets other packages (tostools?) share the pipeline later. |
| `host` | `socket.gethostname()` cached at import | Redundant with the Alloy label but keeps files self-describing when read raw. |
| `component` | 2nd dot-component of `record.name` (`download`, `health`, `scheduler`, `rinex`, `cli`, `archive`, `pipeline`, `monitoring`, `audit`) | ~10 stable values — this is the Loki label. |
| `station` | reuse `_STATION_ID_RE` from `logging_config.py` on the last name component, else `record.station_id` | **Fix**: the current `count('.') >= 2` heuristic mislabels `receivers.scheduler.reconciler`; the regex the `StationLogDispatcher` already uses is the correct test. Rename key `station_id` → `station` (keep emitting `station_id` for one release for downstream grep compat). |
| `run_id`, `command` | `RunContextFilter` | Absent in scheduler-service records (or `run_id: "scheduler"`). |
| `event` | `extra={"event": "..."}` opt-in machine slug | Adopt incrementally where alerting will key on it (download_failed, convert_failed, backfill_gap, push_refused). Not required on every line. |
| `msg`, `src`, `exc_info`, metrics | as today (`message`→`msg`, `module`+`lineno`→`src` — or keep old names; see Q6) | `exc_info`/`stack_info` handling stays exactly as the hard-won comment in `production_logging.py:99` demands. |

**Loki labels vs fields — the cardinality rule.** Labels index streams; every distinct label
combination is a stream. Keep labels to:

- `job="gps-receivers"` (matches central conventions: `job`, `host`, `level` already in use)
- `host="rek-d01"`
- `level` (5 values; extracted by the agent from the JSON)
- `component` (~10 values)
- `source="receivers_log" | "run" | "audit" | "station"` (which file class it came from)

Worst case ≈ 1 job × 1 host × 5 levels × 10 components × 4 sources ≈ 200 streams. Fine.

**Not labels**: `station` (173 values → ×173 streams ≈ 35k, exactly the anti-pattern Loki docs warn
about), `run_id` (unbounded), `msg`. Operators filter stations with a parsed-field query:

```logql
{job="gps-receivers", level=~"error|warning"} | json | station = "RHOF"
```

which Grafana template variables drive just as well as a label — at rek-d01 volumes (~19 MB/h
today, less after #61) the parse cost is negligible. If the central Loki is ≥2.9, promote
`station` to **structured metadata** in the Alloy pipeline (indexed-ish filtering without stream
explosion) — confirm version with IT (Q1).

### 2.4 Loki / Grafana path

**Verified**: grafana.vedur.is datasources include `Loki` (uid `c72827c2-7aa4-45fb-a07f-06ffd1267d9c`),
`Prometheus`, `Tempo`. Current Loki `host` values are all `swarm-*` nodes plus poller jobs — i.e.
IMO IT runs a central Loki with per-host Promtail/Alloy agents. **The design therefore needs no
Loki deployment — only an agent on rek-d01 and the push endpoint/credentials from IT.**

**Recommended ingestion: Grafana Alloy (or Promtail) file scrape on rek-d01.**

```river
loki.source.file "gps_receivers" {
  targets = [
    { __path__ = "/home/gpsops/.cache/gps_receivers/logs/receivers.log",      source = "receivers_log" },
    { __path__ = "/home/gpsops/.cache/gps_receivers/logs/runs/*.log",          source = "run" },
    { __path__ = "/home/gpsops/.cache/gps_receivers/logs/download_audit.jsonl", source = "audit" },
  ]
  forward_to = [loki.process.gps.receiver]
}
loki.process "gps" {
  stage.json      { expressions = { level = "level", component = "component", ts = "ts" } }
  stage.timestamp { source = "ts", format = "RFC3339" }
  stage.labels    { values = { level = "", component = "" } }
  // stage.structured_metadata { values = { station = "", run_id = "" } }  // if Loki >= 2.9
  // stage.match { selector = "{level=\"DEBUG\"}", action = "drop" }        // volume valve, see #61
  forward_to = [loki.write.central.receiver]
}
loki.write "central" { endpoint { url = "<from IT>" } }
```

Notes:
- One agent, static labels `job="gps-receivers"`, `host="rek-d01"`.
- Skip `stations/*.log` — those records are duplicates of receivers.log lines (same dispatcher
  input); scraping them would double-ingest. The per-station files stay as the *local* grep/tail
  convenience; Loki gets station via the JSON field. (If we ever wanted station-as-label cheaply,
  scraping `stations/*.log` with a filename-derived label is the fallback trick — noted, not chosen.)
- Rotation interplay is safe: logrotate uses rename+`create` (not copytruncate) and both
  `WatchedFileHandler` and Alloy's positions-file tailer handle that correctly.
- The `stage.match` drop of DEBUG is the independent volume knob: files keep full DEBUG for
  forensics; Loki ingests INFO+ only. This makes the design robust even before #61 lands.

**Rejected as primary: Python Loki push handler** (`logging-loki` / custom). Couples every CLI
run and the scheduler to network availability of Loki, needs in-process buffering/retry (data loss
on crash — exactly what immediate archiving exists to avoid on the data side), adds a dependency,
and duplicates what a battle-tested agent does. Files remain the source of truth; the agent is
restartable and backfills from its positions file.

**Also viable later**: Alloy `loki.source.journal` for the systemd-user scheduler unit's
stderr — complements, doesn't replace, the file scrape.

### 2.5 Grafana dashboard sketch — "GPS Receivers — Log Follow"

New dashboard JSON in `docs/grafana/gps_log_follow_dashboard.json`, provisioned locally like the
others and pushed to vedur via `scripts/grafana_sync.py` (extend its datasource-UID remap table
with local-Loki → `c72827c2-...`; local dev gets a Loki container in
`deployment/docker-dev/docker-compose.yml` — optional, dev can also point straight at files with
`receivers logs`).

Template variables: `$level` (label), `$component` (label), `$station` (query: `label_values`-style
via JSON field or structured metadata; fallback: text box), `$run_id` (text box), `$search` (text box).

| Row | Panels | Query sketch |
|---|---|---|
| Overview | Error rate + Warning rate (timeseries); Errors by component (stacked bars); Log volume by component (bytes_over_time) | `sum by (component) (count_over_time({job="gps-receivers", level="ERROR"}[$__auto]))` |
| Stations | Top-10 error stations (table/bar) ; errors for `$station` over time | `topk(10, sum by (station) (count_over_time({job="gps-receivers", level=~"ERROR\|WARNING"} \| json [$__range])))` |
| Runs | Recent runs table (distinct run_id, first/last ts, error count); run drill-down logs panel filtered `\| json \| run_id="$run_id"` | run picker feeds the logs panel |
| Live tail | Logs panel, `{job="gps-receivers", level=~"$level", component=~"$component"} \| json \| station=~"$station" \|~ "$search"`, live mode | the ops-follow view |

Cross-links: the existing `gps_station_detail_dashboard.json` gains a data-link "Logs →" passing
`$station` into this dashboard; conversely the run table links back to station detail. Optional
follow-up: Loki-based alert rules (e.g. `download_failed` events > N in 30 min) — but Icinga/email
already covers alerting, so treat as later.

The Postgres `gps_health` dashboards are **not** touched or replaced — metrics/state stay in
Postgres; Loki adds the *why* (log narrative) next to the *what*.

---

## 3. Migration / rollout

Incremental; each phase independently shippable and reversible.

**Phase 0 — schema (code only, no infra).**
- `JSONFormatter`: UTC `ts`; correct `station` extraction via `_STATION_ID_RE`; add `app`, `host`,
  `component`; keep `station_id` alias for one release.
- Unit tests for the formatter (existing tests cover exc_info behavior — extend).
- Risk: near-zero; consumers are greppers and future Loki.

**Phase 1 — run logs (code + docs).**
- `RunContextFilter`, `runs/` handler + janitor, `--log-file` / `--no-run-log` /
  `--capture-stdout` (+ isatty default), hoist setup into `main()`.
- Docs: CLAUDE.md "Unified Logging System" section + ops runbook: *the sanctioned detached-run
  pattern is `nohup receivers ... >/dev/null 2>&1 &`; `> ~/foo.log` redirects are deprecated*.
- logrotate stanza for `logs/runs/*.log` in `deployment/logrotate.d/gps-receivers`
  (deploy via install.sh as today; **not** a gps-config-data file — it's package deployment, same
  as the existing logrotate config).
- Coexists with #60: run-log files are per-run FileHandlers, never rotated by logrotate mid-run
  if the stanza uses `maxage`-style cleanup only; the WatchedFileHandler logic is untouched.

**Phase 2 — ship to Loki (IT ask + deployment).**
- Ticket to IT: Alloy/Promtail on rek-d01, push endpoint + auth for the central Loki, agree
  `job="gps-receivers"` and retention. Config file lives in `deployment/` in this repo (versioned),
  installed by install.sh or by IT's config management — *not* gps-config-data (it's host
  infrastructure, not GPS station config; gps-config-data stays the source of truth only for the
  `[logging]` knobs in `database.cfg`/`receivers.cfg` that the app reads).
- Start with `runs/*.log` + `receivers.log` INFO+ (DEBUG dropped at the agent).

**Phase 3 — dashboard.**
- `gps_log_follow_dashboard.json` + grafana_sync UID remap + station-detail cross-links.

**Phase 4 (with #61) — verbosity + events.**
- Verbosity audit demotes chatty INFO→DEBUG (shrinks both disk and Loki ingest); migrate
  high-value `print()`s to `logger.info(..., extra={"event": ...})`; grow the `event` vocabulary
  where alerting wants it.

---

## 4. Open questions for bgo

1. **Loki ownership/version** — who at IT owns the central Loki; is it ≥2.9 (structured metadata →
   `station` promotable); what's the push endpoint/auth from rek-d01; any per-job retention policy
   (propose 30–90 d; files remain the long archive)?
2. **Retention split** — is 30 d for `runs/*.log` on disk right, given `receivers.log` keeps 30 d
   and audit 90 d?
3. **Path policy** — does IT prefer scraped logs under `/var/log/` (Option B) or is
   `~gpsops/.cache/gps_receivers/logs/` (Option A, recommended) acceptable to point an agent at?
4. **Dashboard scope** — keep Postgres health dashboards and the Loki log dashboard separate with
   cross-links (recommended), or embed a logs panel directly into station-detail?
5. **Audit stream** — ship `download_audit.jsonl` to Loki too (useful for run/session drill-down)
   or keep it file-only? (Proposal: ship it, `source="audit"`.)
6. **Field naming** — adopt the compact names (`ts`/`msg`/`src`) or keep the current
   `timestamp`/`message`/`module`+`line` to avoid breaking any existing grep/jq habits? Either
   works for Loki; pick once, before Phase 0.
7. **Other hosts later** — same pattern for gpsplot / future rek_new ("one queryable stream per
   operational host" = same `job`, different `host` label), any reason to design differently now?

---

## 4b. Decisions locked (bgo, 2026-07-15)

Resolved in an interactive walkthrough; the rest are IT-handover items.

| Topic | Decision |
|---|---|
| Deployment | Ship the log-follow dashboard to the **vedur production Grafana at handover**, in the **GPS folder alongside the health dashboards** |
| Dashboard scope | **Separate** log dashboard + cross-links to the Postgres health dashboards (not embedded) |
| Loki retention | **90 days** |
| runs/*.log on-disk retention | **90 days** (janitor prunes older) |
| Local dev | **Add Loki + Alloy to docker-dev** too (dashboard testable on the laptop) |
| Per-run log dir | **`~/.cache/gps_receivers/logs/runs/`** (XDG-consistent, gpsops-writable) |
| Audit stream | **Ship `download_audit.jsonl` to Loki** as `source="audit"` (still file-backed) |
| Field names | **Keep current** (`timestamp`/`message`/`module`+`line`); ADD `station`/`component`/`host`/`run_id`/`event` — don't break existing grep/jq |
| Level casing | **Keep uppercase** (`WARNING`/`ERROR`/`CRITICAL`) |
| JSONFormatter bugs | **Fix now, standalone** (UTC ts + station regex) — done separately from the rollout |
| Other hosts (gpsplot/rek_new) | Same `job`, different `host` label — no divergence |

**Deferred to IT-handover:** exact `job` label rek-d01 ships under; whether Loki ≥2.9 (→ promote `station`/`run_id` to structured metadata, `station` becomes a dropdown); live-tail websocket permission; Loki push endpoint/auth/ownership.

---

## 5. Summary of touched code (when implemented)

| File | Change |
|---|---|
| `src/receivers/base/production_logging.py` | `JSONFormatter` schema (UTC ts, station regex, app/host/component); no handler changes |
| `src/receivers/logging_config.py` | `run_log` mode: runs/ FileHandler, `RunContextFilter`, janitor, `StreamToLogger` stdout capture; reuse `_STATION_ID_RE` |
| `src/receivers/cli/main.py` | hoist `setup_logging` into `main()` with command context; keep wrapper |
| `src/receivers/cli/arguments.py` | `--log-file`, `--no-run-log`, `--capture-stdout` globals |
| `deployment/logrotate.d/gps-receivers` | `logs/runs/*.log` cleanup stanza |
| `deployment/` (new) | Alloy/Promtail config for rek-d01 |
| `docs/grafana/` | `gps_log_follow_dashboard.json`; `scripts/grafana_sync.py` UID remap |
| `CLAUDE.md` / ops docs | formal detached-run pattern; deprecate `$HOME` redirects |
