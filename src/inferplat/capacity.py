"""Capacity planning: how many requests one model server can hold in flight, and the replicas
the autoscaler should arrive at.

The autoscaler scales on in-flight requests per pod (Little's law: in-flight = arrival rate x
latency), which tracks load directly and needs no tuning per model the way CPU does. The target
comes from a concurrency sweep: the highest concurrency that still adds throughput and whose p95
stays within a share of the latency objective, leaving the rest for the gateway and the network.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    concurrency: int
    rps: float
    p95: float  # seconds
    error_rate: float


def recommend_target(
    levels: list[Level], latency_objective: float = 0.3, budget_share: float = 0.5, throughput_share: float = 0.9
) -> Level:
    """The highest concurrency that keeps p95 within `budget_share` of the objective without errors
    and still delivers `throughput_share` of the best throughput: past it, requests only queue."""
    ok = [lv for lv in levels if lv.p95 <= latency_objective * budget_share and lv.error_rate == 0]
    if not ok:
        raise ValueError("no measured concurrency meets the latency budget")
    peak = max(lv.rps for lv in ok)
    return max((lv for lv in ok if lv.rps >= throughput_share * peak), key=lambda lv: lv.concurrency)


def hpa_desired(
    current_replicas: int, current_value: float, target_value: float, min_replicas: int, max_replicas: int, tolerance: float = 0.1
) -> int:
    """The Kubernetes HPA's rule for one Pods metric: ceil(current x value / target), with a tolerance band."""
    if current_replicas == 0:
        return min_replicas
    ratio = current_value / target_value
    if abs(ratio - 1.0) <= tolerance:
        return current_replicas
    return max(min_replicas, min(max_replicas, math.ceil(current_replicas * ratio)))


def replicas_for(rps: float, per_pod_rps: float, headroom: float = 0.3, min_replicas: int = 2) -> int:
    """Replicas to carry `rps` with `headroom` spare, never fewer than `min_replicas` (one may be draining)."""
    return max(min_replicas, math.ceil(rps / (per_pod_rps * (1 - headroom))))
