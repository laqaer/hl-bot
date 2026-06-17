"""femr_tick deprecation wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hl_bot.cli.main import femr_tick


def test_femr_tick_delegates_to_run_with_max_cycles_one():
    with patch("hl_bot.cli.main.run") as mock_run, \
         patch("hl_bot.cli.main._conn") as mock_conn:
        mock_conn.return_value = (MagicMock(), MagicMock())
        femr_tick(live=True, execution="maker")
        mock_run.assert_called_once_with(live=True, execution="maker", max_cycles=1)


def test_femr_tick_rejects_bad_execution():
    import typer
    with patch("hl_bot.cli.main._conn") as mock_conn:
        mock_conn.return_value = (MagicMock(), MagicMock())
        with pytest.raises(typer.Exit):
            femr_tick(execution="bad")
