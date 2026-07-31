# Station Onboarding — TOS Device Intake Walkthrough

How to bring a GNSS station into TOS end-to-end using the `receivers cfg` verbs:
register every device (receiver, antenna, monument, telemetry), join them to the
station, and either flip it to RTCM3 stream capture or give it a download config.

This is the **walkthrough**. For the full flag reference of each verb, see the
"Configuration Reconciliation" and cfg-verb sections of
[`receivers/CLAUDE.md`](../../CLAUDE.md) and each verb's `--help` (the epilogs carry
copy-pasteable examples). This doc covers *the order to run them in and the traps
between steps* — not a second copy of the flag tables.

Companion design docs:
- [`config-data-flow.md`](config-data-flow.md) — where stations.cfg lives and how it syncs.
- Campaign Sessions & Continuity Transitions — historical campaign occupations (separate,
  metadata-only feature). Vault: `1781609764-campaign-continuity-transitions-feature`.

> **Live writes are the user's to run.** Every `cfg` verb below defaults to dry-run.
> Build the command, preview it with the default dry-run, then run with `--no-dry-run`
> yourself. The examples show `--no-dry-run` only on the *commit* step.

---

## The two onboarding shapes

| Shape | When | Acquisition |
|-------|------|-------------|
| **Existing permanent station, new/changed device** | swap, upgrade, reconcile | unchanged |
| **Brand-new continuous station** | a site that never existed in TOS (e.g. VOTT) | `stream` or `download` |

Both follow the same device-intake order. A brand-new station just needs the TOS
*station entity* created first (web UI, or it pre-exists from a colocated SIL site —
but note VOTT was deliberately given its **own** site, not shared with the SIL station).

---

## The intake order

Run these in sequence. Each verb creates a TOS device and joins it to the station over
an **open** session (`status = virkt`). The session-split gotcha (below) is why the
*dates must line up*.

```
                    ┌─────────────────────────────────────────────┐
  receiver  ───────▶│ 1. add-receiver  (warehouse intake, probed)  │
                    │    └▶ move-device --to STATION (install)      │
                    ├─────────────────────────────────────────────┤
  antenna   ───────▶│ 2. add-antenna   (+radome if not NONE)       │
                    ├─────────────────────────────────────────────┤
  monument  ───────▶│ 3. add-monument  (mark→ARP height)           │
                    ├─────────────────────────────────────────────┤
  telemetry ───────▶│ 4. replace-modem / replace-sim              │
                    │    discover-phone (learn SIM MSISDN)         │
                    ├─────────────────────────────────────────────┤
  go-live   ───────▶│ 5. stream flip  OR  download config          │
                    └─────────────────────────────────────────────┘
```

### 1. Receiver — `add-receiver` then `move-device`

A receiver is **probed**: connect over USB/WiFi/IP and the verb auto-extracts
serial/model/firmware (SBF block 5902 for PolaRX5, vendor HTTP otherwise),
IGS-normalises the model, and registers it as a device in the **warehouse**
(`B9 - Kjallari - Jörð` by default).

```bash
# Bench intake (defaults: Jarðeðlismælihópur owner, B9 location, today):
receivers cfg add-receiver --probe 192.168.3.1 --date-start 2026-05-12
```

Then **install** it at the station with `move-device --to STATION`. A 4-char marker as
`--to` triggers the full station-install workflow (TOS join move + a `Breyting` vitjun
with auto-derived *"Skipt um móttakara"* text + a `stations.cfg` update):

```bash
receivers cfg move-device --serial 4881... --to VOTT \
    --date 2026-05-01T00:00:00 --participants bgo@vedur.is --no-dry-run
```

`move-device` **refuses** to install onto a station that already has an open
`gnss_receiver` child — move the old one out to the warehouse first (a station-less
`--to` value is treated as a warehouse name and runs the bookkeeping-only path).

> A station-destination move also fills the station's **position** attributes
> (lat/lon/height → TOS) from `stations.cfg`, since stations.cfg is ground truth for
> surveyed coordinates. See CLAUDE.md "Install-attribute fill" for the change/correct
> semantics.

### 2. Antenna — `add-antenna`

Antennas **can't be probed**; identity comes from flags. The verb creates an `antenna`
device (and a separate `radome` device when `--radome` is not `NONE`) and joins both to
the station.

```bash
receivers cfg add-antenna --station VOTT --model SEPPOLANT_X_MF \
    --antenna-height 0.0083 --date-start 2026-05-01 --no-dry-run
```

- `--model` must match an accepted IGS name **exactly** (case-insensitively). The name is
  written verbatim into TOS and from there into every RINEX header, so a near-miss is not
  a cosmetic problem. `ANTENNA_IGS`/`RADOME_IGS` are a hand-maintained subset of IGS
  `rcvr_ant.tab` covering what the fleet runs, so check before you type:

  ```bash
  receivers cfg add-antenna --list-models      # antennas + radomes, offline, no TOS call
  receivers cfg replace-antenna --list-models  # same
  receivers cfg replace-radome --list-models   # radomes only
  ```

  The flag short-circuits before the station lookup, so it needs no `--station` and no
  credentials — which matters because the verbs resolve the station against TOS *before*
  validating the model, so you cannot otherwise provoke the "known models" error offline.
  An unlisted model means adding it to `tostools/standards/igs_equipment.py` (this is how
  `SEPPOLANT_X_MF`/`_SF` and `AS-ANT3BCAL` got in) — not a different flag.
- **Unknown serial?** Omit `--serial`; you get a synthetic `antenna-<STID>-<YYYYMMDD>`
  placeholder (mirrors the fleet `radome-REYK-20130502` convention).
- `--antenna-height` is the ARP height (RINEX `ANTENNA: DELTA H`). This belongs to the
  antenna, **not** the monument — see the monument_height gotcha below.

`add-antenna` is **intake only**: it refuses when the station already has an open antenna,
because two open antennas make `current_session()` — and every RINEX header, the stream
SKL and each station.info line derived from it — ambiguous. To swap one, use
`replace-antenna` (next).

#### 2b. Swapping an antenna later — `replace-antenna`

```bash
# Same radome re-fitted to the new antenna (the usual field case):
receivers cfg replace-antenna --station ISAK --model TRM115000.10 \
    --serial 1441234567 --antenna-height 0.0083 --date 2026-07-30 \
    --participants bgo@vedur.is --no-dry-run
```

Retires the open antenna (closes its join at `--date`), creates + joins the new one,
writes a *Breyting* vitjun, and updates `stations.cfg` — `antenna_type`,
`antenna_serial`, `antenna_height`, `rinex_config_valid_from`, plus `antenna_radome`
only when `--radome` was given. Retire-before-create is deliberate: a mid-run failure
leaves the station momentarily antenna-less (honest, recoverable) rather than with two
open antennas.

- **The radome follows the antenna.** It is screwed on, so ~95% of swaps take both down
  and put both up. Default (no flag): the old radome's join is closed and a **new** radome
  device is created carrying the **same model**. Four ways to say it:

  | Flags | Effect |
  |-------|--------|
  | *(none)* | same model, new unit — model carried forward from the old radome |
  | `--radome CODE` | a different radome type went on |
  | `--radome NONE` | the new antenna is bare |
  | `--keep-radome` | the same physical radome was re-fitted; TOS and cfg untouched |

  The carried-forward model is an *inferred* write, so it's named in the dry-run plan
  (`SCIS (new unit, model carried forward)`) — check it before committing. With no radome
  in TOS **and** no `--radome`, nothing is created and cfg `antenna_radome` is left alone:
  the run made no radome decision, and asserting `NONE` from a TOS gap would clobber a
  real cfg value on a station whose radome was simply never registered.
- **`--antenna-height` is the new ARP**; the cfg `antenna_height` composite is derived as
  ARP + the open monument's `monument_height` (gotcha B below). With no monument to read,
  the verb **raises** rather than writing a bare ARP — override via `--cfg-antenna-height`.
- `--warehouse` reparents the old antenna **and the old radome** (they come off the mast
  together); **off by default**, because retired kit is as often scrapped as returned and a
  wrong location claim is indistinguishable from a real one afterwards. `--old-status
  "bilað"` records why it came off.
- Refuses when the station has **two** open antennas (a legacy `add-antenna --force`, or a
  web-UI edit). Clear the stale one first:
  `receivers cfg close-join --station ISAK --subtype antenna --date … --no-dry-run`.

#### 2c. Radome only — `replace-radome`

Antenna and radome are physically joined but modelled as independent TOS device children,
so a radome does get replaced alone: cracked, snow-damaged, or upgraded to a different type
over the same antenna.

```bash
receivers cfg replace-radome --station ISAK --model SNOW \
    --date 2026-07-30 --participants bgo@vedur.is --no-dry-run
```

Retires the open radome, creates + joins the new one, writes a vitjun
(*"Skipt um raðhlíf: SCIS → SNOW"*), and updates cfg `antenna_radome` — **only** that field:
a radome carries no ARP offset, so swapping one cannot change the mark→ARP composite, and
`antenna_height` is deliberately not recomputed.

- With **no** open radome it still creates and joins one — the "a radome was fitted where
  there wasn't one" case, mirroring how `replace-modem` handles a station with no modem.
- `--model NONE` is the inverse: old join closed, nothing created, cfg records `NONE`.
- Refuses on two open radomes, pointing at `close-join --subtype radome`.

`close-join` is the non-destructive sibling of `delete-join` — it ends a join that was
real and is now over (sets `time_to`, history preserved), where `delete-join` removes a
row that should never have existed. Prefer `--station/--subtype`, which resolves the
device *and* verifies the join is open; `--id ID_CONNECTION` patches a row directly with
no open-check (TOS exposes no get-join-by-id), so inspect it with `tos device show` first.

### 3. Monument — `add-monument`

The monument is the survey mark. It carries the **`monument_height`** (mark → ARP
offset), one per height epoch.

```bash
receivers cfg add-monument --station VOTT --height 0.0 \
    --date-start 2026-05-01T00:00:00 --no-dry-run
```

Monuments have no model and can't be probed; unknown serial → synthetic
`monument-<STID>-<YYYYMMDD>`.

### 4. Telemetry — `replace-modem`, `replace-sim`, `discover-phone`

The router is a `modem_gsm` device child of the station; the **IP lives on the SIM**, not
the modem. Use `replace-modem` for the router and `replace-sim` for the SIM/IP.

```bash
receivers cfg replace-modem --station VOTT \
    --new-serial 6001312345 --new-model "Teltonika RUT241" \
    --router-type Teltonika --date 2026-05-01 --no-dry-run
```

`replace-modem` can `--probe HOST` a reachable Teltonika to auto-extract
serial/model/mac via the RutOS REST API.

A SIM can't read its own MSISDN locally. `discover-phone` makes the field router text a
catcher mobile so you read the number off that phone; works on legacy routers (RUT240)
whose REST API is off, because it goes over SSH `gsmctl`:

```bash
receivers cfg discover-phone --host 10.4.2.163 --to +3548XXXXXX --no-dry-run
# or, if SMS is flaky, USSD straight to the network:
receivers cfg discover-phone --host 10.4.2.163 --ussd '*XXX#' --no-dry-run
```

`phone_number` is **optional** — a station is complete without it (VOTT was finished
pending Síminn's record of the MSISDN; the dead modem SMS subsystem had no USSD).

### 5. Go-live — stream flip or download config

- **Stream capture** (RTCM3 via BNC): set `acquisition_mode = stream` for the station in
  `gps-config-data/stations.cfg`, push, and deploy. **The stream RINEX header is built
  exclusively from TOS** (no stations.cfg fallback) — which is the whole reason steps
  1–3 must produce complete TOS device records (vault note
  `reference_stream_capture_rtcm_bnc` has the BNC/RTCM3 details).
- **Download** (FTP/HTTP polling): the default. Push a `stations.cfg` entry with
  `acquisition_mode = download` (or leave unset) and the receiver's IP/ports/session
  layout, then `rec-config` the receiver if it needs a session profile.

---

## Gotchas — read before you commit

### A. Session-split alignment (midnight vs noon)

If a station's receiver and antenna joins don't share the **exact same instant**, TOS's
`_build_history_from_connections` splits them into separate sessions and
`current_session()` (hence the stream SKL) sees only one device — RINEX headers come out
incomplete.

- Bare `YYYY-MM-DD` is promoted to **12:00 noon** by `move-device`, `add-antenna`, and
  `add-monument` (aligned deliberately — fix `00e500e`). Pass the **same date string** to
  all co-installed devices and they land in one session.
- A full ISO datetime (`YYYY-MM-DDTHH:MM:SS`) is preserved exactly — use it when you need
  a specific instant, but then use it *consistently* across all the day's devices.
- If a station already split, repair it with `cfg correct-date` (this is how SEY9 was
  fixed after a midnight/noon mismatch).

### B. `monument_height` vs `antenna_height` scoping

These are **two different TOS attribute codes**:

- `antenna_height` → the **antenna** device (ARP offset, `add-antenna --antenna-height`).
- `monument_height` → the **monument** device (mark→ARP, `add-monument --height`).

Writing the antenna-scoped code on a monument entity gets a **400 from TOS** (the bug
caught in `bd58a94`). The `stations.cfg` `antenna_height` field is a *composite* (antenna
ARP + monument height) that TOS splits across the two entities — which is why a single cfg
number can't be written back to TOS directly.

### C. `find_station` reindex lag

A freshly-created station was invisible to the CLI until TOS reindexed, because
`find_station_by_marker` queried the **lagging** `/basic_search/` index. It now hits the
**live** `/entity/search/station/{domain}/` endpoint (what the web UI uses) with
`/basic_search/` as fallback. If a just-created station still isn't found, you're likely
on old code — pull, or check the search endpoint.

### D. Legacy-router SSH

Legacy Teltonika units (RUT240) have their REST API off, so `--probe` won't reach them.
The SSH path (`discover-phone`) still works via `gsmctl`. Their busybox has **no
`timeout` applet**, so the tooling uses a client-side `_ssh_run_bounded()` bound instead
— don't expect a server-side `timeout` to be available if you extend this.

---

## Worked example — VOTT (brand-new continuous station)

VOTT was created from scratch this way (2026-06-16): station entity (id 21559) on its own
site `Vöttur GNSS stöð` (21558, deliberately separate from the colocated SIL station),
then receiver 4881, antenna 21563 (`SEPPOLANT_X_MF`), SIM 21565 (Síminn), modem 18213
(RUT240), monument 21567. SEY9 was the parallel case: an *existing* station migrated to
stream capture by registering its receiver + antenna in TOS and flipping
`acquisition_mode = stream`.

> VOTT's pre-2026 **campaign occupations** (2012–2016) are out of scope for this
> operational onboarding — they are pure GAMIT/processing metadata handled by the
> separate campaign-import feature (vault note
> `1781609764-campaign-continuity-transitions-feature`).

---

## Quick checklist

- [ ] Station entity exists in TOS (web UI for a brand-new site).
- [ ] `add-receiver` → `move-device --to STATION` (one open `gnss_receiver` child).
- [ ] `add-antenna` (+ radome), same install date as the receiver.
- [ ] `add-monument`, same date, `monument_height` (not antenna_height).
- [ ] Telemetry: `replace-modem` / `replace-sim`; `discover-phone` for the MSISDN.
- [ ] All co-installed devices share one TOS session (verify with `cfg history STATION`).
- [ ] Go-live: `acquisition_mode = stream` (TOS-only header) **or** download config; push
      to gps-config-data and deploy.
