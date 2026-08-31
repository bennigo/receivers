#!/usr/bin/env bash
# sync-config.sh — pull gps-config-data from git and deploy safe config files
#
# Runs as bgo (owns the repo; bgo is in the gpsops group so it can write the
# config dir). Invoked by gps-config-sync.timer every 10 minutes.
#
# Safety guarantees:
#   - network failure on git fetch → exit silently, local config unchanged
#   - pull failure (diverged branch) → logged, no files touched
#   - database.cfg is NEVER synced — it contains server-local credentials
#   - templated configs (receivers.cfg, scheduler.yaml) are RENDERED per-env via
#     deploy.py so credentials stay out of git (the repo tracks only *.template);
#     render failure → fall back to any raw tracked file, never breaks the sync
#   - no-drop guard: a rendered file is never deployed if it would remove a
#     [section] present in the live config (protects against a drifted template
#     silently dropping receiver config)
#   - cmp before cp — only touches files that actually changed (avoids
#     spurious mtime bumps that would trigger unnecessary scheduler reloads)
#
# CONVERGENT, not event-driven: the file comparison below runs on EVERY tick,
# whether or not the fetch brought in new commits. It used to `exit 0` as soon
# as HEAD == upstream, so files were only ever deployed as a side effect of
# *pulling* — and any drift that arose some other way (the clone pulled by hand
# outside this script, an earlier cp that failed, someone editing the deployed
# copy) was never reconciled. The timer then reported success every 10 minutes
# forever while production stayed stale: on 2026-07-28 the deployed
# stations.cfg was still the 2026-07-14 revision, two weeks behind a clone that
# was perfectly up to date, which silently broke NAMC's downloads (its FTP auth
# mode is chosen from receiver_firmware_version in stations.cfg).

set -euo pipefail

REPO=/home/bgo/git/gps-config-data
CONFIG_DIR=/home/gpsops/.config/gpsconfig

# Fail silently if git server unreachable — local config stays in effect.
# Note we do NOT exit here on failure: a stale clone can still be reconciled
# against the deployed copy, which is the whole point of being convergent.
git -C "$REPO" fetch --quiet 2>/dev/null || true

LOCAL=$(git -C "$REPO" rev-parse HEAD)
REMOTE=$(git -C "$REPO" rev-parse '@{u}' 2>/dev/null || echo "$LOCAL")

# Only pull when there is something new. A pull failure is logged but must not
# skip the reconcile — deploying the clone we already have is still correct.
if [ "$LOCAL" != "$REMOTE" ]; then
    if ! git -C "$REPO" pull --ff-only --quiet; then
        logger -t gps-config-sync \
            "ERROR: pull failed (repo may have diverged) — deploying current clone anyway"
    fi
fi

NEW_REV=$(git -C "$REPO" rev-parse --short HEAD)

# Render env-specific templated configs (receivers.cfg, scheduler.yaml, …) so they
# deploy on change like the static configs, while credentials stay out of git
# (templated per-env). Render to a temp dir; on failure, fall back to raw repo files.
RENDER=$(mktemp -d)
trap 'rm -rf "$RENDER"' EXIT
ENV="${GPS_CONFIG_ENV:-$(hostname -f 2>/dev/null || hostname)}"
[ -f "$REPO/environments/$ENV.env" ] || ENV=production
if ! python3 "$REPO/deploy.py" --env "$ENV" --target "$RENDER" --repo "$REPO" >/dev/null 2>&1; then
    logger -t gps-config-sync "WARN: deploy.py render failed (env=$ENV) — using raw repo files"
    RENDER=""
fi

# Files safe to auto-sync.
# database.cfg is intentionally absent — it holds server-local DB credentials.
SYNC_FILES=(stations.cfg receivers.cfg scheduler.yaml icinga.cfg station_areas.yaml sync.yaml agencies.yaml)

CHANGED=()
for f in "${SYNC_FILES[@]}"; do
    # Prefer the rendered (per-env) version when present, else the raw tracked file
    src="$REPO/$f"
    [ -n "$RENDER" ] && [ -f "$RENDER/$f" ] && src="$RENDER/$f"
    dst="$CONFIG_DIR/$f"
    [ -f "$src" ] || continue

    # No-drop guard: never deploy a file that drops an INI [section] present in the
    # live config — a drifted template must not silently remove receiver config.
    if [ -f "$dst" ]; then
        missing=$(comm -23 \
            <(grep -oE '^\[[^]]+\]' "$dst" | sort -u) \
            <(grep -oE '^\[[^]]+\]' "$src" | sort -u) || true)
        if [ -n "$missing" ]; then
            logger -t gps-config-sync \
                "WARN: $f render would drop sections ($(echo "$missing" | tr '\n' ' ')) — keeping current"
            continue
        fi
    fi

    # Only copy when content differs — preserves mtime on unchanged files so
    # the scheduler's config watcher does not reload unnecessarily
    if ! cmp -s "$src" "$dst" 2>/dev/null; then
        cp "$src" "$dst"
        CHANGED+=("$f")
    fi
done

if [ ${#CHANGED[@]} -gt 0 ]; then
    logger -t gps-config-sync "Synced to $NEW_REV: ${CHANGED[*]}"
fi

# Drift alarm: a file that exists in the repo but not in CONFIG_DIR means the
# deploy never happened at all (fresh host, wrong CONFIG_DIR, permissions).
# Silence there is indistinguishable from health, so say it out loud.
for f in "${SYNC_FILES[@]}"; do
    [ -f "$REPO/$f" ] || continue
    [ -f "$CONFIG_DIR/$f" ] || logger -t gps-config-sync \
        "WARNING: $f exists in the repo but not in $CONFIG_DIR — not deployed"
done
