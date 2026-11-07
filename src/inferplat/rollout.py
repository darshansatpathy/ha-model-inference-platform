"""The canary rollout controller.

It moves traffic to the canary in steps, and at each step compares the canary with the stable
version over the step's window: error rate and p95 latency, each against an absolute ceiling and
against the stable version. A step with too few canary requests is extended rather than judged;
a step that fails rolls the canary back to 0% at once. After the last step passes, the canary is
promoted.

Measurements come from a `MetricsSource`: Prometheus in production, or the gateways' own
`/metrics` endpoints (deltas between two scrapes), which needs nothing else running. Traffic is
moved through a `WeightStore`: the routing file locally, or the ConfigMap in Kubernetes. The
controller waits until the gateways report the new weight before it starts measuring.
"""

from __future__ import annotations

import json
import math
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol

import httpx
import yaml
from prometheus_client.parser import text_string_to_metric_families


@dataclass(frozen=True)
class Window:
    requests: float
    errors: float
    p95: float  # seconds; nan when there were no requests

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


@dataclass
class Policy:
    steps: tuple[int, ...] = (5, 25, 50, 100)
    step_seconds: float = 60.0
    min_requests: int = 200
    max_error_rate: float = 0.01
    max_error_ratio: float = 2.0
    error_slack: float = 0.002
    max_p95_ratio: float = 1.25
    p95_slack_seconds: float = 0.02
    max_p95_seconds: float = 0.3
    max_extensions: int = 3


@dataclass
class Verdict:
    outcome: str  # pass | fail | inconclusive
    reasons: list[str]


def analyse(stable: Window, canary: Window, p: Policy) -> Verdict:
    if canary.requests < p.min_requests:
        return Verdict("inconclusive", [f"{canary.requests:.0f} canary requests, {p.min_requests} needed"])
    reasons = []
    if canary.error_rate > p.max_error_rate:
        reasons.append(f"canary error rate {canary.error_rate:.2%} above the {p.max_error_rate:.2%} ceiling")
    if stable.requests >= p.min_requests and canary.error_rate > stable.error_rate * p.max_error_ratio + p.error_slack:
        reasons.append(f"canary error rate {canary.error_rate:.2%} against stable {stable.error_rate:.2%}")
    if math.isfinite(canary.p95) and canary.p95 > p.max_p95_seconds:
        reasons.append(f"canary p95 {canary.p95 * 1000:.0f} ms above the {p.max_p95_seconds * 1000:.0f} ms ceiling")
    if stable.requests >= p.min_requests and math.isfinite(stable.p95) and canary.p95 > stable.p95 * p.max_p95_ratio + p.p95_slack_seconds:
        reasons.append(f"canary p95 {canary.p95 * 1000:.0f} ms against stable {stable.p95 * 1000:.0f} ms")
    return Verdict("fail" if reasons else "pass", reasons or ["within policy"])


class MetricsSource(Protocol):
    def begin(self) -> None: ...
    def windows(self) -> dict[str, Window]: ...
    def weights(self) -> list[float]: ...


def _quantile(buckets: list[tuple[float, float]], q: float) -> float:
    """Prometheus-style histogram_quantile over cumulative (upper bound, count) buckets."""
    buckets = sorted(buckets)
    total = buckets[-1][1] if buckets else 0.0
    if total <= 0:
        return math.nan
    rank = q * total
    prev_bound, prev_count = 0.0, 0.0
    for bound, count in buckets:
        if count >= rank:
            if math.isinf(bound):
                return prev_bound
            return prev_bound + (bound - prev_bound) * (rank - prev_count) / max(count - prev_count, 1e-12)
        prev_bound, prev_count = bound, count
    return prev_bound


@dataclass
class _Snapshot:
    requests: dict[tuple[str, str], float] = field(default_factory=dict)  # (backend, code) -> count
    buckets: dict[tuple[str, float], float] = field(default_factory=dict)  # (backend, le) -> count
    weight: float = math.nan


def parse_gateway_metrics(text: str) -> _Snapshot:
    snap = _Snapshot()
    for fam in text_string_to_metric_families(text):
        for sample in fam.samples:
            if sample.name == "gateway_upstream_requests_total":
                key = (sample.labels["backend"], sample.labels["code"])
                snap.requests[key] = snap.requests.get(key, 0.0) + sample.value
            elif sample.name == "gateway_upstream_duration_seconds_bucket":
                bk = (sample.labels["backend"], float(sample.labels["le"]))
                snap.buckets[bk] = snap.buckets.get(bk, 0.0) + sample.value
            elif sample.name == "gateway_canary_weight":
                snap.weight = sample.value
    return snap


class GatewayScrapeSource:
    """Measures from the gateways' own metrics: the difference between two scrapes of every gateway."""

    def __init__(self, urls: list[str], fetch: Callable[[str], str] | None = None) -> None:
        self.urls = urls
        self.fetch = fetch or (lambda u: httpx.get(u.rstrip("/") + "/metrics", timeout=5).text)
        self._start: list[_Snapshot] = []

    def _scrape(self) -> list[_Snapshot]:
        return [parse_gateway_metrics(self.fetch(u)) for u in self.urls]

    def begin(self) -> None:
        self._start = self._scrape()

    def weights(self) -> list[float]:
        return [s.weight for s in self._scrape()]

    def windows(self) -> dict[str, Window]:
        end = self._scrape()
        out = {}
        for backend in ("stable", "canary"):
            req = err = 0.0
            buckets: dict[float, float] = {}
            for a, b in zip(self._start, end, strict=True):
                for (be, code), v in b.requests.items():
                    if be != backend:
                        continue
                    d = v - a.requests.get((be, code), 0.0)
                    req += d
                    if code.startswith("5") or code == "error":
                        err += d
                for (be, le), v in b.buckets.items():
                    if be == backend:
                        buckets[le] = buckets.get(le, 0.0) + v - a.buckets.get((be, le), 0.0)
            out[backend] = Window(req, err, _quantile(list(buckets.items()), 0.95))
        return out


class PrometheusSource:
    """Measures from Prometheus over the step's window."""

    def __init__(self, url: str, selector: str = "", query: Callable[[str], float] | None = None) -> None:
        self.url = url.rstrip("/")
        self.selector = selector
        self.query = query or self._query
        self._began = 0.0

    def _query(self, promql: str) -> float:
        r = httpx.get(self.url + "/api/v1/query", params={"query": promql}, timeout=10)
        r.raise_for_status()
        result = r.json()["data"]["result"]
        return float(result[0]["value"][1]) if result else 0.0

    def begin(self) -> None:
        self._began = time.time()

    def weights(self) -> list[float]:
        return [self.query(f"min(gateway_canary_weight{{{self.selector}}})"), self.query(f"max(gateway_canary_weight{{{self.selector}}})")]

    def windows(self) -> dict[str, Window]:
        rng = f"{max(int(time.time() - self._began), 30)}s"
        out = {}
        for b in ("stable", "canary"):
            sel = f'backend="{b}"' + (f",{self.selector}" if self.selector else "")
            req = self.query(f"sum(increase(gateway_upstream_requests_total{{{sel}}}[{rng}]))")
            err = self.query(f'sum(increase(gateway_upstream_requests_total{{{sel},code=~"5..|error"}}[{rng}]))')
            p95 = self.query(f"histogram_quantile(0.95, sum by (le) (rate(gateway_upstream_duration_seconds_bucket{{{sel}}}[{rng}])))")
            out[b] = Window(req, err, p95 if req else math.nan)
        return out


class WeightStore(Protocol):
    def set_weight(self, weight: float) -> None: ...
    def promote(self) -> None: ...


class FileStore:
    """The routing file a local gateway reads."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = yaml.safe_load(self.path.read_text())
        return doc

    def _write(self, doc: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(doc, sort_keys=False))
        tmp.replace(self.path)

    def set_weight(self, weight: float) -> None:
        doc = self._doc()
        doc["canary_weight"] = weight
        self._write(doc)

    def promote(self) -> None:
        doc = self._doc()
        doc["stable"] = doc["canary"]
        doc["canary_weight"] = 0
        self._write(doc)


class Kube(Protocol):
    """The few Kubernetes calls a rollout needs."""

    def get(self, kind: str, name: str) -> dict[str, Any]: ...
    def merge_patch(self, kind: str, name: str, patch: dict[str, Any]) -> None: ...


class InClusterKube:
    """The Kubernetes API with the pod's service account (no kubectl in the image)."""

    PATHS: ClassVar[dict[str, str]] = {
        "configmap": "/api/v1/namespaces/{ns}/configmaps/{name}",
        "deployment": "/apis/apps/v1/namespaces/{ns}/deployments/{name}",
    }
    SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self, namespace: str) -> None:
        import os

        self.ns = namespace
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.client = httpx.Client(
            base_url=f"https://{host}:{port}",
            verify=str(self.SA / "ca.crt"),
            headers={"authorization": f"Bearer {(self.SA / 'token').read_text().strip()}"},
            timeout=15,
        )

    def get(self, kind: str, name: str) -> dict[str, Any]:
        r = self.client.get(self.PATHS[kind].format(ns=self.ns, name=name))
        r.raise_for_status()
        doc: dict[str, Any] = r.json()
        return doc

    def merge_patch(self, kind: str, name: str, patch: dict[str, Any]) -> None:
        ctype = "application/merge-patch+json" if kind == "configmap" else "application/strategic-merge-patch+json"
        r = self.client.patch(self.PATHS[kind].format(ns=self.ns, name=name), content=json.dumps(patch), headers={"content-type": ctype})
        r.raise_for_status()


class KubectlKube:
    """The same calls through kubectl and the caller's kubeconfig, for running outside the cluster."""

    def __init__(self, namespace: str, run: Callable[[list[str]], str] | None = None) -> None:
        self.ns = namespace
        self.run = run or (
            lambda args: subprocess.run(["kubectl", "-n", namespace, *args], check=True, capture_output=True, text=True).stdout
        )

    def get(self, kind: str, name: str) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads(self.run(["get", kind, name, "-o", "json"]))
        return doc

    def merge_patch(self, kind: str, name: str, patch: dict[str, Any]) -> None:
        self.run(["patch", kind, name, "--type", "merge" if kind == "configmap" else "strategic", "-p", json.dumps(patch)])


class ConfigMapStore:
    """The gateways' routing ConfigMap. Promotion rolls the stable Deployment to the canary's
    model version, waits for it to be fully available, then returns all traffic to stable."""

    def __init__(
        self,
        kube: Kube,
        configmap: str = "gateway-routing",
        stable_deployment: str = "model-stable",
        canary_deployment: str = "model-canary",
        sleep: Callable[[float], None] = time.sleep,
        rollout_timeout: float = 600.0,
    ) -> None:
        self.kube, self.cm, self.stable, self.canary = kube, configmap, stable_deployment, canary_deployment
        self.sleep, self.rollout_timeout = sleep, rollout_timeout

    def _routing(self) -> dict[str, Any]:
        doc: dict[str, Any] = yaml.safe_load(self.kube.get("configmap", self.cm)["data"]["routing.yaml"])
        return doc

    def set_weight(self, weight: float) -> None:
        doc = self._routing()
        doc["canary_weight"] = weight
        self.kube.merge_patch("configmap", self.cm, {"data": {"routing.yaml": yaml.safe_dump(doc, sort_keys=False)}})

    @staticmethod
    def _env(dep: dict[str, Any]) -> dict[str, str]:
        return {e["name"]: e.get("value", "") for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", [])}

    def _available(self, name: str) -> bool:
        d = self.kube.get("deployment", name)
        st, want = d.get("status", {}), d["spec"].get("replicas", 1)
        return bool(
            st.get("observedGeneration", 0) >= d["metadata"].get("generation", 0)
            and st.get("updatedReplicas", 0) == want
            and st.get("availableReplicas", 0) == want
            and st.get("replicas", 0) == want
        )

    def promote(self) -> None:
        canary = self.kube.get("deployment", self.canary)
        version = self._env(canary)["MODEL_VERSION"]
        container = canary["spec"]["template"]["spec"]["containers"][0]["name"]
        self.kube.merge_patch(
            "deployment",
            self.stable,
            {"spec": {"template": {"spec": {"containers": [{"name": container, "env": [{"name": "MODEL_VERSION", "value": version}]}]}}}},
        )
        waited = 0.0
        while not self._available(self.stable):
            if waited >= self.rollout_timeout:
                raise TimeoutError(f"deployment/{self.stable} did not become available with {version}")
            self.sleep(2.0)
            waited += 2.0
        self.set_weight(0)


@dataclass
class StepRecord:
    weight: float
    stable: Window
    canary: Window
    verdict: Verdict
    extensions: int


@dataclass
class Result:
    outcome: str  # promoted | rolled_back | aborted
    steps: list[StepRecord]
    reason: str


class Controller:
    def __init__(
        self,
        store: WeightStore,
        source: MetricsSource,
        policy: Policy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
        apply_timeout: float = 120.0,
    ) -> None:
        self.store, self.source, self.policy = store, source, policy or Policy()
        self.sleep, self.log, self.apply_timeout = sleep, log, apply_timeout

    def _wait_applied(self, weight: float) -> bool:
        waited = 0.0
        while waited <= self.apply_timeout:
            ws = self.source.weights()
            if ws and all(w == weight for w in ws):
                return True
            self.sleep(1.0)
            waited += 1.0
        return False

    def run(self) -> Result:
        p = self.policy
        steps: list[StepRecord] = []
        baseline: Window | None = None
        for weight in p.steps:
            self.store.set_weight(weight)
            if not self._wait_applied(weight):
                self.store.set_weight(0)
                return Result("aborted", steps, f"gateways did not apply weight {weight} within {self.apply_timeout:.0f} s")
            self.log(f"canary at {weight}%")
            extensions = 0
            self.source.begin()
            while True:
                self.sleep(p.step_seconds)
                w = self.source.windows()
                stable = w["stable"] if w["stable"].requests >= p.min_requests or baseline is None else baseline
                verdict = analyse(stable, w["canary"], p)
                if verdict.outcome != "inconclusive" or extensions >= p.max_extensions:
                    break
                extensions += 1
                self.log(f"  {verdict.reasons[0]}; extending the step ({extensions}/{p.max_extensions})")
            steps.append(StepRecord(weight, stable, w["canary"], verdict, extensions))
            c = w["canary"]
            self.log(
                f"  canary {c.requests:.0f} requests, errors {c.error_rate:.2%}, p95 {c.p95 * 1000:.0f} ms; "
                f"stable errors {stable.error_rate:.2%}, p95 {stable.p95 * 1000:.0f} ms: {verdict.outcome}"
            )
            if verdict.outcome != "pass":
                self.store.set_weight(0)
                reason = "; ".join(verdict.reasons) if verdict.outcome == "fail" else "not enough canary traffic to decide"
                self.log(f"rolled back: {reason}")
                return Result("rolled_back", steps, reason)
            if w["stable"].requests >= p.min_requests:
                baseline = w["stable"]
        self.store.promote()
        self.log("promoted")
        return Result("promoted", steps, "every step within policy")
