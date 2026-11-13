"""合成日内路径：锚点精确 / 带内 / 确定性 / 量守恒。"""

import math

import pytest

from framesandbox.data.synth import synth_minute_path


@pytest.mark.parametrize("seed", [0, 1, 42, 999])
def test_anchors_and_band(seed):
    o, h, l, c = 10.0, 10.8, 9.6, 10.2
    p = synth_minute_path(o, h, l, c, 1_000_000, seed=seed)
    assert len(p) == 240
    assert p.prices[0] == o
    assert p.prices[-1] == c
    assert max(p.prices) == h
    assert min(p.prices) == l
    assert all(l - 1e-9 <= x <= h + 1e-9 for x in p.prices)
    assert math.isclose(math.fsum(p.vols), 1_000_000, rel_tol=1e-9)
    assert p.source == "synthetic"


def test_determinism():
    a = synth_minute_path(10, 11, 9, 10.5, 1e6, seed=7)
    b = synth_minute_path(10, 11, 9, 10.5, 1e6, seed=7)
    assert a.prices == b.prices and a.vols == b.vols
    c = synth_minute_path(10, 11, 9, 10.5, 1e6, seed=8)
    assert a.prices != c.prices


def test_flat_day():
    p = synth_minute_path(5.0, 5.0, 5.0, 5.0, 1000, seed=1)
    assert all(x == 5.0 for x in p.prices)


def test_invalid_ohlc():
    with pytest.raises(ValueError):
        synth_minute_path(10, 9, 8, 10.5, 1000, seed=1)   # high < open
