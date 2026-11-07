# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/darshansatpathy/ha-model-inference-platform/security/advisories/new)
for this repository. You should get an acknowledgement within three working days.

## Operating it safely

- Pods run as a non-root user with a read-only root filesystem, no capabilities and the
  RuntimeDefault seccomp profile; the namespace enforces the `restricted` Pod Security Standard.
- NetworkPolicies deny ingress by default; model servers accept traffic only from the gateway and
  the monitoring namespace. Put authentication (an ingress with OIDC or mTLS) in front of the gateway.
- The rollout controller's Role can read and patch only the routing ConfigMap and the two model
  Deployments.
- Model artifacts are JSON parameters verified against a SHA-256 manifest; no pickles are loaded.
- `FAULT_ERROR_RATE` and `FAULT_LATENCY_MS` exist for drills; leave them unset in production.
