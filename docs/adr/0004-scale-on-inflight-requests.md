# 4. Autoscale on in-flight requests per pod, with admission control

**Status:** accepted

## Context

CPU utilisation is a lagging, model-dependent proxy for load on an inference server. Request rate
ignores how long requests take. Without a limit, an overloaded server queues requests until they
all time out.

## Decision

The HPA scales model servers on `inference_inflight_requests` per pod (Little's law: rate x
latency), served by prometheus-adapter, with CPU as a backstop. The target (8) comes from the
concurrency sweep in `docs/capacity.md`: the highest concurrency that still adds throughput within
half the latency objective. Each server rejects requests beyond `MAX_INFLIGHT` (64) with 503 and
`Retry-After`, and the gateway retries those once on the other backend.

## Consequences

- Scale-up reacts to queueing directly, for any model.
- The target must be re-measured when the model or hardware changes; `ifp sweep` does it.
- Under extreme overload callers get fast 503s rather than slow timeouts, and alerts fire on
  shedding before the latency budget is spent.
