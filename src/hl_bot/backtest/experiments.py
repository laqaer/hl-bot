"""Pre-registered experiment specs — headline confirms frozen before the data exists.

The evidence-bearing reruns this book is waiting on (B-G014's multi-week G0
replica, B-EDGE2b's three-armed breakout revalidation) live in the backlog as
prose plus an ETA. Prose has two failure modes when the sample finally
ripens: the arms get picked in the moment — after a peek at early numbers,
which is exactly the forking-paths bias the confirm harness exists to kill —
and the ripeness question ("is the store span ≥14d yet?") is re-derived by
hand every iteration. A spec freezes the whole experiment as JSON in
``configs/experiments/`` — agent, universes, interval, every arm's config /
execution basis / fill model, the pass thresholds, and the decision rule —
committed BEFORE the deciding sample exists, and ``hlbot experiment`` refuses
to run it until the store is ripe.

Pure of network and CLI: frame loading and agent construction are injected,
so the orchestration is unit-testable offline.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .confirm import AgentFactory, ConfirmationResult, confirm_strategy
from .engine import Frame
from .store import coverage_of, load_store, store_path

PERIODS_PER_YEAR = {"1m": 525_600, "5m": 105_120, "15m": 35_040,
                    "1h": 8_760, "4h": 2_190, "1d": 365}

_PREFERS = ("taker", "maker")
_MAKER_FILLS = ("optimistic", "resting", "resting-close")
_ARM_KEYS = {"name", "coins", "config", "prefer", "maker_fill", "vwap_window"}
_SPEC_KEYS = {"name", "description", "agent", "coins", "interval", "days", "source",
              "vwap_window", "min_span_days", "min_edge_bps", "min_sharpe",
              "min_trades", "decision", "arms"}


@dataclass
class Arm:
    """One frozen confirm run. ``coins``/``vwap_window`` of ``None`` inherit the spec."""

    name: str
    coins: list[str] | None = None
    config: dict[str, Any] = field(default_factory=dict)
    prefer: str = "taker"
    maker_fill: str = "optimistic"
    vwap_window: int | None = None


@dataclass
class ExperimentSpec:
    name: str
    description: str
    agent: str
    coins: list[str]
    interval: str
    arms: list[Arm]
    days: float = 0.0
    source: str = "store"
    vwap_window: int = 60
    min_span_days: float = 0.0
    min_edge_bps: float = 3.0
    min_sharpe: float = 1.0
    min_trades: int = 20
    decision: str = ""

    def arm_coins(self, arm: Arm) -> list[str]:
        return arm.coins if arm.coins is not None else self.coins

    def arm_window(self, arm: Arm) -> int:
        return arm.vwap_window if arm.vwap_window is not None else self.vwap_window

    def universe(self) -> list[str]:
        """Every coin any arm touches, deduped, spec order first."""
        seen: dict[str, None] = dict.fromkeys(self.coins)
        for arm in self.arms:
            seen.update(dict.fromkeys(arm.coins or ()))
        return list(seen)


def _require_keys(obj: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(obj) - allowed
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}; allowed {sorted(allowed)}")


def _coin_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(c, str) and c.strip() for c in value
    ):
        raise ValueError(f"{where}: 'coins' must be a non-empty list of coin symbols")
    return [c.strip() for c in value]


def load_spec(path: str | Path) -> ExperimentSpec:
    """Parse + validate a spec file. Any typo is a hard error, never a silent
    fall-through to defaults — a mislabeled arm would poison the recorded
    evidence the same way a mislabeled ``--config`` sweep would."""
    p = Path(path)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{p}: not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError(f"{p}: spec must be a JSON object, got {type(obj).__name__}")
    _require_keys(obj, _SPEC_KEYS, str(p))
    for key in ("name", "description", "agent", "coins", "interval", "arms"):
        if key not in obj:
            raise ValueError(f"{p}: missing required key {key!r}")

    arms_raw = obj["arms"]
    if not isinstance(arms_raw, list) or not arms_raw:
        raise ValueError(f"{p}: 'arms' must be a non-empty list")
    arms: list[Arm] = []
    for i, a in enumerate(arms_raw):
        if not isinstance(a, dict):
            raise ValueError(f"{p}: arms[{i}] must be a JSON object")
        where = f"{p}: arms[{i}]"
        _require_keys(a, _ARM_KEYS, where)
        if not isinstance(a.get("name"), str) or not a["name"].strip():
            raise ValueError(f"{where}: 'name' is required")
        cfg = a.get("config", {})
        if not isinstance(cfg, dict):
            raise ValueError(f"{where}: 'config' must be a JSON object")
        prefer = a.get("prefer", "taker")
        if prefer not in _PREFERS:
            raise ValueError(f"{where}: prefer {prefer!r} not in {_PREFERS}")
        maker_fill = a.get("maker_fill", "optimistic")
        if maker_fill not in _MAKER_FILLS:
            raise ValueError(f"{where}: maker_fill {maker_fill!r} not in {_MAKER_FILLS}")
        arms.append(Arm(
            name=a["name"].strip(),
            coins=_coin_list(a["coins"], where) if "coins" in a else None,
            config=cfg,
            prefer=prefer,
            maker_fill=maker_fill,
            vwap_window=int(a["vwap_window"]) if "vwap_window" in a else None,
        ))
    names = [a.name for a in arms]
    if len(set(names)) != len(names):
        raise ValueError(f"{p}: duplicate arm names {sorted({n for n in names if names.count(n) > 1})}")

    source = obj.get("source", "store")
    if source not in ("store", "api"):
        raise ValueError(f"{p}: source {source!r} not in ('store', 'api')")

    return ExperimentSpec(
        name=obj["name"],
        description=obj["description"],
        agent=obj["agent"],
        coins=_coin_list(obj["coins"], str(p)),
        interval=obj["interval"],
        arms=arms,
        days=float(obj.get("days", 0.0)),
        source=source,
        vwap_window=int(obj.get("vwap_window", 60)),
        min_span_days=float(obj.get("min_span_days", 0.0)),
        min_edge_bps=float(obj.get("min_edge_bps", 3.0)),
        min_sharpe=float(obj.get("min_sharpe", 1.0)),
        min_trades=int(obj.get("min_trades", 20)),
        decision=obj.get("decision", ""),
    )


@dataclass
class CoinSpan:
    coin: str
    interval: str
    span_days: float | None  # None = no store file / empty
    bars: int = 0


@dataclass
class RipenessReport:
    """Is the store deep enough to run this spec's pre-registered sample?"""

    spec_name: str
    min_span_days: float
    spans: list[CoinSpan]

    @property
    def min_span(self) -> float | None:
        """Worst coin's span; None if any coin has no stored bars at all."""
        if any(s.span_days is None for s in self.spans):
            return None
        return min((s.span_days for s in self.spans), default=None)

    @property
    def ripe(self) -> bool:
        m = self.min_span
        return m is not None and m >= self.min_span_days

    def summary(self) -> str:
        worst = self.min_span
        verdict = (
            f"RIPE (min span {worst:.1f}d >= {self.min_span_days:.0f}d)" if self.ripe
            else f"NOT RIPE (min span {'—' if worst is None else f'{worst:.1f}d'}"
                 f" < {self.min_span_days:.0f}d needed)"
        )
        lines = [f"{self.spec_name}: {verdict}"]
        lines += [
            f"  {s.coin}_{s.interval}: "
            + ("no stored bars" if s.span_days is None else f"{s.span_days:.1f}d ({s.bars} bars)")
            for s in self.spans
        ]
        return "\n".join(lines)


def check_ripeness(spec: ExperimentSpec, *, root: str | Path | None = None) -> RipenessReport:
    """Per-coin store spans for the spec's full universe at its interval.

    Spans only (gap detail is re-reported by the store loader at run time);
    an api-sourced spec is judged the same way — if the store can't cover the
    sample, the retention-capped API certainly can't.
    """
    spans: list[CoinSpan] = []
    for coin in spec.universe():
        cov = coverage_of(coin, spec.interval, load_store(store_path(coin, spec.interval, root)))
        spans.append(CoinSpan(coin=coin, interval=spec.interval,
                              span_days=cov.span_days, bars=cov.bars))
    return RipenessReport(spec_name=spec.name, min_span_days=spec.min_span_days, spans=spans)


@dataclass
class ArmResult:
    arm: Arm
    result: ConfirmationResult


def run_experiment(
    spec: ExperimentSpec,
    *,
    factory_for: Callable[[dict[str, Any]], AgentFactory],
    load_frames: Callable[[list[str], int], list[Frame]],
    confirm_fn: Callable[..., ConfirmationResult] = confirm_strategy,
) -> list[ArmResult]:
    """Run every arm through the G0 confirm harness, in spec order.

    ``load_frames(coins, vwap_window)`` is called once per distinct
    (universe, window) pair — frames bake the VWAP window in, so arms that
    share both share one build; nothing about an arm's cost/fill model
    requires its own frames.
    """
    frames_cache: dict[tuple[tuple[str, ...], int], list[Frame]] = {}
    results: list[ArmResult] = []
    per_year = PERIODS_PER_YEAR.get(spec.interval, 8_760)
    for arm in spec.arms:
        key = (tuple(spec.arm_coins(arm)), spec.arm_window(arm))
        if key not in frames_cache:
            frames_cache[key] = load_frames(list(key[0]), key[1])
        results.append(ArmResult(arm=arm, result=confirm_fn(
            factory_for(arm.config), frames_cache[key],
            prefer=arm.prefer,
            min_edge_bps=spec.min_edge_bps,
            min_sharpe=spec.min_sharpe,
            min_trades=spec.min_trades,
            periods_per_year=per_year,
            maker_fill=arm.maker_fill,
        )))
    return results
