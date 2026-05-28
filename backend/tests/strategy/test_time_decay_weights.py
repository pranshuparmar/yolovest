"""Time-decay sample weighting: older chronological samples get a linearly
decaying weight toward a floor; newest stays 1.0. Off when last_weight=1.0.
"""

import pytest

from yolovest.config import StrategyConfig
from yolovest.skills.model_retrain import _time_decay_multipliers


def test_default_is_disabled():
    assert StrategyConfig().time_decay_last_weight == 1.0


def test_disabled_returns_all_ones():
    assert _time_decay_multipliers(5, 1.0) == [1.0] * 5
    # Single / empty sample → no decay possible.
    assert _time_decay_multipliers(1, 0.5) == [1.0]
    assert _time_decay_multipliers(0, 0.5) == []


def test_linear_ramp_oldest_to_newest():
    w = _time_decay_multipliers(3, 0.5)
    assert w == pytest.approx([0.5, 0.75, 1.0])


def test_monotonic_increasing_and_bounded():
    w = _time_decay_multipliers(50, 0.3)
    assert w[0] == pytest.approx(0.3)  # oldest at floor
    assert w[-1] == pytest.approx(1.0)  # newest unweighted
    assert all(b >= a for a, b in zip(w, w[1:]))  # non-decreasing
    assert all(0.3 <= x <= 1.0 for x in w)
