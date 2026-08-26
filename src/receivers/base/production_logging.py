#!/usr/bin/env python3
"""
Production logging configuration for GPS receiver management system.

Provides:
- Concise INFO level logging for automated systems
- Structured JSON logging for monitoring integration
- Separate audit trail for download statistics
- Error-focused output for critical issues
- Integration-ready format for Icinga monitoring
"""

import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Optional

# 4-character uppercase station IDs used throughout the GNSS network
# (e.g. RHOF, ELEY). Duplicated from ``logging_config._STATION_ID_RE`` to
# keep the import surface clean — ``logging_config`` imports THIS module,
# so importing it back would be circular.
_STATION_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")


def _station_from_logger_name(name: str) -> Optional[str]:
    """Return the 4-char station id encoded in a ``receivers.*`` logger
    name, or ``None``.

    The station is the LAST dotted component *only when it matches the
    station-id pattern* — so ``receivers.download.RHOF`` → ``"RHOF"`` but
    ``receivers.scheduler.reconciler`` → ``None``. The old
    ``record.name.count('.') >= 2`` heuristic wrongly treated any
    3+-segment logger's tail as a station, mislabelling
    ``reconciler`` / ``db_writer`` / ``health_query`` as station ids.
    """
    tail = name.rsplit(".", 1)[-1]
    return tail if _STATION_ID_RE.match(tail) else None


class ProductionFormatter(logging.Formatter):
    """Concise formatter optimized for production automated systems."""

    def __init__(self):
        super().__init__()

    def format(self, record):
        # Production format: timestamp - level - station - message
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")

        # Extract station ID from logger name if available
        station_id = ""
        station = _station_from_logger_name(record.name)
        if station:
            station_id = f"[{station}] "
        elif hasattr(record, "station_id"):
            station_id = f"[{record.station_id}] "

        # Concise level indicators
        level_indicators = {
            "CRITICAL": "🔴",
            "ERROR": "❌",
            "WARNING": "⚠️ ",
            "INFO": "✅",
            "DEBUG": "🔍",
        }

        level_icon = level_indicators.get(record.levelname, record.levelname)

        line = f"{timestamp} {level_icon} {station_id}{record.getMessage()}"

        # Append exception traceback / stack info when present. The base
        # logging.Formatter does this automatically; these custom format()
        # overrides must replicate it or logger.exception() output is silently
        # swallowed (no traceback anywhere) — which is exactly what hid an EPOS
        # dissemination failure until it was reproduced by hand.
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            line = f"{line}\n{record.exc_text}"
        if record.stack_info:
            line = f"{line}\n{self.formatStack(record.stack_info)}"
        return line


class JSONFormatter(logging.Formatter):
    """JSON formatter for monitoring system integration."""

    def format(self, record):
        log_entry = {
            # Explicit UTC — record.created is a POSIX timestamp; the old
            # bare fromtimestamp() emitted a local-naive ISO string with no
            # offset, which Loki/Grafana then misread as UTC.
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }

        # Add station context if available
        station = _station_from_logger_name(record.name)
        if station:
            log_entry["station_id"] = station
        elif hasattr(record, "station_id"):
            log_entry["station_id"] = record.station_id

        # Add performance metrics if available
        if hasattr(record, "duration"):
            log_entry["duration_seconds"] = record.duration
        if hasattr(record, "bytes_downloaded"):
            log_entry["bytes_downloaded"] = record.bytes_downloaded
        if hasattr(record, "files_count"):
            log_entry["files_count"] = record.files_count

        # Add error context if available
        if record.levelname in ["ERROR", "CRITICAL"] and hasattr(record, "error_type"):
            log_entry["error_type"] = record.error_type
            log_entry["error_category"] = getattr(record, "error_category", "unknown")

        # Serialize exception traceback / stack info when present. Without this
        # logger.exception() writes only its message (exc_info is dropped),
        # leaving no traceback in the JSON log — the gap that made an EPOS
        # dissemination "run failed" undiagnosable from the logs alone.
        # json.dumps escapes the embedded newlines, so records stay one-per-line.
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            log_entry["exc_info"] = record.exc_text
        if record.stack_info:
            log_entry["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(log_entry)


# Path install.sh drops on the production server; its presence means logrotate
# owns rotation of the receivers logs (see deployment/logrotate.d/gps-receivers).
_LOGROTATE_MARKER = Path("/etc/logrotate.d/gps-receivers")


def _external_log_rotation() -> bool:
    """True when an external rotator (logrotate) owns the receivers log files.

    On the production server install.sh installs /etc/logrotate.d/gps-receivers,
    so the Python handlers must NOT rotate — a RotatingFileHandler fighting
    logrotate over the same file churned the most recent ~3 h of logs off disk
    (it deletes past backupCount; the fleet logs ~19 MB/h). The laptop/dev box
    has no logrotate, so there the handlers self-rotate to stay bounded.

    Override explicitly with RECEIVERS_LOG_EXTERNAL_ROTATION=1/0.
    """
    env = os.environ.get("RECEIVERS_LOG_EXTERNAL_ROTATION")
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no")
    return _LOGROTATE_MARKER.exists()


def make_log_file_handler(
    path: Path, max_bytes: int, backup_count: int
) -> logging.Handler:
    """File handler that cooperates with whoever owns log rotation.

    - external rotation (logrotate present): ``WatchedFileHandler`` — append-only,
      reopens the file after logrotate's rename+create, never deletes lines.
    - otherwise: ``RotatingFileHandler(max_bytes, backup_count)`` — self-bounded,
      so dev boxes without logrotate don't grow unbounded.
    """
    if _external_log_rotation():
        return logging.handlers.WatchedFileHandler(path, encoding="utf-8")
    return logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )


class AuditLogger:
    """Separate audit logger for download statistics and performance metrics."""

    def __init__(self, log_dir: Path = None):
        if log_dir is None:
            log_dir = Path.home() / ".cache" / "gps_receivers" / "logs"

        log_dir.mkdir(parents=True, exist_ok=True)
        self.audit_file = log_dir / "download_audit.jsonl"

        # Set up audit logger
        self.logger = logging.getLogger("receivers.audit")
        self.logger.setLevel(logging.INFO)

        # Remove existing handlers to avoid duplicates
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)

        # JSON file handler for audit trail. Cooperates with logrotate on the
        # server (WatchedFileHandler), self-rotates on dev (RotatingFileHandler).
        file_handler = make_log_file_handler(
            self.audit_file,
            50 * 1024 * 1024,
            5,  # 50 MB x 5 when self-rotating
        )
        file_handler.setFormatter(JSONFormatter())
        self.logger.addHandler(file_handler)

        # Prevent propagation to avoid duplicate logs
        self.logger.propagate = False

    def log_download_session(self, station_id: str, session_data: Dict[str, Any]):
        """Log complete download session statistics."""
        audit_entry = {
            "event_type": "download_session",
            "station_id": station_id,
            "session": session_data.get("session", "unknown"),
            "status": session_data.get("status", "unknown"),
            "duration_seconds": session_data.get("duration", 0),
            "files_downloaded": session_data.get("files_downloaded", 0),
            "bytes_downloaded": session_data.get("bytes_downloaded", 0),
            "errors": session_data.get("errors", 0),
            "start_time": session_data.get("start_time"),
            "end_time": session_data.get("end_time"),
        }

        # Add performance metrics if available
        if "connection_time" in session_data:
            audit_entry["connection_time_seconds"] = session_data["connection_time"]
        if "download_speed" in session_data:
            audit_entry["download_speed_kbps"] = session_data["download_speed"]

        self.logger.info("Download session completed", extra=audit_entry)

    def log_performance_metrics(self, station_id: str, metrics: Dict[str, Any]):
        """Log performance metrics for monitoring."""
        audit_entry = {
            "event_type": "performance_metrics",
            "station_id": station_id,
            "metrics": metrics,
        }

        self.logger.info("Performance metrics", extra=audit_entry)

    def log_failure_event(self, station_id: str, failure_data: Dict[str, Any]):
        """Log failure events for analysis."""
        audit_entry = {
            "event_type": "failure",
            "station_id": station_id,
            "error_type": failure_data.get("error_type", "unknown"),
            "error_category": failure_data.get("error_category", "unknown"),
            "error_message": failure_data.get("error_message", ""),
            "severity": failure_data.get("severity", "unknown"),
            "validation_triggered": failure_data.get("validation_triggered", False),
        }

        self.logger.error("Station failure", extra=audit_entry)


class ProductionLoggingConfig:
    """Production logging configuration manager."""

    def __init__(
        self, log_dir: Path = None, json_output: bool = False, verbose: bool = False
    ):
        self.log_dir = log_dir or Path.home() / ".cache" / "gps_receivers" / "logs"
        self.json_output = json_output
        self.verbose = verbose
        self.audit_logger = AuditLogger(self.log_dir)

    def setup_production_logging(self) -> logging.Logger:
        """Set up production-optimized logging configuration.

        Delegates to the unified :func:`receivers.logging_config.setup_logging`.
        """
        from ..logging_config import setup_logging

        level = logging.DEBUG if self.verbose else logging.INFO
        return setup_logging(
            level=level,
            json_output=self.json_output,
            log_dir=self.log_dir,
        )

    def get_audit_logger(self) -> AuditLogger:
        """Get the audit logger instance."""
        return self.audit_logger

    def create_station_logger(self, station_id: str) -> logging.Logger:
        """Create a logger for a specific station."""
        logger = logging.getLogger(f"receivers.download.{station_id}")

        # Add station ID to all log records
        class StationContextFilter(logging.Filter):
            def filter(self, record):
                record.station_id = station_id
                return True

        logger.addFilter(StationContextFilter())
        return logger


def setup_production_logging(
    json_output: bool = False, verbose: bool = False, log_dir: Path = None
) -> ProductionLoggingConfig:
    """
    Set up production logging configuration.

    Args:
        json_output: Use JSON format for console output (for monitoring systems)
        verbose: Enable verbose logging (includes DEBUG level)
        log_dir: Custom log directory (defaults to ~/.cache/gps_receivers/logs)

    Returns:
        ProductionLoggingConfig instance
    """
    config = ProductionLoggingConfig(
        log_dir=log_dir, json_output=json_output, verbose=verbose
    )
    config.setup_production_logging()
    return config


# Example usage and testing
if __name__ == "__main__":
    # Test production logging
    config = setup_production_logging(json_output=False, verbose=False)

    # Create station logger
    logger = config.create_station_logger("TEST")

    # Test different log levels
    logger.info("Connection test successful")
    logger.warning("Slow download speed detected")
    logger.error("Connection failed - retrying")
    logger.critical("Station unreachable after 3 attempts")

    # Test audit logging
    audit = config.get_audit_logger()
    audit.log_download_session(
        "TEST",
        {
            "session": "15s_24hr",
            "status": "completed",
            "duration": 45.2,
            "files_downloaded": 3,
            "bytes_downloaded": 15728640,
            "errors": 0,
            "connection_time": 1.2,
            "download_speed": 285.4,
        },
    )

    print("Production logging test completed")
