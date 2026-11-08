from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from inferplat.gateway import Breaker, GatewaySettings, Routing, bucket, create_gateway

PAYLOAD = {"instances": [{"x": 1.0}]}


class Backends:
    """Fake model servers behind an httpx MockTransport; behaviour per host can be changed."""

    def __init__(self) -> None:
        self.mode = {"stable": "ok", "canary": "ok"}
        self.calls: Counter[str] = Counter()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls[host] += 1
        mode = self.mode[host]
        if mode == "down":
            raise httpx.ConnectError("refused", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if mode == "shed":
            return httpx.Response(503, json={"error": "overloaded"})
        if mode == "fail":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"version": host}, headers={"x-model-version": "v1" if host == "stable" else "v2"})


def _routing(path: Path, weight: float, canary: bool = True) -> None:
    doc = {"stable": {"url": "http://stable"}, "canary_weight": weight}
    if canary:
        doc["canary"] = {"url": "http://canary"}
    path.write_text(yaml.safe_dump(doc))


@pytest.fixture
def setup(tmp_path):
    routing = tmp_path / "routing.yaml"
    _routing(routing, 20)
    backends = Backends()
    app = create_gateway(
        GatewaySettings(routing_file=routing, reload_interval=0, breaker_failures=3, breaker_cooldown=0.2),
        transport=httpx.MockTransport(backends),
    )
    with TestClient(app) as client:
        yield client, backends, routing


def test_weighted_and_sticky_routing(setup):
    client, _, _ = setup
    served = Counter(
        client.post("/v1/predict", json=PAYLOAD, headers={"x-routing-key": f"user-{i}"}).headers["x-served-by"] for i in range(2000)
    )
    assert 0.16 < served["canary"] / 2000 < 0.24
    first = {client.post("/v1/predict", json=PAYLOAD, headers={"x-routing-key": "alice"}).headers["x-served-by"] for _ in range(20)}
    assert len(first) == 1
    assert client.post("/v1/predict", json=PAYLOAD, headers={"x-canary": "always"}).headers["x-served-by"] == "canary"
    assert (
        client.post("/v1/predict", json=PAYLOAD, headers={"x-canary": "never", "x-routing-key": "anyone"}).headers["x-model-version"]
        == "v1"
    )


def test_bucket_is_stable_and_uniform():
    assert bucket("alice") == bucket("alice")
    values = [bucket(f"k{i}") for i in range(5000)]
    assert min(values) >= 0 and max(values) < 100
    assert 45 < sum(v < 50 for v in values) / 50 < 55


def test_retry_on_unreachable_or_shedding_backend(setup):
    client, backends, _ = setup
    for mode in ("down", "timeout", "shed"):
        backends.mode["stable"] = mode
        r = client.post("/v1/predict", json=PAYLOAD, headers={"x-canary": "never"})
        assert r.status_code == 200 and r.headers["x-served-by"] == "canary", mode
    metrics = client.get("/metrics").text
    assert 'gateway_retries_total{from_backend="stable",to_backend="canary"} 3.0' in metrics


def test_server_errors_are_not_retried(setup):
    client, backends, _ = setup
    backends.mode["canary"] = "fail"
    r = client.post("/v1/predict", json=PAYLOAD, headers={"x-canary": "always"})
    assert r.status_code == 500 and r.headers["x-served-by"] == "canary"
    assert backends.calls["stable"] == 0


def test_both_backends_down_is_a_502(setup):
    client, backends, _ = setup
    backends.mode = {"stable": "down", "canary": "down"}
    r = client.post("/v1/predict", json=PAYLOAD)
    assert r.status_code == 502
    backends.mode = {"stable": "timeout", "canary": "timeout"}
    assert client.post("/v1/predict", json=PAYLOAD).status_code == 504


def test_circuit_opens_and_recovers(setup):
    client, backends, _ = setup
    backends.mode["stable"] = "down"
    for _ in range(3):
        client.post("/v1/predict", json=PAYLOAD, headers={"x-canary": "never"})
    assert client.get("/v1/routing").json()["circuit_open"]["stable"] is True
    calls = backends.calls["stable"]
    r = client.post("/v1/predict", json=PAYLOAD, headers={"x-canary": "never"})
    assert r.headers["x-served-by"] == "canary" and backends.calls["stable"] == calls  # skipped while open
    backends.mode["stable"] = "ok"
    time.sleep(0.25)
    r = client.post("/v1/predict", json=PAYLOAD, headers={"x-canary": "never"})  # half-open trial
    assert r.headers["x-served-by"] == "stable"
    assert client.get("/v1/routing").json()["circuit_open"]["stable"] is False


def test_routing_reloads_and_bad_documents_keep_the_last_good_one(setup):
    client, _, routing = setup
    _routing(routing, 100)
    time.sleep(0.01)
    assert client.post("/v1/predict", json=PAYLOAD, headers={"x-routing-key": "a"}).headers["x-served-by"] == "canary"
    routing.write_text("canary_weight: 250\nstable: {url: http://stable}\n")
    assert client.get("/v1/routing").json()["canary_weight"] == 100.0
    assert "between 0 and 100" in client.get("/v1/routing").json()["last_error"]
    assert "gateway_routing_reload_errors_total 1.0" in client.get("/metrics").text


def test_connection_pool_is_recycled(tmp_path):
    routing = tmp_path / "routing.yaml"
    _routing(routing, 0)
    backends = Backends()
    for max_age, expected in ((0.0, 3), (3600.0, 0)):
        app = create_gateway(GatewaySettings(routing_file=routing, connection_max_age=max_age), transport=httpx.MockTransport(backends))
        with TestClient(app) as client:
            for _ in range(3):
                assert client.post("/v1/predict", json=PAYLOAD).status_code == 200
            assert app.state.pool["generation"] == expected


def test_no_routing_is_not_ready(tmp_path):
    app = create_gateway(GatewaySettings(routing_file=tmp_path / "missing.yaml"))
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 503
        assert c.post("/v1/predict", json=PAYLOAD).status_code == 503


def test_routing_validation():
    with pytest.raises(ValueError, match="needs a canary url"):
        Routing.parse({"stable": {"url": "http://s"}, "canary_weight": 5})
    r = Routing.parse({"stable": {"url": "http://s/"}, "canary": {"url": "http://c/"}, "canary_weight": 0})
    assert r.url("stable") == "http://s" and r.url("canary") == "http://c"


def test_breaker_half_open_failure_reopens():
    b = Breaker()
    assert not b.failure(0.0, 2) and b.failure(0.1, 2)
    assert not b.available(0.2, 1.0) and b.available(1.2, 1.0)
    assert not b.available(1.3, 1.0)  # only one trial while half open
    assert b.failure(1.4, 2)  # the trial failed: open again
    assert not b.available(1.5, 1.0)


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("ROUTING_FILE", "/etc/gateway/routing.yaml")
    monkeypatch.setenv("BREAKER_FAILURES", "9")
    monkeypatch.setenv("CONNECTION_MAX_AGE_SECONDS", "15")
    s = GatewaySettings.from_env()
    assert str(s.routing_file) == "/etc/gateway/routing.yaml" and s.breaker_failures == 9 and s.connection_max_age == 15
