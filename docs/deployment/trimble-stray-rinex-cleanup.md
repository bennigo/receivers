# Runbook — clean stray `.NNo`/`.dat` files from the archive and backfill missing RINEX

**Status:** drafted 2026-07-27, **NOT yet executed**. Blocked on Precondition A.
**Owner:** bgo. Run from the laptop (CLI) against rek-d01/rawdata; heavy steps as `gpsops`.

## What this fixes

The archive's `…/<session>/rinex/` directories contain files named with the **raw**
pattern (`STAN202607260000a.26o`, `…a.dat`) instead of the RINEX short name
(`STAN2070.26D.Z`). They are uncompressed (~33 MB vs ~3.2 MB Hatanaka+Z) and they
**block the daily pipeline**: they are owned by a different uid than `gpsops`, so the
scheduler's conversion fails with

```
[Errno 13] Permission denied: .../rinex/STAN202607260000a.26o
```

while raw `.T02.gz` downloads keep succeeding — daily RINEX stops silently, per station.

> **CORRECTION (2026-07-27, after this runbook was first drafted).** The section below
> claiming an *external* producer is **WRONG** and is kept only so the reasoning is
> traceable. `firstuser` (uid 1000) is **the Docker container user**: our own
> `trimble_native_converter.py` runs Trimble's `convertToRinex.exe` under Wine in Docker
> and its own comment reads *"world-writable so the Docker container user
> (Wine/firstuser) can write output files."* The `cnvtToRINEX` header is therefore our
> pipeline's own output. **Precondition A below is void — there is no rogue host to hunt,
> and the ~1,500/day rate is just the fleet's daily conversions.** The real defect and fix
> are in "Actual root cause" at the end of this document.

### Two distinct producers — do not conflate them

| Strays | Owner | Produced by | Fixed? |
|---|---|---|---|
| `*.dat` (and some `.NNo`), sporadic since 2020 | `gpsops` | **our** Trimble converter staging intermediates in the archive dir | **Yes — commit `5e3093a`** (staging moved to a private tempdir) |
| `*.26o`, ~1,500/day since 2026-07-23 | uid 1000 → `firstuser` over NFS | **external** `cnvtToRINEX 3.14.0` (`PGM / RUN BY / DATE` header says `convertToRINEX OPR`) — *not* the receivers package | **No — still running** |

Our chain is `runpkr00 → teqc → gfzrnx` and never emits `cnvtToRINEX`. That string in a
file's header is proof it did not come from this codebase.

## Measured scope (2026-07-27)

* **4,339** stray files under `/mnt/data/gpsdata/2026` across **54 stations** — the whole
  Trimble fleet (AKUR ALFD ALHV BAUG BJTV BLEI BLON BRIK BRTT DYNY EYVI FEDG FIHO FTEY
  GAKE GFEL GIGO GJFV GJOG GMEY GRAN GRFS GUSK HAUD HEDI HEID HELC HOTJ HSKC HVSK ISAF
  KISA KVIS LANH LAVI MANA MJSK MOFC OFEL RHOF RHOL RJUC RVIT SAVI SIFJ SJUK SKHA SKSH
  STAN SYRF THOC VARG VOFJ). Sessions: `15s_24hr` and `1Hz_1hr`.
* **12,606** `archive_catalog` rows whose `file_path` ends in `.26o`/`.dat`,
  `storage_location = imo_archive`, spanning **2020-08-12 → 2026-07-27**. The catalog
  pollution is therefore much older and wider than the July burst.
* **0** `file_tracking` rows reference these names — only the catalog needs pruning.
* Both archive roots are affected (`/mnt/data/gpsdata` and `/mnt/rawgpsdata`); confirm
  whether they are the same export before double-counting.

---

## Preconditions — do not start until all are true

**A. The external producer is stopped.** *This is the gate.* It writes ~1,500 files/day;
cleaning first means re-contamination within 24 h. It is **not** rek-d01 (`gpsops`) and
**not** bgo's laptop (checked 2026-07-27: no `cnvtToRINEX`/`receivers` process, no
crontab entry, no user timer). It is a third host with uid 1000 and the archive mounted —
check gpsplot and any other workstation/container:

```bash
pgrep -af 'cnvt|convertToRINEX|runpkr|receivers'
crontab -l | grep -iE 'rinex|cnvt|convert'
systemctl list-timers --all | grep -iE 'rinex|gps'
mount | grep -iE 'gpsdata|rawgps'
```

Confirm it has stopped by re-running the timeline and seeing today's count stay flat:

```bash
ssh gpsops@rek-d01.vedur.is \
  'find /mnt/data/gpsdata/2026 -path "*/rinex/*" \( -name "*.[0-9][0-9]o" -o -name "*.dat" \) \
     -printf "%TY-%Tm-%Td\n" | sort | uniq -c | tail -5'
```

**B. `5e3093a` is deployed on rek-d01** (`git pull` + scheduler restart), otherwise our own
converter keeps leaving `.dat` intermediates behind as you clean.

**C. Free space for quarantine.** ~4,300 × ~33 MB ≈ **140 GB** if you move rather than
delete. `/home` on rek-d01 is tight (~82 GB) — stage on `/mnt/data/gpsops_scratch`, never
`/home`. If space is short, skip the quarantine and delete directly (justified: see
"Why deletion is safe" below), but only after Phase 1's inventory is saved.

**D. Not during the daily window.** Downloads run `:01–:20` and backfill `:30–:55`; the
heaviest conversion load is 00:00–06:00 UTC. Run cleanup outside that.

---

## Why deletion is safe

Every stray is *regenerable*: the corresponding raw `.T02.gz` is present in the sibling
`raw/` directory (verified for STAN Jul 19–26). The strays hold real observation data but
are the **wrong artifact** — wrong name, uncompressed, and carrying `MARKER NUMBER = STAN`
(a 4-char id), which violates the DOMES-only policy the pipeline enforces via
`tostools.rinex.domes.domes_or_skip`. Nothing downstream should be consuming them.

Still: **Phase 2 moves, it does not delete**, on the first pass. Delete only after Phase 5
verification passes.

---

## Phase 1 — inventory (read-only, do this even if you skip quarantine)

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT=/mnt/data/gpsops_scratch/stray-cleanup-$STAMP
ssh gpsops@rek-d01.vedur.is "mkdir -p $OUT"

# Full manifest: path, size, owner, mtime — the rollback record.
ssh gpsops@rek-d01.vedur.is \
  "find /mnt/data/gpsdata /mnt/rawgpsdata -path '*/rinex/*' \
     \( -name '*.[0-9][0-9]o' -o -name '*.dat' \) \
     -printf '%p\t%s\t%u\t%TY-%Tm-%TdT%TH:%TM\n' > $OUT/strays.tsv; wc -l $OUT/strays.tsv"
```

Also snapshot the catalog rows that will be pruned:

```bash
receivers health-query -f - <<'SQL' > catalog-strays-$STAMP.txt
SELECT id, storage_location, station, session_type, file_date, file_path
FROM archive_catalog
WHERE file_path LIKE '%.26o' OR file_path LIKE '%.dat'
ORDER BY station, file_date;
SQL
```

**Checkpoint:** `strays.tsv` line count should be ≈ 4,339 (2026) plus older years.

---

## Phase 2 — quarantine (move, per station, reversible)

Permissions note: moving/removing a file needs write on the **directory**, not the file.
The `rinex/` dirs are `gpsops`-owned, so `gpsops` *can* move the `firstuser`-owned strays.
Verify on one station before batching.

```bash
# ONE station first — prove the mechanics.
ssh gpsops@rek-d01.vedur.is bash -s <<'EOF'
set -euo pipefail
OUT=/mnt/data/gpsops_scratch/stray-cleanup-QUARANTINE
S=STAN
find /mnt/data/gpsdata/2026 -path "*/${S}/*/rinex/*" \
     \( -name "*.[0-9][0-9]o" -o -name "*.dat" \) -print0 |
  while IFS= read -r -d '' f; do
    rel=${f#/mnt/data/gpsdata/}
    mkdir -p "$OUT/$(dirname "$rel")"
    mv -n "$f" "$OUT/$rel"
  done
find "$OUT" -type f | wc -l
EOF
```

Then loop the remaining 53 stations the same way. Do it in batches and re-check free space
between batches.

**Alternative — `receivers archive-rm`** (guarded; dry-run by default, validates paths
against the archive layout, passes argv rather than interpolating, re-checks size
server-side, and **prunes the catalog rows for you**). It takes explicit paths only (no
globs), so feed it from `strays.tsv` in chunks, and it needs a raised `--max-size`
because these are ~33 MB, not empty:

```bash
receivers archive-rm --file <rel1> <rel2> …            # dry-run
receivers archive-rm --file <rel1> <rel2> … --max-size 40000000 --yes
```

Prefer `archive-rm` for the final deletion pass precisely because it keeps the catalog in
step — the manual `mv` above does not.

---

## Phase 3 — prune the polluted catalog rows

12,606 rows point at paths that will no longer exist. If you used `archive-rm`, this is
already done for the files it removed; the older (2020-2025) rows still need it.

```sql
-- Inspect first — expect storage_location = imo_archive only.
SELECT storage_location, count(*) FROM archive_catalog
WHERE file_path LIKE '%.26o' OR file_path LIKE '%.dat'
GROUP BY storage_location;
```

Delete via `archive-rm`'s catalog path where possible. For a bulk SQL prune, remember
**both catalog hosts** — a plain delete on rek-d01 leaves pgdev diverged
(see [[archive-catalog-misdating-and-pgdev-divergence]]); use `--catalog-prod` /
the `[archive] catalog_hosts` set so the prune fans out.

---

## Phase 4 — backfill the missing RINEX

Only after Phase 2, since the strays are what blocks conversion.

```bash
# Per station, for the affected range. Dry-run first.
receivers rinex STAN --start 20260721 --end 20260727 --session 15s_24hr --dry-run
receivers rinex STAN --start 20260721 --end 20260727 --session 15s_24hr
```

Find each station's gap from the catalog rather than guessing:

```sql
SELECT station, session_type, max(file_date) AS newest_good
FROM archive_catalog
WHERE file_category = 'rinex' AND file_path LIKE '%D.Z'
GROUP BY station, session_type
ORDER BY newest_good;
```

Scale note: 54 stations × ~5 days × ~20 s ≈ **1.5 h** for `15s_24hr` alone; `1Hz_1hr` is
~24× the file count — run it separately, in the backfill window, and **log what you skip**.
Do not fold this into the scheduler's window; run it deliberately.

---

## Phase 5 — verify

```bash
# 1. No strays left.
ssh gpsops@rek-d01.vedur.is \
  'find /mnt/data/gpsdata/2026 -path "*/rinex/*" \( -name "*.[0-9][0-9]o" -o -name "*.dat" \) | wc -l'   # expect 0

# 2. Proper artifacts exist for the backfilled days.
ssh gpsops@rek-d01.vedur.is 'ls /mnt/data/gpsdata/2026/jul/STAN/15s_24hr/rinex/ | tail'

# 3. A fresh conversion now succeeds where it used to EACCES.
ssh gpsops@rek-d01.vedur.is 'receivers rinex STAN --start 20260726 --end 20260726 --session 15s_24hr --dry-run'

# 4. Catalog is consistent and the identity probe is clean.
receivers archive-audit STAN --identity --years 2026
```

Then let one nightly cycle run and confirm `15s_24hr_rinex` rows appear again:

```sql
SELECT sid, session_type, max(file_date) FROM file_tracking
WHERE session_type LIKE '%_rinex' GROUP BY sid, session_type ORDER BY 3;
```

---

## Rollback

* **Phase 2 (quarantine):** move files back from
  `/mnt/data/gpsops_scratch/stray-cleanup-QUARANTINE/<rel>` to `/mnt/data/gpsdata/<rel>`.
  `strays.tsv` is the authoritative list.
* **Phase 3 (catalog prune):** rows are regenerable — `receivers archive-reindex` /
  `catalog-backfill-local` rebuild from the files on disk. The snapshot in
  `catalog-strays-$STAMP.txt` holds the deleted ids if an exact restore is needed.
* **Phase 4 (backfill):** re-conversion is idempotent; a bad output can simply be
  reconverted from the untouched `.T02.gz`.

## Open items

1. **Identify the external producer** (Precondition A) — the single blocker.
2. Decide whether the pre-2026 catalog rows (back to 2020-08-12) get pruned in the same
   pass or a separate one; they reference long-gone files and are cosmetic but they skew
   any catalog-completeness metric.
3. Consider a guard: have the integrity checker's identity probe flag a raw-named file in
   a `rinex/` directory as a finding, so this cannot silently recur. It already detects
   stray/stacked RINEX — this is a natural third case.

---

## Actual root cause (supersedes the "external producer" analysis above)

`src/receivers/rinex/trimble_native_converter.py::_run_conversion` (~line 268):

```python
rinex_file = self._find_output_file(docker_out, observation_date)
final_file = output_dir / rinex_file.name   # output_dir IS the archive rinex/ dir
shutil.move(rinex_file, final_file)         # raw-derived name, e.g. STAN202607260000a.26o
self._normalize_epoch_lines(final_file)     # edited in place, inside the archive
```

NetR9 `.T02` conversion runs Trimble's `convertToRinex.exe` under Wine in Docker. Two
consequences:

1. `docker_out` lives under `~/.cache/...` (home fs) while the archive is NFS — different
   filesystems, so `shutil.move` degrades to copy+delete and the resulting archive file
   **keeps the container's uid 1000** (`firstuser`).
2. The raw-named, uncompressed `.26o` is *deliberately* staged in the archive before
   Hatanaka compression. If the downstream compress/cleanup does not complete, it stays —
   and the next run must `open()` that uid-1000 path for write as `gpsops`:
   **`[Errno 13] Permission denied`**. Daily RINEX then stops silently for that station
   while raw downloads keep succeeding.

**Commit `5e3093a` does NOT fix this.** It moved staging to a tempdir in
`trimble_converter.py` (the runpkr00 → teqc → gfzrnx path), which `.T02` does not use.
Verified: after deploying `5e3093a` to rek-d01, the identical EACCES still occurs.

### The fix to write

In `trimble_native_converter.py`:
* Run `_normalize_epoch_lines` **and** the Hatanaka compression in the temp dir.
* Move **only the final `.NND.Z`** into `output_dir`.
* Create that final file as the running user (copy content into a fresh file) so container
  ownership is never inherited.

### Effect on this runbook

* **Precondition A is void** — nothing external to stop.
* Cleanup is still needed for the ~4,339 existing strays, but it is no longer racing a
  producer; it should simply follow the code fix so the strays do not come back.
* Order becomes: **fix `trimble_native_converter.py` → deploy → clean strays → backfill.**
