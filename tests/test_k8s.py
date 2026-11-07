"""The Kubernetes manifests, rendered with kustomize and checked for the properties HA depends on."""

from __future__ import annotations

import subprocess

import pytest
import yaml
from conftest import ROOT, kubectl

KUBECTL = kubectl()
pytestmark = pytest.mark.skipif(KUBECTL is None, reason="kubectl not installed")


def render(path: str) -> list[dict]:
    assert KUBECTL
    out = subprocess.run([KUBECTL, "kustomize", str(ROOT / "deploy" / "k8s" / path)], check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def by_kind(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def test_every_workload_is_hardened_and_probed():
    docs = render("overlays/production")
    pods = [(d["metadata"]["name"], d["spec"]["template"]["spec"]) for d in docs if d["kind"] in ("Deployment", "Job")]
    assert len(pods) == 4
    for name, spec in pods:
        assert spec["securityContext"]["runAsNonRoot"] is True, name
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault", name
        for c in spec["containers"]:
            sc = c["securityContext"]
            assert sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"] and sc["capabilities"]["drop"] == ["ALL"], name
            assert c["image"].startswith("ghcr.io/darshansatpathy/ha-model-inference-platform:"), name
    for name, d in by_kind(docs, "Deployment").items():
        c = d["spec"]["template"]["spec"]["containers"][0]
        assert {"readinessProbe", "livenessProbe"} <= c.keys(), name
        assert c["lifecycle"]["preStop"], name
        assert d["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0, name
        assert "memory" in c["resources"]["limits"], name


def test_high_availability_shape():
    docs = render("overlays/production")
    deps, pdbs, hpas = by_kind(docs, "Deployment"), by_kind(docs, "PodDisruptionBudget"), by_kind(docs, "HorizontalPodAutoscaler")
    assert deps["gateway"]["spec"]["replicas"] == 3 and deps["model-stable"]["spec"]["replicas"] == 3
    assert {"gateway", "model-stable", "model-canary"} <= pdbs.keys()
    assert hpas["model-stable"]["spec"]["minReplicas"] == 3
    metric = hpas["model-stable"]["spec"]["metrics"][0]["pods"]["metric"]["name"]
    assert metric == "inference_inflight_requests"
    zones = [c["topologyKey"] for c in deps["gateway"]["spec"]["template"]["spec"]["topologySpreadConstraints"]]
    assert "topology.kubernetes.io/zone" in zones


def test_routing_configmap_is_mounted_so_updates_arrive():
    docs = render("base")
    gw = by_kind(docs, "Deployment")["gateway"]["spec"]["template"]["spec"]
    mount = gw["containers"][0]["volumeMounts"][0]
    assert "subPath" not in mount and mount["mountPath"] == "/etc/gateway"
    routing = yaml.safe_load(by_kind(docs, "ConfigMap")["gateway-routing"]["data"]["routing.yaml"])
    assert routing["canary_weight"] == 0


def test_network_policies_default_deny():
    docs = render("base")
    pols = by_kind(docs, "NetworkPolicy")
    assert pols["default-deny-ingress"]["spec"]["podSelector"] == {}
    allowed = pols["model-from-gateway-and-monitoring"]["spec"]["ingress"][0]["from"]
    assert {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "gateway"}}} in allowed


def test_rollout_controller_has_least_privilege():
    role = by_kind(render("rollout"), "Role")["rollout-controller"]
    for rule in role["rules"]:
        assert "delete" not in rule["verbs"] and "*" not in rule["verbs"]
    cm_rule = next(r for r in role["rules"] if r["resources"] == ["configmaps"])
    assert cm_rule["resourceNames"] == ["gateway-routing"]


def test_kind_overlay_runs_the_local_image_without_autoscalers():
    docs = render("overlays/kind")
    assert not by_kind(docs, "HorizontalPodAutoscaler")
    for d in by_kind(docs, "Deployment").values():
        c = d["spec"]["template"]["spec"]["containers"][0]
        assert c["image"] == "ifp:e2e" and c["imagePullPolicy"] == "Never"


def test_monitoring_bundle():
    docs = render("monitoring")
    kinds = sorted(d["kind"] for d in docs)
    assert kinds == ["ConfigMap", "PodMonitor", "PodMonitor", "PrometheusRule"]
    cm = by_kind(docs, "ConfigMap")["inference-dashboards"]
    assert cm["metadata"]["labels"]["grafana_dashboard"] == "1" and len(cm["data"]) == 2
