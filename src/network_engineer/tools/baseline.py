"""Baseline computation — turn raw monitor metrics into anomaly-relative signals.

Replaces "8 packet drops" with "8 drops vs. 1.2 over the 7-day baseline (anomalous)".
This is the Waiter-paper Level-3 temporal-signal architecture: by injecting
trajectory context, the monitor's grey region narrows substantially — borderline
metric values that would be ambiguous in isolation become obvious anomalies (or
obvious normal) when compared to history.

Source data: agent_actions.log (which monitor sweeps write to). The log is
JSON-line per row; we parse it once on demand and aggregate.

Usage:
    bl = Baseline.load_from_log(window_days=7)
    stats = bl.metric_stats(device="FlexHD", band="5GHz", metric="tx_retry_rate")
    # → {"mean": 0.03, "p95": 0.07, "samples": 200}

Phase 11: this becomes a Supabase materialized view (cheaper, queryable,
shared across operators). For now, on-demand parsing is fast enough for home
networks (< 1 MB log file even after months of operation).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_LOG = _REPO_ROOT / "logs" / "agent_actions.log"


class Baseline:
    """In-memory rolling stats over agent_actions.log entries."""

    def __init__(self) -> None:
        # samples[(device, band, metric)] = list[float]
        self.samples: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        # global event counters, e.g. ("WAN_DROPS",) → count over window
        self.event_counts: dict[str, int] = defaultdict(int)
        self.window_start: datetime | None = None
        self.window_end: datetime | None = None

    @classmethod
    def load_from_log(
        cls,
        *,
        log_path: Path | None = None,
        window_days: int = 7,
    ) -> Baseline:
        """Walk agent_actions.log and roll up samples for the last *window_days*."""
        baseline = cls()
        path = log_path or _DEFAULT_LOG
        if not path.exists():
            return baseline

        cutoff = datetime.now(UTC) - timedelta(days=window_days)
        baseline.window_start = cutoff
        baseline.window_end = datetime.now(UTC)

        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_str = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if ts < cutoff:
                continue

            # Two interesting log shapes:
            #   1. Monitor events with metrics: extra={"metrics": {...}, "device": ..., "band": ...}
            #   2. Action emissions where action == event_type (e.g. "WAN_DROPS")
            metrics = entry.get("metrics") or {}
            device = entry.get("device") or metrics.get("device") or ""
            band = entry.get("band") or metrics.get("band") or ""

            for k, v in metrics.items():
                if isinstance(v, (int, float)) and k not in ("threshold_ms", "threshold"):
                    baseline.samples[(device, band, k)].append(float(v))

            # Event counter — useful for "how often does this event fire"
            action = entry.get("action") or ""
            if action and action.isupper():  # event-style action (WAN_DROPS, etc.)
                baseline.event_counts[action] += 1

        return baseline

    def metric_stats(
        self,
        *,
        device: str = "",
        band: str = "",
        metric: str = "",
    ) -> dict[str, Any]:
        """Return summary stats for one (device, band, metric) tuple."""
        samples = self.samples.get((device, band, metric), [])
        if not samples:
            return {"mean": None, "p95": None, "samples": 0}
        s = sorted(samples)
        return {
            "mean": round(mean(s), 4),
            "p95": s[int(len(s) * 0.95)] if len(s) >= 20 else s[-1],
            "max": s[-1],
            "min": s[0],
            "samples": len(s),
        }

    def event_count(self, event_type: str) -> int:
        return self.event_counts.get(event_type, 0)

    def is_anomalous(
        self,
        current_value: float,
        *,
        device: str = "",
        band: str = "",
        metric: str = "",
        n_sigmas: float = 2.0,
    ) -> tuple[bool, str]:
        """Return (anomalous?, narrative) given the baseline.

        n_sigmas: how many standard deviations above the mean counts as anomalous.
        Returns (False, "no baseline") when there aren't enough samples yet.
        """
        samples = self.samples.get((device, band, metric), [])
        if len(samples) < 5:
            return False, "no baseline (insufficient samples)"
        avg = mean(samples)
        # Use simple range-based "spread" for small N rather than full stdev
        spread = max(samples) - min(samples)
        threshold = avg + (spread * 0.5 * n_sigmas)
        if current_value > threshold:
            return True, (
                f"{metric}={current_value} vs. {len(samples)}-sample baseline "
                f"mean={avg:.3f} (anomalous, threshold≈{threshold:.3f})"
            )
        return False, (
            f"{metric}={current_value} within {len(samples)}-sample baseline "
            f"mean={avg:.3f}"
        )

    def summary(self) -> str:
        if not self.samples:
            return "no baseline data yet (no monitor history in agent_actions.log)"
        n_metrics = len(self.samples)
        n_samples = sum(len(v) for v in self.samples.values())
        window = "?"
        if self.window_start:
            days = (datetime.now(UTC) - self.window_start).days
            window = f"{days}d"
        return (
            f"{n_samples} samples across {n_metrics} (device, band, metric) "
            f"tuples, window={window}"
        )
