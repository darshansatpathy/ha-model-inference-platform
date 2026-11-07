"""The model server: one model version per process.

- `POST /v1/predict` scores a batch of instances. Input is validated; a batch is all-or-nothing.
- `GET /readyz` is 200 only once the artifact has been verified, loaded and warmed up, and turns
  503 as soon as shutdown starts, so the load balancer stops sending traffic before the process
  stops accepting it. `GET /healthz` is liveness only.
- Admission control: beyond `max_inflight` concurrent requests the server answers 503 with
  `Retry-After` at once, instead of queueing until every request times out.
- `GET /metrics` exposes request counts and latency by model version, in-flight requests,
  rejections and readiness, which the gateway, the canary analysis, the autoscaler and the SLO
  rules all build on.

`fault_error_rate` and `fault_latency_ms` inject failures for canary drills; both default to off.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from inferplat import model as artifacts

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5)


@dataclass
class Settings:
    model_dir: Path = Path("models")
    model_name: str = "tumor-classifier"
    model_version: str = "v1"
    max_inflight: int = 64
    max_batch: int = 256
    drain_seconds: float = 20.0
    fault_error_rate: float = 0.0
    fault_latency_ms: float = 0.0

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ
        return cls(
            model_dir=Path(e.get("MODEL_DIR", "models")),
            model_name=e.get("MODEL_NAME", "tumor-classifier"),
            model_version=e.get("MODEL_VERSION", "v1"),
            max_inflight=int(e.get("MAX_INFLIGHT", "64")),
            max_batch=int(e.get("MAX_BATCH", "256")),
            drain_seconds=float(e.get("DRAIN_SECONDS", "20")),
            fault_error_rate=float(e.get("FAULT_ERROR_RATE", "0")),
            fault_latency_ms=float(e.get("FAULT_LATENCY_MS", "0")),
        )


class PredictRequest(BaseModel):
    instances: list[dict[str, float]] = Field(min_length=1)


class Metrics:
    def __init__(self, registry: CollectorRegistry, name: str, version: str) -> None:
        labels = {"model": name, "version": version}
        self.requests = Counter("inference_requests", "Prediction requests by status code", ["model", "version", "code"], registry=registry)
        self.latency = Histogram(
            "inference_request_duration_seconds", "Prediction latency", ["model", "version"], buckets=LATENCY_BUCKETS, registry=registry
        ).labels(**labels)
        self.inflight = Gauge("inference_inflight_requests", "Requests being processed", ["model", "version"], registry=registry).labels(
            **labels
        )
        self.rejected = Counter("inference_rejected", "Requests refused before scoring", ["model", "version", "reason"], registry=registry)
        self.ready = Gauge("inference_ready", "1 when the model is loaded and serving", ["model", "version"], registry=registry).labels(
            **labels
        )
        self.info = Gauge("inference_model_info", "The loaded artifact", ["model", "version", "sha256"], registry=registry)
        self.max_inflight = Gauge(
            "inference_max_inflight", "Admission limit on concurrent requests", ["model", "version"], registry=registry
        ).labels(**labels)
        self.instances = Counter("inference_instances", "Instances scored", ["model", "version"], registry=registry).labels(**labels)
        self.labels = labels
        for code in ("200", "422", "500", "503"):
            self.requests.labels(**labels, code=code)
        for reason in ("overload", "not_ready"):
            self.rejected.labels(**labels, reason=reason)

    def count(self, code: int) -> None:
        self.requests.labels(**self.labels, code=str(code)).inc()


def create_app(settings: Settings | None = None, seed: int | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    registry = CollectorRegistry()
    m = Metrics(registry, s.model_name, s.model_version)
    m.max_inflight.set(s.max_inflight)
    state: dict[str, Any] = {"model": None, "ready": False, "inflight": 0}
    rng = random.Random(seed)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loaded = artifacts.load(s.model_dir / s.model_name / s.model_version)
        if loaded.version != s.model_version or loaded.name != s.model_name:
            raise artifacts.ArtifactError(f"artifact is {loaded.name}/{loaded.version}, expected {s.model_name}/{s.model_version}")
        loaded.predict_proba(np.asarray([loaded.mean]))  # warm-up
        state["model"] = loaded
        m.info.labels(model=s.model_name, version=s.model_version, sha256=loaded.sha256[:12]).set(1)
        state["ready"] = True
        m.ready.set(1)
        yield
        state["ready"] = False  # readiness fails first; then in-flight requests drain
        m.ready.set(0)
        deadline = time.monotonic() + s.drain_seconds
        while state["inflight"] and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    app = FastAPI(title="Model server", version=s.model_version, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = s
    app.state.state = state

    def headers() -> dict[str, str]:
        return {"x-model-version": s.model_version}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz() -> Response:
        if state["ready"]:
            return JSONResponse({"status": "ready", "version": s.model_version}, headers=headers())
        return JSONResponse({"status": "not ready"}, status_code=503, headers=headers())

    @app.get("/v1/model")
    async def model_info() -> dict[str, Any]:
        md: artifacts.Model | None = state["model"]
        if md is None:
            return {"name": s.model_name, "version": s.model_version, "loaded": False}
        return {
            "name": md.name,
            "version": md.version,
            "sha256": md.sha256,
            "features": list(md.features),
            "metrics": md.metrics,
            "loaded": True,
        }

    @app.post("/v1/predict")
    async def predict(req: Request) -> Response:
        if not state["ready"]:
            m.rejected.labels(**m.labels, reason="not_ready").inc()
            m.count(503)
            return JSONResponse({"error": "not ready"}, status_code=503, headers=headers() | {"retry-after": "1"})
        if state["inflight"] >= s.max_inflight:
            m.rejected.labels(**m.labels, reason="overload").inc()
            m.count(503)
            return JSONResponse({"error": "overloaded"}, status_code=503, headers=headers() | {"retry-after": "1"})
        state["inflight"] += 1
        m.inflight.inc()
        start = time.perf_counter()
        code = 200
        try:
            try:
                body = PredictRequest.model_validate(await req.json())
            except Exception as e:
                code = 422
                return JSONResponse({"error": "invalid request", "detail": str(e)[:500]}, status_code=422, headers=headers())
            if len(body.instances) > s.max_batch:
                code = 422
                return JSONResponse({"error": f"at most {s.max_batch} instances per request"}, status_code=422, headers=headers())
            md: artifacts.Model = state["model"]
            try:
                x = md.matrix(body.instances)
            except artifacts.ArtifactError as e:
                code = 422
                return JSONResponse({"error": str(e)}, status_code=422, headers=headers())
            if s.fault_latency_ms:
                await asyncio.sleep(s.fault_latency_ms / 1000 * (0.5 + rng.random()))
            if s.fault_error_rate and rng.random() < s.fault_error_rate:
                code = 500
                return JSONResponse({"error": "injected fault"}, status_code=500, headers=headers())
            p = md.predict_proba(x)
            m.instances.inc(len(p))
            preds = [{"probability": round(float(v), 6), "label": int(v >= md.threshold)} for v in p]
            return JSONResponse({"model": md.name, "version": md.version, "predictions": preds}, headers=headers())
        finally:
            state["inflight"] -= 1
            m.inflight.dec()
            m.latency.observe(time.perf_counter() - start)
            m.count(code)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        timeout_graceful_shutdown=int(float(os.environ.get("DRAIN_SECONDS", "20"))) + 5,
        access_log=False,
    )
