from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"


@pytest.fixture
def models(tmp_path: Path) -> Path:
    dest = tmp_path / "models"
    shutil.copytree(MODELS, dest)
    return dest


def promtool() -> str | None:
    return shutil.which("promtool") or (str(p) if (p := Path("/opt/bin/promtool")).exists() else None)


def kubectl() -> str | None:
    return shutil.which("kubectl") or (str(p) if (p := Path("/opt/bin/kubectl")).exists() else None)
