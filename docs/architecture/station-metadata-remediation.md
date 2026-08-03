# Station metadata remediation — the deterministic pipeline

Status: runbook (2026-08-03). Distilled from taking ISAK end-to-end in one
session: TOS metadata wrong in six ways → archive headers stale → archive stuck
on RINEX 2 → all three corrected and verified.

Companion to [`metadata-extraction-deterministic-verbs.md`](metadata-extraction-deterministic-verbs.md)
(which covers *reading* the four authorities) and
[`station-onboarding.md`](station-onboarding.md) (which covers a *new* station).
This one covers **repairing an existing station** and is meant to be run by name,
in order, without rediscovering the traps each time.

## The invariant this pipeline establishes

> For every archived observation, the RINEX header states what was actually
> installed on that date, and TOS agrees.

Three things can break it, and they must be fixed in this order — each phase
depends on the previous one being true:

| # | Phase | Fixes | Package |
|---|-------|-------|---------|
| 1 | TOS correctness | the metadata of record | tostools |
| 2 | Header retrofit | archived headers vs TOS | receivers |
| 3 | Format upgrade | R2 → R3 where raw allows | receivers |

**Order is load-bearing.** Phase 2 writes TOS's answer into thousands of files;
running it before Phase 1 propagates wrong metadata at scale. Phase 3 regenerates
headers from TOS again, so it must not run before Phase 1 either.

---

## Phase 1 — TOS correctness (tostools)

```bash
tos audit missing-attributes <SID> --history --subtypes antenna \
    --station-info data/station_config/station.info.sopac.apr05 \
    --triage <sid>/<sid>_antennas_$(date +%Y%m%d).txt
tos audit apply <file>            # dry-run
tos audit apply <file> --apply --commit
```

Then re-run the audit until it reports **CLEAN**.

**Gates before applying any triage:**

1. **`tos audit timeline <ids…>` first.** The generator's suggestions are only
   as good as your knowledge of whether each device is permanently installed.
   Campaign kit that toured other marks needs closed periods bounded to each
   occupation — see the traps table.
2. **Check the covering monument.** Antenna geometry in TOS is the **ARP delta
   above the monument**, never the `station.info` composite.
3. **`--all` is a read-only survey.** It refuses `--triage` by design; the
   per-station verb is how anything gets fixed.

Adjacent one-offs that belong to this phase:

```bash
tos audit orphans --subtype antenna          # I1: every device has exactly one open join
tos audit apply <adopt-file> --apply         # ACTION <id> create-join 4 <removal-date>
```

**B9-by-default policy (bgo, 2026-08-03):** a device always has a parent. When it
comes off a mark it goes to the warehouse (`id_entity=4`) and stays until
*explicitly* discontinued. Joinless is not a state we mean. `decommission` is
unchanged — that verb *is* the explicit write-off.

---

## Phase 2 — header retrofit (receivers)

```bash
nohup receivers rinex <SID> --fix-headers -s <START> -e <END> --session <s> \
    --parallel --push --catalog-prod --cleanup --archive-old \
    > ~/.cache/gps_receivers/logs/retrofits/<sid>_<s>_fix.log 2>&1 &
```

Rewrites headers **in place** without re-decoding raw. Non-hardware fields are
auto-fixed; hardware fields are flag-only unless opted in.

**Do not pass `--correct-receiver` on the strength of its own flags** — see the
`software_version` trap. Read an actual header first.

Fields typically corrected: `MARKER NUMBER` (4-char id → IERS DOMES),
`OBSERVER / AGENCY` (→ English form for EPOS).

---

## Phase 3 — format upgrade (receivers)

Only for dates whose raw can express multiple constellations — `.T02` (NetR9)
and `.sbf` (Septentrio). `.T00` NetRS stays R2 by policy.

```bash
nohup receivers rinex <SID> -s <START> -e <END> --session <s> --from-archive \
    --version 3 --naming short --parallel --force \
    --push --catalog-prod --cleanup --backup-old \
    > ~/.cache/gps_receivers/logs/retrofits/<sid>_<s>_r3.log 2>&1 &
```

`--force` is **required**: every date already has a product, and resume-skip
would otherwise protect all of them.

Establish the era boundaries from the raw itself, not from assumption:

```bash
for y in <years>; do echo -n "$y: "; \
  ls /mnt/rawgpsdata/$y/*/<SID>/<session>/raw/ | \
  grep -oE "\.(T00|T02|sbf)(\.gz|\.Z)?$" | sort | uniq -c | tr "\n" " "; echo; done
```

---

## Verification (all three phases)

```bash
# 1. audit clean
tos audit missing-attributes <SID> --history --subtypes antenna

# 2. headers era-correct — sample one file per hardware era
CRX2RNX - < file.D | grep -E "RINEX VERSION|MARKER NUMBER|OBSERVER|REC # |ANT # "

# 3. catalog parity across hosts (NOT optional — see the divergence trap)
for h in rek-d01.vedur.is pgdev.vedur.is; do
  receivers health-query --host $h \
    "SELECT count(*), min(file_date), max(file_date) FROM archive_catalog
     WHERE station='<SID>' AND session_type='<s>' AND file_category='rinex'"
done
```

Counts must be **equal across hosts** and match the file count.

---

## Traps — each of these cost real time on 2026-08-03

| Trap | Symptom | Rule |
|---|---|---|
| **`--parallel` needs `-s/-e`** | logs `only one chunk — running sequentially` | With `--all` the archive is ONE chunk. Pass an explicit span to get calendar-year chunks. |
| **`software_version` comparator** | 200/202 files flagged "receiver mismatch" | `gps_rinex.py:383` compares against the 2-decimal legacy field, so a correct `5.6.0` loses to `5.60`. **Read a header before trusting the flag.** `--correct-receiver` would write `5.60` over correct data. |
| **Triage emits open periods** | campaign antenna gets a forever-truth value | A device that LEFT needs `add-attribute-period … <from> <to>`. Fixed in the generator, but check any pre-2026-08-03 triage file. |
| **station.info is a composite** | antenna height ~1 m too large | Antenna entity = ARP delta above monument. Subtract `monument_height` — unless it reads `0.000`, which means never measured, not flush. |
| **Same-day move** | a period closed at `date_to == date_from` | `move_device` writes close+open on ONE date, so the next site's attributes start on this removal date. Detector uses `>=`; the verb refuses the inversion. |
| **resume-skip** | re-conversion silently converts nothing | Every date already has a product → pass `--force`. |
| **catalog_hosts (MJ-372)** | reindex silently skipped, hosts diverge | Confirm `✓ catalog preflight: 2 host(s) reachable` in a fresh process before trusting the push. |
| **deploy mid-run (MJ-377)** | `AttributeError` from mixed modules | Never deploy to rek-d01 while a long run is in flight. Restart the run, don't debug it. |
| **`pkill -f <pattern>`** | your own SSH wrapper dies | The wrapper's command line contains the pattern. Match on the interpreter path, or check `ps` and kill by PID. |
| **Un-regenerable files** | RINEX exists with no raw | `rinex_org` preservation is automatic and gated on `check_regenerable()`; it refuses to proceed if preservation fails. `--archive-old` is a *separate* transient backup that `--cleanup` removes once TOS-confirmed. |

**Log location:** `~/.cache/gps_receivers/logs/retrofits/`, never `~gpsops` root.
The todo #67 runbook says `~/<sid>_1hz_fix.log`; following it left 22 stray logs.

---

## Worked example — ISAK, 2026-08-03

| Phase | Result |
|---|---|
| 1 — TOS | 47 writes across 5 triage files; 6 historical antennas + receiver chain corrected; 4 orphans adopted into B9; audit **CLEAN** |
| 2 — headers | 8,738 scanned → **8,524 fixed, 208 skipped, 6 errors** (3h27m, 26 chunks × 6 workers). All 6 errors were 3–115 byte corrupt stubs → `archive-rm`. Catalog parity exact: **8,738 = 8,738** |
| 3 — R3 | 2013-03-27 → 2026-06-01 (`.T02` + `.sbf` eras), 14 chunks × 7 workers |

Phase 3's justification was empirical, not cosmetic: the archived 2015 NetR9 file
is `2.11 … G (GPS)` while the same raw re-converts to `3.04 M (MIXED)` carrying
both `G` and `R` observation types. **R2 was dropping GLONASS entirely.**

---

## Not yet deterministic

Judgement points that still need a human, and are the candidates for the next
round of verbs:

- **Which devices are campaign kit** — currently read off `tos audit timeline` by
  eye. A `--campaign` classifier could bound periods automatically.
- **Monument coverage per occupation** — handled inside the audit now, but the
  `0.000`-means-unrecorded case still surfaces as prose for an operator to read.
- **Era boundaries for Phase 3** — derived by hand from raw extensions; could be
  a `receivers rinex --constellation-capable-span <SID>` query.
- **Fleet fan-out** — blocked on the `software_version` comparator fix and on
  todo #70 (auto-retry of transient failures), without which every station needs
  a manual sweep for the ELEY-class `compress` errors.

---

*Last reviewed: 2026-08-03*
