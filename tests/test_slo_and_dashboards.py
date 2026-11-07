from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import ROOT, promtool

from inferplat import dashboards, slo
from inferplat.cli import main

PROMTOOL = promtool()
needs_promtool = pytest.mark.skipif(PROMTOOL is None, reason="promtool not installed")


def _objectives():
    return slo.load(ROOT / "slo" / "slo.yaml")


def test_generated_files_are_current(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert main(["generate", "--check"]) == 0


def test_rule_shape():
    service, days, objs = _objectives()
    groups = slo.rule_groups(service, days, objs)
    names = [r.get("alert") or r.get("record") for g in groups for r in g["rules"]]
    for o in ("Availability", "Latency"):
        assert f"{o}ErrorBudgetBurnFast" in names and f"{o}ErrorBudgetBurnSlow" in names
    assert names.count("slo:sli_error:ratio_rate5m") == 2
    fast = next(r for g in groups for r in g["rules"] if r.get("alert") == "AvailabilityErrorBudgetBurnFast")
    assert "(14.4 * 0.001)" in fast["expr"] and "(6 * 0.001)" in fast["expr"]
    assert all(r.get("annotations", {}).get("runbook_url", "x").startswith("https://") for g in groups for r in g["rules"] if "alert" in r)


def test_bad_slo_definitions_are_refused(tmp_path: Path):
    doc = yaml.safe_load((ROOT / "slo" / "slo.yaml").read_text())
    doc["objectives"][0]["objective"] = 99.9
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError, match="out of range"):
        slo.load(tmp_path / "s.yaml")
    doc["objectives"][0]["objective"] = 0.999
    doc["objectives"][0]["error_ratio"] = "sum(rate(x[5m]))"
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError, match="window"):
        slo.load(tmp_path / "s.yaml")


@needs_promtool
def test_promtool_checks_and_unit_tests_pass():
    assert PROMTOOL
    subprocess.run([PROMTOOL, "check", "rules", str(ROOT / "slo" / "rules.yaml")], check=True, capture_output=True)
    out = subprocess.run([PROMTOOL, "test", "rules", str(ROOT / "slo" / "tests.yaml")], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


@needs_promtool
def test_every_dashboard_query_parses(tmp_path: Path):
    """Wrap each panel query in a recording rule so promtool parses it."""
    assert PROMTOOL
    _, _, objs = _objectives()
    exprs = dashboards.expressions(dashboards.overview()) + dashboards.expressions(dashboards.slo_dashboard(objs))
    assert len(exprs) > 20
    rules = [{"record": f"check:q{i}", "expr": e.replace("$__rate_interval", "5m")} for i, e in enumerate(exprs)]
    (tmp_path / "q.yaml").write_text(yaml.safe_dump({"groups": [{"name": "dashboards", "rules": rules}]}))
    out = subprocess.run([PROMTOOL, "check", "rules", str(tmp_path / "q.yaml")], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_dashboards_are_well_formed():
    _, _, objs = _objectives()
    for d in (dashboards.overview(), dashboards.slo_dashboard(objs)):
        ids = [p["id"] for p in d["panels"]]
        assert len(ids) == len(set(ids))
        for p in d["panels"]:
            g = p["gridPos"]
            assert g["x"] + g["w"] <= 24 and p["datasource"]["uid"] == "${datasource}"
    assert os.path.exists(ROOT / "deploy" / "k8s" / "monitoring" / "dashboards" / "inference-overview.json")
