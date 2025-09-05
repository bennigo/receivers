# Receivers - GPS/GNSS Receiver Management Toolkit

A Python package for managing GPS/GNSS receivers, downloading data, and monitoring station health. Primarily designed for Septentrio PolaRX5 receivers in the Icelandic Met Office GPS network.

## 🚀 Quick Start

### Installation

```bash
# Development installation
cd receivers
pip install -e .

# With development dependencies  
pip install -e .[dev]
```

### Basic Usage

```bash
# Check receiver health (using ELDC as working example)
receivers health ELDC

# Download data (dry run)
receivers download REYK --start 2024-01-15 --end 2024-01-20

# Actually download data
receivers download REYK --start 2024-01-15 --end 2024-01-20 --sync

# Get station status
receivers status HOFN --json
```

### Python API

```python
from receivers import PolaRX5

# Create receiver instance (requires station configuration)
receiver = PolaRX5("REYK", station_info)

# Check health
health = receiver.get_health_status()
print(f"Status: {health['overall_status']}")

# Download data
result = receiver.download_data(
    start="2024-01-15",
    end="2024-01-20", 
    sync=True
)
print(f"Downloaded {result['files_downloaded']} files")
```

## 🏗️ Architecture

### Receiver Classes

- `BaseReceiver`: Abstract base class defining common interface
- `PolaRX5`: Septentrio PolaRX5 implementation

### CLI Commands

- `receivers health STATION_ID`: Check receiver connectivity and status
- `receivers download STATION_ID`: Download data for specified period
- `receivers status STATION_ID`: Display detailed receiver information

### Design Principles

- **Modular**: Easy to add support for new receiver types
- **Unified Interface**: Consistent API across receiver types
- **Operational Ready**: Designed for 24/7 operational use
- **Rich Output**: Beautiful CLI output with Rich library

## 🔧 Development

### Current Status: Phase 1 MVP

✅ **Completed**:
- Modern package structure with pyproject.toml
- Abstract base receiver class
- PolaRX5 implementation with modernized download logic
- CLI with subcommand structure
- Rich console output
- Type hints and error handling

🔄 **In Progress**:
- Integration with gps_parser for station configuration (requires full receiver config data)
- Comprehensive health monitoring 
- Unit tests

⚠️ **Current Configuration Status**:
- gps_parser package available but only contains basic station info (name, ID)
- Missing router/receiver connection details (IP, ports, etc.)  
- Using fallback configuration for testing (ELDC: 10.6.1.90:2160)
- Full integration requires completing station configuration files

📋 **Planned**:
- Additional receiver types (Leica, NetRS, etc.)
- API integration endpoints
- Advanced health analytics
- Comprehensive documentation

### Testing

```bash
# Run tests (when available)
pytest tests/ -v

# Code quality
ruff check src/ tests/
black src/ tests/
mypy src/receivers/
```

## 📦 Dependencies

### Core Dependencies
- `gtimes>=0.4.0`: GPS time conversions
- `gps_parser`: Station configuration (local package)
- `rich>=13.0.0`: Console output
- `progressbar2`: Download progress display

### Development Dependencies
- `pytest`: Testing framework
- `ruff`: Linting and formatting
- `mypy`: Type checking

## 🌐 Integration

This package is part of the GPS library ecosystem:

- **gtimes**: GPS time processing
- **gps_parser**: Station configuration management  
- **geo_dataread**: GPS data analysis
- **tostools**: TOS API integration

## 📋 Configuration

Receivers require station configuration information including:

```python
station_info = {
    "router": {
        "ip": "10.6.1.90"  # Example: ELDC station
    },
    "receiver": {
        "ftpport": "2160"  # Port forward for FTP access
    }
}
```

Configuration is typically managed through the `gps_parser` package.

## 🚨 Operational Notes

### Septentrio PolaRX5 Specifics

- Uses FTP for data download
- Supports passive/active FTP based on IP range
- Downloads SBF format files (.sbf.gz)
- Organizes data by session type (15s_24hr, 1Hz_1hr, etc.)

### Network Configuration

- Internal IMO network: Uses non-passive FTP (10.4.1.x, 10.4.2.x)
- External networks: Uses passive FTP by default  
- Port forwards available: 2160, 8060 for remote station access
- Configurable timeouts and retry logic

### Tested Stations

- **ELDC** (Eldvörp): 10.6.1.90:2160 - ✅ HEALTHY (solar/wind powered)
- **THOB** (Þorbjörn): Grid powered
- Additional stations require proper configuration setup

### File Organization

Downloaded files follow the structure:
```
/data/YYYY/MMM/STATION/SESSION/raw/
```

## 📄 License

MIT License - See LICENSE file for details.

## 🙏 Acknowledgments

- Based on original work by Fjalar Sigurdsson (fjalar@vedur.is)
- Continued development by Benedikt Gunnar Ófeigsson (bgo@vedur.is)
- Veðurstofan Íslands (Icelandic Met Office) for operational requirements