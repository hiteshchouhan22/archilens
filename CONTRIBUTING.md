# Contributing to ArchiLens

Thank you for your interest in contributing! This guide covers everything you need to get started.

## Table of Contents

- [Development setup](#development-setup)
- [Project structure](#project-structure)
- [Running tests](#running-tests)
- [Submitting changes](#submitting-changes)
- [Areas for contribution](#areas-for-contribution)
- [Code style](#code-style)

---

## Development Setup

**Requirements**: Python 3.10+, Git

```bash
# Fork and clone
git clone https://github.com/<your-username>/archilens.git
cd archilens

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows

# Install in editable mode with all extras
pip install -e ".[dev,viewer,mcp]"
```

---

## Project Structure

```
archilens/
├── analyzers/       # AST extraction, import resolution, module layout, diff, evolution
├── generators/      # Diagram output (Mermaid, D2, PlantUML)
├── viewer/          # Flask interactive viewer
├── mcp/             # MCP server for coding agents + snapshot cache
├── ai/              # LLM integration (Anthropic / OpenAI / LiteLLM)
├── models/          # Pydantic data models
├── utils/           # Module glob matching
├── cli.py           # Click CLI entry point
├── config.py        # YAML config loader
└── engine.py        # Main analysis pipeline
tests/
├── test_core.py         # Core engine tests
├── test_new_features.py # Tree-sitter, generators, viewer, diff, evolution
├── test_internals.py    # Diff rules, generators, engine helpers, glob matching
├── test_cli.py          # CLI commands, stdout hygiene, serve-mcp
├── test_mcp.py          # MCP server tools
├── test_hardening.py    # Discovery, determinism, escaping, AI request shapes
├── test_resolution.py   # Per-language import resolution and module layout fixtures
└── test_coverage.py     # Config, discovery and git-utils edge cases
```

Key data flow: `config.py` → `engine.py` → `analyzers/` → `ArchSnapshot` → `generators/` → diagrams.

---

## Running Tests

```bash
# Full suite with coverage
pytest tests/ -v --cov=archilens --cov-report=term-missing

# Single file
pytest tests/test_new_features.py -v

# Single test class or test
pytest tests/test_new_features.py::TestD2Generator -v
pytest tests/test_new_features.py::TestViewer::test_module_map_returns_sanitised_keys -v
```

All tests must pass before a PR can be merged. CI runs on Python 3.10 through 3.14.

Tests that touch the AI layer mock the SDK client; none of them call the Anthropic API.

---

## Submitting Changes

1. **Open an issue first** for any non-trivial change so we can discuss the approach.
2. Create a feature branch from `master`: `git checkout -b feat/my-feature`
3. Make your changes with tests.
4. Run the full test suite and linters locally (see [Code style](#code-style)).
5. Open a pull request. The CI pipeline will run automatically.

### Commit message format

```
type: short description (under 72 chars)

Optional longer explanation.
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.

---

## Areas for Contribution

### Language parsers
Add Tree-sitter support for a new language (Ruby, Rust, C#, etc.):
1. Add the `tree-sitter-<lang>` package to `pyproject.toml` dependencies.
2. Add query strings to `archilens/analyzers/treesitter.py` (`_IMPORT_QUERIES`, `_CLASS_QUERIES`, `_FUNCTION_QUERIES`).
3. Wire up the language in `_get_lang_parser()`.
4. Teach `archilens/analyzers/resolution.py` to map that language's import strings to repository files
   (`ImportResolver.resolve`), and, if it has a conventional source root, add it to
   `archilens/analyzers/layout.py`.
5. Add extraction tests in `TestTreeSitter` and a golden fixture in `tests/test_resolution.py`
   asserting the module edges a reader would draw by hand.

### Architectural pattern detection
Patterns are detected by the LLM in `archilens/ai/__init__.py` (`detect_patterns`), constrained to the
`ArchPattern` enum in `archilens/models/__init__.py`, and by a lightweight naming heuristic for the
evolution timeline (`_detect_patterns_lightweight` in `archilens/analyzers/evolution.py`). Add a new
pattern to the enum, then extend the heuristic if it has a recognisable naming convention.

### Diagram generators
All generators share the `DiagramGenerator` base class in `archilens/generators/base.py`.
To add a new format, subclass `DiagramGenerator`, implement the four abstract methods (`system_context`, `module_architecture`, `component_detail`, `process_flow`), and register it in `get_generator()`.

### Interactive viewer improvements
The viewer is a self-contained Flask app in `archilens/viewer/app.py`.
The entire frontend is a single embedded HTML template (`_HTML_TEMPLATE`).

---

## Architecture rules for this repository

ArchiLens checks itself. `.archilens.yml` at the repository root declares ArchiLens's capabilities and
rules (for example, static analysis must not depend on the AI module, and the data model is a leaf).
Every pull request runs the composite action on the code under review
(`.github/workflows/archilens.yml`) and posts an architecture-diff comment; a new violation of a
`fail` rule fails the check. Run the same check locally before pushing:

```bash
archilens check --repo .
```

The example config that `archilens init` writes lives in `archilens/templates/archilens.example.yml`.

---

## Code Style

```bash
# Lint (must pass)
ruff check archilens/

# Format check (must pass)
ruff format --check archilens/

# Auto-fix formatting
ruff format archilens/

# Type checking (must pass — strict mode, configured in pyproject.toml)
mypy archilens/
```

- Line length: 120 (configured in `pyproject.toml`)
- Target: Python 3.10+
- No emojis in terminal output (Windows cp1252 compatibility)
- New public functions need a one-line docstring
- Comments only where the *why* is non-obvious

---

## Reporting Issues

Please open an issue at https://github.com/saurabh-oss/archilens/issues with:
- Python version and OS
- Full error traceback
- Minimal reproduction steps (ideally a small sample repo)
