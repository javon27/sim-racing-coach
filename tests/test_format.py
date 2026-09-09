import math

from simcoach.analysis import CornerMetrics
from simcoach.format import format_corner_line, format_laptime


def _cm(track_edge_min: float) -> CornerMetrics:
    return CornerMetrics(
        corner_no=1, name="Turn 1", direction="right", apex_dist=100.0,
        entry_spd=200.0, apex_spd=150.0, min_spd=150.0, exit_spd=180.0,
        brake_point=80.0, throttle_point=110.0, trailbrake_overlap=0.2,
        track_edge_min=track_edge_min,
    )


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


def test_track_edge_small_excursion_renders_figure():
    assert "centre 0.5m past edge" in format_corner_line(_cm(-0.50))


def test_track_edge_positive_renders_figure():
    assert "centre 0.2m from edge" in format_corner_line(_cm(0.24))


def test_track_edge_boundary_still_renders_figure():
    # Exactly at the cap: "below that, keep the existing wording" — boundary
    # is inclusive on the numeric side, only strictly-beyond hedges.
    assert "centre 2.0m past edge" in format_corner_line(_cm(-2.0))


def test_track_edge_large_excursion_renders_phrase():
    assert "centre well past edge" in format_corner_line(_cm(-9.75))
    assert "-9.8" not in format_corner_line(_cm(-9.75))
