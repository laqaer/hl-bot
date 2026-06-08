.PHONY: test lint

# Run the suite inside the project's uv-managed venv (py3.11 + all deps).
# Bare `python3 -m pytest` uses system Python, which lacks the project
# dependency tree (eth_account, hyperliquid, pandas, ...), so always go via uv.
test:
	uv run pytest -q

lint:
	uv run ruff check src tests scripts
