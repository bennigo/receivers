"""Status task implementation for real-time receiver monitoring.

StatusTask performs live status checks on GPS receivers and sends results
to PostgreSQL database and Icinga monitoring system. This is the equivalent
of running: `receivers health STATION --icinga --save-db`

Key characteristics:
- REALTIME priority: Never blocked by backfill tasks
- Runs every 15 minutes for active monitoring
- Connects directly to receiver for current status
- Sends passive check results to Icinga
- Writes health metrics to PostgreSQL
"""

import logging
import time
from datetime import UTC, datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..task_interface import (
    ScheduledTask,
    TaskConfig,
    TaskPriority,
    TaskResult,
)


# ---------------------------------------------------------------------------
# ⚠️  NOT ON THE LIVE EXECUTION PATH (verified 2026-08-26)
#
# This class is written and registered with TaskFactory, but nothing in
# production instantiates it — TaskFactory.create() has no production call
# site. The live scheduler wires APScheduler directly to the module-level
# `_*_job` functions in bulk_scheduler.py. Of the five task classes only
# RINEXTask actually runs (bulk_scheduler.py:1147,1180).
#
# Treat edits here as edits to code that does not execute. A behaviour fixed
# only here will not reach production: StatusTask's offline-sample-on-failure
# path had to be ported by hand into _status_check_job (see the comment at
# bulk_scheduler.py:1450). If you need this behaviour live, change the job —
# or adopt the interface for the jobs in the same change.
#
# Background: docs/architecture-review-2026-08-26.md §4.6
# ---------------------------------------------------------------------------
class StatusTask(ScheduledTask):
    """Live receiver status check task.

    Performs real-time health monitoring of GPS receivers:
    1. Connects to receiver and extracts health metrics
    2. Evaluates thresholds for alerts
    3. Sends results to PostgreSQL database
    4. Sends passive check results to Icinga

    This is the scheduled equivalent of:
        receivers health STATION --icinga --save-db
    """

    # Default to REALTIME priority for status checks
    default_priority = TaskPriority.REALTIME

    def __init__(
        self,
        station_id: str,
        config: TaskConfig,
        logger: Optional[logging.Logger] = None,
        send_to_database: bool = True,
        send_to_icinga: bool = True,
    ):
        """Initialize status task.

        Args:
            station_id: Station identifier
            config: Task configuration
            logger: Optional logger instance
            send_to_database: Write health data to PostgreSQL
            send_to_icinga: Send passive checks to Icinga
        """
        super().__init__(station_id, config, logger)
        self.send_to_database = send_to_database
        self.send_to_icinga = send_to_icinga
        self._station_config: Optional[Dict[str, Any]] = None
        self._receiver = None

    def get_time_parameters(self) -> Tuple[datetime, datetime]:
        """Get time parameters for status check.

        Status checks are point-in-time, not ranges. Returns current time
        as both start and end.

        Returns:
            Tuple of (now, now)
        """
        now = datetime.now(UTC)
        return (now, now)

    def validate_prerequisites(self) -> Tuple[bool, Optional[str]]:
        """Validate that status check can be performed.

        Checks:
        - Station configuration exists
        - Receiver can be created
        - Required connection parameters present

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            from ...cli.main import get_station_config

            self._station_config = get_station_config(self.station_id)
            if not self._station_config:
                return False, f"No configuration found for station {self.station_id}"

            # Check required connection fields
            if not self._station_config.get("ip_number"):
                return False, f"No IP address configured for {self.station_id}"

            return True, None

        except Exception as e:
            return False, f"Validation failed: {str(e)}"

    def execute(self) -> TaskResult:
        """Execute the status check.

        Performs:
        1. Connects to receiver and gathers health data
        2. Writes to PostgreSQL if enabled
        3. Sends to Icinga if enabled

        Returns:
            TaskResult with status check details
        """
        start_time = time.time()

        try:
            self.logger.info(f"Starting status check: {self.station_id}")

            # Validate prerequisites
            valid, error = self.validate_prerequisites()
            if not valid:
                return self._create_failure_result(
                    start_time,
                    "validation_failed",
                    f"Validation failed: {error}",
                    f"ValidationError: {error}",
                )

            # Create receiver and gather health data
            from ...cli.main import create_receiver
            from ...health.live_health import gather_comprehensive_health

            # Station config validated in validate_prerequisites
            station_config = self._station_config
            if station_config is None:
                return self._create_failure_result(
                    start_time,
                    "error",
                    "Station config not loaded",
                    "InternalError: station config is None",
                )

            self._receiver = create_receiver(self.station_id, station_config)

            # Gather comprehensive health (receiver metrics + NTRIP checks)
            health_data = gather_comprehensive_health(
                station_id=self.station_id,
                station_config=station_config,
                receiver=self._receiver,
                include_files=False,  # File checks are separate
                include_ntrip=True,  # Include NTRIP/RTK status
            )

            # Sync the cfg discrepancy log so `cfg list` reflects this probe.
            try:
                from ...cfg.identity_check import flag_from_health_data

                flag_from_health_data(
                    self.station_id, health_data, station_config, self.logger
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(
                    f"[{self.station_id}] cfg discrepancy check failed: {exc}"
                )

            # Write to database
            db_success = False
            if self.send_to_database:
                db_success = self._write_to_database(health_data)
                # Also write connectivity and port status
                self._write_connectivity_status(health_data)

            # Send to Icinga
            icinga_results = {}
            if self.send_to_icinga:
                icinga_results = self._send_to_icinga(health_data)

            # Calculate duration
            duration = time.time() - start_time

            # Build result
            overall_status = health_data.get("overall_status", "unknown")
            success = overall_status in ("healthy", "ok", "warning")

            message = f"Status: {overall_status}"
            if db_success:
                message += ", saved to DB"
            if icinga_results:
                icinga_ok = sum(1 for r in icinga_results.values() if r.get("success"))
                message += f", {icinga_ok}/{len(icinga_results)} Icinga checks sent"

            self.logger.info(
                f"Status check complete: {self.station_id} - {overall_status} ({duration:.1f}s)"
            )

            return TaskResult(
                success=success,
                status=overall_status,
                duration=duration,
                message=message,
                data={
                    "station_id": self.station_id,
                    "overall_status": overall_status,
                    "db_write_success": db_success,
                    "icinga_checks_sent": len(icinga_results),
                    "metrics": health_data.get("metrics", {}),
                },
                metrics={
                    "check_duration": duration,
                    "db_enabled": self.send_to_database,
                    "icinga_enabled": self.send_to_icinga,
                },
            )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"{type(e).__name__}: {str(e)}"
            self.logger.error(f"Status check failed: {self.station_id} - {error_msg}")

            # Record station as offline when health check fails
            if self.send_to_database:
                self._write_ping_status(
                    {"connection": {"tcp": {"status": "failed"}, "error": error_msg}}
                )

            return self._create_failure_result(
                start_time, "error", f"Status check failed: {str(e)}", error_msg
            )

    def _write_connectivity_status(self, health_data: Dict[str, Any]) -> bool:
        """Write ping and port connectivity status to database.

        Delegates to shared ConnectivityWriter which writes both
        block_ping_status and block_port_status in a single transaction.

        Args:
            health_data: Health data dictionary with connection info

        Returns:
            True if write successful
        """
        from ...health.connectivity_writer import ConnectivityWriter

        writer = ConnectivityWriter(self.logger)
        return writer.write_connectivity_status(self.station_id, health_data)

    def _write_ping_status(self, health_data: Dict[str, Any]) -> bool:
        """Write only ping status (for error/offline recording).

        Used when a health check fails and we only want to record
        the station as offline without port data.

        Args:
            health_data: Health data dictionary with connection info

        Returns:
            True if write successful
        """
        from ...health.connectivity_writer import ConnectivityWriter

        writer = ConnectivityWriter(self.logger)
        return writer.write_ping_only(self.station_id, health_data)

    def _write_to_database(self, health_data: Dict[str, Any]) -> bool:
        """Write health data to PostgreSQL.

        Args:
            health_data: Health data dictionary

        Returns:
            True if write successful
        """
        try:
            from ...health.db_writer import HealthDatabaseWriter

            with HealthDatabaseWriter() as db:
                return db.write_health_data(health_data)

        except ImportError:
            self.logger.warning(
                "PostgreSQL writer not available (psycopg2 not installed)"
            )
            return False
        except Exception as e:
            self.logger.error(f"Database write failed: {e}")
            return False

    def _send_to_icinga(self, health_data: Dict[str, Any]) -> Dict[str, Any]:
        """Send health checks to Icinga.

        Args:
            health_data: Health data dictionary

        Returns:
            Dictionary mapping check name to API response
        """
        try:
            from ...monitoring.icinga_client import IcingaClient

            client = IcingaClient()
            return client.send_health_from_json(health_data)

        except ImportError:
            self.logger.warning("Icinga client not available")
            return {}
        except Exception as e:
            self.logger.error(f"Icinga send failed: {e}")
            return {}

    def _create_failure_result(
        self,
        start_time: float,
        status: str,
        message: str,
        error: str,
    ) -> TaskResult:
        """Create a failure TaskResult."""
        return TaskResult(
            success=False,
            status=status,
            duration=time.time() - start_time,
            message=message,
            data={"station_id": self.station_id},
            error=error,
        )
