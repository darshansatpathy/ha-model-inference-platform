"""SLO rules: recording rules and multi-window, multi-burn-rate alerts.

For each objective the error ratio is recorded over 5m, 30m, 1h, 2h, 6h, 1d and 3d, and the
alerts follow the Google SRE Workbook's recommendation for a 30-day budget:

| Severity | Burn rate | Long window | Short window | Budget spent when it fires |
|---|---|---|---|---|
| page | 14.4 | 1h | 5m | 2% |
| page | 6 | 6h | 30m | 5% |
| ticket | 3 | 1d | 2h | 10% |
| ticket | 1 | 3d | 6h | 10% |

The short window makes an alert stop soon after the problem stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

WINDOWS = ["5m", "30m", "1h", "2h", "6h", "1d", "3d"]
BURN = [  # (severity, factor, long, short)
    ("page", 14.4, "1h", "5m"),
    ("page", 6.0, "6h", "30m"),
    ("ticket", 3.0, "1d", "2h"),
    ("ticket", 1.0, "3d", "6h"),
]
RUNBOOK = "https://github.com/darshansatpathy/ha-model-inference-platform/blob/main/docs/runbooks.md"


@dataclass(frozen=True)
class Objective:
    name: str
    description: str
    objective: float
    error_ratio: str

    @property
    def budget(self) -> float:
        return round(1 - self.objective, 10)


def load(path: Path) -> tuple[str, int, list[Objective]]:
    doc = yaml.safe_load(path.read_text())
    objs = [Objective(o["name"], o["description"], float(o["objective"]), " ".join(o["error_ratio"].split())) for o in doc["objectives"]]
    for o in objs:
        if not 0.9 <= o.objective < 1:
            raise ValueError(f"{o.name}: objective {o.objective} out of range")
        if "{w}" not in o.error_ratio:
            raise ValueError(f"{o.name}: error_ratio must use {{w}} for the window")
    return doc["service"], int(doc["window_days"]), objs


def _title(name: str) -> str:
    return "".join(p.capitalize() for p in name.split("_"))


def rule_groups(service: str, window_days: int, objectives: list[Objective]) -> list[dict[str, Any]]:
    groups = []
    for o in objectives:
        labels = {"service": service, "slo": o.name}
        rules: list[dict[str, Any]] = [
            {"record": f"slo:sli_error:ratio_rate{w}", "expr": o.error_ratio.replace("{w}", w), "labels": labels} for w in WINDOWS
        ]
        rules.append(
            {"record": f"slo:sli_error:ratio_rate{window_days}d", "expr": o.error_ratio.replace("{w}", f"{window_days}d"), "labels": labels}
        )
        rules.append(
            {
                "record": "slo:error_budget_remaining:ratio",
                "expr": f'1 - slo:sli_error:ratio_rate{window_days}d{{service="{service}",slo="{o.name}"}} / {o.budget}',
                "labels": labels,
            }
        )
        rules.append({"record": "slo:objective:ratio", "expr": f"vector({o.objective})", "labels": labels})
        sel = f'{{service="{service}",slo="{o.name}"}}'
        for severity in ("page", "ticket"):
            conds = [
                f"(slo:sli_error:ratio_rate{long}{sel} > ({factor:g} * {o.budget}) and slo:sli_error:ratio_rate{short}{sel} > ({factor:g} * {o.budget}))"
                for sev, factor, long, short in BURN
                if sev == severity
            ]
            fast = severity == "page"
            rules.append(
                {
                    "alert": f"{_title(o.name)}ErrorBudgetBurn{'Fast' if fast else 'Slow'}",
                    "expr": "\nor\n".join(conds) + "\n",
                    "for": "2m" if fast else "15m",
                    "labels": {"severity": severity, "service": service, "slo": o.name},
                    "annotations": {
                        "summary": f"{o.name} SLO is burning its error budget {'fast' if fast else 'steadily'}",
                        "description": (
                            f"{o.description} Objective {o.objective:.3%} over {window_days} days. At the current rate the "
                            f"budget runs out in {'hours' if fast else 'days'}: error ratio over 1h is "
                            f"{{{{ with query `slo:sli_error:ratio_rate1h{sel}` }}}}"
                            "{{ . | first | value | humanizePercentage }}{{ end }}."
                        ),
                        "runbook_url": f"{RUNBOOK}#{o.name}-error-budget-burn",
                    },
                }
            )
        groups.append({"name": f"slo-{o.name}", "interval": "30s", "rules": rules})
    groups.append({"name": "inference-operations", "rules": OPERATIONAL})
    return groups


OPERATIONAL: list[dict[str, Any]] = [
    {
        "alert": "InferenceCanaryErrorsHigh",
        "expr": (
            'sum(rate(gateway_upstream_requests_total{backend="canary",code=~"5..|error"}[5m]))'
            ' / sum(rate(gateway_upstream_requests_total{backend="canary"}[5m])) > 0.02'
            ' and sum(rate(gateway_upstream_requests_total{backend="canary"}[5m])) > 1'
        ),
        "for": "2m",
        "labels": {"severity": "page", "service": "inference"},
        "annotations": {
            "summary": "The canary is failing more than 2% of its requests",
            "description": "Set the canary weight to 0 (the rollout controller does this on its own when it is running).",
            "runbook_url": f"{RUNBOOK}#canary-errors",
        },
    },
    {
        "alert": "InferenceModelNotReady",
        "expr": "min by (version) (inference_ready) == 0",
        "for": "5m",
        "labels": {"severity": "ticket", "service": "inference"},
        "annotations": {
            "summary": "A model server has not been ready for 5 minutes",
            "description": "Version {{ $labels.version }} has a pod that is not ready: check the artifact digest and the pod logs.",
            "runbook_url": f"{RUNBOOK}#model-not-ready",
        },
    },
    {
        "alert": "InferenceLoadShedding",
        "expr": 'sum by (version) (rate(inference_rejected_total{reason="overload"}[5m])) > 0.1',
        "for": "5m",
        "labels": {"severity": "ticket", "service": "inference"},
        "annotations": {
            "summary": "Model servers are refusing requests at their concurrency limit",
            "description": "Version {{ $labels.version }} sheds load: the autoscaler is behind or at its maximum.",
            "runbook_url": f"{RUNBOOK}#load-shedding",
        },
    },
    {
        "alert": "InferenceSaturated",
        "expr": "sum by (version) (inference_inflight_requests) / sum by (version) (inference_max_inflight) > 0.8",
        "for": "10m",
        "labels": {"severity": "ticket", "service": "inference"},
        "annotations": {
            "summary": "Model servers are above 80% of their concurrency limit",
            "description": "Version {{ $labels.version }} runs close to its admission limit for 10 minutes.",
            "runbook_url": f"{RUNBOOK}#saturation",
        },
    },
    {
        "alert": "GatewayCircuitOpen",
        "expr": "max by (backend) (gateway_circuit_open) == 1",
        "for": "1m",
        "labels": {"severity": "page", "service": "inference"},
        "annotations": {
            "summary": "The gateway has stopped sending traffic to a backend",
            "description": "The {{ $labels.backend }} backend failed repeatedly; its traffic goes to the other backend.",
            "runbook_url": f"{RUNBOOK}#circuit-open",
        },
    },
    {
        "alert": "InferenceAutoscalerAtMaximum",
        "expr": (
            'kube_horizontalpodautoscaler_status_current_replicas{horizontalpodautoscaler=~"model-.*"}'
            ' >= kube_horizontalpodautoscaler_spec_max_replicas{horizontalpodautoscaler=~"model-.*"}'
        ),
        "for": "15m",
        "labels": {"severity": "ticket", "service": "inference"},
        "annotations": {
            "summary": "An inference autoscaler has been at its maximum for 15 minutes",
            "description": "{{ $labels.horizontalpodautoscaler }} cannot add capacity: raise maxReplicas or the node pool.",
            "runbook_url": f"{RUNBOOK}#autoscaler-at-maximum",
        },
    },
]


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)


_Dumper.add_representer(str, _str)


def dump(doc: Any) -> str:
    return yaml.dump(doc, Dumper=_Dumper, sort_keys=False, width=200, allow_unicode=True)


def rules_file(groups: list[dict[str, Any]]) -> str:
    return "# Generated by `ifp generate` from slo/slo.yaml. Do not edit.\n" + dump({"groups": groups})


def prometheus_rule(groups: list[dict[str, Any]], namespace: str = "inference") -> str:
    doc = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": "inference-slo",
            "namespace": namespace,
            "labels": {"app.kubernetes.io/part-of": "inference-platform", "release": "prometheus"},
        },
        "spec": {"groups": groups},
    }
    return "# Generated by `ifp generate` from slo/slo.yaml. Do not edit.\n" + dump(doc)
