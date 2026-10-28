# hl-bot

Hyperliquid trading bot. H2 2026 plan and Ralph backlog live in docs. Successor to the older `~/dev/Hyperliquid` bot.
GitHub: laqaer/hl-bot. Commit straight to main.
Never put real API keys/private keys in committed files — use `.env` (gitignored).

E2E: no dedicated black-box suite exists. Run automated checks with `uv run pytest -q`.
Safe CLI smoke checks: `uv run hlbot --help` and `uv run hlbot run --help`.
