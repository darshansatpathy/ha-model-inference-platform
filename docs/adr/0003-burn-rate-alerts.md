# 3. Alert on error-budget burn rate, generated and unit-tested

**Status:** accepted

## Context

Threshold alerts on raw error rates either page for blips or stay quiet during slow leaks, and
hand-written PromQL drifts from the objectives it is meant to protect.

## Decision

Objectives live in `slo/slo.yaml`. `ifp generate` produces recording rules over seven windows and
the four multi-window, multi-burn-rate alerts recommended by the Google SRE Workbook (14.4x and 6x
page, 3x and 1x open a ticket), the PrometheusRule resource, and the dashboards. `slo/tests.yaml`
tests each alert with `promtool test rules`: that it fires, that it stays quiet, and that it resolves.

## Consequences

- Changing an objective changes every rule, alert and panel consistently; CI fails if the
  generated files are stale.
- Alerts carry runbook links, and the rendered annotations are part of the tests.
