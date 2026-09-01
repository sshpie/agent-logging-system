# Contributing

## Issues

Open an issue for bugs, missing adapter types, or rule requests. Include:
- Python version
- Minimal reproducible example
- What you expected vs. what happened

## Pull Requests

1. Fork the repo and create a branch from `main`.
2. Write tests for any new logic. The test suite is in `tests/` and uses pytest only.
3. Keep adapters self-contained — no new external dependencies. The stdlib-only constraint is a feature.
4. Run `pytest` before opening a PR.
5. Open the PR against `main`.

## Adapter contributions

New adapters belong in `agent_logging_system/adapters/`. Follow the pattern in `aimap_adapter.py` or `cisco_mcp_adapter.py`:
- Subclass `BaseAdapter`
- Call `self.emit_observation()` — do not construct `Observation` directly
- Implement `wrap_agent()` — pass-through is fine if wrapping is not meaningful for the framework

## Code style

No external formatters are required. Follow the existing style: 4-space indent, descriptive names, no unnecessary comments.

## License

By contributing, you agree that your contributions are licensed under the MIT license.
