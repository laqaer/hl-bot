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
to run it until the store is ripe. When a spec does run, the full verdict is
persisted as a JSON record beside the specs (``experiment_record`` /
``write_experiment_record``) — the evidence the book waits weeks for must not
live only in terminal scrollback plus hand-transcribed prose.

Pure of network and CLI: frame loading and agent construction are injected,
so the orchestration is unit-testable offline.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .confirm import AgentFactory, ConfirmationResult, confirm_strategy
from .engine import Frame
from .store import _t, coverage_of, load_store, store_path

PERIODS_PER_YEAR = {"1m": 525_600, "5m": 105_120, "15m": 35_040,
                    "1h": 8_760, "4h": 2_190, "1d": 365}

_PREFERS = ("taker", "maker")
_MAKER_FILLS = ("optimistic", "resting", "resting-close")
_ARM_KEYS = {"name", "coins", "config", "prefer", "maker_fill", "vwap_window"}
_SPEC_KEYS = {"name", "description", "agent", "coins", "interval", "days", "source",
              "vwap_window", "min_span_days", "max_missing_pct", "min_edge_bps",
              "min_sharpe", "min_trades", "decision", "arms"}


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
    max_missing_pct: float = 1.0
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
        max_missing_pct=float(obj.get("max_missing_pct", 1.0)),
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
    missing: int = 0  # interval-aligned holes between first and last stored bar

    @property
    def missing_pct(self) -> float:
        expected = self.bars + self.missing
        return self.missing / expected * 100 if expected else 0.0


@dataclass
class RipenessReport:
    """Is the store deep enough to run this spec's pre-registered sample?"""

    spec_name: str
    min_span_days: float
    spans: list[CoinSpan]
    max_missing_pct: float = 1.0

    @property
    def min_span(self) -> float | None:
        """Worst coin's span; None if any coin has no stored bars at all."""
        if any(s.span_days is None for s in self.spans):
            return None
        return min((s.span_days for s in self.spans), default=None)

    @property
    def worst_gap(self) -> CoinSpan | None:
        """The coin with the highest missing-bar share (None if nothing stored)."""
        spans = [s for s in self.spans if s.bars]
        return max(spans, key=lambda s: s.missing_pct) if spans else None

    @property
    def gaps_ok(self) -> bool:
        return all(s.missing_pct <= self.max_missing_pct for s in self.spans)

    @property
    def ripe(self) -> bool:
        m = self.min_span
        return m is not None and m >= self.min_span_days and self.gaps_ok

    def summary(self) -> str:
        worst = self.min_span
        gap = self.worst_gap
        if self.ripe:
            gaps = (
                "no gaps" if gap is None or gap.missing == 0
                else f"worst gaps {gap.missing_pct:.1f}% <= {self.max_missing_pct:.1f}% allowed"
            )
            verdict = f"RIPE (min span {worst:.1f}d >= {self.min_span_days:.0f}d, {gaps})"
        elif worst is None or worst < self.min_span_days:
            verdict = (
                f"NOT RIPE (min span {'—' if worst is None else f'{worst:.1f}d'}"
                f" < {self.min_span_days:.0f}d needed)"
            )
        else:
            verdict = (
                f"NOT RIPE (span ok at {worst:.1f}d; {gap.coin}_{gap.interval} "
                f"{gap.missing_pct:.1f}% bars missing > "
                f"{self.max_missing_pct:.1f}% allowed)"
            )
        lines = [f"{self.spec_name}: {verdict}"]
        for s in self.spans:
            if s.span_days is None:
                detail = "no stored bars"
            elif s.missing:
                detail = (f"{s.span_days:.1f}d ({s.bars} bars, "
                          f"{s.missing} missing = {s.missing_pct:.1f}%)")
            else:
                detail = f"{s.span_days:.1f}d ({s.bars} bars)"
            lines.append(f"  {s.coin}_{s.interval}: {detail}")
        return "\n".join(lines)


def check_ripeness(spec: ExperimentSpec, *, root: str | Path | None = None) -> RipenessReport:
    """Per-coin store coverage (span + interval-aligned gaps) for the spec's
    full universe at its interval.

    Span AND contiguity both gate ripeness: a harvester outage longer than
    the API retention window leaves a permanent hole in the store, and a
    span-only check would let the spec "ripen" on a sample that no longer
    exists — the run would then replay a series whose bar-count windows
    silently straddle the hole. ``days > 0`` trims to the window the run
    would use (most recent ``days`` before the universe's last stored bar,
    matching ``frames_from_store``) so an out-of-window gap can't block a
    spec forever. An api-sourced spec is judged the same way — if the store
    can't cover the sample, the retention-capped API certainly can't.
    """
    series = {
        coin: load_store(store_path(coin, spec.interval, root)) for coin in spec.universe()
    }
    if spec.days > 0:
        last = [t for rows in series.values() for row in rows if (t := _t(row)) is not None]
        if last:
            start_ms = max(last) - int(spec.days * 86_400_000)
            series = {
                coin: [row for row in rows if (t := _t(row)) is not None and t >= start_ms]
                for coin, rows in series.items()
            }
    spans: list[CoinSpan] = []
    for coin in spec.universe():
        cov = coverage_of(coin, spec.interval, series[coin])
        spans.append(CoinSpan(coin=coin, interval=spec.interval,
                              span_days=cov.span_days, bars=cov.bars, missing=cov.missing))
    return RipenessReport(spec_name=spec.name, min_span_days=spec.min_span_days,
                          spans=spans, max_missing_pct=spec.max_missing_pct)


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


def experiment_record(
    spec: ExperimentSpec,
    ripeness: RipenessReport,
    results: list[ArmResult],
    *,
    ran_at: str,
    spec_sha256: str,
    forced: bool,
    code_rev: str | None = None,
) -> dict[str, Any]:
    """Self-contained, JSON-serializable record of one experiment run.

    A pre-registered verdict that exists only as stdout + a hand-transcribed
    PROGRESS line re-opens the side channel the spec froze shut: transcription
    can drop an arm, round a number, or quietly omit that the run was forced.
    The record carries the spec's identity (name + sha256 of the frozen file —
    a post-hoc spec edit changes the hash), the sample's provenance (the
    ripeness readout the run happened under, gaps included), the honesty bit
    (``forced`` peeks are recorded AS peeks, so an early look leaves a
    permanent trace), the code revision (fill-model changes flipped verdict
    signs in Iters 50/51), and every arm's resolved knobs + full confirm
    numbers. Pure: timestamp/hash/rev are injected.
    """
    return {
        "spec": {
            "name": spec.name,
            "sha256": spec_sha256,
            "agent": spec.agent,
            "interval": spec.interval,
            "source": spec.source,
            "days": spec.days,
            "thresholds": {
                "min_edge_bps": spec.min_edge_bps,
                "min_sharpe": spec.min_sharpe,
                "min_trades": spec.min_trades,
            },
            "decision": spec.decision,
        },
        "ran_at": ran_at,
        "forced": forced,
        "code_rev": code_rev,
        "ripeness": {
            "ripe": ripeness.ripe,
            "min_span_days_required": ripeness.min_span_days,
            "min_span_days": ripeness.min_span,
            "max_missing_pct": ripeness.max_missing_pct,
            "spans": [{**asdict(s), "missing_pct": s.missing_pct} for s in ripeness.spans],
        },
        "arms": [
            {
                "name": ar.arm.name,
                "coins": spec.arm_coins(ar.arm),
                "vwap_window": spec.arm_window(ar.arm),
                "prefer": ar.arm.prefer,
                "maker_fill": ar.arm.maker_fill,
                "config": ar.arm.config,
                "confirmed": ar.result.confirmed,
                "reasons": list(ar.result.reasons),
                "robust_to_2x_slippage": ar.result.robust_to_2x_slippage,
                "n_frames": ar.result.n_frames,
                "in_sample": asdict(ar.result.in_sample),
                "out_of_sample": asdict(ar.result.out_of_sample),
                "cost_ladder": [asdict(s) for s in ar.result.cost_ladder],
            }
            for ar in results
        ],
    }


def write_experiment_record(record: dict[str, Any], results_dir: str | Path) -> Path:
    """Write a verdict record under ``results_dir``, never overwriting.

    Filename is ``<spec>.<stamp>[.peek].json`` — a forced peek is visible in
    a directory listing, not just inside the file. Same-second reruns get a
    ``-2``/``-3`` suffix instead of clobbering the prior record.
    """
    d = Path(results_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(record["spec"]["name"])) or "experiment"
    stamp = re.sub(r"[^0-9TZ]", "", str(record.get("ran_at", ""))) or "unknown"
    base = f"{name}.{stamp}" + (".peek" if record.get("forced") else "")
    path = d / f"{base}.json"
    n = 2
    while path.exists():
        path = d / f"{base}-{n}.json"
        n += 1
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path
