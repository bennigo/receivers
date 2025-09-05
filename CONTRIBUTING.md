# Contributing to Receivers

Thank you for your interest in contributing to the Receivers project! This guide will help you get started with development and contribution procedures.

## 🚀 Quick Start

### Prerequisites
- Python 3.8 or higher
- Mamba or Conda for environment management
- Git for version control

### Development Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/vedur-is/receivers.git
   cd receivers
   ```

2. **Set up development environment**
   ```bash
   # Create mamba environment
   mamba env create -f environment.yml
   mamba activate receivers
   
   # Install package in development mode
   pip install -e .[dev]
   ```

3. **Verify setup**
   ```bash
   # Test basic functionality
   python -c "import receivers; print('✅ Setup successful')"
   
   # Run CLI help
   receivers --help
   ```

4. **Install local dependencies**
   ```bash
   # Install gtimes (when available)
   cd ../gtimes
   pip install -e .
   
   # Install gps_parser (when available)  
   cd ../gps_parser
   pip install -e .
   ```

## 🏗️ Project Structure

```
receivers/
├── src/receivers/          # Main package code
│   ├── base/              # Base classes and interfaces
│   ├── septentrio/        # Septentrio receiver implementations  
│   └── cli/               # Command-line interface
├── tests/                 # Test suite
├── docs/                  # Documentation (future)
├── .github/workflows/     # GitHub Actions CI/CD
├── pyproject.toml         # Package configuration
└── environment.yml        # Mamba environment
```

## 🧪 Testing

### Running Tests
```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=receivers --cov-report=html

# Run specific test categories
pytest tests/ -m "unit"          # Unit tests
pytest tests/ -m "integration"   # Integration tests
pytest tests/ -m "ftp"           # FTP-specific tests
```

### Adding Tests
- Place unit tests in `tests/test_*.py`
- Use descriptive test names and docstrings
- Mock external dependencies (FTP connections, etc.)
- Test both success and failure scenarios

Example test structure:
```python
def test_polarx5_connection_success(mock_ftp):
    """Test successful FTP connection to PolaRX5 receiver."""
    # Setup mocks
    # Execute test
    # Verify results
```

## 🔍 Code Quality

### Automated Checks
```bash
# Linting and formatting
ruff check src/ tests/
ruff format src/ tests/

# Type checking  
mypy src/receivers/

# Security scanning
bandit -r src/
```

### Code Style Guidelines
- Follow PEP 8 style guidelines
- Use type hints for all function signatures
- Write descriptive docstrings (Google style)
- Keep functions focused and testable
- Use meaningful variable and function names

### Pre-commit Setup (Optional)
```bash
pip install pre-commit
pre-commit install
```

## 📝 Documentation

### Docstring Style
Use Google-style docstrings:

```python
def download_data(self, start: datetime, end: datetime) -> Dict[str, Any]:
    """Download data from receiver for specified time period.
    
    Args:
        start: Start time for data download
        end: End time for data download
        
    Returns:
        Dictionary with download results and file information
        
    Raises:
        ConnectionError: If unable to connect to receiver
        DownloadError: If download fails
    """
```

### Adding Documentation
- Update README.md for user-facing changes
- Update CHANGELOG.md following Keep a Changelog format
- Add docstrings to all public functions and classes
- Include usage examples in docstrings

## 🎯 Contributing Guidelines

### Branch Strategy
- `main`: Stable releases
- `develop`: Integration branch for features
- `feature/xyz`: Individual feature branches
- `fix/xyz`: Bug fix branches

### Pull Request Process

1. **Create feature branch**
   ```bash
   git checkout develop
   git pull origin develop
   git checkout -b feature/your-feature-name
   ```

2. **Make changes**
   - Write code following style guidelines
   - Add/update tests
   - Update documentation
   - Test thoroughly

3. **Quality checks**
   ```bash
   ruff check src/ tests/
   mypy src/receivers/
   pytest tests/ -v
   ```

4. **Commit changes**
   ```bash
   git add .
   git commit -m "feat: add new receiver type support"
   ```

5. **Create pull request**
   - Push branch to GitHub
   - Create PR against `develop` branch
   - Fill out PR template completely
   - Ensure CI passes

### Commit Message Format
Follow conventional commits:
- `feat:` New features
- `fix:` Bug fixes
- `docs:` Documentation changes
- `style:` Code style changes
- `refactor:` Code refactoring
- `test:` Test additions/changes
- `chore:` Build/tooling changes

## 🔧 Adding New Receiver Types

### Step-by-Step Process

1. **Create receiver class**
   ```python
   # src/receivers/manufacturer/model.py
   from ..base.receiver import BaseReceiver
   
   class NewReceiver(BaseReceiver):
       def get_connection_status(self) -> Dict[str, Any]:
           # Implementation
       
       def download_data(self, **kwargs) -> Dict[str, Any]:
           # Implementation
       
       def get_health_status(self) -> Dict[str, Any]:
           # Implementation
   ```

2. **Add to package exports**
   ```python
   # src/receivers/__init__.py
   from .manufacturer.model import NewReceiver
   __all__.append("NewReceiver")
   ```

3. **Update CLI support**
   ```python
   # src/receivers/cli/main.py
   def create_receiver(station_id: str, receiver_type: str):
       if receiver_type == "new_receiver":
           return NewReceiver(station_id, station_info)
   ```

4. **Add comprehensive tests**
   - Unit tests for all methods
   - Integration tests with mocked connections
   - Error handling tests

5. **Update documentation**
   - README.md usage examples
   - CLI help text
   - API documentation

## 🐛 Bug Reports

### Before Reporting
- Check existing issues
- Test with latest version
- Verify it's not a configuration issue

### Bug Report Template
- **Description**: Clear description of the bug
- **Steps to Reproduce**: Detailed steps
- **Expected Behavior**: What should happen
- **Actual Behavior**: What actually happens
- **Environment**: OS, Python version, package versions
- **Logs**: Relevant error messages/logs

## 🚀 Feature Requests

### Feature Request Template
- **Problem**: What problem does this solve?
- **Solution**: Proposed solution
- **Alternatives**: Other solutions considered
- **Use Case**: How would this be used?
- **Priority**: How important is this feature?

## ❓ Getting Help

- **Documentation**: Check README.md and inline documentation
- **Issues**: Search existing GitHub issues
- **Discussions**: Use GitHub Discussions for questions
- **Contact**: bgo@vedur.is for technical questions

## 📄 License

By contributing, you agree that your contributions will be licensed under the MIT License.

## 🙏 Recognition

Contributors will be recognized in:
- CHANGELOG.md for significant contributions
- README.md contributors section
- Git commit history and GitHub insights

Thank you for helping make Receivers better! 🎉