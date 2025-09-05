# Development Setup Guide

This guide covers setting up the development environment for the Receivers package.

## 🚀 Quick Setup

### 1. Create Mamba Environment

```bash
# Navigate to receivers directory
cd /path/to/receivers

# Create mamba environment from file
mamba env create -f environment.yml

# Activate environment
mamba activate receivers
```

### 2. Install Package in Development Mode

```bash
# Install receivers package
pip install -e .

# Install with development dependencies
pip install -e .[dev]

# Install with all optional dependencies
pip install -e .[all]
```

### 3. Install Sibling Packages

```bash
# Install gtimes (GPS time processing)
cd ../gtimes
pip install -e .

# Install gps_parser (station configuration)  
cd ../gps_parser
pip install -e .

# Return to receivers
cd ../receivers
```

### 4. Verify Installation

```bash
# Test basic import
python -c "import receivers; print('✅ Receivers package installed')"

# Test CLI
receivers --help
receivers health --help

# Test with dependencies (when available)
python -c "
try:
    from receivers import PolaRX5
    print('✅ PolaRX5 available')
except ImportError as e:
    print(f'⚠️  PolaRX5 not available: {e}')
"
```

## 🧪 Testing

### Run Basic Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=receivers

# Run specific test categories
pytest tests/ -m "unit"
pytest tests/ -m "integration" 
```

### Code Quality Checks

```bash
# Linting and formatting
ruff check src/ tests/
ruff format src/ tests/

# Type checking
mypy src/receivers/

# Security scanning  
bandit -r src/
```

## 🔧 Development Workflow

### 1. Environment Management

```bash
# List environments
mamba env list

# Update environment
mamba env update -f environment.yml

# Export current environment
mamba env export > environment-dev.yml
```

### 2. Package Development

```bash
# Reinstall package after changes
pip install -e .

# Build package
python -m build

# Test built package
pip install dist/*.whl
```

### 3. Git Workflow

```bash
# Check status
git status

# Create feature branch
git checkout -b feature/new-receiver-type

# Commit changes
git add .
git commit -m "feat: add new receiver support"

# Push to GitHub
git push origin feature/new-receiver-type
```

## 🐛 Troubleshooting

### Common Issues

**Import Errors:**
```bash
# Make sure you're in the right environment
mamba activate receivers

# Reinstall package
pip install -e .
```

**Missing Dependencies:**
```bash
# Install gtimes locally
cd ../gtimes && pip install -e .

# Install gps_parser locally  
cd ../gps_parser && pip install -e .
```

**Test Failures:**
```bash
# Install test dependencies
pip install -e .[test]

# Run with verbose output
pytest tests/ -v --tb=long
```

### Environment Issues

**Mamba/Conda Conflicts:**
```bash
# Remove existing environment
mamba env remove -n receivers

# Recreate from file
mamba env create -f environment.yml
```

**Path Issues:**
```bash
# Check Python path
python -c "import sys; print(sys.path)"

# Verify package location
python -c "import receivers; print(receivers.__file__)"
```

## 📝 Development Tips

### IDE Setup (VS Code)

1. **Select Python Interpreter:**
   - `Ctrl+Shift+P` → "Python: Select Interpreter"
   - Choose mamba environment: `~/mambaforge/envs/receivers/bin/python`

2. **Extensions:**
   - Python
   - Pylance  
   - Ruff
   - GitLens

3. **Settings (.vscode/settings.json):**
   ```json
   {
       "python.defaultInterpreterPath": "~/mambaforge/envs/receivers/bin/python",
       "python.linting.enabled": true,
       "python.linting.ruffEnabled": true,
       "python.formatting.provider": "ruff"
   }
   ```

### Debug Configuration

Create `.vscode/launch.json`:
```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Debug CLI",
            "type": "python",
            "request": "launch",
            "module": "receivers.cli.main",
            "args": ["health", "REYK"],
            "console": "integratedTerminal"
        }
    ]
}
```

### Pre-commit Hooks (Optional)

```bash
# Install pre-commit
pip install pre-commit

# Setup hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

## 🚀 Next Steps

1. **Integration Testing**: Test with real receivers (when operational access available)
2. **Additional Receivers**: Add support for other receiver types
3. **API Integration**: Connect with external monitoring systems
4. **Documentation**: Add comprehensive docs with MkDocs

## 📞 Support

- **Issues**: GitHub Issues for bugs and feature requests
- **Discussion**: GitHub Discussions for questions
- **Contact**: bgo@vedur.is for technical support