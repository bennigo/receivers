# Changelog

All notable changes to the Receivers project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Comprehensive test suite with mock FTP testing
- Integration with gps_parser for station configuration
- Health monitoring and alerting capabilities
- API endpoints for external system integration

### Changed
- Enhanced error handling and recovery mechanisms
- Improved logging and monitoring capabilities
- Performance optimizations for large file downloads

## [0.1.0] - 2024-12-22

### Added
- **Phase 1: MVP Implementation** 🎉
  - Modern Python package structure with pyproject.toml
  - Abstract BaseReceiver class for extensible receiver support
  - Complete PolaRX5 implementation with modernized download logic
  - CLI with subcommand structure (health, download, status)
  - Rich console output with progress bars and status tables
  - Comprehensive error handling and logging
  - Type hints throughout codebase
  - Basic test framework with pytest
  - Mamba environment configuration
  - MIT license and professional documentation

- **Septentrio PolaRX5 Support**
  - FTP-based data download with progress tracking
  - Configurable session types (15s_24hr, 1Hz_1hr, status_1hr)
  - Passive/active FTP mode detection based on IP ranges
  - Archive management with proper directory structure
  - Connection health monitoring and status reporting
  - Support for compressed file formats (.gz)

- **Command-Line Interface**
  - `receivers health STATION_ID` - Check receiver connectivity
  - `receivers download STATION_ID` - Download data with flexible options
  - `receivers status STATION_ID` - Display detailed receiver information
  - JSON output option for automation and scripting
  - Verbose logging and error reporting
  - Graceful handling of missing dependencies

- **Development Infrastructure**
  - Modern packaging with hatchling build system
  - Comprehensive dependency management
  - Code quality tools (ruff, black, mypy)
  - GitHub Actions CI/CD pipeline
  - Documentation structure ready for MkDocs

### Technical Details
- **Dependencies**: gtimes>=0.4.0, gps_parser, rich>=13.0.0, progressbar2
- **Python Support**: 3.8+ with full type hint coverage
- **Architecture**: Modular design supporting multiple receiver types
- **Backwards Compatibility**: Preserves all proven FTP download logic from legacy getSeptentrio3

### Migration Notes
- Replaces script-based getSeptentrio3 with object-oriented PolaRX5 class
- Maintains compatibility with existing file organization and archive structure
- Configuration still managed through gps_parser (station configuration)
- Command-line interface follows modern subcommand pattern

---

## Development Philosophy

This project follows incremental development principles:
- **Start Small**: Begin with core functionality and proven algorithms
- **Build Modular**: Design for easy extension to new receiver types  
- **Maintain Quality**: Comprehensive testing and code quality standards
- **Stay Operational**: Preserve reliability of existing workflows

## Contribution Guidelines

See [Contributing Guide](docs/development/contributing.md) for development setup and contribution procedures.