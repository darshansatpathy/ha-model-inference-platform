# 1. Route canary traffic in a small gateway configured by a ConfigMap

**Status:** accepted

## Context

A canary release needs a traffic split that can change in seconds, keeps each caller on one
version, and can be undone instantly. Options: a service mesh, an ingress controller's weighted
backends, Argo Rollouts, or the platform's own gateway.

## Decision

A gateway in front of the model servers splits traffic by a hash of the caller's routing key, and
reads the split from a ConfigMap mounted as a directory, reloading it when it changes. The rollout
controller writes the ConfigMap. The gateway also retries and circuit-breaks.

## Consequences

- No mesh is required; the same code runs locally, in CI on kind and in production.
- A ConfigMap change reaches pods after the kubelet's sync (seconds to a minute), so the
  controller waits until every gateway reports the new weight before measuring.
- Sticky routing means a canary step measures a stable population of callers, and a caller does not
  flip between versions mid-session.
- The gateway is one more hop and one more thing to scale; it is stateless and has its own HPA and PDB.
