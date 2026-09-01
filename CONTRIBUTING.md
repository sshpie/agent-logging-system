# Contributing

## Ways to contribute

- Bug reports and fixes
- New adapters for host systems
- New anomaly rules
- Test coverage improvements

## Development setup

```bash
git clone https://github.com/sshpie/agent-logging-system
cd agent-logging-system
pip install -e ".[dev]"
pytest
```

## Submitting changes

1. Fork the repo
2. Create a feature branch
3. Run `pytest` — all tests must pass
4. Open a pull request with a clear description of what changed and why

## Adding an adapter

1. Subclass `BaseAdapter` in `agent_logging_system/adapters/`
2. Implement `wrap_agent()` (pass-through is fine if the host system doesn't need wrapping)
3. Add at least 5 tests covering normal path, error path, and latency kind behavior
4. Add a usage example to the Adapters section of the README

## Code style

- Stdlib only — no new external dependencies
- No docstrings longer than two lines
- Type hints on all public methods

## Reporting bugs

Open an issue at https://github.com/sshpie/agent-logging-system/issues with:
- Python version
- Minimal reproduction
- Expected vs actual behavior
