"""Model artifacts: a portable, verifiable format with no pickles.

An artifact is a JSON document holding a logistic model's feature names, standardisation
parameters, weights and intercept, plus its version and evaluation metrics. A manifest beside it
records the document's SHA-256; the server refuses to load an artifact whose digest does not
match, so a tampered or half-copied model never becomes ready.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


class ArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class Model:
    name: str
    version: str
    features: tuple[str, ...]
    mean: np.ndarray
    scale: np.ndarray
    weights: np.ndarray
    intercept: float
    threshold: float
    sha256: str
    metrics: dict[str, float]

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        """x: one row per instance, columns in `features` order."""
        z = (x - self.mean) / self.scale
        logits = z @ self.weights + self.intercept
        return np.asarray(1.0 / (1.0 + np.exp(-logits)), dtype=float)

    def matrix(self, instances: list[dict[str, float]]) -> np.ndarray:
        missing = sorted({f for row in instances for f in self.features if f not in row})
        if missing:
            raise ArtifactError(f"missing features: {', '.join(missing[:5])}")
        x = np.array([[float(row[f]) for f in self.features] for row in instances], dtype=float)
        if not np.isfinite(x).all():
            raise ArtifactError("features must be finite numbers")
        return x


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(doc: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "model.json"
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    manifest = {"name": doc["name"], "version": doc["version"], "file": "model.json", "sha256": digest(path), "metrics": doc["metrics"]}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return path


def load(directory: Path) -> Model:
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
    except FileNotFoundError as e:
        raise ArtifactError(f"no manifest in {directory}") from e
    path = directory / manifest["file"]
    actual = digest(path)
    if actual != manifest["sha256"]:
        raise ArtifactError(f"{path} digest {actual[:12]} does not match the manifest's {manifest['sha256'][:12]}")
    doc = json.loads(path.read_text())
    n = len(doc["features"])
    arrays = {k: np.asarray(doc[k], dtype=float) for k in ("mean", "scale", "weights")}
    if any(a.shape != (n,) for a in arrays.values()):
        raise ArtifactError("mean, scale and weights must have one value per feature")
    if not all(np.isfinite(a).all() for a in arrays.values()) or (arrays["scale"] <= 0).any() or not math.isfinite(doc["intercept"]):
        raise ArtifactError("parameters must be finite, and scales positive")
    return Model(
        name=doc["name"],
        version=doc["version"],
        features=tuple(doc["features"]),
        mean=arrays["mean"],
        scale=arrays["scale"],
        weights=arrays["weights"],
        intercept=float(doc["intercept"]),
        threshold=float(doc.get("threshold", 0.5)),
        sha256=actual,
        metrics=doc["metrics"],
    )
