"""Train the two model versions the platform serves (development dependency: scikit-learn).

The data is scikit-learn's bundled copy of the Breast Cancer Wisconsin (Diagnostic) dataset
(UCI, CC BY 4.0), so training needs no download. v1 uses the ten "mean" measurements; v2 uses all
thirty. Both are logistic regressions on standardised features, exported as plain parameters.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from inferplat import model

NAME = "tumor-classifier"


def train(version: str, n_features: int, out: Path, seed: int = 7) -> dict[str, float]:
    from sklearn.datasets import load_breast_cancer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, roc_auc_score
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler

    data = load_breast_cancer()
    features = [f.replace(" ", "_") for f in data.feature_names[:n_features]]
    x = data.data[:, :n_features]
    y = (data.target == 0).astype(int)  # 1 = malignant
    x_tr, x_te, y_tr, y_te = train_test_split(x, y, test_size=0.25, stratify=y, random_state=seed)
    scaler = StandardScaler().fit(x_tr)
    clf = LogisticRegression(C=0.5, max_iter=2000).fit(scaler.transform(x_tr), y_tr)
    p = clf.predict_proba(scaler.transform(x_te))[:, 1]
    metrics = {"test_auc": round(float(roc_auc_score(y_te, p)), 4), "test_accuracy": round(float(accuracy_score(y_te, p >= 0.5)), 4)}
    doc = {
        "name": NAME,
        "version": version,
        "features": features,
        "mean": np.round(scaler.mean_, 10).tolist(),
        "scale": np.round(scaler.scale_, 10).tolist(),
        "weights": np.round(clf.coef_[0], 10).tolist(),
        "intercept": round(float(clf.intercept_[0]), 10),
        "threshold": 0.5,
        "metrics": metrics,
        "training": {"dataset": "Breast Cancer Wisconsin (Diagnostic), UCI, CC BY 4.0", "rows": len(x_tr), "seed": seed},
    }
    model.save(doc, out / NAME / version)
    return metrics


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="models")
    args = p.parse_args(argv)
    for version, n in (("v1", 10), ("v2", 30)):
        print(version, train(version, n, Path(args.out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
