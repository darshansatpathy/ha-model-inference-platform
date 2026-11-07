from __future__ import annotations

import pytest

from inferplat.capacity import Level, hpa_desired, recommend_target, replicas_for


def test_hpa_formula():
    assert hpa_desired(4, 16, 8, 2, 20) == 8
    assert hpa_desired(4, 8.5, 8, 2, 20) == 4  # inside the 10% tolerance
    assert hpa_desired(4, 1, 8, 2, 20) == 2  # never below the minimum
    assert hpa_desired(10, 40, 8, 2, 20) == 20  # never above the maximum
    assert hpa_desired(0, 40, 8, 2, 20) == 2


def test_target_is_the_last_level_within_budget():
    levels = [Level(1, 300, 0.004, 0), Level(8, 1800, 0.012, 0), Level(16, 1900, 0.09, 0), Level(32, 1900, 0.2, 0.01)]
    assert recommend_target(levels).concurrency == 16
    assert recommend_target(levels, budget_share=0.1).concurrency == 8
    collapse = [Level(4, 1450, 0.004, 0), Level(8, 1340, 0.007, 0), Level(16, 760, 0.067, 0)]
    assert recommend_target(collapse).concurrency == 8  # 16 meets the latency budget but only queues
    with pytest.raises(ValueError):
        recommend_target([Level(1, 10, 1.0, 0)])


def test_replicas_for_peak():
    assert replicas_for(100, 1000) == 2
    assert replicas_for(5000, 1000) == 8
