"""Load generation: an open-loop rate test and a closed-loop concurrency sweep.

Open loop sends requests at a fixed rate whatever the latency, as real callers do, so overload
shows up as errors and latency rather than as a politely slower client. The sweep holds a fixed
number of requests in flight to find how much one model server can take.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np

from inferplat import model


def instances(model_dir: Path, name: str = "tumor-classifier", version: str = "v2", n: int = 500, seed: int = 1) -> list[dict[str, float]]:
    """Plausible inputs: each feature drawn around the training mean."""
    m = model.load(model_dir / name / version)
    rng = np.random.default_rng(seed)
    x = np.abs(m.mean + rng.normal(size=(n, len(m.features))) * m.scale)
    return [{f: round(float(v), 5) for f, v in zip(m.features, row, strict=True)} for row in x]


@dataclass
class LoadResult:
    sent: int = 0
    seconds: float = 0.0
    latencies: list[float] = field(default_factory=list)
    codes: Counter[str] = field(default_factory=Counter)
    versions: Counter[str] = field(default_factory=Counter)
    served_by: Counter[str] = field(default_factory=Counter)

    @property
    def errors(self) -> int:
        return sum(n for c, n in self.codes.items() if not c.startswith("2"))

    @property
    def error_rate(self) -> float:
        return self.errors / self.sent if self.sent else 0.0

    def quantile(self, q: float) -> float:
        return float(np.quantile(self.latencies, q)) if self.latencies else math.nan

    def summary(self) -> dict[str, object]:
        return {
            "requests": self.sent,
            "rps": round(self.sent / self.seconds, 1) if self.seconds else 0.0,
            "error_rate": round(self.error_rate, 5),
            "p50_ms": round(self.quantile(0.5) * 1000, 1),
            "p95_ms": round(self.quantile(0.95) * 1000, 1),
            "p99_ms": round(self.quantile(0.99) * 1000, 1),
            "codes": dict(self.codes),
            "versions": dict(self.versions),
            "served_by": dict(self.served_by),
        }


async def _one(client: httpx.AsyncClient, url: str, payload: Mapping[str, object], res: LoadResult, key: str | None) -> None:
    start = time.perf_counter()
    headers = {"x-routing-key": key} if key else {}
    try:
        r = await client.post(url + "/v1/predict", json=payload, headers=headers)
        res.codes[str(r.status_code)] += 1
        res.versions[r.headers.get("x-model-version", "-")] += 1
        res.served_by[r.headers.get("x-served-by", "-")] += 1
    except httpx.HTTPError as e:
        res.codes[type(e).__name__] += 1
    res.latencies.append(time.perf_counter() - start)


async def open_loop(
    url: str, rate: float, seconds: float, data: list[dict[str, float]], batch: int = 1, timeout: float = 5.0, seed: int = 3
) -> LoadResult:
    rng = random.Random(seed)
    res = LoadResult()
    url = url.rstrip("/")
    tasks: list[asyncio.Task[None]] = []
    async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=500)) as client:
        start = time.perf_counter()
        n = int(rate * seconds)
        for i in range(n):
            due = start + i / rate
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            payload = {"instances": rng.sample(data, batch)}
            tasks.append(asyncio.create_task(_one(client, url, payload, res, f"user-{rng.randrange(100_000)}")))
            res.sent += 1
        await asyncio.gather(*tasks)
        res.seconds = time.perf_counter() - start
    return res


async def closed_loop(url: str, concurrency: int, seconds: float, data: list[dict[str, float]], timeout: float = 5.0) -> LoadResult:
    res = LoadResult()
    url = url.rstrip("/")
    deadline = time.perf_counter() + seconds
    rng = random.Random(concurrency)

    async def worker(client: httpx.AsyncClient) -> None:
        while time.perf_counter() < deadline:
            res.sent += 1
            await _one(client, url, {"instances": [rng.choice(data)]}, res, None)

    async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=concurrency + 10)) as client:
        start = time.perf_counter()
        await asyncio.gather(*(worker(client) for _ in range(concurrency)))
        res.seconds = time.perf_counter() - start
    return res
