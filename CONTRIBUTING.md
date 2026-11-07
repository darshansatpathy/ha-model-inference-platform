# Contributing

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make check          # needs promtool and kubectl on the PATH
```

- Changing `slo/slo.yaml` or the rule or dashboard generators: run `ifp generate`, commit the output,
  and extend `slo/tests.yaml` for new alerts.
- Changing the gateway, server or controller: `ifp drill` must still promote the healthy canary and
  roll back the failing and slow ones.
- Changing manifests: `kubectl kustomize` every overlay and keep `tests/test_k8s.py` passing; the
  kind end-to-end job runs on every push.
