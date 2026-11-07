from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from inferplat import loadtest, model
from inferplat.server import Settings, create_app


def _row(models, version="v2"):
    return loadtest.instances(models, version=version, n=1)[0]


def test_lifecycle_and_prediction(models):
    app = create_app(Settings(model_dir=models, model_version="v2"))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        r = c.get("/readyz")
        assert r.status_code == 200 and r.headers["x-model-version"] == "v2"
        r = c.post("/v1/predict", json={"instances": [_row(models)] * 3})
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == "v2" and len(body["predictions"]) == 3
        assert {"probability", "label"} <= body["predictions"][0].keys()
        info = c.get("/v1/model").json()
        assert info["loaded"] and len(info["features"]) == 30
        metrics = c.get("/metrics").text
        assert 'inference_requests_total{code="200",model="tumor-classifier",version="v2"} 1.0' in metrics
        assert "inference_ready" in metrics and "inference_max_inflight" in metrics
    # After shutdown the app reports not ready.
    assert app.state.state["ready"] is False


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"instances": []}, "invalid request"),
        ({"rows": [1]}, "invalid request"),
        ({"instances": [{"mean_radius": "abc"}]}, "invalid request"),
        ({"instances": [{"mean_radius": 1.0}]}, "missing features"),
    ],
)
def test_bad_requests_are_422(models, payload, fragment):
    with TestClient(create_app(Settings(model_dir=models, model_version="v1"))) as c:
        r = c.post("/v1/predict", json=payload)
        assert r.status_code == 422 and fragment in r.json()["error"]


def test_batch_limit(models):
    with TestClient(create_app(Settings(model_dir=models, model_version="v1", max_batch=2))) as c:
        r = c.post("/v1/predict", json={"instances": [_row(models)] * 3})
        assert r.status_code == 422 and "at most 2" in r.json()["error"]


def test_overload_is_shed_with_retry_after(models):
    with TestClient(create_app(Settings(model_dir=models, model_version="v1", max_inflight=0))) as c:
        r = c.post("/v1/predict", json={"instances": [_row(models)]})
        assert r.status_code == 503 and r.headers["retry-after"] == "1"
        assert 'inference_rejected_total{model="tumor-classifier",reason="overload",version="v1"} 1.0' in c.get("/metrics").text


def test_fault_injection(models):
    with TestClient(create_app(Settings(model_dir=models, model_version="v2", fault_error_rate=1.0, fault_latency_ms=1), seed=1)) as c:
        assert c.post("/v1/predict", json={"instances": [_row(models)]}).status_code == 500


def test_wrong_or_tampered_artifact_never_becomes_ready(models):
    (models / "tumor-classifier" / "v1" / "model.json").write_text("{}")
    with pytest.raises(model.ArtifactError), TestClient(create_app(Settings(model_dir=models, model_version="v1"))):
        pass


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_VERSION", "v2")
    monkeypatch.setenv("MAX_INFLIGHT", "7")
    monkeypatch.setenv("FAULT_ERROR_RATE", "0.5")
    s = Settings.from_env()
    assert (s.model_version, s.max_inflight, s.fault_error_rate) == ("v2", 7, 0.5)
