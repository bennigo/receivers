"""Receivers - GPS/GNSS receiver management and data download toolkit."""

__version__ = "0.1.0"
__author__ = "Benedikt Gunnar Ófeigsson"
__email__ = "bgo@vedur.is"

# Import base classes and exceptions
from .base.exceptions import (
    ConfigurationError,
    ConnectionError,
    DownloadError,
    ReceiverError,
)
from .base.receiver import BaseReceiver

# Conditionally import receiver implementations
__all__ = [
    "BaseReceiver",
    "ReceiverError",
    "ConnectionError",
    "DownloadError",
    "ConfigurationError",
]

try:
    from .septentrio.polarx5 import PolaRX5
    __all__.append("PolaRX5")
except ImportError:
    # Dependencies not available, skip receiver implementations
    pass
