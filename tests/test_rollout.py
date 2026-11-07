from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

from inferplat.rollout import (
    ConfigMapStore,
    Controller,
    FileStore,
    GatewayScrapeSource,
    KubectlKube,
    Policy,
    PrometheusSource,
    Window,
    _quantile,
    analyse,
    parse_gateway_metrics,
)

P = Policy(steps=(10, 50, 100), step_seconds=1, min_requests=100, max_extensions=2)
GOOD = Window(1000, 1, 0.010)


@pytest.mark.parametrize(
    ("canary", "outcome", "fragment"),
    [
        (Window(500, 0, 0.011), "pass", "within policy"),
        (Window(50, 0, 0.01), "inconclusive", "50 canary requests"),
        (Window(500, 10, 0.01), "fail", "above the 1.00% ceiling"),
        (Window(500, 4, 0.01), "fail", "against stable"),  # 0.8% vs 0.1% stable
        (Window(500, 0, 0.45), "fail", "ceiling"),
        (Window(500, 0, 0.060), "fail", "p95 60 ms against stable 10 ms"),
    ],
)
def test_analysis(canary, outcome, fragment):
    v = analyse(GOOD, canary, P)
    assert v.outcome == outcome and any(fragment in r for r in v.reasons)


def test_relative_checks_need_a_stable_sample():
    assert analyse(Window(10, 0, 0.001), Window(500, 4, 0.05), P).outcome == "pass"


class FakeStore:
    def __init__(self) -> None:
        self.weights: list[float] = []
        self.promoted = False

    def set_weight(self, weight: float) -> None:
        self.weights.append(weight)

    def promote(self) -> None:
        self.promoted = True


class FakeSource:
    """Canary behaviour per weight; stable is healthy unless the weight is 100."""

    def __init__(self, store: FakeStore, canary_by_weight: dict[float, list[Window]], applied: bool = True) -> None:
        self.store, self.canary, self.applied = store, {k: list(v) for k, v in canary_by_weight.items()}, applied

    def begin(self) -> None:
        pass

    def weights(self) -> list[float]:
        return [self.store.weights[-1]] if self.applied else [-1.0]

    def windows(self) -> dict[str, Window]:
        w = self.store.weights[-1]
        stable = Window(0, 0, math.nan) if w == 100 else GOOD
        queue = self.canary[w]
        return {"stable": stable, "canary": queue.pop(0) if len(queue) > 1 else queue[0]}


def _ctl(store, source):
    return Controller(store, source, P, sleep=lambda s: None, log=lambda m: None, apply_timeout=3)


def test_healthy_canary_is_promoted():
    store = FakeStore()
    ok = [Window(500, 0, 0.011)]
    res = _ctl(store, FakeSource(store, {10: ok, 50: ok, 100: ok})).run()
    assert res.outcome == "promoted" and store.promoted and store.weights == [10, 50, 100]
    assert res.steps[-1].stable == GOOD  # at 100% the last stable window is the baseline


def test_failing_canary_is_rolled_back_at_the_first_step():
    store = FakeStore()
    res = _ctl(store, FakeSource(store, {10: [Window(500, 25, 0.01)]})).run()
    assert res.outcome == "rolled_back" and store.weights == [10, 0] and not store.promoted
    assert "ceiling" in res.reason


def test_a_quiet_step_is_extended_then_judged():
    store = FakeStore()
    ok = [Window(500, 0, 0.011)]
    res = _ctl(store, FakeSource(store, {10: [Window(40, 0, 0.01), Window(140, 0, 0.01)], 50: ok, 100: ok})).run()
    assert res.outcome == "promoted" and res.steps[0].extensions == 1


def test_never_enough_traffic_rolls_back():
    store = FakeStore()
    res = _ctl(store, FakeSource(store, {10: [Window(10, 0, 0.01)]})).run()
    assert res.outcome == "rolled_back" and "not enough canary traffic" in res.reason


def test_unapplied_weight_aborts():
    store = FakeStore()
    res = _ctl(store, FakeSource(store, {}, applied=False)).run()
    assert res.outcome == "aborted" and store.weights == [10, 0]


def test_quantile_matches_prometheus_interpolation():
    buckets = [(0.1, 50.0), (0.2, 90.0), (0.5, 100.0), (math.inf, 100.0)]
    assert _quantile(buckets, 0.5) == pytest.approx(0.1)
    assert _quantile(buckets, 0.95) == pytest.approx(0.35)
    assert math.isnan(_quantile([(0.1, 0.0)], 0.95))


def _metrics(stable_ok: int, canary_ok: int, canary_err: int, weight: float) -> str:
    return f"""
# TYPE gateway_upstream_requests_total counter
gateway_upstream_requests_total{{backend="stable",code="200"}} {stable_ok}
gateway_upstream_requests_total{{backend="canary",code="200"}} {canary_ok}
gateway_upstream_requests_total{{backend="canary",code="500"}} {canary_err}
# TYPE gateway_upstream_duration_seconds histogram
gateway_upstream_duration_seconds_bucket{{backend="canary",le="0.01"}} {canary_ok}
gateway_upstream_duration_seconds_bucket{{backend="canary",le="0.1"}} {canary_ok + canary_err}
gateway_upstream_duration_seconds_bucket{{backend="canary",le="+Inf"}} {canary_ok + canary_err}
gateway_upstream_duration_seconds_count{{backend="canary"}} {canary_ok + canary_err}
gateway_upstream_duration_seconds_sum{{backend="canary"}} 1
# TYPE gateway_canary_weight gauge
gateway_canary_weight {weight}
"""


def test_gateway_scrape_source_uses_deltas_across_gateways():
    pages = {
        "a": [_metrics(100, 10, 0, 10), _metrics(100, 10, 0, 10), _metrics(500, 110, 5, 10)],
        "b": [_metrics(50, 5, 0, 10), _metrics(50, 5, 0, 10), _metrics(150, 105, 0, 10)],
    }
    src = GatewayScrapeSource(["a", "b"], fetch=lambda u: pages[u].pop(0))
    src.begin()
    assert src.weights() == [10, 10]
    w = src.windows()
    assert w["stable"].requests == 500 and w["canary"].requests == 205 and w["canary"].errors == 5
    assert w["canary"].p95 == pytest.approx(0.01 * 194.75 / 200)  # 95% of 205 falls in the first bucket
    assert parse_gateway_metrics(_metrics(1, 2, 3, 25)).weight == 25


def test_prometheus_source_queries():
    seen: list[str] = []

    def query(q: str) -> float:
        seen.append(q)
        return 7.0 if "canary_weight" in q else (2.0 if 'code=~"5' in q else (0.05 if "histogram_quantile" in q else 400.0))

    src = PrometheusSource("http://prom", query=query)
    src.begin()
    assert src.weights() == [7.0, 7.0]
    w = src.windows()
    assert w["canary"] == Window(400.0, 2.0, 0.05)
    assert any('backend="canary"' in q and "increase(" in q for q in seen)


def test_file_store(tmp_path: Path):
    p = tmp_path / "routing.yaml"
    p.write_text(yaml.safe_dump({"stable": {"url": "http://v1"}, "canary": {"url": "http://v2"}, "canary_weight": 0}))
    store = FileStore(p)
    store.set_weight(25)
    assert yaml.safe_load(p.read_text())["canary_weight"] == 25
    store.promote()
    doc = yaml.safe_load(p.read_text())
    assert doc["stable"]["url"] == "http://v2" and doc["canary_weight"] == 0


class FakeKube:
    def __init__(self) -> None:
        self.cm = {
            "data": {
                "routing.yaml": "stable: {url: 'http://model-stable:8080'}\ncanary: {url: 'http://model-canary:8080'}\ncanary_weight: 0\n"
            }
        }
        env = [{"name": "MODEL_VERSION", "value": "v2"}, {"name": "MAX_INFLIGHT", "value": "64"}]
        self.deps = {
            "model-canary": {
                "metadata": {"generation": 1},
                "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "server", "env": env}]}}},
                "status": {},
            },
            "model-stable": {
                "metadata": {"generation": 1},
                "spec": {"replicas": 2, "template": {"spec": {"containers": [{"name": "server", "env": []}]}}},
                "status": {},
            },
        }
        self.patches: list[tuple[str, str, dict]] = []
        self.polls = 0

    def get(self, kind: str, name: str) -> dict:
        if kind == "configmap":
            return self.cm
        dep = self.deps[name]
        if name == "model-stable":
            self.polls += 1
            ready = self.polls >= 3  # becomes available on the third poll
            dep["status"] = {
                "observedGeneration": 2,
                "replicas": 2,
                "updatedReplicas": 2 if ready else 1,
                "availableReplicas": 2 if ready else 1,
            }
        return dep

    def merge_patch(self, kind: str, name: str, patch: dict) -> None:
        self.patches.append((kind, name, patch))
        if kind == "configmap":
            self.cm["data"].update(patch["data"])
        else:
            self.deps[name]["metadata"]["generation"] = 2


def test_configmap_store_moves_weight_and_promotes():
    kube = FakeKube()
    store = ConfigMapStore(kube, sleep=lambda s: None)
    store.set_weight(50)
    assert yaml.safe_load(kube.cm["data"]["routing.yaml"])["canary_weight"] == 50
    store.promote()
    kinds = [(k, n) for k, n, _ in kube.patches]
    assert kinds == [("configmap", "gateway-routing"), ("deployment", "model-stable"), ("configmap", "gateway-routing")]
    env = kube.patches[1][2]["spec"]["template"]["spec"]["containers"][0]
    assert env == {"name": "server", "env": [{"name": "MODEL_VERSION", "value": "v2"}]}
    assert kube.polls >= 3  # waited for the stable rollout before moving traffic back
    assert yaml.safe_load(kube.cm["data"]["routing.yaml"])["canary_weight"] == 0


def test_configmap_store_times_out_on_a_stuck_rollout():
    kube = FakeKube()
    kube.polls = -10_000
    with pytest.raises(TimeoutError):
        ConfigMapStore(kube, sleep=lambda s: None, rollout_timeout=10).promote()


def test_kubectl_kube_commands():
    calls: list[list[str]] = []

    def run(args: list[str]) -> str:
        calls.append(args)
        return json.dumps({"data": {}})

    k = KubectlKube("inference", run=run)
    k.get("configmap", "gateway-routing")
    k.merge_patch("deployment", "model-stable", {"spec": {}})
    assert calls[0] == ["get", "configmap", "gateway-routing", "-o", "json"]
    assert calls[1][:5] == ["patch", "deployment", "model-stable", "--type", "strategic"]
