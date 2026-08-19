# External data download — `receivers download --external`

Status: **design + implementation in progress (2026-08-19)**

## Motivation

Some stations are not downloaded from their own receivers — the data arrives
from a third-party server (LMI/Landmælingar, Háskóli Íslands, …). Today this
is a pile of legacy `get*.sh` cron scripts on rek2, outside the receivers
codebase, each hard-coded to one provider's FTP layout. `download_method`
in stations.cfg was meant to describe this but is dead code.

This design replaces the scripts with a general, gtimes-templated external
fetcher inside `receivers download`, so a new provider is a config entry, not
a new shell script.

## Config — per-station fields in stations.cfg

```ini
[MYVA]
operational_status = external            # triggers external fetch (vs direct)
external_data_type = rinex               # rinex | raw  (downstream handling)
external_url_template = ftp://ftp.lmi.is/.gnsmart_data/15s_data/%Y/%j/{station}%j0.%yO
external_frequency   = 1D                # gtimes/pandas frequency
external_username    = anonymous         # optional; clear text (internal IMO)
external_password    =                   # optional
```

The **whole remote structure is one template string** — `%Y`, `%j`, `%m`,
`%d`, `#b`, `#Rin2`, `#hourl`, `#gpsw` (everything `gtimes.datepathlist()`
accepts) plus a `{station}` placeholder. A provider whose station id lives in
the directory rather than the filename, or that needs RINEX session letters,
works from the same code path — no branching.

Split `external_dir_template`/`external_file_template` is deliberately *not*
done until a provider needs it; the single-string form is strictly more
general.

## Flow

`receivers download STATION --external` (manual) and the scheduler
(auto-routing `operational_status = external`) both call:

1. Read `external_url_template` + `external_frequency` from stations.cfg.
2. `gtimes.datepathlist(template, frequency, start, end)` → ordered remote URLs.
3. Fetch each URL via the protocol implied by the scheme (ftp/http/https/sftp).
4. Route downstream by `external_data_type`:
   - `rinex` → straight to the archive (already RINEX, skip conversion)
   - `raw`    → existing SBF→RINEX converter, then archive

This reuses `cmd_download`'s existing time-range / session-frequency
resolution, so `--start/--end/--days/--session` behave identically to the
direct path.

## Providers

| Provider | Station(s) | Template (illustrative) |
|---|---|---|
| LMI (Landmælingar) | MYVA | `ftp://ftp.lmi.is/.gnsmart_data/15s_data/%Y/%j/{station}%j0.%yO` |
| UI (Háskóli Íslands) | KRAC, THRC, TORK | `ftp://<ui>/<layout>/%Y/%j/{station}%j0.%yO` (to confirm) |

## Sequencing

1. ✅ Stop redundant rek2 `get*.sh` crons (AKUR/HEID/ISAF/GUSK removed;
   getmyva.sh + daily_lmi_fetch.sh kept until the fetcher replaces them).
2. Implement external fetcher + `--external` flag + scheduler auto-route.
3. Migrate MYVA, then KRAC/THRC/TORK (fixing the broken UI pull), then remove
   getmyva.sh + daily_lmi_fetch.sh from rek2.
4. Eventually expose `external_url_template` from TOS (the cfg-from-TOS goal).

See also: `operational-status-model.md` (the `operational_status` /
`external_data_type` attributes this builds on).
