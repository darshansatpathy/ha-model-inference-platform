"""Real servers in their own processes: the canary drills and a failover under load."""

from __future__ import annotations

import asyncio
import threading

import pytest
import yaml

from inferplat import drill, loadtest
from inferplat.rollout import Policy

pytestmark = pytest.mark.drill
FAST = Policy(steps=(20, 100), step_seconds=2.0, min_requests=150, max_extensions=6)


@pytest.mark.parametrize("name", ["healthy", "errors", "slow"])
def test_drill(models, tmp_path, name):
    sc = next(s for s in drill.SCENARIOS if s.name == name)
    res, load = drill.run_scenario(sc, models, tmp_path, FAST, rate=120)
    assert res.outcome == sc.expect, res.reason
    if sc.expect == "promoted":
        assert load["final_routing"] == {"stable": "v2", "canary_weight": 0}
        assert load["error_rate"] == 0
    else:
        # Rolled back, normally at the first step; stable keeps serving v1.
        assert load["final_routing"] == {"stable": "v1", "canary_weight": 0}


def test_killing_a_backend_under_load_fails_no_requests(models, tmp_path):
    """SIGKILL the stable server mid-load: the gateway retries on the canary, then opens the circuit."""
    data = loadtest.instances(models)
    routing = tmp_path / "routing.yaml"
    env = {"MODEL_DIR": str(models)}
    with (
        drill.process(["serve"], env | {"MODEL_VERSION": "v1"}) as (s_url, stable),
        drill.process(["serve"], env | {"MODEL_VERSION": "v2"}) as (c_url, _),
    ):
        routing.write_text(yaml.safe_dump({"stable": {"url": s_url}, "canary": {"url": c_url}, "canary_weight": 10}))
        with drill.process(["gateway", "--routing-file", str(routing)]) as (g_url, _):
            threading.Timer(1.5, stable.kill).start()
            res = asyncio.run(loadtest.open_loop(g_url, 150, 4, data))
    assert res.sent == 600 and res.errors == 0, res.codes
    assert res.served_by["canary"] > 300  # everything moved over once stable was gone
