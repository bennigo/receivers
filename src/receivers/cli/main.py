"""Main CLI entry point for receivers package."""

import argparse
import json
import logging
import sys
from typing import Any, Dict

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Import receiver classes (conditionally)
try:
    from ..septentrio.polarx5 import PolaRX5

    HAS_POLARX5 = True
except ImportError:
    HAS_POLARX5 = False

# Import configuration parser (will be updated when gps_parser is available)
try:
    import gps_parser

    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False


console = Console()


def create_receiver(station_id: str, receiver_type: str = "polarx5") -> Any:
    """Create a receiver instance based on type.

    Args:
        station_id: Station identifier
        receiver_type: Type of receiver

    Returns:
        Receiver instance

    Raises:
        ValueError: If receiver type is not supported or configuration missing
    """
    if not HAS_GPS_PARSER:
        # For now, create a minimal config for testing
        station_info = {
            "router": {"ip": "10.6.1.90"},  # Updated IP
            "receiver": {"ftpport": "2160"},
        }
        console.print(
            "[yellow]Warning: gps_parser not available, using minimal config[/yellow]"
        )
    else:
        parser = gps_parser.ConfigParser()
        parsed_info = parser.getStationInfo(station_id.upper())
        if not parsed_info or 'router' not in parsed_info:
            # Fall back to minimal config if full config not available
            console.print(
                "[yellow]Warning: Full station config not available, using minimal config[/yellow]"
            )
            station_info = {
                "router": {"ip": "10.6.1.90"},  # Updated IP
                "receiver": {"ftpport": "2160"},
            }
        else:
            station_info = parsed_info

    if receiver_type.lower() == "polarx5":
        if not HAS_POLARX5:
            raise ValueError("PolaRX5 support not available (missing dependencies)")
        return PolaRX5(station_id, station_info)
    else:
        raise ValueError(f"Unsupported receiver type: {receiver_type}")


def format_health_status(health: Dict[str, Any]) -> None:
    """Format and display health status using Rich."""

    # Main status panel
    status_color = "green" if health["overall_status"] == "healthy" else "red"
    status_panel = Panel(
        f"[bold {status_color}]{health['overall_status'].upper()}[/bold {status_color}]",
        title=f"Station {health['station_id']} ({health['receiver_type']})",
        expand=False,
    )
    console.print(status_panel)

    # Connection details table
    table = Table(title="Connection Status", box=box.ROUNDED)
    table.add_column("Component", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Details", style="dim")

    conn = health["connection"]
    router_status = "✅ OK" if conn["router"] else "❌ FAIL"
    receiver_status = "✅ OK" if conn["receiver"] else "❌ FAIL"

    table.add_row("Router", router_status, f"{conn['ip']}:{conn['port']}")
    table.add_row("Receiver", receiver_status, conn.get("error", ""))

    console.print(table)

    # Timestamp
    console.print(f"\n[dim]Last checked: {health['timestamp']}[/dim]")


def cmd_health(args) -> int:
    """Check receiver health status."""
    try:
        receiver = create_receiver(args.station_id, args.receiver_type)
        health = receiver.get_health_status()

        if args.json:
            console.print(json.dumps(health, indent=2))
        else:
            format_health_status(health)

        return 0 if health["overall_status"] == "healthy" else 1

    except Exception as e:
        console.print(f"[red]Error checking health: {e}[/red]")
        return 1


def cmd_download(args) -> int:
    """Download data from receiver."""
    try:
        receiver = create_receiver(args.station_id, args.receiver_type)

        # Prepare download parameters
        download_args = {
            "start": args.start,
            "end": args.end,
            "session": args.session,
            "sync": not args.dry_run,
            "clean_tmp": args.clean_tmp,
            "archive": args.archive,
            "loglevel": logging.DEBUG if args.verbose else logging.INFO,
        }

        if args.tmp_dir:
            download_args["tmp_dir"] = args.tmp_dir

        console.print(f"[bold]Downloading data for {args.station_id}[/bold]")
        if args.dry_run:
            console.print("[yellow]DRY RUN - No files will be downloaded[/yellow]")

        result = receiver.download_data(**download_args)

        # Display results
        if args.json:
            console.print(json.dumps(result, indent=2, default=str))
        else:
            status_color = "green" if result["status"] == "completed" else "yellow"
            console.print(
                f"\n[{status_color}]Status: {result['status']}[/{status_color}]"
            )
            console.print(f"Files checked: {result['files_checked']}")
            console.print(f"Files missing: {result['files_missing']}")
            console.print(f"Files downloaded: {result['files_downloaded']}")
            console.print(f"Duration: {result['duration']:.2f} seconds")

        return 0

    except Exception as e:
        console.print(f"[red]Error during download: {e}[/red]")
        return 1


def cmd_status(args) -> int:
    """Show receiver status information."""
    try:
        receiver = create_receiver(args.station_id, args.receiver_type)

        info = receiver.get_station_info()
        connection = receiver.get_connection_status()

        if args.json:
            data = {"station_info": info, "connection": connection}
            console.print(json.dumps(data, indent=2))
        else:
            # Station info table
            table = Table(title=f"Station {args.station_id} Status", box=box.ROUNDED)
            table.add_column("Property", style="cyan")
            table.add_column("Value", style="bold")

            table.add_row("Station ID", info["station_id"])
            table.add_row("Receiver Type", info["receiver_type"])
            table.add_row("IP Address", info["ip"])
            table.add_row("FTP Port", str(info["port"]))
            table.add_row("Passive Mode", "Yes" if info["pasv_mode"] else "No")

            # Connection status
            conn_status = (
                "✅ Connected" if connection["receiver"] else "❌ Disconnected"
            )
            table.add_row("Connection", conn_status)

            console.print(table)

        return 0

    except Exception as e:
        console.print(f"[red]Error getting status: {e}[/red]")
        return 1


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="receivers",
        description="GPS/GNSS receiver management and data download toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers health REYK                    # Check health of REYK station
  receivers download REYK --sync           # Download missing data
  receivers status HOFN --json             # Get status as JSON
  receivers download VMEY --start 2024-01-15 --end 2024-01-20 --dry-run
        """,
    )

    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")

    # Global options
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output"
    )

    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    # Subcommands
    subparsers = parser.add_subparsers(
        dest="command", help="Available commands", metavar="COMMAND"
    )

    # Health check command
    health_parser = subparsers.add_parser(
        "health",
        help="Check receiver health status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Check the health status of a GPS/GNSS receiver",
    )
    health_parser.add_argument(
        "station_id", help="Station identifier (e.g., REYK, HOFN, VMEY)"
    )
    health_parser.add_argument(
        "-t",
        "--receiver-type",
        default="polarx5",
        choices=["polarx5"],
        help="Receiver type (default: polarx5)",
    )
    health_parser.set_defaults(func=cmd_health)

    # Download command
    download_parser = subparsers.add_parser(
        "download",
        help="Download data from receiver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Download GPS/GNSS data from receiver to local archive",
    )
    download_parser.add_argument(
        "station_id", help="Station identifier (e.g., REYK, HOFN, VMEY)"
    )
    download_parser.add_argument(
        "-t",
        "--receiver-type",
        default="polarx5",
        choices=["polarx5"],
        help="Receiver type (default: polarx5)",
    )
    download_parser.add_argument(
        "-s",
        "--start",
        help="Start date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
    )
    download_parser.add_argument(
        "-e", "--end", help="End date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
    )
    download_parser.add_argument(
        "--session",
        default="15s_24hr",
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Data session type (default: 15s_24hr)",
    )
    download_parser.add_argument(
        "--sync",
        action="store_true",
        help="Actually download files (default is dry-run)",
    )
    download_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without downloading",
    )
    download_parser.add_argument(
        "--clean-tmp",
        action="store_true",
        default=True,
        help="Clean temporary directory before download",
    )
    download_parser.add_argument(
        "--no-archive",
        action="store_false",
        dest="archive",
        help="Don't archive downloaded files",
    )
    download_parser.add_argument("--tmp-dir", help="Temporary download directory")
    download_parser.set_defaults(func=cmd_download)

    # Status command
    status_parser = subparsers.add_parser(
        "status",
        help="Show receiver status information",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Display detailed status information for a receiver",
    )
    status_parser.add_argument(
        "station_id", help="Station identifier (e.g., REYK, HOFN, VMEY)"
    )
    status_parser.add_argument(
        "-t",
        "--receiver-type",
        default="polarx5",
        choices=["polarx5"],
        help="Receiver type (default: polarx5)",
    )
    status_parser.set_defaults(func=cmd_status)

    # Parse arguments
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Set up logging
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    # Execute command
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
