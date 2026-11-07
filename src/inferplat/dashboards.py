"""Grafana dashboards, generated so that every query is reviewed and parse-checked in CI."""

from __future__ import annotations

import json
from typing import Any

from inferplat.slo import WINDOWS, Objective

DS = {"type": "prometheus", "uid": "${datasource}"}


class _Layout:
    def __init__(self) -> None:
        self.x = self.y = self.row_h = 0
        self.next_id = 1

    def place(self, w: int, h: int) -> dict[str, int]:
        if self.x + w > 24:
            self.x, self.y = 0, self.y + self.row_h
            self.row_h = 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def id(self) -> int:
        self.next_id += 1
        return self.next_id - 1


def _targets(queries: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [{"datasource": DS, "expr": q, "legendFormat": legend, "refId": chr(65 + i)} for i, (q, legend) in enumerate(queries)]


def timeseries(
    lay: _Layout,
    title: str,
    queries: list[tuple[str, str]],
    unit: str,
    w: int = 12,
    h: int = 8,
    desc: str = "",
    thresholds: list[float] | None = None,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {"unit": unit, "custom": {"lineWidth": 1, "fillOpacity": 10, "showPoints": "never"}}
    if thresholds:
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": "green", "value": None}] + [{"color": "red", "value": t} for t in thresholds],
        }
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "id": lay.id(),
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": lay.place(w, h),
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
            "tooltip": {"mode": "multi"},
        },
        "targets": _targets(queries),
    }


def stat(
    lay: _Layout, title: str, query: str, unit: str, steps: list[tuple[str, float | None]], w: int = 6, h: int = 4, desc: str = ""
) -> dict[str, Any]:
    return {
        "id": lay.id(),
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": lay.place(w, h),
        "fieldConfig": {
            "defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}},
            "overrides": [],
        },
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "area"},
        "targets": _targets([(query, title)]),
    }


def _dashboard(uid: str, title: str, panels: list[dict[str, Any]], tags: list[str]) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "tags": tags,
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "editable": False,
        "templating": {"list": [{"name": "datasource", "type": "datasource", "query": "prometheus", "label": "Prometheus", "current": {}}]},
        "annotations": {"list": []},
        "panels": panels,
    }


def overview() -> dict[str, Any]:
    lay = _Layout()
    r = "$__rate_interval"
    panels = [
        stat(
            lay,
            "Availability, 30 days",
            'clamp_max(1 - slo:sli_error:ratio_rate30d{slo="availability"}, 1)',
            "percentunit",
            [("red", None), ("orange", 0.998), ("green", 0.999)],
        ),
        stat(
            lay,
            "Availability error budget left",
            'slo:error_budget_remaining:ratio{slo="availability"}',
            "percentunit",
            [("red", None), ("orange", 0.25), ("green", 0.5)],
        ),
        stat(lay, "Canary weight", "max(gateway_canary_weight)", "percent", [("blue", None)]),
        stat(lay, "Requests per second", f"sum(rate(gateway_requests_total[{r}]))", "reqps", [("blue", None)]),
        timeseries(lay, "Requests by backend", [(f"sum by (route) (rate(gateway_requests_total[{r}]))", "{{route}}")], "reqps"),
        timeseries(
            lay,
            "Error ratio by backend",
            [
                (
                    f'sum by (backend) (rate(gateway_upstream_requests_total{{code=~"5..|error"}}[{r}])) / sum by (backend) (rate(gateway_upstream_requests_total[{r}]))',
                    "{{backend}}",
                )
            ],
            "percentunit",
            desc="Attempts that failed, per backend; what the rollout controller compares.",
            thresholds=[0.01],
        ),
        timeseries(
            lay,
            "Latency by backend",
            [
                (
                    f"histogram_quantile({q}, sum by (backend, le) (rate(gateway_upstream_duration_seconds_bucket[{r}])))",
                    f"{{{{backend}}}} p{int(q * 100)}",
                )
                for q in (0.5, 0.95, 0.99)
            ],
            "s",
            thresholds=[0.3],
        ),
        timeseries(
            lay,
            "End-to-end latency at the gateway",
            [
                (f"histogram_quantile({q}, sum by (le) (rate(gateway_request_duration_seconds_bucket[{r}])))", f"p{int(q * 100)}")
                for q in (0.5, 0.95, 0.99)
            ],
            "s",
            thresholds=[0.3],
        ),
        timeseries(
            lay,
            "In-flight requests per pod",
            [
                ("sum by (version, pod) (inference_inflight_requests)", "{{version}} {{pod}}"),
                ("max(inference_max_inflight)", "admission limit"),
            ],
            "short",
            desc="The autoscaler's custom metric. Above the limit, requests are shed with 503.",
        ),
        timeseries(
            lay,
            "Shed and retried requests",
            [
                (f"sum by (version) (rate(inference_rejected_total[{r}]))", "shed {{version}}"),
                (f"sum by (from_backend) (rate(gateway_retries_total[{r}]))", "retried from {{from_backend}}"),
            ],
            "reqps",
        ),
        timeseries(
            lay,
            "Ready model servers by version",
            [
                ("sum by (version) (inference_ready)", "{{version}}"),
                ('sum by (deployment) (kube_deployment_status_replicas{deployment=~"model-.*"})', "{{deployment}} desired"),
            ],
            "short",
        ),
        timeseries(
            lay,
            "Circuit breakers",
            [("max by (backend) (gateway_circuit_open)", "{{backend}}")],
            "short",
            desc="1 while the gateway sends no traffic to a backend.",
        ),
    ]
    return _dashboard("inference-overview", "Inference platform: overview", panels, ["inference", "canary"])


def slo_dashboard(objectives: list[Objective]) -> dict[str, Any]:
    lay = _Layout()
    panels: list[dict[str, Any]] = []
    for o in objectives:
        sel = f'{{slo="{o.name}"}}'
        panels.append(
            stat(
                lay,
                f"{o.name}: budget left",
                f"slo:error_budget_remaining:ratio{sel}",
                "percentunit",
                [("red", None), ("orange", 0.25), ("green", 0.5)],
                w=8,
            )
        )
        panels.append(stat(lay, f"{o.name}: objective", f"slo:objective:ratio{sel}", "percentunit", [("blue", None)], w=8))
        panels.append(
            stat(
                lay,
                f"{o.name}: error ratio, 1h",
                f"slo:sli_error:ratio_rate1h{sel}",
                "percentunit",
                [("green", None), ("red", 14.4 * o.budget)],
                w=8,
            )
        )
        panels.append(
            timeseries(
                lay,
                f"{o.name}: burn rate",
                [(f"slo:sli_error:ratio_rate{w}{sel} / {o.budget}", w) for w in WINDOWS],
                "short",
                w=24,
                desc="1 spends the budget exactly over 30 days. Pages at 14.4 (1h and 5m) or 6 (6h and 30m); tickets at 3 (1d and 2h) or 1 (3d and 6h).",
                thresholds=[1, 3, 6, 14.4],
            )
        )
    return _dashboard("inference-slo", "Inference platform: SLOs", panels, ["inference", "slo"])


def render(d: dict[str, Any]) -> str:
    return json.dumps(d, indent=2, sort_keys=False) + "\n"


def expressions(d: dict[str, Any]) -> list[str]:
    return [t["expr"] for p in d["panels"] for t in p.get("targets", [])]
