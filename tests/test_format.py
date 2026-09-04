import math

from simcoach.format import format_laptime


def test_format_laptime_basic():
    assert format_laptime(83.456) == "1:23.456"
    assert format_laptime(23.4) == "0:23.400"
    assert format_laptime(120.0) == "2:00.000"
    assert format_laptime(59.999) == "0:59.999"


def test_format_laptime_pads_seconds():
    # Sub-10s seconds must be zero-padded so it reads m:ss.mmm, not m:s.mmm.
    assert format_laptime(63.05) == "1:03.050"


def test_format_laptime_minute_boundary_rounding():
    # Just under a whole minute must roll over the minute, not render ":60.000".
    assert format_laptime(119.9996) == "2:00.000"
    assert format_laptime(59.9999) == "1:00.000"


def test_format_laptime_invalid():
    for bad in (None, -1.0, float("nan"), float("inf"), "x"):
        assert format_laptime(bad) == "—"
    assert math.isfinite(1.0)  # sanity
