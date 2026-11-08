#!/usr/bin/env bash
# End to end on a kind cluster: deploy, serve under load, roll back a failing canary, promote a
# healthy one, and restart the stable servers under load without losing a request.
# Needs: a kind cluster with the image loaded as ifp:e2e, kubectl, and `ifp` installed locally.
set -euo pipefail

NS=inference
PORT=18080
GW="http://127.0.0.1:${PORT}"
LOGS=${LOGS:-e2e-logs}
mkdir -p "$LOGS"

step() { printf '\n== %s\n' "$*"; }

rollout_wait() {
  for d in "$@"; do kubectl -n "$NS" rollout status "deploy/$d" --timeout=240s; done
  # A replaced pod keeps serving through its preStop wait; measure only once it has gone.
  for _ in $(seq 1 90); do
    terminating=$(kubectl -n "$NS" get pods -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{" "}{end}')
    [ -z "$terminating" ] && return 0
    sleep 1
  done
  echo "pods still terminating: $terminating" >&2
  return 1
}

forward() {
  kubectl -n "$NS" port-forward svc/gateway "${PORT}:80" >"$LOGS/port-forward.log" 2>&1 &
  PF=$!
  for _ in $(seq 1 60); do
    if curl -fsS "$GW/readyz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "gateway not reachable" >&2
  return 1
}

cleanup() {
  kubectl -n "$NS" get pods -o wide >"$LOGS/pods.txt" 2>&1 || true
  kubectl -n "$NS" get events --sort-by=.lastTimestamp >"$LOGS/events.txt" 2>&1 || true
  kubectl -n "$NS" logs deploy/gateway --tail=200 >"$LOGS/gateway.log" 2>&1 || true
  kill "${PF:-0}" "${LOAD:-0}" 2>/dev/null || true
}
trap cleanup EXIT

step "deploy"
kubectl apply -k deploy/k8s/overlays/kind
rollout_wait model-stable model-canary gateway
forward

step "serve under load: no errors"
ifp load --url "$GW" --rate 50 --seconds 20 --max-error-rate 0 --max-p95-ms 300 | tee "$LOGS/load-baseline.json"

POLICY=(--steps 20,50,100 --step-seconds 15 --min-requests 100 --max-extensions 4 --apply-timeout 180)

step "a canary failing 5% of requests is rolled back"
kubectl -n "$NS" set env deploy/model-canary FAULT_ERROR_RATE=0.05
rollout_wait model-canary
ifp load --url "$GW" --rate 50 --seconds 900 >"$LOGS/load-failing-canary.json" &
LOAD=$!
set +e
ifp rollout --store configmap --namespace "$NS" --gateway "$GW" "${POLICY[@]}" | tee "$LOGS/rollout-failing.json"
code=${PIPESTATUS[0]}
set -e
kill "$LOAD" 2>/dev/null || true
wait "$LOAD" 2>/dev/null || true
[ "$code" -eq 2 ] || { echo "expected a rollback (exit 2), got $code" >&2; exit 1; }
grep -q '"rolled_back"' "$LOGS/rollout-failing.json"
kubectl -n "$NS" get configmap gateway-routing -o jsonpath='{.data.routing\.yaml}' | grep -q 'canary_weight: 0'

step "a healthy canary is promoted"
kubectl -n "$NS" set env deploy/model-canary FAULT_ERROR_RATE-
rollout_wait model-canary
ifp load --url "$GW" --rate 50 --seconds 900 >"$LOGS/load-healthy-canary.json" &
LOAD=$!
ifp rollout --store configmap --namespace "$NS" --gateway "$GW" "${POLICY[@]}" | tee "$LOGS/rollout-healthy.json"
kill "$LOAD" 2>/dev/null || true
wait "$LOAD" 2>/dev/null || true
grep -q '"promoted"' "$LOGS/rollout-healthy.json"
version=$(kubectl -n "$NS" get deploy/model-stable -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="MODEL_VERSION")].value}')
[ "$version" = "v2" ] || { echo "stable runs $version after promotion" >&2; exit 1; }

step "stable now answers with v2"
rollout_wait model-stable
sleep 2
curl -fsS -X POST "$GW/v1/predict" -H 'content-type: application/json' -H 'x-canary: never' \
  -d "$(python -c 'import json; from inferplat.loadtest import instances; print(json.dumps({"instances": instances(__import__("pathlib").Path("models"), n=1)}))')" \
  | tee "$LOGS/after-promotion.json" | grep -q '"version":"v2"'

step "rolling restart of the stable servers under load: no failed requests"
ifp load --url "$GW" --rate 50 --seconds 60 --max-error-rate 0 >"$LOGS/load-restart.json" &
LOAD=$!
sleep 5
kubectl -n "$NS" rollout restart deploy/model-stable
rollout_wait model-stable
wait "$LOAD"
cat "$LOGS/load-restart.json"

step "passed"
