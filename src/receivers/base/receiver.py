"""Abstract base class for GPS/GNSS receivers."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Union


class BaseReceiver(ABC):
    """Abstract base class for GPS/GNSS receivers.

    This class defines the common interface that all receiver implementations
    must follow to ensure consistency across different receiver types.
    """

    def __init__(self, station_id: str, station_info: Dict[str, Any]):
        """Initialize receiver with station information.

        Args:
            station_id: Station identifier (e.g., 'REYK', 'HOFN')
            station_info: Station configuration dictionary
        """
        self.station_id = station_id.upper()
        self.station_info = station_info
        self.connection_status = {"router": None, "receiver": None}

    @abstractmethod
    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to receiver.

        Returns:
            Dictionary with connection status information
        """
        pass

    @abstractmethod
    def download_data(
        self,
        start: Union[datetime, str],
        end: Union[datetime, str],
        session: str = "15s_24hr",
        sync: bool = True,
        clean_tmp: bool = True,
        archive: bool = True,
        **kwargs
    ) -> Dict[str, Any]:
        """Download data from receiver for specified time period.

        Args:
            start: Start time for data download
            end: End time for data download
            session: Data session type (e.g., '15s_24hr', '1Hz_1hr')
            sync: Whether to sync missing files
            clean_tmp: Whether to clean temporary download directory
            archive: Whether to archive downloaded files
            **kwargs: Additional receiver-specific parameters

        Returns:
            Dictionary with download results and file information
        """
        pass

    @abstractmethod
    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status of receiver.

        Returns:
            Dictionary with health metrics and status information
        """
        pass

    @abstractmethod
    def get_station_info(self) -> Dict[str, Any]:
        """Get station information and configuration.

        Returns:
            Dictionary with station information
        """
        pass

    def get_receiver_type(self) -> str:
        """Get receiver type identifier.

        Returns:
            String identifier for receiver type
        """
        return self.__class__.__name__

    def get_station_id(self) -> str:
        """Get station identifier.

        Returns:
            Station ID string
        """
        return self.station_id

    def __str__(self) -> str:
        """String representation of receiver."""
        return f"{self.get_receiver_type()}({self.station_id})"

    def __repr__(self) -> str:
        """Detailed string representation of receiver."""
        return f"{self.__class__.__name__}(station_id='{self.station_id}')"
