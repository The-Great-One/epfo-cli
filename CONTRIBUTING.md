# Contributing to epfo-cli

First off — thank you for taking the time to contribute! 🎉

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How Can I Contribute?](#how-can-i-contribute)
  - [Reporting Bugs](#reporting-bugs)
  - [Suggesting Features](#suggesting-features)
  - [Pull Requests](#pull-requests)
- [Development Setup](#development-setup)
- [Style Guide](#style-guide)
- [Commit Messages](#commit-messages)

## Code of Conduct

This project is governed by the [Code of Conduct](CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it. Report unacceptable behavior to
**sahilgoel187@gmail.com**.

## How Can I Contribute?

### Reporting Bugs

Before creating a bug report, check the
[existing issues](https://github.com/The-Great-One/epfo-cli/issues). When you
create one, include:

- A clear and descriptive title
- Exact steps to reproduce
- Expected vs. actual behavior
- Screenshots if applicable
- Your environment (OS, Python version)

### Suggesting Features

Feature requests are welcome. Check existing issues first, then open a new one.

### Pull Requests

1. Fork the repository and create your branch from `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```
2. Make your changes and add tests where applicable.
3. Run the test suite:
   ```bash
   env -u PYTHONPATH .venv/bin/python -m pytest -q
   ```
4. Commit with a clear message (see below).
5. Open a pull request.

## Development Setup

```bash
git clone https://github.com/The-Great-One/epfo-cli.git
cd epfo-cli
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[keychain,ddddocr,browser,dev]'
brew install tesseract
```

> Install into the venv, never a shared interpreter. Strip `PYTHONPATH` if it
> leaks in your shell.

## Style Guide

- Python 3.10+, PEP 8.
- Read-only client: never mutate portal state.
- Tests live in `tests/`; add coverage for new behavior.

## Commit Messages

Use conventional commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`,
`test:`. Keep the subject under 72 characters.
