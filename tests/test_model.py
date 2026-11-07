from __future__ import annotations

import json

import numpy as np
import pytest

from inferplat import model


def test_load_verifies_and_predicts(models):
    m = model.load(models / "tumor-classifier" / "v1")
    assert m.version == "v1" and len(m.features) == 10
    p = m.predict_proba(np.asarray([m.mean, m.mean + 3 * m.scale]))
    assert p.shape == (2,) and 0 < p[0] < 1 and p[1] > p[0]
    assert m.metrics["test_auc"] > 0.95


def test_tampered_artifact_is_refused(models):
    path = models / "tumor-classifier" / "v2" / "model.json"
    doc = json.loads(path.read_text())
    doc["intercept"] += 1
    path.write_text(json.dumps(doc))
    with pytest.raises(model.ArtifactError, match="does not match"):
        model.load(path.parent)


def test_malformed_parameters_are_refused(models, tmp_path):
    doc = json.loads((models / "tumor-classifier" / "v1" / "model.json").read_text())
    doc["scale"][0] = 0
    model.save(doc, tmp_path / "bad")
    with pytest.raises(model.ArtifactError, match="positive"):
        model.load(tmp_path / "bad")
    doc["scale"][0] = 1
    doc["weights"] = doc["weights"][:-1]
    model.save(doc, tmp_path / "bad")
    with pytest.raises(model.ArtifactError, match="one value per feature"):
        model.load(tmp_path / "bad")
    with pytest.raises(model.ArtifactError, match="no manifest"):
        model.load(tmp_path / "missing")


def test_matrix_validates_input(models):
    m = model.load(models / "tumor-classifier" / "v1")
    row = dict(zip(m.features, m.mean.tolist(), strict=True))
    assert m.matrix([row, row | {"extra": 1.0}]).shape == (2, 10)
    with pytest.raises(model.ArtifactError, match="missing features"):
        m.matrix([{k: v for k, v in row.items() if k != m.features[0]}])
    with pytest.raises(model.ArtifactError, match="finite"):
        m.matrix([row | {m.features[0]: float("inf")}])
