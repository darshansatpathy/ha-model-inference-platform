"""The inference gateway: weighted canary routing, retries and circuit breaking.

Routing comes from a small YAML document (a ConfigMap in Kubernetes) that is reloaded when it
changes, so the rollout controller moves traffic without restarting anything:

    stable: {url: http://model-stable:8080}
    canary: {url: http://model-canary:8080}
    canary_weight: 5        # percent

A request goes to the canary when a hash of its routing key falls under the weight, so the same
caller sees the same version for the whole rollout (`x-routing-key`, else `x-request-id`; a
request with neither is assigned at random). `x-canary: always|never` forces a side, for testing.

Scoring is a pure function of the input, so a request is retried once on the other backend when
the chosen one cannot be reached, times out or sheds load (503). A 500 is not retried: it counts
against the version that produced it. After `breaker_failures` consecutive failures a backend's
circuit opens for `breaker_cooldown` seconds and its traffic goes to the other backend.
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from inferplat.server import LATENCY_BUCKETS

BACKENDS = ("stable", "canary")


@dataclass
class GatewaySettings:
    routing_file: Path = Path("routing.yaml")
    timeout_seconds: float = 2.0
    breaker_failures: int = 5
    breaker_cooldown: float = 10.0
    reload_interval: float = 1.0

    @classmethod
    def from_env(cls) -> GatewaySettings:
        e = os.environ
        return cls(
            routing_file=Path(e.get("ROUTING_FILE", "routing.yaml")),
            timeout_seconds=float(e.get("UPSTREAM_TIMEOUT_SECONDS", "2")),
            breaker_failures=int(e.get("BREAKER_FAILURES", "5")),
            breaker_cooldown=float(e.get("BREAKER_COOLDOWN_SECONDS", "10")),
        )


@dataclass
class Routing:
    stable_url: str
    canary_url: str | None
    canary_weight: float

    @classmethod
    def parse(cls, doc: dict[str, Any]) -> Routing:
        weight = float(doc.get("canary_weight", 0))
        if not 0 <= weight <= 100:
            raise ValueError(f"canary_weight must be between 0 and 100, got {weight}")
        canary = (doc.get("canary") or {}).get("url")
        if weight > 0 and not canary:
            raise ValueError("canary_weight > 0 needs a canary url")
        return cls(doc["stable"]["url"].rstrip("/"), canary.rstrip("/") if canary else None, weight)

    def url(self, backend: str) -> str | None:
        return self.stable_url if backend == "stable" else self.canary_url


@dataclass
class Breaker:
    failures: int = 0
    opened_at: float | None = None
    trial: bool = False

    def available(self, now: float, cooldown: float) -> bool:
        if self.opened_at is None:
            return True
        if now - self.opened_at >= cooldown and not self.trial:
            self.trial = True  # half open: let one request through
            return True
        return False

    def success(self) -> None:
        self.failures, self.opened_at, self.trial = 0, None, False

    def failure(self, now: float, threshold: int) -> bool:
        """Record a failure; True when this opens the circuit."""
        self.failures += 1
        if self.trial or (self.opened_at is None and self.failures >= threshold):
            self.opened_at, self.trial = now, False
            return True
        return False


def bucket(key: str) -> float:
    """A stable position in [0, 100) for a routing key."""
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % 10_000 / 100


@dataclass
class _State:
    routing: Routing | None = None
    mtime: float = -1.0
    checked: float = 0.0
    error: str | None = None
    breakers: dict[str, Breaker] = field(default_factory=lambda: {b: Breaker() for b in BACKENDS})


def create_gateway(settings: GatewaySettings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    s = settings or GatewaySettings.from_env()
    registry = CollectorRegistry()
    requests = Counter(
        "gateway_requests", "Requests answered by the gateway, by the backend that served them", ["route", "code"], registry=registry
    )
    upstream = Counter("gateway_upstream_requests", "Attempts sent to each backend", ["backend", "code"], registry=registry)
    up_latency = Histogram("gateway_upstream_duration_seconds", "Backend latency", ["backend"], buckets=LATENCY_BUCKETS, registry=registry)
    latency = Histogram("gateway_request_duration_seconds", "End-to-end latency at the gateway", buckets=LATENCY_BUCKETS, registry=registry)
    retries = Counter("gateway_retries", "Requests retried on the other backend", ["from_backend", "to_backend"], registry=registry)
    weight = Gauge("gateway_canary_weight", "Percentage of traffic routed to the canary", registry=registry)
    circuit = Gauge("gateway_circuit_open", "1 while a backend's circuit is open", ["backend"], registry=registry)
    config_errors = Counter("gateway_routing_reload_errors", "Routing documents that failed to load", registry=registry)
    for b in BACKENDS:
        circuit.labels(backend=b).set(0)
        up_latency.labels(backend=b)
        for code in ("200", "422", "500", "503", "504", "error"):
            upstream.labels(backend=b, code=code)
            requests.labels(route=b, code=code)
    st = _State()
    client: dict[str, httpx.AsyncClient] = {}

    def reload(now: float) -> None:
        if now - st.checked < s.reload_interval and st.routing is not None:
            return
        st.checked = now
        missing: str | None = None
        try:
            mtime = s.routing_file.stat().st_mtime
        except OSError as e:
            mtime, missing = -2.0, str(e)
        if mtime == st.mtime:
            return
        st.mtime = mtime  # each version of the document is tried once
        try:
            if missing:
                raise FileNotFoundError(missing)
            r = Routing.parse(yaml.safe_load(s.routing_file.read_text()))
        except Exception as e:  # keep serving with the last good routing
            config_errors.inc()
            st.error = str(e)
            return
        st.routing, st.error = r, None
        weight.set(r.canary_weight)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client["c"] = httpx.AsyncClient(timeout=s.timeout_seconds, transport=transport, limits=httpx.Limits(max_connections=200))
        reload(time.monotonic())
        yield
        await client["c"].aclose()

    app = FastAPI(title="Inference gateway", lifespan=lifespan, docs_url=None, redoc_url=None)

    def choose(req: Request, r: Routing, now: float) -> list[str]:
        force = req.headers.get("x-canary", "").lower()
        if force == "always" and r.canary_url:
            first = "canary"
        elif force == "never" or not r.canary_url or r.canary_weight == 0:
            first = "stable"
        else:
            key = req.headers.get("x-routing-key") or req.headers.get("x-request-id")
            position = bucket(key) if key else random.random() * 100
            first = "canary" if position < r.canary_weight else "stable"
        order = [first, "canary" if first == "stable" else "stable"]
        order = [b for b in order if r.url(b)]
        available = [b for b in order if st.breakers[b].available(now, s.breaker_cooldown)]
        return available or order[:1]

    async def attempt(backend: str, url: str, body: bytes, headers: dict[str, str]) -> tuple[str, httpx.Response | None]:
        start = time.perf_counter()
        try:
            resp = await client["c"].post(url + "/v1/predict", content=body, headers=headers)
            code = str(resp.status_code)
        except httpx.TimeoutException:
            resp, code = None, "504"
        except httpx.TransportError:
            resp, code = None, "error"
        up_latency.labels(backend=backend).observe(time.perf_counter() - start)
        upstream.labels(backend=backend, code=code).inc()
        now = time.monotonic()
        if code in ("504", "error", "503") or code.startswith("5"):
            if st.breakers[backend].failure(now, s.breaker_failures):
                circuit.labels(backend=backend).set(1)
        elif st.breakers[backend].opened_at is not None or st.breakers[backend].failures:
            st.breakers[backend].success()
            circuit.labels(backend=backend).set(0)
        return code, resp

    @app.post("/v1/predict")
    async def predict(req: Request) -> Response:
        start = time.perf_counter()
        now = time.monotonic()
        reload(now)
        r = st.routing
        if r is None:
            requests.labels(route="stable", code="503").inc()
            return JSONResponse({"error": "no routing configuration", "detail": st.error}, status_code=503)
        body = await req.body()
        fwd = {"content-type": "application/json"}
        if rid := req.headers.get("x-request-id"):
            fwd["x-request-id"] = rid
        order = choose(req, r, now)
        code, resp, served = "error", None, order[0]
        for i, backend in enumerate(order):
            url = r.url(backend)
            assert url is not None
            served = backend
            code, resp = await attempt(backend, url, body, fwd)
            if code in ("504", "error", "503") and i + 1 < len(order):
                retries.labels(from_backend=backend, to_backend=order[i + 1]).inc()
                continue
            break
        latency.observe(time.perf_counter() - start)
        if resp is None:
            status = 504 if code == "504" else 502
            requests.labels(route=served, code="504" if status == 504 else "error").inc()
            return JSONResponse({"error": "backend unavailable", "backend": served}, status_code=status)
        requests.labels(route=served, code=str(resp.status_code)).inc()
        out_headers = {"x-served-by": served}
        if v := resp.headers.get("x-model-version"):
            out_headers["x-model-version"] = v
        return Response(resp.content, status_code=resp.status_code, media_type="application/json", headers=out_headers)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz() -> Response:
        reload(time.monotonic())
        if st.routing is None:
            return JSONResponse({"status": "no routing"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/v1/routing")
    async def routing() -> dict[str, Any]:
        reload(time.monotonic())
        r = st.routing
        return {
            "stable": r.stable_url if r else None,
            "canary": r.canary_url if r else None,
            "canary_weight": r.canary_weight if r else None,
            "circuit_open": {b: st.breakers[b].opened_at is not None for b in BACKENDS},
            "last_error": st.error,
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        reload(time.monotonic())  # so an idle gateway still reports the weight it would apply
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    uvicorn.run(create_gateway(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")), access_log=False)
