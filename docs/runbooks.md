# Runbooks

Every alert in `slo/rules.yaml` links here. Start with the overview dashboard
(`Inference platform: overview`), which shows traffic, errors and latency per backend, the canary
weight and the circuit breakers on one page.

## Availability error budget burn

**Meaning:** more prediction requests are failing (5xx or a gateway failure) than the 99.9%
objective allows. *Fast* pages: at this rate 2% (1h window) or 5% (6h window) of the month's budget
is gone already. *Slow* opens a ticket: the budget will run out within days.

1. Is a canary taking traffic? `gateway_canary_weight` above 0 and the *Error ratio by backend*
   panel shows the canary worse than stable: set the weight to 0 (below) and the alert clears
   within minutes.
2. Is one backend's circuit open? See [Circuit open](#circuit-open).
3. Are servers shedding load (503s)? See [Load shedding](#load-shedding).
4. Otherwise read the gateway and server logs for the failing status codes, and check recent
   deploys (`kubectl -n inference rollout history deploy/model-stable`).

Set the canary weight to 0:

```bash
kubectl -n inference get configmap gateway-routing -o jsonpath='{.data.routing\.yaml}' \
  | sed 's/canary_weight: .*/canary_weight: 0/' \
  | kubectl -n inference create configmap gateway-routing --from-file=routing.yaml=/dev/stdin --dry-run=client -o yaml \
  | kubectl -n inference apply -f -
```

## Latency error budget burn

**Meaning:** more than 1% of requests take longer than 300 ms at the gateway.

1. Compare *Latency by backend*: a slow canary is rolled back the same way as a failing one.
2. *In-flight requests per pod* near the admission limit means the servers are saturated: check the
   autoscaler ([Autoscaler at maximum](#autoscaler-at-maximum)) and node capacity.
3. Gateway p99 far above the backends' means the gateway itself is short of CPU: its HPA scales on
   CPU; check it is not at maximum.

## Canary errors

**Meaning:** the canary fails more than 2% of its requests. The rollout controller rolls back on its
own when it is running; this alert catches a canary left at a non-zero weight without one.
Set the weight to 0 (see above) and inspect the canary's logs and `/v1/model` (version, digest).

## Model not ready

**Meaning:** a model server pod has not passed `/readyz` for 5 minutes. A server only becomes ready
after its artifact's SHA-256 matches the manifest and the model has loaded.

```bash
kubectl -n inference describe pod -l app.kubernetes.io/name=model-server
kubectl -n inference logs -l app.kubernetes.io/name=model-server --tail=50
```

A digest mismatch means the image holds a modified or partially copied artifact: rebuild the
image from a clean checkout.

## Load shedding

**Meaning:** servers answer 503 because they are at their admission limit (`MAX_INFLIGHT`). The
gateway retries a shed request once on the other backend, so callers may not see errors yet.
Scale up (`kubectl -n inference scale deploy/model-stable --replicas=N`) while finding out why
the autoscaler did not: see [Autoscaler at maximum](#autoscaler-at-maximum) and check that
prometheus-adapter serves `inference_inflight_requests`
(`kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1 | grep inflight`).

## Saturation

**Meaning:** in-flight requests have been above 80% of the admission limit for 10 minutes. Load
shedding is next. Same checks as [Load shedding](#load-shedding).

## Circuit open

**Meaning:** a backend failed several requests in a row (unreachable, timing out or answering 5xx)
and the gateway has stopped sending it traffic; it tries one request again after the cool-down.
While the stable circuit is open, all traffic goes to the canary, whatever its weight.

1. `kubectl -n inference get pods -l app.kubernetes.io/name=model-server` for crash loops or
   pending pods.
2. `curl http://<gateway>/v1/routing` shows the breaker state and the last routing error.
3. If the canary circuit is open during a rollout, the controller will see the errors and roll back.

## Autoscaler at maximum

**Meaning:** a model server HPA has been at `maxReplicas` for 15 minutes and cannot add capacity.
Raise `maxReplicas` in `deploy/k8s/base/autoscaling.yaml` (or the overlay) if the node pool has
room, otherwise add nodes. `docs/capacity.md` gives the requests per second one pod carries.
