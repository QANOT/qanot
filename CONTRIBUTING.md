# Contributing to Qanot AI

Thanks for your interest in contributing! Qanot is built in Tashkent and welcomes contributors from everywhere.

## Quick Start

```bash
git clone https://github.com/QANOT/qanot.git
cd qanot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[rag,docs]"
python -m pytest tests/ -v
```

## Development Workflow

1. Fork the repo and create a feature branch from `main`
2. Make your changes
3. Run tests: `python -m pytest tests/ -v`
4. Commit with a clear message describing the "why"
5. Open a PR against `main`

## Code Style

- Python 3.11+ with type hints on all public functions
- `from __future__ import annotations` at the top of every module
- Follow existing patterns in the codebase
- Docstrings on public classes and functions

## Testing

- All new features need tests in `tests/`
- All bug fixes need a regression test
- Tests must pass before merge: `python -m pytest tests/ -v`
- Currently 960+ tests running in under 2 seconds

## What to Contribute

- Bug fixes (always welcome)
- New LLM providers (implement `LLMProvider` ABC in `qanot/providers/`)
- New plugins (create a directory in `plugins/` with `plugin.py`)
- Documentation improvements
- Uzbek/English translation fixes

## Project Structure

```
qanot/           # Core framework
  agent.py       # Agent loop
  config.py      # Configuration
  providers/     # LLM providers (Claude, GPT, Gemini, Groq, Ollama)
  plugins/       # Plugin system
  tools/         # Built-in tools
  rag/           # RAG engine
  telegram.py    # Telegram adapter
plugins/         # Community plugins
tests/           # Test suite
docs/            # Documentation (English + Uzbek)
```

## Adding a New Provider

1. Create `qanot/providers/yourprovider.py`
2. Implement the `LLMProvider` ABC from `qanot/providers/base.py`
3. Register it in `qanot/providers/__init__.py`
4. Add tests in `tests/test_yourprovider.py`

## Adding a Plugin

1. Create `plugins/yourplugin/plugin.py`
2. Subclass `Plugin` from `qanot.plugins.base`
3. Use the `@tool` decorator for tool functions
4. Add a `plugin.json` manifest
5. See existing plugins (e.g., `plugins/absmarket/`) for examples

## Reporting Issues

- Use GitHub Issues
- Include: Python version, OS, error traceback, steps to reproduce
- For security issues, see [SECURITY.md](SECURITY.md) (or email hello@sirli.ai)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
