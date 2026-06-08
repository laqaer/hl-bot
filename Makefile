.PHONY: test lint check deploy

# Run the suite inside the project's uv-managed venv (py3.11 + all deps).
# Bare `python3 -m pytest` uses system Python, which lacks the project
# dependency tree (eth_account, hyperliquid, pandas, ...), so always go via uv.
test:
	uv run pytest -q

lint:
	uv run ruff check src tests scripts

# Full local gate — matches CI.
check: lint test

# One-command 24/7 deploy (see deploy/README.md). Run on the target host.
deploy:
	sudo REPO_URL="$(REPO_URL)" BRANCH="$(BRANCH)" bash deploy/install.sh
