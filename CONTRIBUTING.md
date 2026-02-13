# Contributing to Nexus

Thank you for your interest in contributing to Nexus! This document provides guidelines and instructions for contributing.

## Code of Conduct

- Be respectful and inclusive
- Provide constructive feedback
- Focus on what is best for the community
- Show empathy towards other community members

## How to Contribute

### Reporting Bugs

Before submitting a bug report:

1. Check existing issues to avoid duplicates
2. Verify the bug with the latest version
3. Collect relevant information (logs, configuration, etc.)

When submitting a bug report, include:

- Clear, descriptive title
- Steps to reproduce
- Expected vs actual behavior
- Environment details (OS, Docker version, etc.)
- Relevant logs and error messages
- Screenshots if applicable

### Suggesting Features

Feature requests are welcome! Include:

- Clear description of the feature
- Use case and motivation
- Examples of how it would work
- Any relevant technical details

### Code Contributions

#### Getting Started

1. **Fork the repository**

```bash
git clone https://github.com/YOUR-USERNAME/nexus.git
cd nexus
git remote add upstream https://github.com/gevanoff/nexus.git
```

2. **Create a branch**

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix
```

3. **Set up development environment**

```bash
chmod +x quickstart.sh deploy/scripts/*.sh
./quickstart.sh
```

Or manually:

```bash
cp .env.example .env
# Edit .env with development settings
docker compose up -d
```

#### Making Changes

1. **Follow coding standards**
   - Python: PEP 8
   - Use type hints where applicable
   - Add docstrings to functions and classes
   - Keep functions focused and small

2. **Write tests**
   - Add tests for new features
   - Ensure existing tests pass
   - Aim for good test coverage

3. **Update documentation**
   - Update README if needed
   - Add/update docstrings
   - Update relevant guides

4. **Commit your changes**

Use clear, descriptive commit messages:

```bash
# Good
git commit -m "Add streaming support to TTS service"
git commit -m "Fix race condition in gateway health check"

# Not as good
git commit -m "Update code"
git commit -m "Fix bug"
```

Follow commit message format:
```
<type>: <subject>

<body>

<footer>
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting)
- `refactor`: Code refactoring
- `test`: Adding/updating tests
- `chore`: Maintenance tasks

5. **Push to your fork**

```bash
git push origin feature/your-feature-name
```

6. **Create a Pull Request**

- Use a clear, descriptive title
- Reference related issues
- Describe your changes
- Include screenshots for UI changes
- Check that CI passes

#### Pull Request Guidelines

- One feature/fix per PR
- Keep PRs focused and small
- Rebase on latest main before submitting
- Respond to review feedback promptly
- Squash commits before merging (if requested)

### Adding a New Service

To add a new service to Nexus:

1. **Create service directory**

```bash
mkdir services/my-service
cd services/my-service
```

2. **Copy template**

```bash
cp ../template/example-service.py app/main.py
```

3. **Implement required endpoints**
   - `/health` - Liveness check
   - `/readyz` - Readiness check
   - `/v1/metadata` - Service metadata

4. **Create Dockerfile**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
EXPOSE 9000
HEALTHCHECK CMD curl -f http://localhost:9000/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
```

5. **Add a per-component compose file** (create `docker-compose.<service>.yml`)

6. **Add documentation**
   - Service README.md
   - Update main README.md
   - Update services/README.md

7. **Add tests**

8. **Submit PR**

See [Template Service](services/template/README.md) for detailed guide.

## Development Setup

### Prerequisites

- Docker 20.10+
- Docker Compose 2.0+
- Python 3.11+ (for local development)
- Git

### Local Development

1. **Clone repository**

```bash
git clone https://github.com/gevanoff/nexus.git
cd nexus
```

2. **Start services**

```bash
docker compose up -d
```

3. **View logs**

```bash
docker compose logs -f
```

4. **Make changes**

Edit files in `services/gateway/app/` or other service directories.

5. **Restart services**

```bash
docker compose restart gateway
```

6. **Test changes**

```bash
# Test gateway
curl http://localhost:8800/health

# Run tests
docker compose exec gateway pytest
```

### Running Tests

```bash
# Run all tests
docker compose exec gateway pytest

# Run specific test
docker compose exec gateway pytest tests/test_health.py

# Run with coverage
docker compose exec gateway pytest --cov=app --cov-report=html
```

### Code Style

Format code before committing:

```bash
# Install formatters
pip install black isort flake8

# Format code
black services/gateway/app/
isort services/gateway/app/

# Check style
flake8 services/gateway/app/
```

## Documentation

### Writing Documentation

- Use clear, concise language
- Include examples
- Keep formatting consistent
- Update table of contents if needed
- Test all commands/examples

### Documentation Structure

```
nexus/
├── README.md                    # Main readme
├── ARCHITECTURE.md              # System design
├── SERVICE_API_SPECIFICATION.md # API standards
├── CONTRIBUTING.md              # This file
├── docs/
│   ├── DEPLOYMENT.md           # Deployment guide
│   └── MIGRATION.md            # Migration guide
└── services/
    ├── README.md               # Services overview
    └── <service>/
        └── README.md           # Service-specific docs
```

## Release Process

1. **Update version numbers**
   - Update version in service files
   - Update CHANGELOG.md

2. **Create release branch**

```bash
git checkout -b release/v1.0.0
```

3. **Test thoroughly**
   - Run all tests
   - Test deployment
   - Test migration
   - Test upgrades

4. **Create release**
   - Tag release: `git tag -a v1.0.0 -m "Release v1.0.0"`
   - Push tag: `git push origin v1.0.0`
   - Create GitHub release

5. **Announce release**
   - Update main branch
   - Announce in discussions
   - Update documentation

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/gevanoff/nexus/issues)
- **Discussions**: [GitHub Discussions](https://github.com/gevanoff/nexus/discussions)
- **Documentation**: [docs/](docs/)

## Code Review Process

1. PR submitted
2. Automated checks run
3. Maintainer review (1-2 reviewers)
4. Feedback addressed
5. Approved and merged

### Review Criteria

- Code quality and style
- Test coverage
- Documentation
- Performance impact
- Security implications
- Breaking changes

## License

By contributing to Nexus, you agree that your contributions will be licensed under the same license as the project.

## Recognition

Contributors are recognized in:
- CONTRIBUTORS.md (coming soon)
- Release notes
- GitHub contributors page

## Questions?

Don't hesitate to ask questions:
- Open a discussion
- Comment on relevant issue
- Reach out to maintainers

Thank you for contributing to Nexus! 🎉
