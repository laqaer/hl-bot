"""Offline backtest / replay harness.

The single most important missing piece for trading research: a way to evaluate a
strategy's edge *before* risking real capital. The engine drives an Agent's
``decide()`` over a sequence of historical market frames, simulates fills with an
explicit cost + funding model, writes synthetic fills into an in-memory DB, and
reuses the production ``score_agent`` so backtest and live numbers are computed by
the exact same code.

See ``engine.py`` for the simulator and ``data.py`` for loading real Hyperliquid
candle/funding history into frames.
"""

from .engine import Backtester, BacktestResult, CostModel, Frame

__all__ = ["Backtester", "BacktestResult", "CostModel", "Frame"]
