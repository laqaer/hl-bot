"""`hlbot confirm --windows N` window-spec construction.

The durability bar (Iteration 21) is only one command away when the CLI builds
the right disjoint, back-to-back windows. ``_window_specs`` is the pure core of
that wiring: window 0 trails to *now* (``end_ms=None``); window i ends ``i*days``
days earlier so the windows abut without overlapping (the out-of-time test).
"""

from __future__ import annotations

from hl_bot.cli.main import _window_specs

DAY_MS = 86_400_000


def test_single_window_trails_to_now():
    specs = _window_specs(1, 120, now_ms=1_000 * DAY_MS)
    assert specs == [("trailing 120d", None)]


def test_windows_are_disjoint_and_back_to_back():
    now = 1_000 * DAY_MS
    specs = _window_specs(3, 120, now_ms=now)

    assert [label for label, _ in specs] == [
        "trailing 120d",
        "120d ending 120d ago",
        "120d ending 240d ago",
    ]
    # window 0 trails to now; the older windows end exactly one window-length apart
    assert specs[0][1] is None
    assert specs[1][1] == now - 120 * DAY_MS
    assert specs[2][1] == now - 240 * DAY_MS
    # back-to-back: each older window's end is the previous window's start
    assert specs[2][1] == specs[1][1] - 120 * DAY_MS
