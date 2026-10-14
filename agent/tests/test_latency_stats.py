import pytest

from latency_stats import percentile, summarize


def test_percentile_nearest_rank():
    values = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    assert percentile(values, 50) == 500
    assert percentile(values, 95) == 1000
    assert percentile(values, 0) == 100


def test_percentile_ignores_input_order():
    assert percentile([900, 100, 500], 50) == 500


def test_percentile_single_value():
    assert percentile([42.0], 95) == 42.0


def test_percentile_rejects_bad_input():
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1, 2], 101)


def test_summarize():
    s = summarize([300, 100, 200])
    assert s == {"n": 3, "min": 100, "p50": 200, "p95": 300, "max": 300}
