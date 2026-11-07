# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-08

### Added

- Model server with verified artifacts, readiness and draining, admission control and Prometheus metrics.
- Gateway with sticky weighted canary routing, retries and circuit breakers, configured by a reloadable ConfigMap.
- Rollout controller comparing canary and stable at each step, with Prometheus and gateway-scrape sources and
  file and ConfigMap stores.
- SLO recording rules, multi-window burn-rate alerts with `promtool` tests, and generated Grafana dashboards.
- Kustomize manifests (base, kind, production, rollout Job, monitoring) hardened for the restricted Pod Security Standard.
- Load generation, a capacity sweep, canary drills, and an end-to-end test on kind.
