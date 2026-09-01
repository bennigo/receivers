"""
Standardized CLI argument definitions for receivers commands.

Flag Conventions:
- All long flags use kebab-case (--start-date, not --start_date)
- Short flags reserved for frequently used options
- Common flags shared across subcommands where applicable

Common Flags:
  -v, --verbose     Verbose output (all commands)
  -f, --force       Force overwrite/redo (download, health)

Date Flags (download, health):
  -s, --start       Start date YYYYMMDD
  -e, --end         End date YYYYMMDD
  -d, --days        Number of periods back
"""

import argparse
import logging
from typing import Iterable, List, Optional

# Try to get defaults from gps_parser
try:
    import gps_parser

    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    gps_parser = None


def normalize_station_tokens(tokens: Optional[Iterable[str]]) -> List[str]:
    """Normalize a list of station tokens from positional CLI args.

    Splits each token on commas, strips whitespace and stray punctuation,
    filters empty strings, and uppercases. Tolerates the common shell mishaps
    of pasting comma-separated lists where args are expected to be space-separated:

        ['AFST', 'ENTC']            -> ['AFST', 'ENTC']
        ['AFST,', 'ENTC,', 'FAGD']  -> ['AFST', 'ENTC', 'FAGD']
        ['AFST,ENTC,FAGD']          -> ['AFST', 'ENTC', 'FAGD']
        ['afst', ',', '']           -> ['AFST']
    """
    out: List[str] = []
    for tok in tokens or []:
        for piece in tok.split(","):
            sid = piece.strip().strip(";").strip()
            if sid:
                out.append(sid.upper())
    return out


def get_default_values():
    """Get default values from gps_parser or use fallbacks."""
    try:
        if HAS_GPS_PARSER:
            parser_config = gps_parser.ConfigParser()
            return {
                "days": parser_config.getDefaultValue("default_days_back") or 10,
                "session": parser_config.getDefaultValue("default_session")
                or "15s_24hr",
                "compression": parser_config.getDefaultValue("default_compression")
                or ".gz",
            }
    except Exception:
        pass

    return {
        "days": 10,
        "session": "15s_24hr",
        "compression": ".gz",
    }


def add_verbose_flag(parser: argparse.ArgumentParser) -> None:
    """Add standard verbose flag."""
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.INFO,
        help="Enable verbose output",
    )


def add_force_flag(parser: argparse.ArgumentParser) -> None:
    """Add standard force flag."""
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force overwrite even if data unchanged",
    )


def add_date_flags(parser: argparse.ArgumentParser, include_days: bool = True) -> None:
    """Add standard date range flags.

    Args:
        parser: ArgumentParser to add flags to
        include_days: Whether to include --days flag
    """
    parser.add_argument(
        "-s",
        "--start",
        type=str,
        metavar="YYYYMMDD",
        help="Start date (format: YYYYMMDD or YYYYMMDD-HHMM)",
    )

    parser.add_argument(
        "-e",
        "--end",
        type=str,
        metavar="YYYYMMDD",
        help="End date (format: YYYYMMDD or YYYYMMDD-HHMM)",
    )

    if include_days:
        defaults = get_default_values()
        parser.add_argument(
            "-d",
            "--days",
            type=int,
            default=defaults["days"],
            metavar="N",
            help=f"Number of periods back (default: {defaults['days']})",
        )


def setup_download_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the download subcommand parser."""
    defaults = get_default_values()

    parser = subparsers.add_parser(
        "download",
        help="Download data from GPS receivers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Download GPS receiver data for specified stations.

Examples:
  receivers download ELDC THOB --sync --archive
  receivers download ELDC -s 20260101 -e 20260107 --sync
  receivers download ELDC -d 5 --session 1Hz_1hr --sync
        """,
    )

    # Positional: stations (multiple, optional when --all is used)
    parser.add_argument(
        "stations",
        nargs="*",
        metavar="STATION",
        help="Station IDs to download (e.g., ELDC THOB)",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_stations",
        help="Download all configured stations",
    )

    # Date/time options
    add_date_flags(parser, include_days=True)

    # Session options
    parser.add_argument(
        "--session",
        type=str,
        default=defaults["session"],
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help=f"Data session type (default: {defaults['session']})",
    )

    parser.add_argument(
        "--compression",
        type=str,
        default=defaults["compression"],
        metavar="EXT",
        help=f"Compression type (default: {defaults['compression']})",
    )

    # Download behavior
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Enable actual download (sync new/partial files)",
    )

    parser.add_argument(
        "--archive",
        action="store_true",
        help="Archive downloaded data to final location",
    )

    parser.add_argument(
        "--external",
        action="store_true",
        help=(
            "Fetch from an external server (stations.cfg "
            "external_url_template) instead of the receiver directly. "
            "Respects --sync (actually fetch) and --archive (archive + "
            "index in file_tracking). Implied by operational_status=external."
        ),
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        dest="clean_tmp",  # Keep internal name for compatibility
        help="Clean temp directory before download",
    )

    parser.add_argument(
        "--test-connection", action="store_true", help="Test connection before download"
    )

    parser.add_argument(
        "--respect-known-missing",
        action="store_true",
        help=(
            "Honor file_tracking 'missing' markers (skip files previously "
            "marked as not on receiver). Default: ignore them — operator-issued "
            "downloads always retry, since 'missing' may have been written by "
            "a transient connection failure."
        ),
    )

    # Production/logging options
    parser.add_argument(
        "--production",
        action="store_true",
        help="Enable production logging (concise, structured)",
    )

    parser.add_argument(
        "--json-log",
        action="store_true",
        help="Output logs in JSON format (for monitoring systems)",
    )

    # Advanced options (rarely used)
    advanced = parser.add_argument_group("advanced options")
    advanced.add_argument(
        "--file-frequency",
        type=str,
        default="",
        metavar="FREQ",
        dest="ffrequency",
        help="Override file frequency (auto-detected from session)",
    )

    advanced.add_argument(
        "--acq-frequency",
        type=str,
        default="",
        metavar="FREQ",
        dest="afrequency",
        help="Override acquisition frequency (auto-detected from session)",
    )

    # Parallel download options
    parallel_group = parser.add_argument_group("parallel download options")
    parallel_group.add_argument(
        "--parallel",
        action="store_true",
        help="Download stations in parallel (grouped batches with stagger)",
    )
    parallel_group.add_argument(
        "--batches",
        type=int,
        default=None,
        metavar="N",
        help="Number of batch groups (default: from scheduler.yaml or 10)",
    )
    parallel_group.add_argument(
        "--distribution-window",
        type=float,
        default=None,
        metavar="MINUTES",
        dest="distribution_window",
        help="Minutes to spread batch groups across (default: from scheduler.yaml)",
    )
    parallel_group.add_argument(
        "--retry-delay",
        type=float,
        default=90.0,
        metavar="SECONDS",
        dest="retry_delay",
        help="Seconds before retrying unreachable stations (default: 90)",
    )
    parallel_group.add_argument(
        "--max-retries",
        type=int,
        default=3,
        metavar="N",
        dest="max_retries",
        help=(
            "Maximum retry attempts per file (default: 3). In parallel mode "
            "also controls the number of station-level retry passes. For "
            "slow/large files (e.g. AUST), increase this so chip-away "
            "progress can complete in one run."
        ),
    )

    # Post-download processing
    post_group = parser.add_argument_group("post-download processing")
    post_group.add_argument(
        "--rinex",
        action="store_true",
        help="Convert raw files to RINEX after download (fire-and-forget, non-blocking)",
    )

    add_verbose_flag(parser)

    return parser


def add_host_flags(parser: argparse.ArgumentParser) -> None:
    """Add --host / --receiver-type flags for direct (desk) receiver connections.

    When --host is given the command bypasses stations.cfg and connects
    directly to the receiver using native ports (21 FTP, 80 HTTP, 28784 control).
    Only one station label is allowed when --host is specified.
    """
    direct_group = parser.add_argument_group("direct connection (desk/bench setup)")
    direct_group.add_argument(
        "--host",
        metavar="IP",
        help=(
            "Direct connection IP/hostname — bypasses stations.cfg and uses native "
            "receiver ports (21 FTP, 80 HTTP, 28784 TCP control). "
            "Only one station label is allowed with --host."
        ),
    )
    direct_group.add_argument(
        "--receiver-type",
        metavar="TYPE",
        default="PolaRX5",
        help="Receiver type for direct connection (default: PolaRX5)",
    )


def setup_status_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the status subcommand parser."""
    parser = subparsers.add_parser(
        "status",
        help="Quick receiver status check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Quick status check for a GPS receiver.

Shows a compact overview of receiver health including connection status,
voltage, temperature, and CPU load. Uses the same data source as the
'health' command but with simplified output for quick checks.

Examples:
  # Quick status check
  receivers status ISFS

  # JSON output for scripting
  receivers status ISFS --json
""",
    )

    parser.add_argument(
        "stations", metavar="STATION", nargs="+", help="Station ID(s) to check"
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON (same format as health command)",
    )

    parser.add_argument(
        "--save-db", action="store_true", help="Save health data to database"
    )

    parser.add_argument(
        "--icinga",
        action="store_true",
        help="Send check results to Icinga monitoring system",
    )

    add_host_flags(parser)
    add_verbose_flag(parser)

    return parser


def setup_health_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the health subcommand parser."""
    parser = subparsers.add_parser(
        "health",
        help="Get receiver health information",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Get health metrics from GPS receiver or extract historical data.

Live health check (no date flags):
  receivers health ISFS
  receivers health ISFS --json

Extract historical data:
  receivers health ISFS -s 20260113              # Single day
  receivers health ISFS -s 20260110 -e 20260113  # Date range
  receivers health ISFS -d 24                    # Last 24 hours (spans 1-2 days)

Multiple stations:
  receivers health ELDC THOB ISFS --import-json  # Import all
  receivers health ELDC ELEY --export-json       # Export all
  receivers health ELDC THOB -s 20260101 -e 20260115 --save-db

All stations (parallel):
  receivers health --all --workers 10 --compact
  receivers health --all --workers 20 --save-db
        """,
    )

    parser.add_argument(
        "stations",
        metavar="STATION",
        nargs="*",
        help="Station ID(s) to process (e.g., ISFS or ELDC THOB ISFS)",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_stations",
        help="Run health check on all configured stations",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel workers (default: 1, sequential)",
    )

    # Date options for extraction (same as download command)
    date_group = parser.add_argument_group("extraction options")
    add_date_flags(date_group, include_days=False)  # Add -s, -e

    date_group.add_argument(
        "-d",
        "--days",
        type=int,
        metavar="N",
        help="Extract N periods back (hours for status_1hr, consistent with download)",
    )

    date_group.add_argument(
        "--extract-all", action="store_true", help="Extract all available SBF files"
    )

    date_group.add_argument(
        "--import-json",
        action="store_true",
        help="Import existing JSON health files to database",
    )

    date_group.add_argument(
        "--export-json",
        action="store_true",
        help="Export health data from database to JSON files",
    )

    date_group.add_argument(
        "--json-dir",
        type=str,
        metavar="PATH",
        help="Directory for JSON health files (auto-detected if not specified)",
    )

    # Output options
    output_group = parser.add_argument_group("output options")
    output_group.add_argument(
        "--json", action="store_true", help="Output health data as JSON"
    )

    output_group.add_argument(
        "--save-json", action="store_true", help="Save health data to JSON file"
    )

    output_group.add_argument(
        "--save-db", action="store_true", help="Save health data to PostgreSQL database"
    )

    # Live-mode options (no date flags)
    live_group = parser.add_argument_group("live-mode options")
    live_group.add_argument(
        "--compact",
        action="store_true",
        help='Compact status display (same format as "status" command)',
    )

    live_group.add_argument(
        "--icinga",
        action="store_true",
        help="Send check results to Icinga monitoring system",
    )

    live_group.add_argument(
        "--no-files", action="store_true", help="Skip file system checks in live mode"
    )

    live_group.add_argument(
        "--no-ntrip", action="store_true", help="Skip NTRIP/RTK checks in live mode"
    )

    live_group.add_argument(
        "--update-cfg",
        action="store_true",
        help=(
            "Write reported receiver identity (model/firmware/serial) into "
            "stations.cfg. Off by default; the canonical workflow is "
            "'receivers cfg reconcile'."
        ),
    )

    # Extraction behavior
    behavior_group = parser.add_argument_group("behavior options")
    add_force_flag(behavior_group)

    behavior_group.add_argument(
        "--skip-blocks", action="store_true", help="Skip per-block JSON extraction"
    )

    add_host_flags(parser)
    add_verbose_flag(parser)

    return parser


def setup_validate_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the validate subcommand parser."""
    parser = subparsers.add_parser(
        "validate",
        help="Validate receiver type configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Validate that configured receiver types match actual hardware.

Examples:
  receivers validate              # Validate all stations
  receivers validate ELDC THOB    # Validate specific stations
  receivers validate --web        # Use web scraping (more accurate)
        """,
    )

    parser.add_argument(
        "stations",
        nargs="*",
        metavar="STATION",
        help="Station IDs to validate (default: all stations)",
    )

    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-fix mismatched receiver types (EXPERIMENTAL)",
    )

    parser.add_argument(
        "--report", action="store_true", help="Generate detailed correction report"
    )

    parser.add_argument(
        "--web",
        "-w",
        action="store_true",
        dest="web_accuracy",
        help="Use web interface scraping (more accurate)",
    )

    parser.add_argument("--summary", action="store_true", help="Show summary only")

    add_verbose_flag(parser)

    return parser


def setup_rec_config_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the rec-config subcommand parser."""
    parser = subparsers.add_parser(
        "rec-config",
        help="Extract or push configuration for Septentrio receivers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Extract or push configuration for Septentrio PolaRx5 receivers via TCP.

By default, extracted configs are printed to stdout (Unix convention).
Use --save to store to file with naming: {ReceiverType}_{StationID}_{ConfigType}_{Timestamp}.txt

Examples:
  # Extract config to stdout
  receivers rec-config THOB --extract
  receivers rec-config THOB --extract --config-type Boot

  # Save config to file (uses rec_config_dir from receivers.cfg or /tmp/polarconfig/)
  receivers rec-config THOB --extract --save
  receivers rec-config THOB,ISFS --extract --save --output-dir ~/configs/

  # Push config to receiver
  receivers rec-config THOB --push config_file.txt
  receivers rec-config THOB --push config_file.txt --dry-run
  receivers rec-config THOB --push config_file.txt --no-save

  # Compare configs
  receivers rec-config THOB --extract --diff-with ~/configs/old_config.txt

  # Check whether a logging session is enabled
  receivers rec-config THOB --check-session status_1hr
  receivers rec-config THOB,ISFS,ELDC --check-session status_1hr

  # Enable status_1hr logging session (pushes canonical SBF stream + LogSession)
  receivers rec-config THOB --enable-session status_1hr --dry-run
  receivers rec-config THOB --enable-session status_1hr

  # Force re-push of session template (after editing canonical block list)
  receivers rec-config THOB,ELDC --update-session status_1hr --dry-run
  receivers rec-config THOB,ELDC --update-session status_1hr

  # Audit drift between receiver state and canonical template
  receivers rec-config THOB --audit-session status_1hr
  receivers rec-config THOB,ELDC,OLKE --audit-session status_1hr
        """,
    )

    parser.add_argument(
        "stations",
        metavar="STATIONS",
        help="Station ID(s), comma-separated (e.g., THOB or THOB,ISFS)",
    )

    # Operation mode (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--extract",
        action="store_true",
        help="Extract configuration from receiver (prints to stdout by default)",
    )
    mode_group.add_argument(
        "--push", metavar="CONFIG_FILE", help="Push configuration file to receiver"
    )
    mode_group.add_argument(
        "--tracking",
        metavar="SPEC",
        help="Set signal tracking to a constellation spec, e.g. 'gps+glonass' "
        "(power-save for wind/solar). Pushes ONLY setSignalTracking/setSignalUsage "
        "+ boot save — never touches marker/mountpoint/sessions, so it's safe on a "
        "live stream station. Constellations: gps, glonass, galileo, beidou.",
    )
    mode_group.add_argument(
        "--set-antenna",
        action="store_true",
        help="Push the station's reconciled antenna identity (type/radome/serial/"
        "offsets from stations.cfg) into the receiver — ONLY setAntennaOffset + "
        "boot save, identity-safe. Closes the loop after `cfg reconcile`: RINEX "
        "ANT # / TYPE headers echo the RECEIVER's configured antenna, so after a "
        "swap the box keeps emitting the old antenna until this push. Unknown "
        "serial (cfg zeros) is pushed as '0000000000'. Per-station values — each "
        "station gets its own push. Septentrio PolaRx5 only.",
    )
    mode_group.add_argument(
        "--set-domes",
        action="store_true",
        help="Push the station's IERS DOMES into the receiver's MARKER NUMBER "
        "(rinex_marker_number from stations.cfg) — ONLY setMarkerParameters "
        "arg 2 + boot save, so the marker name and 4-char station code are left "
        "untouched. Stations without a real DOMES are SKIPPED, never filled with "
        "the 4-char id (MARKER NUMBER carries the DOMES and nothing else). "
        "Marker-side sibling of --set-antenna. Septentrio PolaRx5 only.",
    )
    mode_group.add_argument(
        "--check-session",
        metavar="SESSION",
        help="Check whether a logging session is enabled (e.g. status_1hr). "
        "Exit code 0 if enabled on all targets, non-zero otherwise.",
    )
    mode_group.add_argument(
        "--enable-session",
        metavar="SESSION",
        help="Enable a logging session on receiver "
        "(currently supports: status_1hr, 15s_24hr). "
        "Pre-checks each station and skips if already enabled.",
    )
    mode_group.add_argument(
        "--update-session",
        metavar="SESSION",
        help="Force re-push of a session template, overwriting receiver state. "
        "Use after editing the canonical template to propagate changes. "
        "Currently supports: status_1hr, 15s_24hr.",
    )
    mode_group.add_argument(
        "--audit-session",
        metavar="SESSION",
        help="Compare receiver session config to canonical template; report drift "
        "(SBF block list, interval, retention, priority, file naming). "
        "Exit code 0 only when all targets match the template.",
    )
    mode_group.add_argument(
        "--ntrip-stream",
        nargs=2,
        metavar=("NTR", "STATE"),
        help="Turn an NTRIP server connection on/off, e.g. '--ntrip-stream NTR2 off'. "
        "STATE: off | on | server | client. Pushes ONLY setNtripSettings + boot save "
        "(+ setSBFOutput …, none for the feeding stream with --drop-sbf) — identity-safe: "
        "leaves other mounts / marker / file logging untouched. Disabling keeps the "
        "connection's caster/credentials/mountpoint, so it's trivially re-enabled.",
    )
    mode_group.add_argument(
        "--disable-mount",
        metavar="MOUNTPOINT",
        help="Disable the NTRIP server stream serving MOUNTPOINT by name "
        "(e.g. '--disable-mount HRIC1') — reads the receiver to resolve the mountpoint "
        "to its NTRx connection, then sets it off. Add --drop-sbf to also stop the "
        "SBF stream that fed it.",
    )

    parser.add_argument(
        "--drop-sbf",
        action="store_true",
        help="With --ntrip-stream/--disable-mount: also disable the SBF output "
        "stream(s) feeding that connection (setSBFOutput …, none), so the receiver "
        "stops generating an output that no longer goes anywhere. Does NOT affect "
        "file logging — the LOG* streams are separate.",
    )

    parser.add_argument(
        "--config-type",
        choices=["Current", "Boot"],
        default="Current",
        help="Configuration type to extract (default: Current)",
    )

    parser.add_argument(
        "--save",
        action="store_true",
        help="Save extracted config to file (default: print to stdout)",
    )

    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        help="Override output directory for --save (default: rec_config_dir from config or /tmp/polarconfig/)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing",
    )

    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not save config to boot after push (default: saves to Boot)",
    )

    parser.add_argument(
        "--diff-with",
        metavar="FILE",
        help="Compare extracted config with existing file",
    )

    parser.add_argument(
        "--port",
        type=int,
        metavar="PORT",
        help="Override control port (default from config: 28784)",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=10,
        metavar="SECONDS",
        help="Connection timeout in seconds (default: 10)",
    )

    add_host_flags(parser)
    add_verbose_flag(parser)

    return parser


def setup_rec_provision_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the rec-provision subcommand parser."""
    parser = subparsers.add_parser(
        "rec-provision",
        help="Provision a Septentrio PolaRx5 receiver (fw 5.7.0)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Provision a Septentrio PolaRx5 receiver running fw 5.7.0.

Creates standard user accounts, enables FTP, disables HTTPS redirect,
pushes the SSH public key to the gpsops account, and saves to Boot.

Handles two cases automatically:
  - Fresh receiver (no accounts): uses factory bootstrap credentials
    (RxAdmin / S3pt3ntr10) to create gpsops as User1, then sets up
    the rest.
  - Already provisioned: skips account creation, updates FTP/HTTPS
    settings and SSH key if needed.

Credentials are read from receivers.cfg [polarx5]:
  tcp_username, tcp_password, tcp_ssh_key_path

Examples:
  receivers rec-provision GJAC
  receivers rec-provision ORFC GJAC
  receivers rec-provision GJAC --dry-run
  receivers rec-provision GJAC --skip-ssh-key

Desk/bench setup (one-shot bootstrap):
  receivers rec-provision BENCH --host 192.168.3.1 --bootstrap
  receivers rec-provision BENCH --host 192.168.3.1 --bootstrap --apply-config path/to/config.txt
        """,
    )

    parser.add_argument(
        "stations",
        metavar="STATIONS",
        nargs="+",
        help="Station ID(s) to provision",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing",
    )

    parser.add_argument(
        "--skip-ssh-key",
        action="store_true",
        help="Do not push SSH public key to receiver",
    )

    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "Standard bench profile: sets --set-ip to desk_bootstrap_ip from receivers.cfg "
            "(default 192.168.100.60), fills --dns1/--dns2 from config. "
            "Requires --host. Bench/desk use only — do not use on deployed stations."
        ),
    )

    parser.add_argument(
        "--port",
        type=int,
        metavar="PORT",
        help="Override control port (default from config: 28784)",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=15,
        metavar="SECONDS",
        help="Connection timeout in seconds (default: 15)",
    )

    net_group = parser.add_argument_group("network setup (desk/bench)")
    net_group.add_argument(
        "--set-ip",
        metavar="IP",
        help=(
            "Assign static IP to receiver via setIPSettings. "
            "Permanent command — takes effect on reboot. Desk/bench use only."
        ),
    )
    net_group.add_argument(
        "--gateway",
        metavar="GW",
        default=None,
        help="Gateway for --set-ip (default: desk_gateway from receivers.cfg or 192.168.100.1)",
    )
    net_group.add_argument(
        "--netmask",
        metavar="MASK",
        default=None,
        help="Subnet mask for --set-ip (default: 255.255.255.0)",
    )
    net_group.add_argument(
        "--dns1",
        metavar="IP",
        default=None,
        help="Primary DNS for --set-ip (default: desk_dns1 from receivers.cfg)",
    )
    net_group.add_argument(
        "--dns2",
        metavar="IP",
        default=None,
        help="Secondary DNS for --set-ip (default: desk_dns2 from receivers.cfg)",
    )

    cfg_group = parser.add_argument_group("receiver config")
    cfg_group.add_argument(
        "--apply-config",
        metavar="PATH",
        help=(
            "Push a receiver config script after provisioning "
            "(e.g. TEST_PolaRx5_GPS_GLONASS_only.txt). "
            "Sent as Expert Console upload — file must not contain sual/setUserAccessLevel lines."
        ),
    )

    add_host_flags(parser)
    add_verbose_flag(parser)

    return parser


def setup_rec_upgrade_firmware_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the rec-upgrade-firmware subcommand parser."""
    parser = subparsers.add_parser(
        "rec-upgrade-firmware",
        help="Upgrade PolaRX5 firmware (stream-download flash + chained aftermath)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Flash new GNSS firmware onto a Septentrio PolaRX5, then restore services and
record the new version. DRY-RUN BY DEFAULT — pass --no-dry-run to actually flash.

Two flash cores (--flash-backend):

  rxupgrade  (DEFAULT)  Shell out to Septentrio's own RxTools tool. This is the
                        proven path — it flashed VMEY 5.6.0→5.7.0 on 2026-08-20 —
                        and it RESTORES THE RECEIVER CONFIG afterwards, which is
                        the likely reason RxTools-flashed stations (AFST, ROTH,
                        JONC) keep plaintext 28784 where the tcp core leaves
                        sis=secure. Needs the gps-tools RxTools toolchain.
  tcp                   The hand-rolled manual-download implementation over the
                        command port. EXPERIMENTAL — it left OLAC in recovery
                        mode — so on a deployed station it still refuses without
                        --allow-deployed-flash.

What it does per station:
  1. Resolve router_ip:receiver_controlport from stations.cfg (handles shared-IP
     / non-standard port-forward stations, e.g. OLAC/KASC on 10.4.1.43).
  2. Ensure the TLS reconnect lifeline exists: a control_port-1 → receiver:28783
     DNAT forward on the router (auto-added with --ensure-port-forward). The tcp
     core reboots the receiver into sis=secure, so 28784 closes and 28783 is the
     only way back in; the rxupgrade core normally preserves 28784, but the
     forward is cheap insurance either way.
  3. Probe current firmware; skip if already at/above target.
  4. Flash via the selected core.
  5. Confirm the new version (lif,Identification), over TLS if plaintext closed.
  6. Chain the aftermath (unless suppressed): rec-provision (restore sis/shs/FTP)
     → cfg update-device --change (TOS) → cfg reconcile --global --push
     (stations.cfg + repo) → health.

⚠ The vendor tool's EXIT STATUS IS NOT THE VERDICT. It flashes, the receiver
   reboots, then it reconnects to confirm — and that reconnect times out whenever
   the reboot closed the port it came in on. VMEY's successful run ended with
   "Upgrade Finished" followed by "Error: Connection timed out." So the verdict is
   read from the transcript, and a trailing error there is expected, not a fault.

⚠ A botched flash with the 28783 forward missing needs a physical site visit.

Examples:
  receivers rec-upgrade-firmware VONC --to 5.7.0                 # dry-run plan
  receivers rec-upgrade-firmware VONC --to 5.7.0 --no-dry-run    # vendor flash
  receivers rec-upgrade-firmware KASC --suf /path/PolaRx5-5.7.0.suf --no-dry-run
  receivers rec-upgrade-firmware BENCH --host 192.168.3.1 --flash-backend tcp \\
      --to 5.7.0 --no-dry-run                                    # bench, tcp core
        """,
    )
    parser.add_argument("stations", metavar="STATIONS", nargs="+", help="Station ID(s)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--to",
        metavar="VERSION",
        help="Target firmware version (resolves .suf from --firmware-dir)",
    )
    src.add_argument(
        "--suf", metavar="PATH", help="Explicit .suf firmware file (overrides --to)"
    )
    parser.add_argument(
        "--firmware-dir",
        metavar="DIR",
        help="Root holding <version>/firmware/PolaRx5-<version>.suf "
        "(default: receivers.cfg [paths] firmware_dir).",
    )
    parser.add_argument(
        "--via",
        metavar="V1,V2",
        help="Explicit stepped path before --to (default: direct; .suf is a full image).",
    )
    parser.add_argument(
        "--host",
        metavar="HOST[:PORT]",
        help="Bench/manual host override (skips stations.cfg).",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually flash. Without it, validates the plan and sends nothing.",
    )
    parser.add_argument(
        "--ensure-port-forward",
        action="store_true",
        help="Auto-add the TLS-port → receiver:28783 forward on the router (needs router API).",
    )
    parser.add_argument(
        "--tls-port",
        type=int,
        metavar="PORT",
        help="Router port that forwards to receiver:28783 (default: control_port-1). "
        "The post-upgrade reconnect uses this; it is verified by a TLS handshake.",
    )
    parser.add_argument(
        "--flash-backend",
        choices=("rxupgrade", "tcp"),
        default="rxupgrade",
        help="Which flash core to use. 'rxupgrade' (default) shells out to "
        "Septentrio's own RxTools tool — the proven path, and it restores the "
        "receiver config afterwards, which is why RxTools-flashed stations keep "
        "plaintext 28784. 'tcp' is the hand-rolled manual-download implementation: "
        "EXPERIMENTAL, not hardware-proven, and still gated by "
        "--allow-deployed-flash on deployed stations.",
    )
    parser.add_argument(
        "--rxupgrade-bin",
        metavar="PATH",
        help="Override the rxupgrade binary (default: runRxupgrade on PATH, then "
        "/usr/local/rxtools/bin).",
    )
    parser.add_argument(
        "--no-restore-config",
        action="store_true",
        help="Pass the vendor's -n: do NOT restore the receiver configuration "
        "after upgrading. Off by default — letting rxupgrade restore the config is "
        "what keeps plaintext 28784 open across the flash.",
    )
    parser.add_argument(
        "--ignore-ssl-errors",
        action="store_true",
        help="Pass the vendor's -i (ignore SSL errors during the upgrade).",
    )
    parser.add_argument(
        "--rxupgrade-timeout",
        type=int,
        default=1800,
        metavar="SECONDS",
        help="Wall-clock budget for the vendor tool (default 1800). The image is "
        "~35 MB and stations sit on 3G/4G links. A timeout is not read as a failed "
        "flash — the transcript is the verdict.",
    )
    parser.add_argument(
        "--allow-deployed-flash",
        action="store_true",
        help="Override the bench-only guard on the 'tcp' backend. That core is "
        "EXPERIMENTAL (left a deployed receiver in recovery mode) — with "
        "--flash-backend tcp and no --host, a real flash is refused unless this is "
        "set. Irrelevant to the default rxupgrade backend.",
    )
    parser.add_argument(
        "--no-provision",
        action="store_true",
        help="Skip the chained post-flash rec-provision step.",
    )
    parser.add_argument(
        "--no-pre-provision",
        action="store_true",
        help="Skip the PRE-flash rec-provision that runs when the target crosses "
        "the 5.7 auth cliff. Below 5.7 a receiver serves commands even with no "
        "user accounts, so an account-less station looks healthy — until 5.7 "
        "makes auth mandatory and the scheduler's health poll locks it out "
        "globally for ~19 h (VONC, 2026-08-20). Only use this if you know the "
        "accounts already exist.",
    )
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="Skip the chained TOS/cfg recording steps.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the pre-flash confirmation prompt.",
    )
    parser.add_argument(
        "--port", type=int, metavar="PORT", help="Override control port."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="Connect timeout (default 15).",
    )
    parser.add_argument(
        "--reboot-wait",
        type=int,
        default=240,
        metavar="SECONDS",
        help="Max wait for the receiver to return after flashing (default 240).",
    )

    parser.add_argument(
        "--post-flash-settle",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Seconds to let the receiver settle AFTER its control port starts "
            "listening, before the single post-flash login attempt (default "
            "180). The port returns well before the auth subsystem does, and a "
            "login fired at that moment is rejected for 'not ready' — which is "
            "indistinguishable from bad credentials and counts toward the "
            "5.7.0 brute-force lockout. 0 restores the old immediate attempt."
        ),
    )
    add_verbose_flag(parser)
    return parser


def setup_rinex_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the rinex subcommand parser."""
    defaults = get_default_values()

    parser = subparsers.add_parser(
        "rinex",
        help="Convert raw GPS data to RINEX format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Convert raw GPS receiver data (SBF, T02, T00, m00) to RINEX format with proper
header metadata from TOS database.

Supported formats:
  - Septentrio SBF (.sbf, .sbf.gz) - using sbf2rin
  - Trimble T02 (.T02) - using runpkr00 + GFZRNX
  - Trimble T00 (.T00) - using runpkr00 + GFZRNX
  - Leica m00 (.m00, .m00.gz) - using teqc + GFZRNX

THREE MODES - pick ONE (they are mutually exclusive):
  1. Daily convert (default) - convert recent raw from the live collection
     dir, in place. No archive read/write.
  2. Header fix (--fix-headers) - rewrite stale header fields in ARCHIVED
     RINEX files to match TOS. No re-conversion from raw.
  3. Re-rinex (--from-archive) - re-convert the ARCHIVE from raw (e.g. to
     upgrade RINEX 2 -> 3 and recover GLONASS/Galileo/BeiDou). Converts and
     stages into a work-dir, then --push writes back via the rawdata gateway.

Version defaults: RINEX 3 for modern receivers (PolaRX5, NetR9); NetRS pins
RINEX 2 (its L2 is codeless - RINEX 3 codes it C2D, which GAMIT drops).
--version overrides.

Examples:
  # Daily: convert the last 7 days
  receivers rinex ELDC -d 7

  # Daily: a specific range, RINEX 3, long naming
  receivers rinex ELDC -s 20260101 -e 20260107 --version 3 --naming long

  # Validate existing RINEX headers against TOS (read-only)
  receivers rinex ELDC --validate-only -d 30

  # Header fix: repair stale headers across the WHOLE archive, then push
  receivers rinex RHOF --fix-headers --all --session 15s_24hr \\
      --push --catalog-prod

  # Re-rinex: re-convert the whole archive R2 -> R3, then push it back
  receivers rinex VONC --session 15s_24hr --from-archive \\
      -s 20130801 -e 20260820 --parallel --push --catalog-prod --backup-old

  # Re-rinex: convert + stage ONLY (no push - verify before writing the archive)
  receivers rinex VONC --session 15s_24hr --from-archive \\
      -s 20130801 -e 20260820 --parallel

RESUME: --fix-headers and re-rinex runs resume on re-run - already-fixed /
already-staged files are skipped. Re-run WITHOUT --force (--force restarts from
zero) and keep the same --work-dir (the staging tree IS the resume state).
        """,
    )

    # Positional: stations (multiple)
    parser.add_argument(
        "stations",
        nargs="+",
        metavar="STATION",
        help="Station IDs to convert (e.g., ELDC THOB MANA)",
    )

    # Date/time options
    date_group = parser.add_argument_group("date options")
    add_date_flags(date_group, include_days=True)

    # Format options
    format_group = parser.add_argument_group("format options")
    format_group.add_argument(
        "--session",
        type=str,
        default=defaults["session"],
        choices=["15s_24hr", "1Hz_1hr"],
        help=f"Data session type (default: {defaults['session']})",
    )

    format_group.add_argument(
        "--version",
        "-V",
        type=int,
        default=None,
        choices=[2, 3, 4],
        dest="rinex_version",
        help="RINEX version: 2, 3, or 4 (default: from receivers.cfg)",
    )

    format_group.add_argument(
        "--naming",
        type=str,
        default=None,
        choices=["short", "long"],
        help="Filename convention: short (RINEX 2 style) or long (IGS) (default: from receivers.cfg)",
    )

    format_group.add_argument(
        "--observation-types",
        type=str,
        metavar="TYPES",
        help="Observation types to include (comma-separated, e.g., C1C,L1C,S1C)",
    )

    format_group.add_argument(
        "--native-trimble",
        action="store_true",
        help="Use native Trimble converter via Docker (requires trm2rinex image)",
    )

    format_group.add_argument(
        "--r00",
        action="store_true",
        help="Restrict a re-rinex run to Trimble R00 raw (.r00) regardless of "
        "the station's CURRENT receiver type — the 4000SSi/4000Si era, roughly "
        "pre-2013 (e.g. VMEY is a PolaRX5 today but its 2008-2012 raw is .r00). "
        "Pipeline is runpkr00 + teqc, the same as .T02; teqc's '-week' is "
        "derived from the observation date rather than guessed, because R00 "
        "predates the GPS week rollover and a wrong guess silently shifts the "
        "data ~19.6 years. Output is native RINEX 2.11.",
    )

    format_group.add_argument(
        "--ashtech",
        action="store_true",
        help="Convert Ashtech .atc raw via teqc (pre-2012 archive; the -ash u/r "
        "flag is chosen from the file's magic bytes, never the name)",
    )

    format_group.add_argument(
        "--trimble",
        action="store_true",
        help="Restrict a re-rinex run to Trimble .T00/.T02 raw regardless of the "
        "station's CURRENT receiver type — for reconverting historical Trimble "
        "raw after a station was swapped (e.g. NYLA is a PolaRX5 today but its "
        "pre-2019 raw is NetRS .T00). Sub-type is forced by extension and the "
        "version follows policy: .T00→NetRS (RINEX-2 default), .T02→NetR9 "
        "(RINEX-3); pass --version to override. Escape hatch; normally the format "
        "is auto-detected from the raw file.",
    )

    # Metadata options
    meta_group = parser.add_argument_group("metadata options")
    meta_group.add_argument(
        "--no-header-correction",
        action="store_true",
        help="Skip TOS metadata header corrections",
    )

    meta_group.add_argument(
        "--force-config-metadata",
        action="store_true",
        help="Use current station config even for old data (skip TOS lookup)",
    )

    # Output options
    output_group = parser.add_argument_group("output options")
    output_group.add_argument(
        "--output-dir",
        "-o",
        type=str,
        metavar="DIR",
        help="Override output directory (default: archive path)",
    )

    output_group.add_argument(
        "--format",
        type=str,
        default=None,
        choices=["modern", "legacy"],
        dest="output_format",
        help="Output format: modern (.rnx.gz) or legacy (.D.Z) (default: from config)",
    )

    output_group.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="Keep intermediate files (e.g., .tgd from runpkr00)",
    )

    # Operation modes
    mode_group = parser.add_argument_group("operation modes")
    mode_group.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate existing RINEX headers against TOS (no conversion)",
    )

    mode_group.add_argument(
        "--rename-only",
        action="store_true",
        help="Only rename existing RINEX files to new convention (no conversion)",
    )

    mode_group.add_argument(
        "--fix-headers",
        action="store_true",
        help="Rewrite discrepant header fields in archived RINEX files to match TOS, "
        "in place (no SBF re-conversion). Walks the RINEX archive for the "
        "station/session/date-range; for each file, compares the header to TOS "
        "via the legacy validator and rewrites only the fields that actually "
        "differ. Combine with --archive-old to keep the pre-fix file. "
        "Use --all to scan the entire archive (all years/months) — no date "
        "range needed.",
    )

    mode_group.add_argument(
        "--all",
        action="store_true",
        help="With --fix-headers: scan the ENTIRE RINEX archive (all years/months) "
        "for the station/session. No date range needed — discovers all files "
        "under <data_prepath>/YYYY/mon/<STA>/<session>/rinex/ and fixes any "
        "whose headers disagree with TOS.",
    )

    mode_group.add_argument(
        "--work-dir",
        default="~/tmp/rinex_fixes",
        help="Staging directory for --fix-headers AND re-rinex (--from-archive). "
        "Default: ~/tmp/rinex_fixes for --fix-headers, ~/tmp/rinex_reconvert for "
        "re-rinex. Stage on a DATA volume (e.g. /mnt/data/gpsops_scratch), not "
        "/home — a full-station run can be tens of GB and /home is the small OS "
        "volume. Staging is the NORMAL mode, not a fallback: the long-term "
        "archive (ananas:/gps/gpsdata) is mounted READ-ONLY everywhere — "
        "including rek-d01, where it is /mnt/rawgpsdata — because "
        "rawdata.vedur.is is the sole writer, by design, for traceability. Read "
        "over NFS, stage here, push via the gpsops@rawdata gateway with --push. "
        "Pass an empty string (--work-dir '') to fix in place, which works ONLY "
        "on a locally-writable tree such as rek-d01's rolling collection dir "
        "/mnt/data/gpsdata — never on the historical archive.",
    )

    mode_group.add_argument(
        "--source-dir",
        help="With --fix-headers: discover RINEX files from this directory. "
        "Defaults to /mnt_data/rawgpsdata if mounted (the IMO global archive), "
        "otherwise falls back to data_prepath from receivers.cfg.",
    )

    mode_group.add_argument(
        "--archive-old",
        action="store_true",
        help="With --fix-headers (or re-conversion): move the existing file to a "
        "parallel <rinex_archive>/<reason>_<date>/ directory (filename "
        "unchanged) before overwriting. Default without this flag: overwrite "
        "in place.",
    )

    mode_group.add_argument(
        "--correct-receiver",
        action="store_true",
        help="With --fix-headers: ALSO rewrite the REC # / TYPE / VERS header "
        "from TOS (normally flag-only). For a station whose header is "
        "known-stale — e.g. a BNC-stream 1Hz whose receiver label was frozen "
        "across an instrument swap. NEVER use on a dual-instrument site "
        "(the header correctly records a separate streaming box). Refuses a "
        "multi-station run; use one station at a time.",
    )
    mode_group.add_argument(
        "--correct-antenna",
        action="store_true",
        help="With --fix-headers: ALSO rewrite the ANT # / TYPE header from TOS "
        "(normally flag-only). Same stale-header caveats as --correct-receiver.",
    )

    mode_group.add_argument(
        "--skip-fields",
        metavar="FIELD[,..]",
        help="With --fix-headers: do NOT rewrite these fields, even when they "
        "differ from TOS. Comma-separated keys: marker, domes, antenna_height, "
        "coordinates, observer_agency. Chiefly 'coordinates' — a header's APPROX "
        "POSITION XYZ is the receiver's own autonomous solution and legitimately "
        "sits tens of metres from the surveyed TOS value, so it flags on nearly "
        "every historical file; skipping it keeps a MARKER NUMBER repair from "
        "silently rewriting a station's entire archive as well.",
    )

    mode_group.add_argument(
        "--from-archive",
        action="store_true",
        help="Re-rinex mode: read raw from the persistent archive (the sync.yaml "
        "dissemination source_root, e.g. /mnt/rawgpsdata) instead of the ephemeral "
        "data_prepath, and stage into a ~/tmp work-dir (default "
        "~/tmp/rinex_reconvert) for a --push back. Equivalent to passing "
        "--source-dir <archive> explicitly; --source-dir overrides the config path. "
        "This is the mode selector — the daily convert (no --from-archive/"
        "--source-dir) is untouched.",
    )

    mode_group.add_argument(
        "--dates",
        metavar="YYYYMMDD[,..]|@FILE",
        help="Convert ONLY these dates (comma-separated, or @file with one "
        "per line) — the targeted-regen path, typically fed by "
        "'receivers archive-audit'. Overrides -s/-e. NOTE: the list collapses "
        "to the full min..max range, so `a,b,z` scans EVERY date between a and "
        "z, not just the listed ones. Pair with --force to redo dates whose "
        "existing product is bad (resume-skip would otherwise protect it).",
    )

    mode_group.add_argument(
        "--parallel",
        nargs="?",
        const="auto",
        default=None,
        metavar="N",
        help="Convert calendar-year chunks in parallel (chunks are "
        "date-disjoint, so workers never race on the same file). "
        "--parallel alone sizes the pool from free cores minus current "
        "loadavg and keeps re-checking load before each new chunk; "
        "--parallel N forces N workers. The final --push stays a single "
        "serialized step.",
    )

    mode_group.add_argument(
        "--backup-old",
        action="store_true",
        help="During RE-CONVERSION (re-rinex): before a new RINEX overwrites the "
        "existing one, COPY the current archive file to a LOCAL "
        "'<work-dir>_prev/' tree. The copy is a verification-window artefact — "
        "keep it until you have confirmed the regeneration is good, then delete "
        "it. NOTHING is written to the archive: the archive holds raw/, rinex/ "
        "and rinex_org/ only, where rinex_org is the terminal state for data "
        "that CANNOT be regenerated (there, the preserved copy IS the original). "
        "Copy, not move, so the archive's live file stays in place until rsync "
        "replaces it — the old server-side move left a window where a failed "
        "rsync stranded the only copy in rinex_bak/.",
    )

    mode_group.add_argument(
        "--del-backup",
        action="store_true",
        help="Delete the rinex_bak/ backups (from --backup-old) for the "
        "station/session/date-range (or --all). Run only after verifying the "
        "re-rinexed files are good. Dry-run with --dry-run first.",
    )

    mode_group.add_argument(
        "--push",
        action="store_true",
        help="With --fix-headers OR re-rinex (--from-archive): rsync the files "
        "staged in --work-dir back to the archive via the gpsops@rawdata gateway "
        "(the archive itself is read-only everywhere). Pushes ONLY the files this "
        "run staged — no whole-tree scan; skipped entirely when 0 files were "
        "produced. In re-rinex mode the push is incremental (every --push-batch "
        "files) and three gates — regenerability, degradation, format — refuse "
        "any file that would be unsafe to overwrite. Each file transfers in "
        "full: a header change rewrites the Hatanaka/.Z stream, so rsync "
        "block-deltas save nothing.",
    )

    mode_group.add_argument(
        "--clean",
        action="store_true",
        help="With --fix-headers: empty the staging work-dir before staging so it "
        "holds only this run's files (the work-dir otherwise accumulates across "
        "runs). No effect on the source archive or on --dry-run.",
    )

    mode_group.add_argument(
        "--cleanup",
        action="store_true",
        help="With --fix-headers --push: after a successful push+reindex, delete "
        "this run's staged rinex/ obs from the work-dir, and delete each "
        "rinex_archive/ pre-fix backup ONLY once the re-read archive header "
        "matches TOS (fix confirmed on the archive). rinex_org/ preservations "
        "(un-regenerable originals) are NEVER auto-deleted. (Distinct from "
        "--clean, which empties the work-dir BEFORE staging.)",
    )
    mode_group.add_argument(
        "--reindex",
        action="store_true",
        help="With --push (--fix-headers or re-rinex): after the push, re-hash each "
        "pushed file and update its archive_catalog.content_sha256 (the rewrite/"
        "re-convert changes the hash, so the catalog row would otherwise be stale "
        "and the integrity verify would flag it). Targets the local dev gps_health "
        "by default; use --catalog-prod for production. Note: passing --catalog-prod "
        "or --catalog-host ALREADY implies this reindex — so `--push --catalog-prod` "
        "alone does push + prod-reindex in one command.",
    )
    mode_group.add_argument(
        "--push-batch",
        type=int,
        default=100,
        metavar="N",
        help="With --push (--fix-headers or re-rinex): push+reindex every N "
        "staged files instead of once at the end, so an interruption loses at "
        "most one batch. Default: 100 for --fix-headers, 300 for re-rinex. Use a "
        "large value to push once at the end.",
    )
    mode_group.add_argument(
        "--retry-attempts",
        type=int,
        default=2,
        metavar="N",
        help="With --fix-headers: re-run TRANSIENT per-file failures up to N times "
        "before reporting them (default: 2; 0 disables). Permanent failures are "
        "never retried — unreadable/corrupt files, truncated raw, a missing TOS "
        "session, or a refused rinex_org preservation all produce the same error "
        "on a second pass and need an operator, not another attempt. Driving case: "
        "the ELEY 1Hz retrofit ended with ~13 transient `compress` failures that "
        "succeeded on a manual re-run.",
    )
    mode_group.add_argument(
        "--retry-backoff",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="Base delay before each retry, multiplied by the attempt number "
        "(default: 3.0). These failures come from subprocess/IO pressure, so the "
        "wait is the point — set 0 only in tests.",
    )
    mode_group.add_argument(
        "--catalog-host",
        default=None,
        metavar="HOST",
        help="Override the --reindex catalog target with an explicit host (or "
        "comma-separated hosts, e.g. localhost for a dev test). Default (no flag) "
        "= database.cfg gps_health; --catalog-prod = the production catalog set.",
    )
    mode_group.add_argument(
        "--catalog-prod",
        action="store_true",
        help="Write the PRODUCTION catalog set from receivers.cfg [archive] "
        "catalog_hosts (e.g. rek-d01 + pgdev). Explicit opt-in so a dev run stays "
        "on the local DB by default. On a --push run this IMPLIES --reindex, so "
        "`receivers rinex … --push --catalog-prod` pushes AND refreshes the prod "
        "catalog in one command (no separate archive-reindex pass to forget).",
    )

    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )

    add_force_flag(parser)
    add_verbose_flag(parser)

    return parser


def setup_tools_parser(subparsers) -> argparse.ArgumentParser:
    """Set up argument parser for tools management subcommand."""
    parser = subparsers.add_parser(
        "tools",
        help="Manage RINEX conversion tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Manage external tools required for RINEX conversion.

Tools are installed to ~/.local/share/gps-rinex-tools/bin/ by default.

Supported tools:
  teqc      - Leica MDB/m00 to RINEX 2 (UNAVCO, auto-install)
  gfzrnx    - RINEX format conversion and QC (GFZ, auto-install)
  rnx2crx   - Hatanaka compression (GSI, auto-install)
  mdb2rinex - Leica MDB to RINEX 3 (Leica myWorld, manual)
  runpkr00  - Trimble T00/T02 extraction (Trimble, manual)
  sbf2rin   - Septentrio SBF to RINEX (Septentrio, manual)

Examples:
  receivers tools list              # Show all tools and status
  receivers tools install teqc      # Install specific tool
  receivers tools install-all       # Install all auto-installable tools
  receivers tools check             # Verify tools are working
  receivers tools configure         # Update receivers.cfg with tool paths
        """,
    )

    # Subcommands for tools
    tools_subparsers = parser.add_subparsers(
        dest="tools_command",
        title="tools commands",
    )

    # list
    tools_subparsers.add_parser(
        "list", help="List all tools and their installation status"
    )

    # install
    install_parser = tools_subparsers.add_parser(
        "install", help="Install a specific tool"
    )
    install_parser.add_argument(
        "tool_name", help="Name of tool to install (teqc, gfzrnx, rnx2crx, etc.)"
    )
    install_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force reinstall even if already installed",
    )

    # install-all
    install_all_parser = tools_subparsers.add_parser(
        "install-all", help="Install all auto-installable tools"
    )
    install_all_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force reinstall even if already installed",
    )

    # check
    check_parser = tools_subparsers.add_parser(
        "check", help="Check tool availability for a receiver type"
    )
    check_parser.add_argument(
        "--receiver-type",
        help="Check tools needed for specific receiver type (e.g., G10, PolaRX5)",
    )

    # configure
    configure_parser = tools_subparsers.add_parser(
        "configure", help="Update receivers.cfg with installed tool paths"
    )
    configure_parser.add_argument(
        "--config",
        help="Path to receivers.cfg (default: ~/.config/gpsconfig/receivers.cfg)",
    )

    add_verbose_flag(parser)

    return parser


def create_argument_parser() -> argparse.ArgumentParser:
    """Create the main argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="receivers",
        description="GPS Receiver Data Management Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers download ELDC --sync --archive
  receivers status THOB
  receivers health ISFS --json
  receivers validate --web
  receivers rinex ELDC -d 7

For subcommand help: receivers <command> --help
        """,
    )

    # Global verbose (applies if no subcommand)
    add_verbose_flag(parser)

    # Subcommands
    subparsers = parser.add_subparsers(
        dest="command", title="commands", description="Available commands"
    )

    # Add all subcommand parsers
    setup_download_parser(subparsers)
    setup_status_parser(subparsers)
    setup_health_parser(subparsers)
    setup_validate_parser(subparsers)
    setup_rec_config_parser(subparsers)
    setup_rec_provision_parser(subparsers)
    setup_rec_upgrade_firmware_parser(subparsers)
    setup_rinex_parser(subparsers)
    setup_tools_parser(subparsers)

    # Scheduler (optional - requires APScheduler)
    try:
        from .scheduler import create_scheduler_parser

        create_scheduler_parser(subparsers)
    except ImportError:
        pass

    # TOS integration (optional - requires tostools)
    try:
        from .tos import create_tos_parser

        create_tos_parser(subparsers)
    except ImportError:
        pass

    # cfg reconciliation (always available — TOS source is gated at runtime)
    from .cfg import create_cfg_parser

    create_cfg_parser(subparsers)

    # Database management
    from .db import create_db_parser

    create_db_parser(subparsers)

    # health-query (EXPLAIN-gated SELECT against gps_health)
    from .health_query import create_health_query_parser

    create_health_query_parser(subparsers)

    # archive-sync (batch delta push to the long-term archive gateway)
    from .archive_sync import (
        create_archive_audit_parser,
        create_archive_catalog_backfill_parser,
        create_archive_index_backfill_parser,
        create_archive_prune_parser,
        create_archive_reindex_parser,
        create_archive_rm_parser,
        create_archive_sort_parser,
        create_archive_sync_parser,
        create_archive_verify_parser,
    )

    create_archive_sync_parser(subparsers)
    # archive-verify (re-hash archived files + local↔archive cross-check)
    create_archive_verify_parser(subparsers)

    # archive-audit (junk + regen-candidate scan, emits fix commands)
    create_archive_audit_parser(subparsers)
    # archive-reindex (refresh catalog sha256 after out-of-band file edits)
    create_archive_reindex_parser(subparsers)
    # archive-rm (guarded deletion of empty/bad files from the archive)
    create_archive_rm_parser(subparsers)
    # archive-sort (relocate misfiled raw files: decoded date ≠ filename)
    create_archive_sort_parser(subparsers)
    # archive-prune (local ring-buffer: catalog-gated age-out with disk guardrails)
    create_archive_prune_parser(subparsers)
    # catalog-backfill-local (seed local_raw/local_rinex catalog from file_tracking)
    create_archive_catalog_backfill_parser(subparsers)
    # archive-index-backfill (one-time index of on-disk archive files → catalog)
    create_archive_index_backfill_parser(subparsers)
    # missing (query the unified-file-index differential worklists — no ls)
    from .missing import create_missing_parser

    create_missing_parser(subparsers)

    # epos-disseminate (RINEX3 long-name dissemination to the EPOS files server)
    from .epos_disseminate import create_epos_disseminate_parser

    create_epos_disseminate_parser(subparsers)

    # m3g (M3G site-log submission: validate / upload draft / diff)
    from .m3g import create_m3g_parser

    create_m3g_parser(subparsers)

    # station onboard (EPOS onboarding pipeline orchestration, todo #150)
    from .station_onboard import create_station_onboard_parser

    create_station_onboard_parser(subparsers)

    return parser
