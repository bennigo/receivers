#!/bin/bash
# ===========================================================================
# GPS Receivers Scheduler — Dev/Production Server Installation
# Veðurstofa Íslands
#
# Usage:
#   cd ~/git/receivers
#   sudo bash deployment/server/install.sh              # Fresh install or update (URL-pinned deps)
#   sudo bash deployment/server/install.sh --dev        # Editable installs of gtimes/gps_parser/tostools
#   sudo bash deployment/server/install.sh --wipe       # Wipe venv + redeploy config
#   sudo bash deployment/server/install.sh --wipe-all   # Drop DB + delete data + reinstall
#   sudo bash deployment/server/install.sh --wipe-db    # Drop and recreate database only
#   sudo bash deployment/server/install.sh --pg-external # Open gps_health to operator laptops
#                                                        # (dual-write archive_catalog from laptops)
#
# Dependency modes:
#   Default (URL-pinned):  gtimes/gps_parser/tostools resolved from pyproject.toml git URLs
#                          (immutable, reproducible — best for production)
#   --dev                  gtimes/gps_parser/tostools cloned to ~/git/ and installed editable
#                          (for hacking on sibling packages live; `git pull` picks up changes)
#
# Layout:
#   /home/bgo/git/           — repos + venv (owned by bgo, world-readable)
#   /home/gpsops/.config/gpsconfig/ — config (bgo:gpsops, group-readable)
#   /home/gpsops/.cache/gps_receivers/ — logs, scheduler DB (owned by gpsops)
#   /mnt/gpsdata/            — working data (owned by gpsops)
#
# bgo owns repos + venv, does git pull + pip install directly.
# gpsops runs the scheduler, reads config from its own ~/.config/gpsconfig/.
# ===========================================================================

set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────
readonly SERVICE_USER="gpsops"
readonly SERVICE_GROUP="gpsops"
readonly ADMIN_USER="bgo"

readonly ADMIN_HOME="/home/$ADMIN_USER"
readonly GPSOPS_HOME="/home/$SERVICE_USER"

# Repos + venv under bgo's home
readonly GIT_BASE="$ADMIN_HOME/git"
readonly INSTALL_DIR="$GIT_BASE/receivers"
readonly GTIMES_DIR="$GIT_BASE/gtimes"
readonly GPS_PARSER_DIR="$GIT_BASE/gps_parser"
readonly TOSTOOLS_DIR="$GIT_BASE/tostools"
readonly CONFIG_REPO_DIR="$GIT_BASE/gps-config-data"
readonly TOOLS_DIR="$GIT_BASE/gps-tools"
readonly VENV_DIR="$INSTALL_DIR/venv"

# Minimum Python version (see pyproject.toml requires-python)
readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=10

# gpsops owns config + cache + data
readonly CONFIG_DIR="$GPSOPS_HOME/.config/gpsconfig"
readonly CACHE_DIR="$GPSOPS_HOME/.cache/gps_receivers"
readonly DATA_DIR="/mnt/data/gpsdata"
readonly NFS_MOUNT="/mnt/rawgpsdata"
readonly DB_NAME="gps_health"

readonly NFS_SOURCE="ananas.vedur.is:/gps/gpsdata"
readonly NFS_OPTS="mountvers=3,auto,nofail,nolock,tcp,ro"

# Git repositories — public (HTTPS, no auth needed)
readonly REPO_GTIMES="https://github.com/bennigo/gtimes.git"
readonly REPO_GPS_PARSER="https://github.com/bennigo/gps_parser.git"
readonly REPO_TOSTOOLS="https://github.com/bennigo/tostools.git"
# Internal (requires bgo's LDAP credentials)
readonly REPO_CONFIG="https://git.vedur.is/bgo/gps-config-data.git"
readonly REPO_TOOLS="https://git.vedur.is/bgo/gps-tools.git"  # TODO: move to gps/gps-tools once IT grants write access

# ── Flags ──────────────────────────────────────────────────────────────────
FLAG_DEV=false
FLAG_WIPE=false
FLAG_WIPE_ALL=false
FLAG_WIPE_DB=false
FLAG_SKIP_TOOLS=false
FLAG_SKIP_DB=false
FLAG_SKIP_DOCKER=false
FLAG_SKIP_CONFIG_SYNC=false
FLAG_ONLY_CONFIG_SYNC=false
FLAG_PG_EXTERNAL=false

# Subnet allowed to reach gps_health when --pg-external is used. Operators run
# fix-headers/reindex/archive-rm from laptops/servers and dual-write the
# archive_catalog to this host. Default = the whole IMO internal private range
# (server subnet 10.170.x + VPN pool 10.250.x etc.) — none of it is reachable
# from outside IMO, so no in-network restriction is wanted. Tighten if needed:
# PG_OPERATOR_CIDR=10.250.0.0/16 install.sh --pg-external
readonly PG_OPERATOR_CIDR="${PG_OPERATOR_CIDR:-10.0.0.0/8}"

# ── Color helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $*"; }
err()  { echo -e "  ${RED}✗${NC} $*"; }
phase(){ echo -e "\n${BLUE}━━━ Phase $1: $2 ━━━${NC}"; }

# ── User-level systemctl helper ────────────────────────────────────────────
# Run `systemctl --user` as $SERVICE_USER from a root install context.
# Requires linger enabled (Phase 10 sets that up) so /run/user/<uid> exists.
# Errors are tolerated by callers via `|| true` where appropriate.
gpsops_systemctl() {
    local uid
    uid=$(id -u "$SERVICE_USER" 2>/dev/null) || return 1
    sudo -u "$SERVICE_USER" \
        XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        systemctl --user "$@"
}

# Set once Phase 4 stops the scheduler, so the exit trap can tell the operator
# whether an aborted run left it down. Phase 10 is what brings it back.
SCHEDULER_STOPPED=false

# `set -euo pipefail` means any failed step aborts mid-install. If that happens
# after Phase 4 stopped the scheduler, it stays stopped — say so loudly rather
# than auto-restarting onto a venv whose state we no longer know is coherent.
warn_if_scheduler_left_down() {
    local rc=$?
    if [[ $rc -ne 0 ]] && $SCHEDULER_STOPPED; then
        echo ""
        echo -e "${RED}  install.sh aborted (rc=$rc) — THE SCHEDULER IS STOPPED.${NC}"
        echo "  The venv may be half-installed; inspect before restarting."
        echo "  Restart as $SERVICE_USER:"
        echo "    systemctl --user start gps-receivers-scheduler"
    fi
}
trap warn_if_scheduler_left_down EXIT

# Stop the scheduler regardless of whether it's running as a user unit
# (current) or a legacy system unit (pre-migration). Used by --wipe* flags
# and by Phase 10 before the unit is reinstalled.
stop_scheduler() {
    local svc="gps-receivers-scheduler"
    # User-unit attempt — runs only if linger is set up and runtime dir exists.
    local uid
    uid=$(id -u "$SERVICE_USER" 2>/dev/null || echo "")
    if [[ -n "$uid" ]] && [[ -d "/run/user/$uid" ]]; then
        gpsops_systemctl stop "$svc" 2>/dev/null || true
    fi
    # Legacy system unit — safe no-op if absent.
    systemctl stop "$svc" 2>/dev/null || true
    # Set here rather than at the call sites so every path that stops the
    # scheduler (Phase 4 and the --wipe* branches) is covered by the exit trap.
    SCHEDULER_STOPPED=true
}

# ── Parse arguments ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --dev)          FLAG_DEV=true ;;
        --wipe)         FLAG_WIPE=true ;;
        --wipe-all)     FLAG_WIPE_ALL=true; FLAG_WIPE=true ;;
        --wipe-db)      FLAG_WIPE_DB=true ;;
        --skip-tools)        FLAG_SKIP_TOOLS=true ;;
        --skip-db)           FLAG_SKIP_DB=true ;;
        --skip-docker)       FLAG_SKIP_DOCKER=true ;;
        --skip-config-sync)  FLAG_SKIP_CONFIG_SYNC=true ;;
        --only-config-sync)  FLAG_ONLY_CONFIG_SYNC=true ;;
        --pg-external)       FLAG_PG_EXTERNAL=true ;;
        -h|--help)
            echo "Usage: $0 [--dev] [--wipe] [--wipe-all] [--wipe-db] [--skip-tools] [--skip-db] [--skip-docker] [--pg-external]"
            echo ""
            echo "  --dev          Editable installs of gtimes/gps_parser/tostools"
            echo "                 (default: URL-pinned from pyproject.toml — production mode)"
            echo "  --wipe         Wipe venv + redeploy config (keep data + DB)"
            echo "  --wipe-all     Drop DB + delete data + full reinstall"
            echo "  --wipe-db      Drop and recreate database only"
            echo "  --skip-tools        Skip external tool installation (RxTools, teqc)"
            echo "  --skip-db           Skip database setup (for remote DB)"
            echo "  --skip-docker       Skip Docker/Grafana setup"
            echo "  --skip-config-sync  Skip config sync timer (use when config is local-only, no git repo)"
            echo "  --only-config-sync  Install/enable config sync timer only, skip all other phases"
            echo "  --pg-external       Open gps_health to operator laptops (listen_addresses + pg_hba"
            echo "                      for \$PG_OPERATOR_CIDR, scram auth); restarts postgres if needed"
            exit 0
            ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ── Pre-flight checks ────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (or with sudo)"
    exit 1
fi

# --only-config-sync: install the timer and exit, skip all other phases
if $FLAG_ONLY_CONFIG_SYNC; then
    phase "—" "Config sync timer (--only-config-sync)"
    if [[ ! -d "$CONFIG_REPO_DIR/.git" ]]; then
        err "gps-config-data repo not found at $CONFIG_REPO_DIR"
        err "Clone it first or omit --only-config-sync for local-only config"
        exit 1
    fi
    install -m 644 "$INSTALL_DIR/deployment/systemd/gps-config-sync.service" \
        /etc/systemd/system/gps-config-sync.service
    install -m 644 "$INSTALL_DIR/deployment/systemd/gps-config-sync.timer" \
        /etc/systemd/system/gps-config-sync.timer
    chmod +x "$INSTALL_DIR/deployment/server/sync-config.sh"
    systemctl daemon-reload
    systemctl enable --now gps-config-sync.timer
    ok "Config sync timer installed and enabled (pulls every 10 min)"
    exit 0
fi

# Verify we're running from the receivers repo
if [[ ! -f "$INSTALL_DIR/pyproject.toml" ]]; then
    err "receivers repo not found at $INSTALL_DIR"
    err "Clone it first: git clone https://github.com/bennigo/receivers.git $INSTALL_DIR"
    exit 1
fi

# Verify Python version (pyproject.toml requires-python = ">=3.10")
if ! command -v python3 &>/dev/null; then
    err "python3 not found"
    exit 1
fi
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
PY_MAJOR=${PYTHON_VERSION%%.*}
PY_MINOR=${PYTHON_VERSION##*.}
if (( PY_MAJOR < MIN_PYTHON_MAJOR )) || \
   (( PY_MAJOR == MIN_PYTHON_MAJOR && PY_MINOR < MIN_PYTHON_MINOR )); then
    err "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ required (found $PYTHON_VERSION)"
    err "Upgrade python3 (Ubuntu 22.04+ ships 3.10, 24.04 ships 3.12)"
    exit 1
fi

echo -e "${BLUE}=== GPS Receivers Scheduler — Server Installation ===${NC}"
echo "  Host:     $(hostname)"
echo "  Date:     $(date -Iseconds)"
echo "  OS:       $(lsb_release -ds 2>/dev/null || grep PRETTY_NAME /etc/os-release | cut -d= -f2)"
echo "  Python:   $PYTHON_VERSION"
echo "  Mode:     $($FLAG_DEV && echo 'dev (editable siblings)' || echo 'production (URL-pinned siblings)')"
echo "  Wipe:     $FLAG_WIPE  Wipe-all: $FLAG_WIPE_ALL  Wipe-db: $FLAG_WIPE_DB"

# ── Handle wipe modes ────────────────────────────────────────────────────
if $FLAG_WIPE_ALL; then
    echo ""
    echo -e "${RED}WARNING: --wipe-all will DROP the database and delete $DATA_DIR/*${NC}"
    read -p "  Type 'yes' to confirm: " confirm
    if [[ "$confirm" != "yes" ]]; then echo "Aborted."; exit 1; fi
    stop_scheduler
    sudo -u postgres dropdb --if-exists "$DB_NAME"
    rm -rf "$VENV_DIR"
    rm -rf "$DATA_DIR"/*
    ok "Wipe-all complete, proceeding with fresh install"
elif $FLAG_WIPE_DB; then
    echo ""
    echo -e "${RED}WARNING: --wipe-db will DROP and recreate the $DB_NAME database${NC}"
    read -p "  Type 'yes' to confirm: " confirm
    if [[ "$confirm" != "yes" ]]; then echo "Aborted."; exit 1; fi
    stop_scheduler
    sudo -u postgres dropdb --if-exists "$DB_NAME"
    ok "Database dropped, will be recreated in Phase 6"
elif $FLAG_WIPE; then
    stop_scheduler
    rm -rf "$VENV_DIR"
    ok "Venv wiped, will be recreated in Phase 4"
fi

# ===========================================================================
# Phase 1: System packages
# ===========================================================================
phase 1 "System packages"

PACKAGES=(
    postgresql postgresql-contrib libpq-dev
    python3 python3-pip python3-venv python3-dev
    git jq curl wget
    nfs-common
    # BNC (BKG Ntrip Client) runtime libs — BNC ships as a mostly-static Qt binary
    # but still dynamically links X11-client + glib libs at load time, even when run
    # headless (-nw). Only needed for the stream_capture acquisition mode (Phase 8
    # symlinks the bnc binary); cheap to install unconditionally.
    libx11-6 libxext6 libxcb1 libglib2.0-0
)

MISSING=()
for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -l "$pkg" 2>/dev/null | grep -q '^ii'; then
        MISSING+=("$pkg")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "  Installing: ${MISSING[*]}"
    # Disable third-party PostgreSQL repos that may not support this Ubuntu release
    for f in /etc/apt/sources.list.d/*pgdg* /etc/apt/sources.list.d/*postgresql*; do
        [[ -f "$f" ]] && mv "$f" "${f}.disabled" && warn "Disabled $(basename "$f")"
    done
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}"
    ok "Installed ${#MISSING[@]} packages"
else
    ok "All system packages already installed"
fi

# Docker
if ! $FLAG_SKIP_DOCKER; then
    if ! command -v docker &>/dev/null; then
        echo "  Installing Docker..."
        if [[ -f /etc/apt/sources.list.d/docker.list ]]; then
            apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
        else
            apt-get install -y -qq docker.io docker-compose-v2 2>/dev/null || \
                apt-get install -y -qq docker.io
        fi
        systemctl enable --now docker
        ok "Docker installed"
    else
        ok "Docker already installed"
    fi
fi

# ===========================================================================
# Phase 2: Users + directories
# ===========================================================================
phase 2 "Users and directories"

# Create service user with a real home directory
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --create-home --home-dir "$GPSOPS_HOME" \
            --shell /bin/bash --comment "GPS Receivers Service" "$SERVICE_USER"
    ok "Created user: $SERVICE_USER (home: $GPSOPS_HOME)"
else
    ok "User exists: $SERVICE_USER"
    # Ensure home directory exists (AD/LDAP users may not have one yet)
    if [[ ! -d "$GPSOPS_HOME" ]]; then
        mkdir -p "$GPSOPS_HOME"
        chown "$SERVICE_USER":"$(id -gn "$SERVICE_USER")" "$GPSOPS_HOME"
        ok "Created home directory: $GPSOPS_HOME"
    fi
fi

# Create service group if it doesn't exist (AD/LDAP environments)
if ! getent group "$SERVICE_GROUP" &>/dev/null; then
    groupadd "$SERVICE_GROUP"
    usermod -aG "$SERVICE_GROUP" "$SERVICE_USER"
    ok "Created group: $SERVICE_GROUP"
else
    ok "Group exists: $SERVICE_GROUP"
fi

# Add admin to service group
if ! id -nG "$ADMIN_USER" 2>/dev/null | grep -qw "$SERVICE_GROUP"; then
    usermod -aG "$SERVICE_GROUP" "$ADMIN_USER"
    ok "Added $ADMIN_USER to $SERVICE_GROUP group"
else
    ok "$ADMIN_USER already in $SERVICE_GROUP group"
fi

# Add service user to docker group
if getent group docker &>/dev/null; then
    if ! id -nG "$SERVICE_USER" 2>/dev/null | grep -qw docker; then
        usermod -aG docker "$SERVICE_USER"
        ok "Added $SERVICE_USER to docker group"
    fi
    # Add admin user too — so `docker ps` / `docker logs` work without sudo.
    # Note: group membership only takes effect on next login (or `newgrp docker`).
    if ! id -nG "$ADMIN_USER" 2>/dev/null | grep -qw docker; then
        usermod -aG docker "$ADMIN_USER"
        ok "Added $ADMIN_USER to docker group (re-login needed to take effect)"
    fi
fi

# Ensure bgo's home + git dir are traversable by gpsops (for venv + editable installs)
chmod o+x "$ADMIN_HOME"
chmod o+x "$GIT_BASE"

# gpsops config + cache directories
sudo -u "$SERVICE_USER" mkdir -p "$CONFIG_DIR"
sudo -u "$SERVICE_USER" mkdir -p "$CACHE_DIR"/{logs,tmp}

# Allow $ADMIN_USER (and anyone else in $SERVICE_GROUP) to traverse into
# $GPSOPS_HOME and read the cache/logs tree. Two layers matter:
#
# 1. $GPSOPS_HOME itself is mode 700 by default on Ubuntu. chmod 750 allows
#    group traverse into /home/$SERVICE_USER/.
# 2. On LDAP-integrated systems (e.g. Veðurstofa), new users can inherit a
#    non-service primary group (e.g. starfsmenn). That makes files created
#    by $SERVICE_USER land with the wrong group. Force the cache tree's
#    group to $SERVICE_GROUP, and set the SGID bit on the cache dirs so
#    subsequently created files (log rotations, scheduler.db re-inits)
#    inherit the group too.
# 3. `$CACHE_DIR/..` (i.e. /home/$SERVICE_USER/.cache/) also gets created
#    with mode 700 — have to chmod it 750 separately or traversal fails
#    one level before the cache tree.
chgrp "$SERVICE_GROUP" "$GPSOPS_HOME"
chmod 750 "$GPSOPS_HOME"
# .cache parent (don't recurse — only adjust the traverse bit)
chgrp "$SERVICE_GROUP" "$(dirname "$CACHE_DIR")"
chmod 750 "$(dirname "$CACHE_DIR")"
# Full cache tree: group ownership + group-read + SGID on directories
chgrp -R "$SERVICE_GROUP" "$CACHE_DIR"
chmod -R g+rX "$CACHE_DIR"
find "$CACHE_DIR" -type d -exec chmod g+s {} \;

# Data directories (system-level)
mkdir -p "$DATA_DIR"
chown "$SERVICE_USER":"$SERVICE_GROUP" "$DATA_DIR"
chmod 755 "$DATA_DIR"

mkdir -p "$NFS_MOUNT"

ok "Directory structure ready"

# NFS mount for production archive
if ! grep -q "$NFS_MOUNT" /etc/fstab 2>/dev/null; then
    echo "$NFS_SOURCE $NFS_MOUNT nfs $NFS_OPTS 0 0" >> /etc/fstab
    ok "Added NFS entry to fstab"
fi
if ! mountpoint -q "$NFS_MOUNT" 2>/dev/null; then
    mount "$NFS_MOUNT" 2>/dev/null && ok "NFS mounted" || \
        warn "NFS mount failed (nofail allows boot without archive server)"
fi

# SSH key for gpsops (rsync to rawdata.vedur.is)
GPSOPS_SSH_DIR="$GPSOPS_HOME/.ssh"
if [[ ! -f "$GPSOPS_SSH_DIR/id_ed25519" ]]; then
    sudo -u "$SERVICE_USER" mkdir -p "$GPSOPS_SSH_DIR"
    sudo -u "$SERVICE_USER" ssh-keygen -t ed25519 -N "" -C "gpsops@$(hostname)" \
        -f "$GPSOPS_SSH_DIR/id_ed25519" >/dev/null
    chmod 700 "$GPSOPS_SSH_DIR"
    chmod 600 "$GPSOPS_SSH_DIR/id_ed25519"
    ok "Generated SSH key for $SERVICE_USER"
    warn "Authorize on rawdata.vedur.is:"
    echo "    $(cat "$GPSOPS_SSH_DIR/id_ed25519.pub")"
else
    ok "SSH key exists for $SERVICE_USER"
fi

# ===========================================================================
# Phase 3: Git repositories
# ===========================================================================
phase 3 "Git repositories"

# All repos cloned/updated as bgo (HTTPS, public repos need no auth)
clone_or_update() {
    local repo_url="$1" target_dir="$2"
    if [[ ! -d "$target_dir/.git" ]]; then
        echo "  Cloning $repo_url"
        sudo -u "$ADMIN_USER" git clone "$repo_url" "$target_dir" 2>&1 | tail -1
        ok "Cloned $(basename "$target_dir")"
    else
        cd "$target_dir"
        sudo -u "$ADMIN_USER" git pull --ff-only 2>&1 | tail -1 || \
            warn "git pull failed for $(basename "$target_dir")"
        ok "Updated $(basename "$target_dir")"
    fi
    # Ensure world-readable so gpsops can access
    chmod -R o+rX "$target_dir"
}

# receivers is already cloned (we're running from it)
ok "receivers: $INSTALL_DIR"
chmod -R o+rX "$INSTALL_DIR"

# Sibling packages: cloned for editable installs in --dev mode only.
# In production mode, pyproject.toml's git-URL pins handle them.
if $FLAG_DEV; then
    clone_or_update "$REPO_GTIMES"     "$GTIMES_DIR"
    clone_or_update "$REPO_GPS_PARSER" "$GPS_PARSER_DIR"
    clone_or_update "$REPO_TOSTOOLS"   "$TOSTOOLS_DIR"
else
    ok "Siblings (gtimes/gps_parser/tostools): resolved from pyproject.toml git URLs"
fi

# Internal repo — may need bgo's LDAP credentials if not public.
# GIT_TERMINAL_PROMPT=0: fail fast instead of hanging at a username prompt.
# Cached credentials (~/.netrc, credential helper) still work if present.
if [[ ! -d "$CONFIG_REPO_DIR/.git" ]]; then
    echo "  Cloning gps-config-data..."
    if sudo -u "$ADMIN_USER" GIT_TERMINAL_PROMPT=0 git clone "$REPO_CONFIG" "$CONFIG_REPO_DIR" 2>&1 | tail -1; then
        chmod -R o+rX "$CONFIG_REPO_DIR"
        ok "Cloned gps-config-data"
    else
        warn "Clone failed — run manually: git clone $REPO_CONFIG $CONFIG_REPO_DIR"
    fi
else
    cd "$CONFIG_REPO_DIR"
    sudo -u "$ADMIN_USER" GIT_TERMINAL_PROMPT=0 git pull --ff-only 2>&1 | tail -1 || true
    chmod -R o+rX "$CONFIG_REPO_DIR"
    ok "Updated gps-config-data"
fi

# ===========================================================================
# Phase 4: Python virtual environment
# ===========================================================================
phase 4 "Python virtual environment"

# Stop the scheduler before touching the venv, config, or database.
#
# Everything from here to the Phase 10 restart mutates state the running
# scheduler depends on: pip swaps packages underneath it, `receivers` is an
# EDITABLE install (so the working tree IS the live code — a `git pull` before
# this script already moved it), imports are lazy, and Phase 6 runs migrations.
# A job that resolves a lazy import mid-install gets new code against old
# objects. Until now stop_scheduler ran only for --wipe*, so ordinary deploys
# ran straight over a live process.
stop_scheduler
ok "Scheduler stopped for the install (Phase 10 restarts it)"

echo "  Python: $PYTHON_VERSION"

if [[ ! -d "$VENV_DIR" ]]; then
    sudo -u "$ADMIN_USER" python3 -m venv "$VENV_DIR"
    ok "Created venv"
else
    ok "Venv exists"
fi

# Install packages as bgo (bgo owns the venv)
sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel -q

# Preflight: prove the dependency tree resolves BEFORE the destructive
# uninstall below.
#
# `set -euo pipefail` aborts the script the moment the install fails — but by
# then the uninstall has already run, so the venv is left with NO tostools
# (or gtimes/gps_parser). The deploy makes things worse than it found them.
#
# One mismatched direct-URL pin between two siblings is enough to trigger it:
# pip treats two direct URLs for the same package as conflicting unless they
# match exactly, so `receivers → gps-parser@257a2ee` against
# `tostools → gps-parser` (unpinned) is a hard ResolutionImpossible. Hit for
# real on rek-d01 2026-07-31; the fix was pinning both to 257a2ee.
#
# Verified on rek-d01 that this catches it even with tostools already
# installed (pip does not short-circuit): broken pin rc=1, fixed pin rc=0,
# ~10 s. Turns "deploy bricks production" into "deploy refuses to start".
if ! $FLAG_DEV; then
    PREFLIGHT_LOG=$(mktemp)
    echo "  Preflight: resolving dependency tree…"
    if ! sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" \
            install --dry-run -e "$INSTALL_DIR" >"$PREFLIGHT_LOG" 2>&1; then
        err "Dependency preflight FAILED — venv left untouched."
        echo ""
        tail -25 "$PREFLIGHT_LOG"
        echo ""
        echo "  Nothing was uninstalled. Fix the pins in pyproject.toml"
        echo "  (sibling direct-URL pins must match EXACTLY), then re-run."
        rm -f "$PREFLIGHT_LOG"
        exit 1
    fi
    rm -f "$PREFLIGHT_LOG"
    ok "Dependency tree resolves"
fi

# Force a clean re-resolve of the git-pinned siblings. pip skips re-fetching a
# direct-URL (git) dependency when its installed VERSION already satisfies the
# requirement — so bumping a pin to a new COMMIT at the SAME version (e.g.
# tostools ba36022->adaa495, both 0.6.1) silently would NOT install, leaving the
# venv on stale code. Uninstalling them first makes the receivers install below
# fetch each pin fresh from pyproject.toml. Skipped in --dev, where the editable
# installs override these anyway.
if ! $FLAG_DEV; then
    sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" uninstall -y -q \
        gtimes gps_parser tostools 2>/dev/null || true
fi

# Install receivers — this resolves the git-URL pins in pyproject.toml for
# gtimes/gps_parser/tostools (the production path).
sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install -e "$INSTALL_DIR" -q

if $FLAG_DEV; then
    # Override the URL-resolved installs with editable siblings. `pip install -e`
    # force-replaces prior installs of the same package, so `git pull` in any
    # sibling dir becomes live without reinstall churn.
    sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install -e "$GTIMES_DIR" -q
    sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install -e "$GPS_PARSER_DIR" -q
    sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install -e "$TOSTOOLS_DIR" -q
    ok "Packages installed (receivers + editable gtimes/gps_parser/tostools)"
else
    ok "Packages installed (receivers + URL-pinned gtimes/gps_parser/tostools)"
fi

# Ensure venv is world-readable + executable
chmod -R o+rX "$VENV_DIR"

# Symlink receivers CLI to /usr/local/bin/ for all-user access
ln -sf "$VENV_DIR/bin/receivers" /usr/local/bin/receivers
ok "receivers CLI available system-wide (/usr/local/bin/receivers)"

# Verify CLI works as gpsops
if sudo -u "$SERVICE_USER" "$VENV_DIR/bin/receivers" --help &>/dev/null; then
    ok "receivers CLI works as $SERVICE_USER"
else
    err "receivers CLI failed as $SERVICE_USER — check permissions"
    exit 1
fi

# ===========================================================================
# Phase 5: Configuration deployment
# ===========================================================================
phase 5 "Configuration"

# Deploy config files: gps-config-data → package defaults → skip
DEFAULTS_DIR="$INSTALL_DIR/config/defaults"
CONFIG_FILES=(stations.cfg receivers.cfg scheduler.yaml database.cfg icinga.cfg station_areas.yaml sync.yaml agencies.yaml)
# database.cfg may contain credentials edited directly on the server — never
# overwrite it on update runs; only deploy when the file is absent or --wipe.
PROTECTED_FILES=(database.cfg)
for f in "${CONFIG_FILES[@]}"; do
    dst="$CONFIG_DIR/$f"
    src=""

    # Source priority: gps-config-data > package defaults
    if [[ -f "$CONFIG_REPO_DIR/$f" ]]; then
        src="$CONFIG_REPO_DIR/$f"
    elif [[ -f "$DEFAULTS_DIR/$f" ]]; then
        src="$DEFAULTS_DIR/$f"
    fi

    if [[ -z "$src" ]]; then
        warn "Not found: $f (not in config repo or package defaults)"
        continue
    fi

    # Protected files: deploy only on first install (not on --wipe-unless-asked)
    if [[ " ${PROTECTED_FILES[*]} " =~ " $f " ]] && [[ -f "$dst" ]] && ! $FLAG_WIPE; then
        ok "$f protected — not overwritten (use --wipe to force redeploy)"
        continue
    fi

    if [[ ! -f "$dst" ]] || [[ "$src" -nt "$dst" ]] || $FLAG_WIPE; then
        cp "$src" "$dst"
        if [[ "$src" == "$DEFAULTS_DIR"* ]]; then
            ok "Deployed $f (from package defaults)"
        else
            ok "Deployed $f"
        fi
    else
        ok "$f unchanged"
    fi
done

# Config owned by the service user (no admin-user assumption in software).
# Admin has write access via group membership: install.sh Phase 2 adds
# $ADMIN_USER to $SERVICE_GROUP, and the files are mode 660 (group-writable).
# This matches the Unix convention that files under /home/<user>/ belong to <user>.
chown ${SERVICE_USER}:${SERVICE_GROUP} "$CONFIG_DIR"
chown ${SERVICE_USER}:${SERVICE_GROUP} "$CONFIG_DIR"/*
chmod 770 "$CONFIG_DIR"
chmod 660 "$CONFIG_DIR"/*

# Set GPS_CONFIG_PATH system-wide so all users (bgo, gpsops) find the shared config
# without having to set it in their own shell profiles.
# /etc/profile.d/ covers interactive login shells; /etc/environment covers
# non-interactive PAM sessions (cron, sudo, ssh non-login).
echo "export GPS_CONFIG_PATH=\"$CONFIG_DIR\"" > /etc/profile.d/gps-receivers.sh
chmod 644 /etc/profile.d/gps-receivers.sh
grep -q '^GPS_CONFIG_PATH=' /etc/environment 2>/dev/null || \
    echo "GPS_CONFIG_PATH=$CONFIG_DIR" >> /etc/environment
ok "GPS_CONFIG_PATH=$CONFIG_DIR (profile.d + /etc/environment)"

# Patch database.cfg for local PostgreSQL + mirror. Scope the host/user rewrite
# to the [postgresql] section ONLY — a global sed would clobber the host/user of
# any other DB section (e.g. [epos_db] → psql.vedur.is/importer_epos) and point it
# at the local DB, silently breaking that connection.
if [[ -f "$CONFIG_DIR/database.cfg" ]]; then
    sed -i '/^\[postgresql\]/,/^\[/ s/^host\s*=.*/host = localhost/' "$CONFIG_DIR/database.cfg"
    sed -i "/^\[postgresql\]/,/^\[/ s/^user\s*=.*/user = $SERVICE_USER/" "$CONFIG_DIR/database.cfg"
    # Mirror writes to external DB (grafana.vedur.is reads from it)
    if ! grep -q '^mirror_host' "$CONFIG_DIR/database.cfg"; then
        sed -i '/^\[postgresql\]/a mirror_host = pgdev.vedur.is' "$CONFIG_DIR/database.cfg"
    fi
    # Mirror authenticates as bgo (LDAP auth on pgdev)
    if ! grep -q '^mirror_user' "$CONFIG_DIR/database.cfg"; then
        sed -i '/^mirror_host/a mirror_user = bgo' "$CONFIG_DIR/database.cfg"
    fi
    ok "Patched database.cfg (localhost/$SERVICE_USER, mirror=pgdev.vedur.is/bgo)"
fi

# Mirror DB credentials (pgdev.vedur.is) must be configured manually by IT:
# - A dedicated operational DB user on pgdev.vedur.is (not a personal account)
# - ~/.pgpass entries for both $SERVICE_USER and $ADMIN_USER on this host
# - pg_hba.conf on pgdev.vedur.is: allow password auth from rek-d01 for that user
# The install script intentionally does not touch .pgpass — credentials stay
# under IT control, never propagated by automation.
MIRROR_HOST="pgdev.vedur.is"
for u in "$SERVICE_USER" "$ADMIN_USER"; do
    u_home=$(getent passwd "$u" | cut -d: -f6)
    if grep -q "^$MIRROR_HOST" "$u_home/.pgpass" 2>/dev/null; then
        ok "Mirror .pgpass configured for $u"
    else
        warn "Mirror .pgpass not set for $u — IT must add $MIRROR_HOST entry to $u_home/.pgpass"
    fi
done

# Patch receivers.cfg for server paths
if [[ -f "$CONFIG_DIR/receivers.cfg" ]]; then
    sed -i "s|^data_prepath\s*=.*|data_prepath = $DATA_DIR/|" "$CONFIG_DIR/receivers.cfg"
    # Download staging on the dedicated /tmp LV: isolated from data/logs/DB
    # (a runaway download can't fill them) and systemd-tmpfiles ages out
    # orphaned staging dirs automatically (2026-07-06: 30 GB of stale staging
    # had accumulated on /home under the old ~/.cache/gps_receivers/tmp).
    sed -i "s|^tmp_dir\s*=.*|tmp_dir = /tmp/gps_receivers/|" "$CONFIG_DIR/receivers.cfg"
    mkdir -p /tmp/gps_receivers
    chown "$SERVICE_USER:$SERVICE_GROUP" /tmp/gps_receivers 2>/dev/null || true
    # Uncomment PolaRX5 TCP credentials (commented-out in package defaults for portability;
    # idempotent: sed is a no-op when lines are already uncommented)
    sed -i 's/^# tcp_username = /tcp_username = /' "$CONFIG_DIR/receivers.cfg"
    sed -i 's/^# tcp_password = /tcp_password = /' "$CONFIG_DIR/receivers.cfg"
    ok "Patched receivers.cfg (data_prepath=$DATA_DIR/, tmp_dir=/tmp/gps_receivers/, TCP auth activated)"
fi

# journald size cap — /var is 24G and journald grows unbounded by default
# (2.4G observed 2026-07-06 with /var at 84%). Idempotent drop-in.
if [[ ! -f /etc/systemd/journald.conf.d/size.conf ]]; then
    mkdir -p /etc/systemd/journald.conf.d
    printf '[Journal]\nSystemMaxUse=1G\n' > /etc/systemd/journald.conf.d/size.conf
    systemctl restart systemd-journald
    ok "journald capped at 1G (/etc/systemd/journald.conf.d/size.conf)"
else
    ok "journald size cap already in place"
fi

# ===========================================================================
# Phase 6: PostgreSQL database setup
# ===========================================================================
if ! $FLAG_SKIP_DB; then
phase 6 "PostgreSQL database"

systemctl enable --now postgresql
ok "PostgreSQL running"

# ── PGDATA volume check (todo #62) ──────────────────────────────────────────
# gps_health grows continuously (health block_* + the unified file-index
# catalog), so its data dir must live on the large data volume, NOT a small OS
# partition like /var (a full /var takes down the production DB). We do NOT move
# the cluster here — that's a gated manual op (docs/deployment/
# relocate-pgdata-to-mnt-data.md) — but we WARN if PGDATA is on a different
# volume than the GPS data root. The expected location is DERIVED from DATA_DIR
# (the same constant that drives data_prepath), so nothing is hardcoded here.
PGDATA=$(sudo -u postgres psql -tAc "SHOW data_directory" 2>/dev/null | tr -d '[:space:]')
if [[ -n "$PGDATA" ]]; then
    data_vol=$(stat -c '%m' "$(dirname "$DATA_DIR")" 2>/dev/null || echo "")
    pgdata_vol=$(stat -c '%m' "$PGDATA" 2>/dev/null || echo "")
    if [[ -n "$data_vol" && -n "$pgdata_vol" && "$data_vol" != "$pgdata_vol" ]]; then
        warn "PGDATA ($PGDATA) is on volume '$pgdata_vol', not the GPS data volume '$data_vol'."
        warn "  A growing gps_health DB on a small OS volume can fill it and halt production."
        warn "  Relocate to $(dirname "$DATA_DIR")/postgresql/ — see"
        warn "  docs/deployment/relocate-pgdata-to-mnt-data.md (todo #62)."
    else
        ok "PGDATA on the GPS data volume ($pgdata_vol)"
    fi
fi

# Create database roles
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$SERVICE_USER'" | grep -q 1; then
    sudo -u postgres createuser "$SERVICE_USER"
    ok "Created PostgreSQL role: $SERVICE_USER"
else
    ok "PostgreSQL role exists: $SERVICE_USER"
fi

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$ADMIN_USER'" | grep -q 1; then
    sudo -u postgres createuser -s "$ADMIN_USER"
    ok "Created PostgreSQL superuser role: $ADMIN_USER"
else
    ok "PostgreSQL role exists: $ADMIN_USER"
fi

# Create database owned by service user
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_catalog.pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    sudo -u postgres createdb -O "$SERVICE_USER" "$DB_NAME"
    ok "Created database: $DB_NAME"
else
    ok "Database exists: $DB_NAME"
    sudo -u postgres psql -c "ALTER DATABASE $DB_NAME OWNER TO $SERVICE_USER" 2>/dev/null
fi

# Disable JIT (adds overhead for small result sets — see migration 030 notes)
sudo -u postgres psql -c "ALTER DATABASE $DB_NAME SET jit = off" 2>/dev/null
ok "JIT disabled for $DB_NAME"

# Configure pg_hba.conf
PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file")
if [[ -f "$PG_HBA" ]]; then
    if ! grep -q "$DB_NAME" "$PG_HBA" 2>/dev/null; then
        sed -i "/^# TYPE/a\\
# GPS health monitoring\\
local   $DB_NAME    $SERVICE_USER                       peer\\
local   $DB_NAME    $ADMIN_USER                         peer\\
host    $DB_NAME    $SERVICE_USER   127.0.0.1/32        trust\\
host    $DB_NAME    $SERVICE_USER   ::1/128             trust\\
host    $DB_NAME    $ADMIN_USER     127.0.0.1/32        trust\\
host    $DB_NAME    $ADMIN_USER     ::1/128             trust" "$PG_HBA"
        systemctl reload postgresql
        ok "Configured pg_hba.conf (peer + trust for localhost)"
    else
        ok "pg_hba.conf already configured"
    fi
fi

# ── External access for operator laptops (--pg-external) ────────────────────
# Operators (bgo, Hildur, …) run fix-headers/reindex/archive-rm from laptops and
# dual-write the archive_catalog to THIS host (see receivers.cfg [archive]
# catalog_hosts). That needs postgres to (a) listen beyond localhost and (b)
# allow scram auth from the operator subnet. Config only — role PASSWORDS stay
# under IT control (never in this script), like the mirror .pgpass note above.
if $FLAG_PG_EXTERNAL; then
    restart_pg=false

    # (a) listen beyond localhost — via ALTER SYSTEM (postgresql.auto.conf, no
    # hand-editing of the main conf). Idempotent; needs a RESTART when changed.
    cur_listen=$(sudo -u postgres psql -tAc "SHOW listen_addresses")
    if [[ "$cur_listen" != "*" ]]; then
        sudo -u postgres psql -c "ALTER SYSTEM SET listen_addresses = '*'" >/dev/null
        restart_pg=true
        ok "listen_addresses → '*' (was: $cur_listen) — restart required"
    else
        ok "listen_addresses already '*'"
    fi

    # (b) pg_hba host rule for the operator subnet (scram-sha-256), idempotent.
    PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file")
    HBA_MARK="# GPS operator external access"
    if ! grep -qF "$HBA_MARK" "$PG_HBA" 2>/dev/null; then
        cp -a "$PG_HBA" "$PG_HBA.bak.$(date +%Y%m%d%H%M%S)"
        {
            echo ""
            echo "$HBA_MARK ($DB_NAME from $PG_OPERATOR_CIDR — install.sh --pg-external)"
            echo "host    $DB_NAME    all    $PG_OPERATOR_CIDR    scram-sha-256"
        } >> "$PG_HBA"
        systemctl reload postgresql
        ok "pg_hba.conf: allow $DB_NAME from $PG_OPERATOR_CIDR (scram-sha-256)"
    else
        ok "pg_hba.conf external rule already present"
    fi

    # New passwords use scram (affects future ALTER ROLE ... PASSWORD only).
    sudo -u postgres psql -c "ALTER SYSTEM SET password_encryption = 'scram-sha-256'" >/dev/null 2>&1 || true

    if $restart_pg; then
        warn "Restarting postgresql to apply listen_addresses (brief scheduler blip)…"
        systemctl restart postgresql
        ok "postgresql restarted — now listening beyond localhost"
    fi

    warn "Operator ROLE PASSWORDS are NOT set here (credentials stay with IT):"
    warn "  IT: ALTER ROLE <user> WITH LOGIN PASSWORD '…';  (per operator role)"
    warn "  Operator laptop: ~/.pgpass line $(hostname -f):5432:$DB_NAME:<user>:<pw>"
    warn "  Also confirm the network firewall permits tcp/5432 from $PG_OPERATOR_CIDR."
fi

# Verify connection
if sudo -u "$SERVICE_USER" psql -d "$DB_NAME" -c "SELECT 1" &>/dev/null; then
    ok "Database connection verified ($SERVICE_USER → $DB_NAME)"
else
    err "Cannot connect to database as $SERVICE_USER"
    err "Check pg_hba.conf: $PG_HBA"
    exit 1
fi

else
    warn "Skipping database setup (--skip-db)"
fi

# ===========================================================================
# Phase 7: Database migrations
# ===========================================================================
if ! $FLAG_SKIP_DB; then
phase 7 "Database migrations"

MIGRATIONS_DIR="$INSTALL_DIR/migrations"

# Check if schema_migrations table exists
HAS_MIGRATIONS=$(sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -tAc \
    "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='schema_migrations')" 2>/dev/null || echo "f")

if [[ "$HAS_MIGRATIONS" != "t" ]]; then
    echo "  Fresh database, running consolidated schema..."
    sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -f "$MIGRATIONS_DIR/000_consolidated_schema.sql" -q
    ok "Consolidated schema applied (migrations 001-028 marked as done)"
fi

# Apply pending migrations
APPLIED=$(sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -tAc \
    "SELECT migration_name FROM schema_migrations ORDER BY migration_name" 2>/dev/null)

PENDING_COUNT=0
SKIPPED_COUNT=0
for migration_file in "$MIGRATIONS_DIR"/[0-9][0-9][0-9]_*.sql; do
    [[ ! -f "$migration_file" ]] && continue
    basename=$(basename "$migration_file" .sql)
    [[ "$basename" == *_rollback ]] && continue
    echo "$APPLIED" | grep -qx "$basename" && continue

    # Prerequisite gate: a migration may declare `-- requires-extension: <name>`
    # in its header. If that extension is not installed, SKIP the migration and
    # leave it PENDING (do NOT mark it applied) so it runs for real once the
    # prerequisite is met. Used by 048 (TimescaleDB) so a routine code deploy
    # isn't blocked by infra that isn't stood up yet.
    REQ_EXT=$(grep -m1 -oE '^-- requires-extension:[[:space:]]*[a-zA-Z0-9_]+' "$migration_file" \
        | sed -E 's/^-- requires-extension:[[:space:]]*//')
    if [[ -n "$REQ_EXT" ]]; then
        HAS_EXT=$(sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -tAc \
            "SELECT 1 FROM pg_extension WHERE extname='$REQ_EXT'" 2>/dev/null)
        if [[ "$HAS_EXT" != "1" ]]; then
            warn "Skipping $basename — requires '$REQ_EXT' extension (not installed); left PENDING"
            SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
            continue
        fi
    fi

    echo "  Applying: $basename"
    # ON_ERROR_STOP: psql is invoked without it here, so a mid-migration error
    # would otherwise exit 0 and get falsely marked applied over an aborted
    # transaction. Force a non-zero exit on the first error so the shell `if`
    # below catches it and aborts the install.
    if sudo -u "$ADMIN_USER" psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$migration_file" -q 2>&1; then
        sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -c \
            "INSERT INTO schema_migrations (migration_name) VALUES ('$basename') ON CONFLICT DO NOTHING" -q
        ok "Applied $basename"
        PENDING_COUNT=$((PENDING_COUNT + 1))
    else
        err "Failed to apply $basename"
        exit 1
    fi
done

if [[ $SKIPPED_COUNT -gt 0 ]]; then
    warn "$SKIPPED_COUNT migration(s) skipped (missing prerequisite extension); still pending"
fi

if [[ $PENDING_COUNT -eq 0 ]]; then
    TOTAL=$(echo "$APPLIED" | wc -l)
    ok "All migrations up to date ($TOTAL applied)"
else
    ok "Applied $PENDING_COUNT new migrations"
fi

# Grant gpsops full access (migrations run as bgo, objects owned by bgo)
sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -q <<GRANTS
GRANT ALL ON ALL TABLES IN SCHEMA public TO $SERVICE_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $SERVICE_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $SERVICE_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $SERVICE_USER;
GRANTS
ok "Granted $SERVICE_USER full access to all database objects"

else
    warn "Skipping migrations (--skip-db)"
fi

# ===========================================================================
# Phase 8: External tools
# ===========================================================================
if ! $FLAG_SKIP_TOOLS; then
phase 8 "External tools"

# gps-tools repo (git.vedur.is is IMO-internal; intended to be public-anon
# once populated — tracked as TODO on receivers project hub).
# GIT_TERMINAL_PROMPT=0 so we fail fast instead of prompting when the
# repo doesn't exist yet.
if [[ ! -d "$TOOLS_DIR/.git" ]]; then
    echo "  Cloning gps-tools..."
    if sudo -u "$ADMIN_USER" GIT_TERMINAL_PROMPT=0 git clone "$REPO_TOOLS" "$TOOLS_DIR" 2>/dev/null; then
        chmod -R o+rX "$TOOLS_DIR"
        ok "Cloned gps-tools"
    else
        warn "gps-tools not available — proprietary tools must be installed manually"
        warn "See docs/gps-tools-repo.md (or ask bgo) for how to populate it"
    fi
else
    cd "$TOOLS_DIR"
    sudo -u "$ADMIN_USER" GIT_TERMINAL_PROMPT=0 git pull --ff-only 2>/dev/null || true
    chmod -R o+rX "$TOOLS_DIR"
    ok "gps-tools updated"
fi

# Symlink RxTools binaries
# Septentrio bundles .so libraries alongside binaries in rxtools/bin/ (no
# separate lib/). Register bin/ with ld.so so the dynamic linker finds
# libcomms/libgeod/Qt6/etc. when the binaries are called via the
# /usr/local/bin/ symlinks.
if [[ -d "$TOOLS_DIR/rxtools/bin" ]]; then
    # sbfanalyzer is wrapped by runSbfanalyzer (Qt plugin path setup).
    # Symlink the wrapper, not the raw binary.
    [[ -f "$TOOLS_DIR/rxtools/bin/bin2asc" ]] && ln -sf "$TOOLS_DIR/rxtools/bin/bin2asc" /usr/local/bin/bin2asc
    [[ -f "$TOOLS_DIR/rxtools/bin/sbf2rin" ]] && ln -sf "$TOOLS_DIR/rxtools/bin/sbf2rin" /usr/local/bin/sbf2rin
    [[ -f "$TOOLS_DIR/rxtools/bin/runSbfanalyzer" ]] && ln -sf "$TOOLS_DIR/rxtools/bin/runSbfanalyzer" /usr/local/bin/sbfanalyzer
    echo "$TOOLS_DIR/rxtools/bin" > /etc/ld.so.conf.d/rxtools.conf
    ldconfig
    ok "RxTools symlinked (bin + ld.so path)"
fi

# Symlink other tools
for bin in teqc gfzrnx RNX2CRX CRX2RNX runpkr00 mdb2rinex; do
    [[ -f "$TOOLS_DIR/bin/$bin" ]] && ln -sf "$TOOLS_DIR/bin/$bin" /usr/local/bin/
done

# BNC (BKG Ntrip Client) — RTCM3 stream capture for the streaming acquisition mode
# (receivers.streaming / stream_scheduler). Lives in gps-tools/bin/ like the other
# binaries; runtime X11/glib libs come from Phase 1. Optional: stream capture is
# opt-in (gated behind stream_capture.enabled), so a missing bnc is not an error.
[[ -f "$TOOLS_DIR/bin/bnc" ]] && ln -sf "$TOOLS_DIR/bin/bnc" /usr/local/bin/bnc

# Report tool status
echo "  Tool availability:"
for tool in bin2asc sbf2rin teqc gfzrnx RNX2CRX runpkr00 mdb2rinex; do
    if command -v "$tool" &>/dev/null; then
        ok "$tool: $(which "$tool")"
    else
        warn "$tool: not found"
    fi
done

# BNC reported separately — it is optional (only the stream_capture mode needs it).
if command -v bnc &>/dev/null; then
    ok "bnc: $(which bnc) ($(bnc --version 2>/dev/null | head -1))"
else
    echo "  bnc: not installed (optional — only for stream_capture acquisition mode;"
    echo "       add the BNC binary to gps-tools/bin/ to enable it)"
fi

else
    warn "Skipping external tools (--skip-tools)"
fi

# ===========================================================================
# Phase 9: Docker + Grafana + Trimble converter
# ===========================================================================
if ! $FLAG_SKIP_DOCKER; then
phase 9 "Docker + Grafana + Trimble converter"

if command -v docker &>/dev/null; then
    # ── Grafana ──
    COMPOSE_FILE="$INSTALL_DIR/deployment/server/docker-compose.yml"
    if [[ -f "$COMPOSE_FILE" ]]; then
        cd "$(dirname "$COMPOSE_FILE")"
        docker compose down 2>/dev/null || true
        docker compose up -d
        ok "Grafana started on port 3000"
    else
        warn "docker-compose.yml not found at $COMPOSE_FILE"
    fi

    # ── Trimble native RINEX 3 converter (trm2rinex) ──
    TRM_IMAGE="trm2rinex:cli-light"
    TRM_SOURCE="geodesyewsp/trm2rinex:cli-light"

    if docker image inspect "$TRM_IMAGE" &>/dev/null; then
        ok "Trimble converter already installed ($TRM_IMAGE)"
    else
        echo "  Pulling Trimble converter image (~2.4 GB)..."
        if docker pull "$TRM_SOURCE"; then
            docker tag "$TRM_SOURCE" "$TRM_IMAGE"
            ok "Trimble converter installed ($TRM_IMAGE)"
        else
            warn "Failed to pull trm2rinex image"
            warn "Manual: docker pull $TRM_SOURCE && docker tag $TRM_SOURCE $TRM_IMAGE"
        fi
    fi

    # Verify converter
    if docker image inspect "$TRM_IMAGE" &>/dev/null; then
        if docker run --rm --entrypoint="" "$TRM_IMAGE" \
            /opt/wine/bin/wine \
            "C:\\Program Files\\Trimble\\convertToRINEX\\convertToRinex.exe" \
            --help 2>&1 | grep -q "No input file specified"; then
            ok "Trimble convertToRinex verified"
        else
            warn "Trimble converter image exists but verification failed"
        fi
    fi
else
    warn "Docker not installed, skipping Grafana and Trimble converter"
fi

else
    warn "Skipping Docker/Grafana/Trimble converter (--skip-docker)"
fi

# ===========================================================================
# Phase 10: systemd + logrotate
# ===========================================================================
phase 10 "systemd + logrotate"

# ── Scheduler runs as a USER-LEVEL systemd unit owned by gpsops ──
# Rationale: gpsops is the operator account; the operator should not need sudo
# to restart their own service. Operator workflow becomes (no sudo):
#   ssh gpsops@host 'systemctl --user restart gps-receivers-scheduler'
#   ssh gpsops@host 'journalctl --user-unit gps-receivers-scheduler -f'
# bgo continues to own code + venv + install.sh; gpsops owns runtime.

# Step 1 — enable linger so the user unit runs without an active gpsops session.
# Without this, `systemctl --user start` only works while gpsops has a TTY.
SERVICE_UID=$(id -u "$SERVICE_USER")
SERVICE_RUNDIR="/run/user/$SERVICE_UID"

if loginctl show-user "$SERVICE_USER" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
    ok "Linger already enabled for $SERVICE_USER"
else
    loginctl enable-linger "$SERVICE_USER"
    ok "Enabled linger for $SERVICE_USER (user manager auto-starts on boot)"
fi

# Wait for the user manager to come up (LDAP-backed accounts can take longer
# than a hard-coded sleep). Guards against a race where daemon-reload runs
# before /run/user/<uid> is ready.
for _ in {1..30}; do
    [[ -d "$SERVICE_RUNDIR" ]] && break
    sleep 1
done
if [[ ! -d "$SERVICE_RUNDIR" ]]; then
    err "User runtime dir $SERVICE_RUNDIR did not appear after 30s"
    err "Check: loginctl user-status $SERVICE_USER"
    exit 1
fi
ok "User manager ready ($SERVICE_RUNDIR)"

# Step 1b — enable cgroup delegation so MemoryMax / CPUQuota in the user unit
# actually enforce. Without this, those directives in a user unit are accepted
# but silently ignored. The drop-in applies to the user@.service template, so
# all user services for any user get delegation. Idempotent: only writes the
# drop-in if missing or different.
DELEGATE_DROPIN_DIR=/etc/systemd/system/user@.service.d
DELEGATE_DROPIN_FILE="$DELEGATE_DROPIN_DIR/delegate.conf"
DELEGATE_CONTENT='[Service]
Delegate=yes
'
if [[ ! -f "$DELEGATE_DROPIN_FILE" ]] || ! diff -q <(printf '%s' "$DELEGATE_CONTENT") "$DELEGATE_DROPIN_FILE" >/dev/null 2>&1; then
    mkdir -p "$DELEGATE_DROPIN_DIR"
    printf '%s' "$DELEGATE_CONTENT" > "$DELEGATE_DROPIN_FILE"
    chmod 644 "$DELEGATE_DROPIN_FILE"
    systemctl daemon-reload
    ok "Installed user@.service Delegate=yes drop-in (enables MemoryMax/CPUQuota for user units)"
else
    ok "user@.service cgroup delegation already enabled"
fi

# Step 2 — remove legacy system-level unit if present (one-way migration).
LEGACY_UNIT=/etc/systemd/system/gps-receivers-scheduler.service
if [[ -f "$LEGACY_UNIT" ]]; then
    systemctl stop gps-receivers-scheduler 2>/dev/null || true
    systemctl disable gps-receivers-scheduler 2>/dev/null || true
    rm -f "$LEGACY_UNIT"
    systemctl daemon-reload
    ok "Removed legacy system-level unit ($LEGACY_UNIT)"
fi

# Step 3 — install the user unit owned by gpsops.
USER_UNIT_DIR="$GPSOPS_HOME/.config/systemd/user"
USER_UNIT_FILE="$USER_UNIT_DIR/gps-receivers-scheduler.service"
sudo -u "$SERVICE_USER" mkdir -p "$USER_UNIT_DIR"

# Patch paths for this installation. Same sed pattern as before.
sed -e "s|WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" \
    -e "s|ExecStart=.*/receivers |ExecStart=$VENV_DIR/bin/receivers |" \
    -e "s|ExecStop=.*/receivers |ExecStop=$VENV_DIR/bin/receivers |" \
    -e "s|ReadWritePaths=.*|ReadWritePaths=$CACHE_DIR $DATA_DIR /tmp|" \
    "$INSTALL_DIR/deployment/systemd/gps-receivers-scheduler.service" \
    > "$USER_UNIT_FILE"
chown "$SERVICE_USER:$SERVICE_GROUP" "$USER_UNIT_FILE"
chmod 644 "$USER_UNIT_FILE"
ok "User unit installed: $USER_UNIT_FILE"

# Step 4 — reload + enable + start as gpsops via systemctl --user.
gpsops_systemctl daemon-reload
gpsops_systemctl enable gps-receivers-scheduler.service >/dev/null
gpsops_systemctl restart gps-receivers-scheduler.service

# Verify it's actually running. is-active returns 0 when active, 3 otherwise —
# so gate on the exit code rather than scraping output.
if gpsops_systemctl is-active gps-receivers-scheduler.service >/dev/null 2>&1; then
    ok "User-level scheduler service active (managed as $SERVICE_USER, no sudo)"
else
    err "Scheduler failed to start as user unit"
    gpsops_systemctl status gps-receivers-scheduler.service --no-pager 2>&1 | head -25
    exit 1
fi

# Config sync timer — pulls gps-config-data every 10 min and copies safe files
# (stations.cfg, receivers.cfg, scheduler.yaml, icinga.cfg) to CONFIG_DIR.
# Skipped automatically when ~/git/gps-config-data doesn't exist (local-only config).
# Use --skip-config-sync to suppress it explicitly on servers without a git-managed config.
if $FLAG_SKIP_CONFIG_SYNC; then
    warn "Skipping config sync timer (--skip-config-sync)"
elif [[ ! -d "$CONFIG_REPO_DIR/.git" ]]; then
    warn "Skipping config sync timer (gps-config-data repo not found at $CONFIG_REPO_DIR)"
else
    install -m 644 "$INSTALL_DIR/deployment/systemd/gps-config-sync.service" \
        /etc/systemd/system/gps-config-sync.service
    install -m 644 "$INSTALL_DIR/deployment/systemd/gps-config-sync.timer" \
        /etc/systemd/system/gps-config-sync.timer
    chmod +x "$INSTALL_DIR/deployment/server/sync-config.sh"
    systemctl daemon-reload
    systemctl enable --now gps-config-sync.timer
    ok "Config sync timer installed and enabled (pulls every 10 min)"
fi

# Morning recovery report timer — runs daily at 02:00 UTC, summarizes the
# 01:30 UTC morning_recovery job and writes a plaintext report to
# /home/gpsops/morning-recovery-reports/. Operator reviews next morning.
install -m 644 "$INSTALL_DIR/deployment/systemd/gps-morning-recovery-report.service" \
    /etc/systemd/system/gps-morning-recovery-report.service
install -m 644 "$INSTALL_DIR/deployment/systemd/gps-morning-recovery-report.timer" \
    /etc/systemd/system/gps-morning-recovery-report.timer
chmod +x "$INSTALL_DIR/deployment/scripts/morning_recovery_report.sh"
# Pre-create the report dir so the first run doesn't race on mkdir.
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 755 \
    "/home/$SERVICE_USER/morning-recovery-reports"
systemctl daemon-reload
systemctl enable --now gps-morning-recovery-report.timer
ok "Morning recovery report timer installed and enabled (daily 02:00 UTC)"

# Archive-sync health alert timer — runs every 15 min, pushes a passive check
# result to Icinga (rek-d01.gps.vedur.is!Archive sync) so an archive-sync failure
# alerts operators while away. Independent of the scheduler so it catches
# scheduler-down too. Needs the Icinga service object defined server-side to
# actually notify — see docs/monitoring/archive-sync-alert.md.
#
# Installed as a gpsops USER unit (no sudo to manage, credential-light operational
# account) — same model as gps-receivers-scheduler. Linger (Phase 10) lets the
# timer fire without an active gpsops session. ExecStart venv path is patched for
# this install dir.
ASA_SVC="$USER_UNIT_DIR/gps-archive-sync-alert.service"
ASA_TIMER="$USER_UNIT_DIR/gps-archive-sync-alert.timer"
sudo -u "$SERVICE_USER" mkdir -p "$USER_UNIT_DIR"
sed -e "s|ExecStart=.*/bin/python |ExecStart=$VENV_DIR/bin/python |" \
    "$INSTALL_DIR/deployment/systemd/gps-archive-sync-alert.service" > "$ASA_SVC"
install -m 644 "$INSTALL_DIR/deployment/systemd/gps-archive-sync-alert.timer" "$ASA_TIMER"
chown "$SERVICE_USER:$SERVICE_GROUP" "$ASA_SVC" "$ASA_TIMER"
chmod 644 "$ASA_SVC" "$ASA_TIMER"
gpsops_systemctl daemon-reload
gpsops_systemctl enable --now gps-archive-sync-alert.timer >/dev/null
ok "Archive-sync alert timer installed as gpsops user unit (every 15 min → Icinga)"

# Health-monitoring LIVENESS alert timer — runs every 10 min, pushes a passive
# check result to Icinga (rek-d01.gps.vedur.is!Health monitoring). Catches the
# 2026-08-10 failure mode: the health executor starved, fleet monitoring went
# blind for 5.5 h, and nothing alerted — downloads/archiving/RINEX all kept
# running so every other signal stayed green, and a ThreadPoolExecutor queues
# rather than rejects, so pool exhaustion is silent. Runs OUT OF PROCESS from
# the scheduler because the thing it watches is the scheduler failing to run
# its own jobs. Needs the Icinga service object defined server-side to notify.
#
# Installed as a gpsops USER unit (same model as gps-archive-sync-alert). Linger
# (Phase 10) lets the timer fire without an active gpsops session. ExecStart venv
# path is patched for this install dir.
HFA_SVC="$USER_UNIT_DIR/gps-health-freshness-alert.service"
HFA_TIMER="$USER_UNIT_DIR/gps-health-freshness-alert.timer"
sudo -u "$SERVICE_USER" mkdir -p "$USER_UNIT_DIR"
sed -e "s|ExecStart=.*/bin/python |ExecStart=$VENV_DIR/bin/python |" \
    "$INSTALL_DIR/deployment/systemd/gps-health-freshness-alert.service" > "$HFA_SVC"
install -m 644 "$INSTALL_DIR/deployment/systemd/gps-health-freshness-alert.timer" "$HFA_TIMER"
chown "$SERVICE_USER:$SERVICE_GROUP" "$HFA_SVC" "$HFA_TIMER"
chmod 644 "$HFA_SVC" "$HFA_TIMER"
gpsops_systemctl daemon-reload
gpsops_systemctl enable --now gps-health-freshness-alert.timer >/dev/null
ok "Health-liveness alert timer installed as gpsops user unit (every 10 min → Icinga)"

# Open-transaction age alert timer — runs every 5 min, pushes a passive check
# result to Icinga (rek-d01.gps.vedur.is!Transaction age). The RUNTIME half of
# the transaction-leak work: dev/audits/tx_audit.py is blind by construction to
# functions that DO commit while interleaving DB statements with slow non-DB
# work (sync_archive_to_db 373 s, verify_archive_catalog 371 s — both fixed,
# but nothing would report a third). An open transaction pins the vacuum xmin
# horizon, blocks CREATE/DROP INDEX CONCURRENTLY (killed migration 065 six
# times) and holds a connection slot.
#
# Installed as a gpsops USER unit (same model as gps-archive-sync-alert).
TXA_SVC="$USER_UNIT_DIR/gps-tx-age-alert.service"
TXA_TIMER="$USER_UNIT_DIR/gps-tx-age-alert.timer"
sudo -u "$SERVICE_USER" mkdir -p "$USER_UNIT_DIR"
sed -e "s|ExecStart=.*/bin/python |ExecStart=$VENV_DIR/bin/python |" \
    "$INSTALL_DIR/deployment/systemd/gps-tx-age-alert.service" > "$TXA_SVC"
install -m 644 "$INSTALL_DIR/deployment/systemd/gps-tx-age-alert.timer" "$TXA_TIMER"
chown "$SERVICE_USER:$SERVICE_GROUP" "$TXA_SVC" "$TXA_TIMER"
chmod 644 "$TXA_SVC" "$TXA_TIMER"
gpsops_systemctl daemon-reload
gpsops_systemctl enable --now gps-tx-age-alert.timer >/dev/null
ok "Transaction-age alert timer installed as gpsops user unit (every 5 min → Icinga)"

# Host disk + scheduler-liveness alert timer — runs every 5 min, pushes a passive
# check result to Icinga (rek-d01.gps.vedur.is!Host disk and liveness). Catches
# the 2026-07-21 failure mode: a full OS volume (/home) that freezes the scheduler
# while systemd still reports it "active". Runs OUT OF PROCESS from the scheduler
# so it still reports when the scheduler is wedged. Needs the Icinga service object
# defined server-side to actually notify — see
# docs/monitoring/host-disk-liveness-alert.md.
#
# Installed as a gpsops USER unit (same model as gps-archive-sync-alert). Linger
# (Phase 10) lets the timer fire without an active gpsops session. ExecStart venv
# path is patched for this install dir.
HM_SVC="$USER_UNIT_DIR/gps-host-monitor.service"
HM_TIMER="$USER_UNIT_DIR/gps-host-monitor.timer"
sudo -u "$SERVICE_USER" mkdir -p "$USER_UNIT_DIR"
sed -e "s|ExecStart=.*/bin/python |ExecStart=$VENV_DIR/bin/python |" \
    "$INSTALL_DIR/deployment/systemd/gps-host-monitor.service" > "$HM_SVC"
install -m 644 "$INSTALL_DIR/deployment/systemd/gps-host-monitor.timer" "$HM_TIMER"
chown "$SERVICE_USER:$SERVICE_GROUP" "$HM_SVC" "$HM_TIMER"
chmod 644 "$HM_SVC" "$HM_TIMER"
gpsops_systemctl daemon-reload
gpsops_systemctl enable --now gps-host-monitor.timer >/dev/null
ok "Host disk + liveness alert timer installed as gpsops user unit (every 5 min → Icinga)"

# Mirror parity timer — daily 05:20, pushes a passive check to Icinga
# (rek-d01.gps.vedur.is!Mirror parity). The dual-write mirror is best-effort:
# _DualCursor logs a failed mirror leg and DROPS the statement, so every pgdev
# blip is a permanent divergence that nothing noticed until someone looked
# (32,330 rows adrift in file_tracking when first measured, 2026-08-10). This
# only REPORTS — the fix (sweep vs real replication) is still open. Same gpsops
# USER-unit model as the two timers above; the ExecStart is the console script,
# not `python -m`, so the sed patches the venv bin dir rather than the python.
MP_SVC="$USER_UNIT_DIR/gps-mirror-parity.service"
MP_TIMER="$USER_UNIT_DIR/gps-mirror-parity.timer"
sed -e "s|ExecStart=.*/bin/receivers |ExecStart=$VENV_DIR/bin/receivers |" \
    "$INSTALL_DIR/deployment/systemd/gps-mirror-parity.service" > "$MP_SVC"
install -m 644 "$INSTALL_DIR/deployment/systemd/gps-mirror-parity.timer" "$MP_TIMER"
chown "$SERVICE_USER:$SERVICE_GROUP" "$MP_SVC" "$MP_TIMER"
chmod 644 "$MP_SVC" "$MP_TIMER"
gpsops_systemctl daemon-reload
gpsops_systemctl enable --now gps-mirror-parity.timer >/dev/null
ok "Mirror parity timer installed as gpsops user unit (daily 05:20 → Icinga)"

# Logrotate
if [[ -f "$INSTALL_DIR/deployment/logrotate.d/gps-receivers" ]]; then
    # Patch log path to match this installation
    sed -e "s|/home/gpsops/.cache/gps_receivers|$CACHE_DIR|g" \
        "$INSTALL_DIR/deployment/logrotate.d/gps-receivers" \
        > /etc/logrotate.d/gps-receivers
    chmod 644 /etc/logrotate.d/gps-receivers
    ok "logrotate configured"
fi

# ===========================================================================
# Phase 11: Verification
# ===========================================================================
phase 11 "Verification"

WARNINGS=0

# CLI — basic help (does not load config).
# Use full path: sudo-rs sets a restricted PATH that may not include /usr/local/bin.
if sudo -u "$SERVICE_USER" "$VENV_DIR/bin/receivers" --help &>/dev/null; then
    ok "receivers CLI (as $SERVICE_USER)"
else
    err "receivers CLI failed as $SERVICE_USER"
    WARNINGS=$((WARNINGS + 1))
fi

# Config load — actually reads GPS_CONFIG_PATH; catches path/permission bugs
# for both the service user and the admin user (different $HOME → different default path).
for check_user in "$SERVICE_USER" "$ADMIN_USER"; do
    if sudo -u "$check_user" env "GPS_CONFIG_PATH=$CONFIG_DIR" \
            receivers scheduler status --show-jobs &>/dev/null 2>&1; then
        ok "Config loads as $check_user (GPS_CONFIG_PATH=$CONFIG_DIR)"
    else
        # Tolerate "no scheduler running" — the failure we care about is config-not-found
        config_err=$(sudo -u "$check_user" env "GPS_CONFIG_PATH=$CONFIG_DIR" \
            receivers scheduler status 2>&1 | grep -i "does not exist\|not found\|No such file" || true)
        if [[ -n "$config_err" ]]; then
            err "Config not found for $check_user: $config_err"
            WARNINGS=$((WARNINGS + 1))
        else
            ok "Config loads as $check_user (GPS_CONFIG_PATH=$CONFIG_DIR)"
        fi
    fi
done

# Config files
for f in stations.cfg receivers.cfg scheduler.yaml database.cfg agencies.yaml; do
    if [[ -f "$CONFIG_DIR/$f" ]]; then
        ok "Config: $f"
    else
        err "Missing: $CONFIG_DIR/$f"
        WARNINGS=$((WARNINGS + 1))
    fi
done

# Database
if ! $FLAG_SKIP_DB; then
    MIGRATION_COUNT=$(sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -tAc \
        "SELECT count(*) FROM schema_migrations" 2>/dev/null || echo "0")
    ok "Database: $MIGRATION_COUNT migrations applied"
fi

# External tools
TOOL_COUNT=0
for tool in bin2asc sbf2rin teqc gfzrnx; do
    command -v "$tool" &>/dev/null && TOOL_COUNT=$((TOOL_COUNT + 1))
done
if [[ $TOOL_COUNT -eq 4 ]]; then
    ok "External tools: all present"
elif [[ $TOOL_COUNT -gt 0 ]]; then
    warn "External tools: $TOOL_COUNT/4 present"
else
    warn "External tools: none (install via gps-tools repo)"
fi

# Docker
if ! $FLAG_SKIP_DOCKER && command -v docker &>/dev/null; then
    if docker ps --format '{{.Names}}' | grep -q gps-grafana; then
        ok "Grafana: running on port 3000"
    else
        warn "Grafana: not running"
        WARNINGS=$((WARNINGS + 1))
    fi
    if docker image inspect trm2rinex:cli-light &>/dev/null; then
        ok "Trimble converter: installed"
    else
        warn "Trimble converter: not installed"
        WARNINGS=$((WARNINGS + 1))
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────
IP_ADDR=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo -e "${BLUE}=== Installation Complete ===${NC}"
echo ""
echo "  Repos:      $GIT_BASE (owned by $ADMIN_USER)"
echo "  Venv:       $VENV_DIR (owned by $ADMIN_USER)"
echo "  Config:     $CONFIG_DIR (bgo:gpsops)"
echo "  Data:       $DATA_DIR (owned by $SERVICE_USER)"
echo "  Logs:       $CACHE_DIR/logs/ (owned by $SERVICE_USER)"
if $FLAG_DEV; then
    echo "  Mode:       dev — gtimes/gps_parser/tostools are editable at $GIT_BASE/{gtimes,gps_parser,tostools}"
else
    echo "  Mode:       production — gtimes/gps_parser/tostools pinned via pyproject.toml git URLs"
    echo "              (to switch to editable siblings later: re-run with --dev)"
fi
if [[ $WARNINGS -gt 0 ]]; then
    echo ""
    warn "$WARNINGS warnings — review output above"
fi
echo ""
echo "Day-to-day operations:"
echo ""
echo "  # bgo owns code + venv. gpsops owns the runtime service."
echo ""
echo "  # Update code (as bgo, no sudo for the service restart):"
echo "  cd ~/git/receivers && git pull"
echo "  ~/git/receivers/venv/bin/pip install -e ."
echo "  ssh $SERVICE_USER@\$(hostname) 'systemctl --user restart gps-receivers-scheduler'"
if $FLAG_DEV; then
    echo ""
    echo "  # Update sibling package (editable — git pull is live, no reinstall):"
    echo "  cd ~/git/gtimes && git pull"
    echo "  ssh $SERVICE_USER@\$(hostname) 'systemctl --user restart gps-receivers-scheduler'"
fi
echo ""
echo "  # Manual download (as gpsops directly, no sudo):"
echo "  ssh $SERVICE_USER@\$(hostname) 'receivers download ELDC --sync --archive'"
echo ""
echo "  # Service management (as gpsops, no sudo):"
echo "  ssh $SERVICE_USER@\$(hostname) 'systemctl --user status  gps-receivers-scheduler'"
echo "  ssh $SERVICE_USER@\$(hostname) 'systemctl --user restart gps-receivers-scheduler'"
echo "  ssh $SERVICE_USER@\$(hostname) 'journalctl --user-unit gps-receivers-scheduler -f'"
echo ""
echo "  # Grafana: http://${IP_ADDR:-<server-ip>}:3000"
echo ""
