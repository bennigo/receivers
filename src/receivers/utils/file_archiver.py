"""File archiving utilities for receivers package.

This module provides unified file archiving logic with support for both
immediate and bulk archiving modes. Extracted from receiver implementations
to eliminate code duplication.

Design for extensibility:
- Support for multiple archiving strategies (immediate vs bulk)
- Plugin architecture for compression formats
- Configurable validation and error handling
- Support for custom archiving workflows
"""

import gzip
import logging
import shutil
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from .compression_detector import CompressionDetector
from .file_validator import FileValidator


class ArchiveMode(Enum):
    """Archiving strategy mode."""

    IMMEDIATE = "immediate"  # Archive each file right after download
    BULK = "bulk"  # Archive all files after all downloads complete


class CompressionStrategy(Protocol):
    """Protocol for compression strategies.

    Allows pluggable compression implementations for future formats.
    """

    def compress(self, source: Path, destination: Path) -> bool:
        """Compress file from source to destination.

        Args:
            source: Source file path
            destination: Destination file path

        Returns:
            True if compression succeeded
        """
        ...

    def get_extension(self) -> str:
        """Get file extension for this compression format."""
        ...

    def get_compression_ratio(self, source_size: int, compressed_size: int) -> float:
        """Calculate compression ratio percentage."""
        ...


class GzipCompression:
    """Gzip compression strategy."""

    def compress(self, source: Path, destination: Path) -> bool:
        """Compress file using gzip."""
        try:
            with open(source, "rb") as f_in:
                with gzip.open(destination, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            return True
        except OSError as e:
            raise OSError(f"Gzip compression failed: {e}")

    def get_extension(self) -> str:
        return ".gz"

    def get_compression_ratio(self, source_size: int, compressed_size: int) -> float:
        """Calculate compression ratio as percentage reduction."""
        if source_size == 0:
            return 0.0
        return ((source_size - compressed_size) / source_size) * 100


class NoCompression:
    """No compression (direct copy) strategy."""

    def compress(self, source: Path, destination: Path) -> bool:
        """Copy file without compression."""
        try:
            shutil.copy2(source, destination)
            return True
        except OSError as e:
            raise OSError(f"File copy failed: {e}")

    def get_extension(self) -> str:
        return ""

    def get_compression_ratio(self, source_size: int, compressed_size: int) -> float:
        return 0.0  # No compression


class ArchiveResult:
    """Result of an archiving operation."""

    def __init__(
        self,
        success: bool,
        tmp_file: Path,
        archive_file: Optional[Path] = None,
        error: Optional[str] = None,
        source_size: int = 0,
        archived_size: int = 0,
        compression_ratio: float = 0.0,
    ):
        self.success = success
        self.tmp_file = tmp_file
        self.archive_file = archive_file
        self.error = error
        self.source_size = source_size
        self.archived_size = archived_size
        self.compression_ratio = compression_ratio

    def __repr__(self) -> str:
        if self.success:
            return (
                f"ArchiveResult(success=True, file={self.archive_file.name}, "
                f"size={self.archived_size}, ratio={self.compression_ratio:.1f}%)"
            )
        else:
            return f"ArchiveResult(success=False, error={self.error})"


class FileArchiver:
    """Unified file archiving with immediate and bulk modes.

    This class consolidates archiving logic that was duplicated across
    all receiver implementations. It provides:
    - Immediate archiving (file-by-file during download)
    - Bulk archiving (all files after download completes)
    - Compression support with pluggable strategies
    - Comprehensive error handling and logging
    - Validation and verification

    Design considerations:
    - Strategy pattern for archiving modes
    - Plugin architecture for compression formats
    - Preserves tmp files on failure for debugging
    - Detailed metrics for monitoring
    """

    def __init__(
        self,
        file_validator: Optional[FileValidator] = None,
        logger: Optional[logging.Logger] = None,
        mode: ArchiveMode = ArchiveMode.BULK,
        yield_guard: Optional["YieldGuardConfig"] = None,
    ):
        """Initialize file archiver.

        Args:
            file_validator: Optional FileValidator for integrity checks
            logger: Optional logger instance
            mode: Archiving mode (immediate or bulk)
            yield_guard: Optional data-yield guard. When supplied, a raw file
                far below the station's own size baseline is quarantined instead
                of archived (see receivers.utils.yield_guard). Omitted =
                disabled, and the guard itself fails open on any doubt.
        """
        self.file_validator = file_validator
        self.logger = logger or logging.getLogger(__name__)
        self.mode = mode
        self.yield_guard = yield_guard

        # Pending archives queue (for bulk mode)
        self._pending_archives: List[Tuple[Path, Path, bool, bool]] = []

        # Archiving results for reporting
        self._results: List[ArchiveResult] = []

        # Compression strategies registry
        self._compression_strategies: Dict[str, CompressionStrategy] = {
            ".gz": GzipCompression(),
            "": NoCompression(),
            # Future: '.bz2': Bzip2Compression(), '.xz': XzCompression(), etc.
        }

    def _check_yield(self, tmp_file: Path, archive_path: Path, size: int):
        """Run the yield guard, or return None when it is not in play.

        Every failure path here returns None (= archive it). The guard exists to
        stop useless data entering the archive; it must never become a reason
        that real data does not.
        """
        if self.yield_guard is None or not self.yield_guard.enabled:
            return None
        try:
            from .yield_guard import check_yield, parse_archive_path

            station, session_type = parse_archive_path(archive_path)
            if not station or not session_type:
                return None
            return check_yield(
                size,
                station,
                session_type,
                self.yield_guard.connection,
                min_fraction=self.yield_guard.min_fraction,
                lookback_days=self.yield_guard.lookback_days,
                min_samples=self.yield_guard.min_samples,
            )
        except Exception as exc:  # noqa: BLE001 - guard must never break archiving
            self.logger.debug(f"yield guard skipped for {tmp_file.name}: {exc}")
            return None

    def _quarantine_result(
        self, tmp_file: Path, verdict, remove_tmp: bool
    ) -> ArchiveResult:
        """Move a rejected file aside and report it as a non-archive."""
        from .yield_guard import quarantine

        station = verdict.station
        dest = None
        if self.yield_guard.quarantine_root:
            dest = quarantine(tmp_file, self.yield_guard.quarantine_root, station)

        self.logger.error(
            f"🚫 QUARANTINED {tmp_file.name}: {verdict.reason}"
            + (f" → {dest}" if dest else " (not archived)")
        )
        if dest is None and remove_tmp:
            try:
                tmp_file.unlink()
            except OSError:
                pass
        return ArchiveResult(
            success=False,
            tmp_file=tmp_file,
            source_size=verdict.size,
            error=f"quarantined by yield guard: {verdict.reason}",
        )

    def register_compression_strategy(
        self, extension: str, strategy: CompressionStrategy
    ) -> None:
        """Register custom compression strategy.

        Args:
            extension: File extension (e.g., '.zst', '.br')
            strategy: Compression strategy instance
        """
        self._compression_strategies[extension] = strategy
        self.logger.debug(f"Registered compression strategy for {extension}")

    def set_mode(self, mode: ArchiveMode) -> None:
        """Change archiving mode.

        Args:
            mode: New archiving mode
        """
        if mode != self.mode and self._pending_archives:
            self.logger.warning(
                f"Changing mode from {self.mode.value} to {mode.value} "
                f"with {len(self._pending_archives)} pending archives. "
                f"Consider flushing first."
            )
        self.mode = mode

    def archive_file(
        self,
        tmp_file: Path,
        archive_path: Path,
        compress: bool = True,
        remove_tmp: bool = True,
    ) -> bool:
        """Archive single file with optional compression.

        Behavior depends on archiving mode:
        - IMMEDIATE: Archives file immediately
        - BULK: Adds to pending queue, archives on flush()

        Args:
            tmp_file: Path to temporary file to archive
            archive_path: Destination archive path
            compress: Whether to compress (default True)
            remove_tmp: Whether to remove tmp file after archiving (default True)

        Returns:
            True if operation succeeded (or queued in BULK mode)
        """
        if self.mode == ArchiveMode.IMMEDIATE:
            result = self._archive_immediately(
                tmp_file, archive_path, compress, remove_tmp
            )
            self._results.append(result)
            return result.success
        else:
            # BULK mode: add to pending queue
            self._pending_archives.append(
                (tmp_file, archive_path, compress, remove_tmp)
            )
            self.logger.debug(
                f"Queued for bulk archiving: {tmp_file.name} "
                f"(total queued: {len(self._pending_archives)})"
            )
            return True

    def flush_pending_archives(self) -> int:
        """Archive all pending files (BULK mode).

        Returns:
            Number of files successfully archived
        """
        if not self._pending_archives:
            self.logger.debug("No pending archives to flush")
            return 0

        self.logger.info(f"Archiving {len(self._pending_archives)} queued files")
        archived_count = 0

        for tmp_file, archive_path, compress, remove_tmp in self._pending_archives:
            result = self._archive_immediately(
                tmp_file, archive_path, compress, remove_tmp
            )
            self._results.append(result)
            if result.success:
                archived_count += 1

        # Clear queue
        self._pending_archives.clear()

        # Log summary
        if archived_count > 0:
            self.logger.info(f"✅ Successfully archived {archived_count} files")

        failed_count = len(self._results) - archived_count
        if failed_count > 0:
            self.logger.warning(f"❌ Failed to archive {failed_count} files")

        return archived_count

    def _archive_immediately(
        self, tmp_file: Path, archive_path: Path, compress: bool, remove_tmp: bool
    ) -> ArchiveResult:
        """Perform immediate archiving with compression.

        This is the core archiving logic extracted from all receivers.

        Args:
            tmp_file: Source temporary file
            archive_path: Destination archive path
            compress: Whether to compress file
            remove_tmp: Whether to remove tmp file after success

        Returns:
            ArchiveResult with operation details
        """
        filename = tmp_file.name

        try:
            # Check if tmp file exists
            if not tmp_file.exists():
                return ArchiveResult(
                    success=False,
                    tmp_file=tmp_file,
                    error=f"Tmp file not found: {tmp_file}",
                )

            # Get tmp file size
            tmp_file_size = tmp_file.stat().st_size
            self.logger.info(f"File to archive {filename} ({tmp_file_size:,} bytes)")

            # Data-yield guard: a receiver that has lost its sky view keeps
            # producing files on schedule, they are just nearly empty. Reject
            # here — before the archive dir is created — so nothing rejected
            # ever lands in the tree for a reindex or converter to pick up.
            guard_verdict = self._check_yield(tmp_file, archive_path, tmp_file_size)
            if guard_verdict is not None and not guard_verdict.allowed:
                return self._quarantine_result(tmp_file, guard_verdict, remove_tmp)

            # Create archive directory
            archive_path.parent.mkdir(parents=True, exist_ok=True)

            # Check if archive file already exists
            if archive_path.exists():
                archive_file_size = archive_path.stat().st_size
                if tmp_file_size == archive_file_size:
                    self.logger.warning(
                        f"Archive file already exists with same size "
                        f"({tmp_file_size:,} bytes): {archive_path.name}"
                    )
                    # Remove tmp file and consider this a success
                    if remove_tmp:
                        tmp_file.unlink()
                    return ArchiveResult(
                        success=True,
                        tmp_file=tmp_file,
                        archive_file=archive_path,
                        source_size=tmp_file_size,
                        archived_size=archive_file_size,
                    )

            # Select compression strategy
            if compress:
                # Determine compression format from archive path extension
                archive_ext = "".join(archive_path.suffixes[-1:])
                if archive_ext in self._compression_strategies:
                    strategy = self._compression_strategies[archive_ext]
                else:
                    # Default to gzip
                    strategy = self._compression_strategies[".gz"]
                    archive_ext = ".gz"
                    # Adjust archive path to have .gz extension
                    if not str(archive_path).endswith(".gz"):
                        archive_path = Path(str(archive_path) + ".gz")

                # Check if source file is already compressed in target format
                # This prevents double-compression (e.g., gzipping an already gzipped file)
                detector = CompressionDetector(self.logger)
                source_compression = detector.detect_compression(tmp_file)

                if source_compression:
                    source_format, source_ext = source_compression
                    if source_ext == archive_ext:
                        # Source already in target format - just copy, don't re-compress
                        self.logger.info(
                            f"📦 Source already {source_format} compressed, copying: "
                            f"{filename} → {archive_path.name} ({tmp_file_size:,} bytes)"
                        )
                        strategy = self._compression_strategies[
                            ""
                        ]  # NoCompression (copy)
                    else:
                        self.logger.info(
                            f"📦 Archiving with compression: {filename} → {archive_path.name} "
                            f"({tmp_file_size:,} bytes)"
                        )
                else:
                    self.logger.info(
                        f"📦 Archiving with compression: {filename} → {archive_path.name} "
                        f"({tmp_file_size:,} bytes)"
                    )
            else:
                strategy = self._compression_strategies[""]
                self.logger.info(
                    f"📦 Archiving without compression: {filename} → {archive_path.name}"
                )

            # Perform archiving/compression
            strategy.compress(tmp_file, archive_path)

            # Verify archived file
            if not archive_path.exists():
                return ArchiveResult(
                    success=False,
                    tmp_file=tmp_file,
                    archive_file=archive_path,
                    error="Archived file not found after operation",
                )

            # Get archived file size and calculate metrics
            archived_size = archive_path.stat().st_size
            compression_ratio = strategy.get_compression_ratio(
                tmp_file_size, archived_size
            )

            if compress and compression_ratio > 0:
                self.logger.info(
                    f"✅ Compressed and archived to: {archive_path} "
                    f"({archived_size:,} bytes, {compression_ratio:.1f}% reduction)"
                )
            else:
                self.logger.info(
                    f"✅ Archived to: {archive_path} ({archived_size:,} bytes)"
                )

            # Remove tmp file if requested
            if remove_tmp and tmp_file.exists():
                tmp_file.unlink()
                self.logger.debug(f"🧹 Removed tmp file: {tmp_file}")

            return ArchiveResult(
                success=True,
                tmp_file=tmp_file,
                archive_file=archive_path,
                source_size=tmp_file_size,
                archived_size=archived_size,
                compression_ratio=compression_ratio,
            )

        except Exception as e:
            error_msg = f"Failed to archive {filename}: {e}"
            self.logger.error(f"❌ {error_msg}")

            # Clean up tmp file on failure if requested
            if remove_tmp and tmp_file.exists():
                try:
                    tmp_file.unlink()
                    self.logger.info(f"🧹 Cleaned up failed tmp file: {tmp_file}")
                except Exception as cleanup_e:
                    self.logger.error(
                        f"❌ Failed to cleanup tmp file {tmp_file}: {cleanup_e}"
                    )

            return ArchiveResult(
                success=False,
                tmp_file=tmp_file,
                archive_file=archive_path,
                error=error_msg,
            )

    def batch_archive_files(
        self,
        downloaded_files: List[str],
        archive_files_dict: Dict[str, str],
        compress: bool = True,
        remove_tmp: bool = True,
    ) -> int:
        """Archive multiple files based on mapping dictionary.

        This method matches the signature used in receiver implementations
        for easy drop-in replacement.

        Args:
            downloaded_files: List of downloaded file paths
            archive_files_dict: Dict mapping filename to archive path
            compress: Whether to compress files
            remove_tmp: Whether to remove tmp files after archiving

        Returns:
            Number of files successfully archived
        """
        archived_count = 0

        for file_path_str in downloaded_files:
            file_path = Path(file_path_str)
            filename = file_path.name

            if not file_path.exists():
                self.logger.warning(f"Cannot archive - file not found: {file_path}")
                continue

            if filename in archive_files_dict:
                archive_path = Path(archive_files_dict[filename])

                # Use appropriate mode
                success = self.archive_file(
                    file_path, archive_path, compress=compress, remove_tmp=remove_tmp
                )

                if self.mode == ArchiveMode.IMMEDIATE and success:
                    archived_count += 1
            else:
                self.logger.warning(f"No archive path found for {filename}")

        # If in BULK mode, flush pending archives now
        if self.mode == ArchiveMode.BULK:
            archived_count = self.flush_pending_archives()

        return archived_count

    def get_results(self) -> List[ArchiveResult]:
        """Get archiving results for reporting.

        Returns:
            List of ArchiveResult objects
        """
        return self._results.copy()

    def get_statistics(self) -> Dict[str, any]:
        """Get archiving statistics summary.

        Returns:
            Dictionary with statistics:
            {
                'total_files': int,
                'successful': int,
                'failed': int,
                'total_source_size': int,
                'total_archived_size': int,
                'average_compression_ratio': float
            }
        """
        total_files = len(self._results)
        successful = sum(1 for r in self._results if r.success)
        failed = total_files - successful

        total_source_size = sum(r.source_size for r in self._results)
        total_archived_size = sum(r.archived_size for r in self._results)

        # Calculate average compression ratio (only for compressed files)
        compressed_results = [r for r in self._results if r.compression_ratio > 0]
        avg_compression = (
            sum(r.compression_ratio for r in compressed_results)
            / len(compressed_results)
            if compressed_results
            else 0.0
        )

        return {
            "total_files": total_files,
            "successful": successful,
            "failed": failed,
            "total_source_size": total_source_size,
            "total_archived_size": total_archived_size,
            "average_compression_ratio": avg_compression,
        }

    def clear_results(self) -> None:
        """Clear archiving results history."""
        self._results.clear()

    def get_pending_count(self) -> int:
        """Get number of pending archives (BULK mode).

        Returns:
            Number of files waiting to be archived
        """
        return len(self._pending_archives)

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - auto-flush pending archives."""
        if self._pending_archives:
            self.logger.info(
                f"Auto-flushing {len(self._pending_archives)} pending archives"
            )
            self.flush_pending_archives()
        return False
