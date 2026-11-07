"""Canary drills: the whole release path on one machine, under load.

Each scenario starts a stable server (v1), a canary server (v2, healthy or with an injected
fault) and a gateway, sends open-loop traffic through the gateway, and runs the rollout
controller against the gateway's metrics. A healthy canary must be promoted; a canary that fails
more requests or answers more slowly must be rolled back before it reaches all traffic.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml

from inferplat import loadtest
from inferplat.rollout import Controller, FileStore, GatewayScrapeSource, Policy, Result


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    fault_error_rate: float = 0.0
    fault_latency_ms: float = 0.0
    expect: str = "promoted"


SCENARIOS = [
    Scenario("healthy", "v2 behaves", expect="promoted"),
    Scenario("errors", "v2 fails 3% of requests", fault_error_rate=0.03, expect="rolled_back"),
    Scenario("slow", "v2 adds about 150 ms", fault_latency_ms=150, expect="rolled_back"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if httpx.get(url + "/readyz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"{url} did not become ready")
        time.sleep(0.05)


@contextmanager
def process(args: list[str], env: dict[str, str] | None = None) -> Iterator[tuple[str, subprocess.Popen[bytes]]]:
    """Run `ifp <args>` in its own process on a free port, as a deployment would."""
    port = free_port()
    full_env = os.environ | {"PORT": str(port), "DRAIN_SECONDS": "1"} | (env or {})
    proc = subprocess.Popen(
        [sys.executable, "-m", "inferplat.cli", *args, "--port", str(port)], env=full_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    url = f"http://127.0.0.1:{port}"
    try:
        try:
            _wait_ready(url)
        except TimeoutError:
            proc.kill()
            raise RuntimeError((proc.stderr.read() if proc.stderr else b"").decode()[-2000:]) from None
        yield url, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@contextmanager
def serve(app: Any, port: int) -> Iterator[str]:
    """Run an app in a thread of this process (for tests that need to reach into it)."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, timeout_graceful_shutdown=2)
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}"
    _wait_ready(url, 20)
    try:
        yield url
    finally:
        server.should_exit = True
        t.join(timeout=10)


def run_scenario(
    sc: Scenario, model_dir: Path, workdir: Path, policy: Policy, rate: float = 150.0, log: Any = print
) -> tuple[Result, dict[str, object]]:
    data = loadtest.instances(model_dir)
    routing = workdir / f"routing-{sc.name}.yaml"
    base = {"MODEL_DIR": str(model_dir)}
    canary_env = base | {"MODEL_VERSION": "v2", "FAULT_ERROR_RATE": str(sc.fault_error_rate), "FAULT_LATENCY_MS": str(sc.fault_latency_ms)}
    with process(["serve"], base | {"MODEL_VERSION": "v1"}) as (s_url, _), process(["serve"], canary_env) as (c_url, _):
        routing.write_text(yaml.safe_dump({"stable": {"url": s_url}, "canary": {"url": c_url}, "canary_weight": 0}))
        with process(["gateway", "--routing-file", str(routing)]) as (g_url, _):
            duration = len(policy.steps) * policy.step_seconds * (1 + policy.max_extensions) + 30
            load: dict[str, loadtest.LoadResult] = {}
            stop = threading.Event()

            def drive() -> None:
                load["r"] = asyncio.run(_until(stop, g_url, rate, duration, data))

            t = threading.Thread(target=drive, daemon=True)
            t.start()
            time.sleep(1.0)
            ctl = Controller(FileStore(routing), GatewayScrapeSource([g_url]), policy, log=log, apply_timeout=10)
            result = ctl.run()
            stop.set()
            t.join(timeout=duration + 10)
            final = yaml.safe_load(routing.read_text())
    summary = load["r"].summary() if "r" in load else {}
    summary["final_routing"] = {"stable": "v2" if final["stable"]["url"] == c_url else "v1", "canary_weight": final["canary_weight"]}
    return result, summary


async def _until(stop: threading.Event, url: str, rate: float, max_seconds: float, data: list[dict[str, float]]) -> loadtest.LoadResult:
    """Open-loop load that ends when the controller is done."""
    import random

    rng = random.Random(5)
    res = loadtest.LoadResult()
    tasks = []
    async with httpx.AsyncClient(timeout=5, limits=httpx.Limits(max_connections=300)) as client:
        start = time.perf_counter()
        i = 0
        while not stop.is_set() and time.perf_counter() - start < max_seconds:
            delay = start + i / rate - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(
                asyncio.create_task(loadtest._one(client, url, {"instances": [rng.choice(data)]}, res, f"user-{rng.randrange(100_000)}"))
            )
            res.sent += 1
            i += 1
        await asyncio.gather(*tasks)
        res.seconds = time.perf_counter() - start
    return res


def report(rows: list[tuple[Scenario, Result, dict[str, Any]]], policy: Policy, rate: float) -> str:
    lines = [
        "# Canary drills",
        "",
        f"Generated by `ifp drill`. Stable v1 and canary v2 model servers behind the gateway on one machine, {rate:.0f} requests "
        f"per second of open-loop traffic, the rollout controller reading the gateway's metrics. Steps {', '.join(f'{s}%' for s in policy.steps)}, "
        f"{policy.step_seconds:g} s each, at least {policy.min_requests} canary requests per decision; the canary fails a step above "
        f"{policy.max_error_rate:.0%} errors, {policy.max_error_ratio:g}x the stable error rate, a {policy.max_p95_seconds * 1000:.0f} ms p95, "
        f"or {policy.max_p95_ratio:g}x the stable p95 + {policy.p95_slack_seconds * 1000:.0f} ms. At 100% the stable figures are the previous step's.",
        "",
        "| Scenario | Canary | Expected | Outcome | Reached | Requests | Errors seen by callers | p95 | Final routing |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for sc, res, load in rows:
        reached = f"{res.steps[-1].weight:g}%" if res.steps else "-"
        fr = load.get("final_routing", {})
        lines.append(
            f"| {sc.name} | {sc.description} | {sc.expect.replace('_', ' ')} | **{res.outcome.replace('_', ' ')}** | {reached} | "
            f"{load.get('requests', 0):,} | {float(load.get('error_rate', 0)):.2%} | {load.get('p95_ms', '-')} ms | "
            f"stable {fr.get('stable')}, canary {fr.get('canary_weight')}% |"
        )
    for sc, res, _ in rows:
        lines += [
            "",
            f"## {sc.name}",
            "",
            "| Step | Canary requests | Canary errors | Canary p95 | Stable errors | Stable p95 | Verdict |",
            "|---|---|---|---|---|---|---|",
        ]
        for st in res.steps:
            lines.append(
                f"| {st.weight:g}% | {st.canary.requests:.0f} | {st.canary.error_rate:.2%} | {st.canary.p95 * 1000:.0f} ms | "
                f"{st.stable.error_rate:.2%} | {st.stable.p95 * 1000:.0f} ms | {st.verdict.outcome}: {'; '.join(st.verdict.reasons)} |"
            )
        lines += ["", f"Outcome: {res.outcome.replace('_', ' ')} ({res.reason})."]
    return "\n".join(lines) + "\n"
