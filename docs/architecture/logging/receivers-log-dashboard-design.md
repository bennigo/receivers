# Design Writeup — "GPS Receivers — Log Follow" Grafana Dashboard

**Status**: Draft for review (nothing deployed)
**Date**: 2026-07-14
**Companion doc**: `receivers-logging-design.md` (same scratchpad) — the log schema, Loki label
decisions, and Alloy ingestion plan this dashboard assumes.
**Draft JSON**: `gps_log_follow_dashboard.json` (same scratchpad; destination
`docs/grafana/gps_log_follow_dashboard.json` in the receivers repo)
**Dashboard UID**: `gps-log-follow` · Title: *GPS Receivers — Log Follow* · timezone UTC, default range 6 h

---

## 1. What was borrowed from each reference dashboard

### `services-logs` (uid `f74849d6-4fa1-4052-b756-bddb0481cfc4`, Docker Swarm folder) — the skeleton

This is IT's own Loki log-follow pattern on grafana.vedur.is; the new dashboard is a direct
adaptation so it feels familiar to anyone who already uses it:

- **Structure**: a collapsed/compact "Timelines" row of `count_over_time` timeseries on top of a
  big logs panel. Adopted, expanded to three rows (Overview / Stations & runs / Live follow).
- **Timeline queries**: `sum(count_over_time({job=~"$job", swarm_service=~"$service",
  source=~"$source"} |= `` [$__auto]))` with `step: 1m` → became our error/warning-rate and
  volume-by-component panels (same `$__auto` range, `sum by (...)`).
- **Variables**: `job` (query var), `service` (Loki label-values query var *chained* through
  `include_regex`/`exclude_regex`), `source` (label-values var). Our `host`, `component`,
  `level`, `source` query variables use the identical Loki variable payload shape
  (`{"label": ..., "stream": "{job=~\"$job\"}", "type": 1}`, `refresh` on time-range change).
- **`include_regex` / `exclude_regex`**: services-logs uses *custom* variables holding
  service-name prefixes that gate the `$service` variable query. For receivers there is no
  prefix taxonomy to enumerate, so these became free-text **line filters**
  (`|~ `$include_regex`` / `!~ `$exclude_regex``) — same names, same spirit, applied to log
  lines instead of to the variable chain. Exclude defaults to `^$` (matches no non-empty line)
  because an empty `!~` pattern would exclude *everything*.
- **Logs panel options**: `showTime`, `wrapLogMessage`, `enableLogDetails`, sortOrder
  Ascending-for-narrative — copied; the live-tail panel flips to Descending (newest first).
- **Loki datasource**: uid `c72827c2-7aa4-45fb-a07f-06ffd1267d9c` — the only Loki datasource on
  grafana.vedur.is (confirmed via `list_datasources`), matching the companion doc's finding.

### `api-consumers` (uid `hjljbtr`) — panel idioms

- The **2xx / 4xx / 5xx split into separate same-shaped timeseries** became the
  ERROR+CRITICAL vs WARNING split with fixed severity colors (red/dark-red/yellow) via
  `byName` field overrides — one panel, two targets, instead of three panels (our level space
  is smaller).
- Row-per-concern layout with descriptive row titles.
- `$custom_interval` interval variable: **not** adopted — `$__auto` (used by services-logs)
  does the same job with zero user decisions; noted as an easy add if bgo wants explicit
  interval control.

### `arnar` (uid `ar5bwjh`) — the `| json` field-aggregation proof

Its single Loki query is the exact pattern our station/run aggregations rely on:

```logql
sum by(swarm_stack) (count_over_time({job="$custom_cluster", swarm_stack=~"$custom_api"}
  |= `access_log` | json | route_template != `/` [$custom_interval]))
```

i.e. *aggregate over a parsed JSON field, filter on parsed fields, drive with variables* —
already in production on this Grafana/Loki, so `sum by (station) (... | json ...)` is a proven
pattern on this instance, not a hope. Also borrowed its custom/quantile variable UX for the
textbox-variable descriptions (each variable in the new dashboard carries a `description`
tooltip explaining regex semantics).

### House style (existing `docs/grafana/*.json`)

- Top-nav `links` array to sibling dashboards with `keepTime: true` (station-detail does
  exactly this), tags (`gps`, …), variables with human labels, `graphTooltip: 2` (shared
  crosshair), instructional text conventions (`README.md` documents per-dashboard sections).
- **Local UIDs in links** (`/d/gps-station-detail/...`) so `grafana_sync.py`'s
  `remap_dashboard_links()` rewrites them per target — see §5.

---

## 2. Panel inventory + LogQL

Base selector used throughout (labels only, all low-cardinality):
`{job=~"$job", host=~"$host", component=~"$component", level=~"$level", source=~"$source"}`
Line filters: `|~ `$include_regex` !~ `$exclude_regex``
Field filters: `| json | station =~ `$station` | run_id =~ `$run_id``

| # | Panel | Type | LogQL (essentials) |
|---|-------|------|--------------------|
| 1 | How to use | text | — (blog best-practice #6: instructions on the dashboard) |
| — | **Row: Overview** | | |
| 3 | Errors (range) | stat | `sum(count_over_time({...level=~"ERROR\|CRITICAL"} <line><field> [$__range]))`, instant |
| 4 | Warnings (range) | stat | same with `level="WARNING"` |
| 5 | Stations with errors | stat | `count(sum by (station) (count_over_time({...level=~"ERROR\|CRITICAL"} \| json \| station =~ `.+` [$__range])))` — `.+` = only lines that *have* a station |
| 6 | Log volume (range) | stat, bytes | `sum(bytes_over_time({labels} [$__range]))` — the ingest knob for todo #61 |
| 7 | Error / warning rate | timeseries (bars) | 2 targets: `sum by (level)(count_over_time({...level=~"ERROR\|CRITICAL"} <line><field> [$__auto]))` + WARNING; overrides pin ERROR=red, CRITICAL=dark-red, WARNING=yellow |
| 8 | Log volume by component | timeseries (stacked bars) | `sum by (component)(count_over_time({labels} [$__auto]))` |
| — | **Row: Stations & runs** | | |
| 10 | Top error stations | table, instant | `topk(15, sum by (station)(count_over_time({...level=~"ERROR\|WARNING\|CRITICAL"} <line> \| json \| station =~ `.+` [$__range])))`; organize-transform renames `Value #A` → "err+warn lines"; **data links** on the station cell → (a) this dashboard with `var-station=${__data.fields.station}`, (b) `/d/gps-station-detail` |
| 11 | Recent runs | table, instant | A: `topk(20, sum by (run_id, command)(count_over_time({... source="run"} \| json \| run_id =~ `.+` [$__range])))`; B: same grouped by `run_id` with `level=~"ERROR\|CRITICAL"`; merge + organize transforms → columns run_id / command / lines / errors; data link on run_id sets `var-run_id` |
| 12 | Run log — $run_id | logs, Ascending | `{job=~"$job", host=~"$host"} \| json \| run_id =~ `$run_id` \| station =~ `$station`` — deliberately ignores level/component/source so a run reads like the complete log file it mirrors (`runs/<run_id>.log`) |
| — | **Row: Live follow** | | |
| 14 | Live tail — $component / $station | logs, Descending, Live-capable | full base selector + line filters + field filters — the one panel that honors *every* variable |

Query-shape decisions worth calling out:

- **Instant queries for tables/stats** with `[$__range]`, not range queries — aggregate once
  over the picker window (the Grafana blog explicitly flags misusing Range type for these as a
  performance mistake).
- **Overview panels honor the station/run field filters too**, so once you focus on RHOF the
  error-rate curve *is* RHOF's — the whole dashboard narrows coherently rather than only the
  tail panel.
- Discovery panels (#5, #10, #11) intentionally use `.+`/no station filter where the point is
  *finding* the offender, not confirming it.
- `run_id =~ ".*"` (the default) matches records with no `run_id` (scheduler-service lines),
  so the dashboard is complete before Phase 1 run-logs even ship.

## 3. Variable design — and why `station` is a field, not a label

| Variable | Type | Backing | Default |
|----------|------|---------|---------|
| `job` | custom (editable) | `gps-receivers` constant until IT confirms the label (open Q) | `gps-receivers` |
| `host` | query, multi+All | Loki `label_values(host)` over `{job=~"$job"}` | All |
| `component` | query, multi+All | `label_values(component)` — ~10 values from the `receivers.*` logger hierarchy | All |
| `level` | query, multi+All | `label_values(level)` — INFO/WARNING/ERROR/CRITICAL (DEBUG dropped at agent) | All |
| `source` | query, multi+All | `label_values(source)` — receivers_log / run / audit | All |
| `station` | **textbox regex** | `\| json \| station =~ `$station`` | `.*` |
| `run_id` | **textbox regex** | `\| json \| run_id =~ `$run_id`` | `.*` |
| `include_regex` | textbox | line filter `\|~` | `.*` |
| `exclude_regex` | textbox | line filter `!~` | `^$` (excludes nothing) |

**Why station is a JSON field.** Loki indexes *streams*: every distinct label combination is a
stream with its own chunks. Promoting `station` (173 values) to a label multiplies the existing
job×host×level×component×source space (~200 streams) by 173 → ~35k streams — exactly the
high-cardinality anti-pattern the Loki docs warn about; `run_id` is unbounded and even more
disqualified. The dashboard instead filters *after* the label selector with
`| json | station =~ ...`. At rek-d01 volume (~19 MB/h today, less post-#61) parsing at query
time is negligible, and the arnar dashboard proves the pattern performs on this very Loki.
Grafana's own guidance for values you need to query-by but must not index is **structured
metadata** (Loki ≥ 2.9 / 3.x): if IT's Loki qualifies, the Alloy stage promotes `station` and
`run_id` to structured metadata and the only dashboard change is dropping `| json` in favor of
direct `| station=...` matchers — and `station` can then even become a query variable instead
of a textbox. The textbox is the version-agnostic draft choice.

Variable chaining (`host` → filtered by `$job`, `component`/`level`/`source` → by `$job`+`$host`)
mirrors services-logs, so dropdowns only offer values that actually exist in the selection.

## 4. Cross-links to the Postgres `gps_health` dashboards

`station` is the shared dimension across both worlds (Postgres `sid` / dashboard `$station` ↔
log JSON `station`). Wiring, in both directions:

- **This dashboard → health**: top-nav links to `gps-health-voltage`, `gps-station-detail`
  (passes `var-station=${station}`) and `gps-data-delivery`; plus a per-row data link on the
  *Top error stations* table → station detail for the clicked station.
- **Health → this dashboard** (separate follow-up edit, per companion doc Phase 3):
  `gps_station_detail_dashboard.json` gains a nav link
  `/d/gps-log-follow/gps-receivers-log-follow?var-station=${station}` ("Logs →"), turning every
  health anomaly into a one-click jump to that station's log narrative. The Postgres dashboards
  are otherwise untouched — metrics/state stay in Postgres, logs add the *why*.
- All link URLs use **local UIDs**; `remap_dashboard_links()` + `link_mappings` translate per
  target (needs a `gps-log-follow: {vedur: gps-log-follow}` entry, plus the reverse entries
  already exist for the health UIDs → `bgqb686` etc.).

Time range is carried on nav links (`keepTime: true`), so "voltage dipped at 03:12 → what was
the download job doing" lands on the right window.

## 5. Shipping via `scripts/grafana_sync.py`

The file goes to `docs/grafana/gps_log_follow_dashboard.json` (source of truth, like the four
existing dashboards) and is pushed with `python scripts/grafana_sync.py push --target vedur`.
Three small sync-tooling changes are required:

1. **`grafana_targets.yaml`**: add `gps_log_follow_dashboard: "gps-log-follow"` under both
   targets' `dashboards:`, and a `link_mappings` entry for `gps-log-follow`.
2. **`remap_datasource_uid()` is Postgres-only today** — it walks the JSON replacing UIDs where
   `type == "grafana-postgresql-datasource"`. It needs a second mapping for `type == "loki"`
   driven by a new per-target `loki_datasource_uid` key (vedur:
   `c72827c2-7aa4-45fb-a07f-06ffd1267d9c`; local: whatever the dev Loki container gets, e.g.
   `gps_loki`). ~15-line change, same structural-walk shape.
3. **Local dev story**: the draft JSON carries the *vedur* Loki UID directly, so it can be
   pushed as-is before any local Loki exists. Once a Loki + Alloy pair is added to
   `deployment/docker-dev/docker-compose.yml` (optional, per companion doc), the source file
   flips to the local UID and the remap in (2) handles vedur — the same convention as the
   Postgres dashboards. Until then, dashboard provisioning of this file into the local Grafana
   should be skipped (it would render "datasource not found").

Suggested folder on vedur: the existing GPS folder `ffctg7iot7tvkb` (same `folder_uid` the
target already uses), keeping health + logs side by side.

## 6. Web-research takeaways folded in (sources)

- **Labels vs structured metadata / cardinality**: keep labels low-cardinality and bounded;
  use structured metadata for high-cardinality query-by values (station, run_id) on Loki ≥ 2.9
  — [Grafana Loki docs: What is structured metadata](https://grafana.com/docs/loki/latest/get-started/labels/structured-metadata/);
  community guidance on when labels vs metadata: [Grafana community thread](https://community.grafana.com/t/so-when-to-use-structured-metadata-and-when-to-use-labels/120337);
  high-cardinality label pitfalls: [charmhub: Solving high cardinality labels in Loki](https://discourse.charmhub.io/t/solving-high-cardinality-labels-in-loki/15426).
- **Log-dashboard construction**: textbox variable as line filter; label-filter dropdowns with
  `=~` for multi/All; ad-hoc/instant `sum by(...) (count_over_time(... | json ...))` tables;
  **data links for drill-down** (`var-x=${__data.fields.x}`); *instant* query type for
  range-aggregated tables (Range type misuse is slow); an instructions text panel to speed
  adoption — [Grafana blog: 6 easy ways to improve your log dashboards](https://grafana.com/blog/6-easy-ways-to-improve-your-log-dashboards-with-grafana-and-grafana-loki/).
  All six are represented in the draft (the pie chart idea became the stacked
  volume-by-component bars — more useful over time than as a static distribution).
- **Aggregate → filtered stream drill-down flow** (volumes by label on top, filtered log panel
  below) mirrors Grafana's Logs Drilldown app model —
  [Grafana Logs Drilldown docs](https://grafana.com/docs/grafana/latest/explore/simplified-exploration/logs/).
- **RED-for-workers framing**: for a background fleet, Rate = log volume by component,
  Errors = level-split rate + top-N offenders, Duration has no log-side signal yet — covered
  today by the Postgres health dashboards; if `duration_seconds` events (schema §2.3) become
  widespread, an `avg_over_time(... | json | unwrap duration_seconds ...)` panel is the natural
  Phase-4 addition.

## 7. Open questions for bgo

1. **`job` label value** — draft assumes `job="gps-receivers"`; confirm with IT before the Alloy
   config lands (the dashboard keeps it as an editable variable so a mismatch is a 5-second fix).
2. **Loki datasource + version** — uid `c72827c2-7aa4-45fb-a07f-06ffd1267d9c` verified as the
   only Loki on grafana.vedur.is; is the backing Loki ≥ 2.9 so station/run_id can move to
   structured metadata (and `station` become a dropdown query variable)?
3. **Level value casing** — schema emits Python `WARNING`/`ERROR` (uppercase). The swarm
   streams use lowercase-ish level values; fine while streams are separate, but if IT ever
   normalizes level at the agent, panels #3/#4/#7 hardcode the uppercase forms.
4. **Retention** — per-job Loki retention for `gps-receivers` (proposal: 30–90 d, files remain
   the archive); affects how far back the runs table is useful.
5. **Audit stream** — companion-doc Q5: if `download_audit.jsonl` ships (`source="audit"`),
   the Recent-runs table could join per-run download outcomes; the draft works either way.
6. **Live tail** — confirm the central Loki/Grafana permits tail websockets for the datasource
   (some proxies block it); otherwise the 1 m auto-refresh is the fallback.
7. **`message` vs `msg`** — no panel query references the message field by name (deliberately),
   so companion-doc Q6 can be decided independently of this dashboard.
8. **Local Loki dev container** — add Loki+Alloy to docker-dev now (dashboard testable on the
   laptop, matching the Postgres dashboards' dev story) or push straight to vedur and iterate
   there?

---

*Draft by Claude (Fable 5), 2026-07-14. Reference dashboards pulled live from grafana.vedur.is
via MCP; sync-script and house-style facts verified against the receivers repo at
`/home/bgo/work/projects/gpslibrary/receivers/`.*
