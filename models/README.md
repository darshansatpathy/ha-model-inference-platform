# Model artifacts

`tumor-classifier/v1` and `v2` are logistic regressions trained by `ifp train` (see
`src/inferplat/train.py`) on the Breast Cancer Wisconsin (Diagnostic) dataset, as bundled with
scikit-learn.

- Source: W. Wolberg, O. Mangasarian, N. Street, W. Street, *Breast Cancer Wisconsin (Diagnostic)*,
  UCI Machine Learning Repository, https://doi.org/10.24432/C5DW2B
- License: Creative Commons Attribution 4.0 International (CC BY 4.0)

| Version | Features | Test AUC | Test accuracy |
|---|---|---|---|
| v1 | the 10 "mean" measurements | 0.982 | 0.937 |
| v2 | all 30 measurements | 0.996 | 0.972 |

Each directory holds `model.json` (the parameters) and `manifest.json` (its SHA-256). The server
refuses an artifact whose digest does not match. The models are here to give the platform
something real to serve; they are not for clinical use.
