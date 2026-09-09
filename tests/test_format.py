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


# --- time-loss formatting (issue #38 slice 1) ---------------------------------

def _buckets():
    # delta > 0 = time LOST vs the reference (align_time_axes' sign).
    return [
        {"label": "lap start → T1 entry", "kind": "straight", "delta": 0.01,
         "start_dist": 0.0, "end_dist": 800.0},
        {"label": "T1 Left Hairpin", "kind": "corner", "delta": 0.31,
         "start_dist": 800.0, "end_dist": 1200.0},
        {"label": "T1 exit → T2 entry", "kind": "straight", "delta": 0.18,
         "start_dist": 1200.0, "end_dist": 2000.0},
        {"label": "T2 Right Sweeper", "kind": "corner", "delta": -0.12,
         "start_dist": 2000.0, "end_dist": 2400.0},
    ]


def test_format_time_loss_ranks_losses_biggest_first_and_signs_them_negative():
    from simcoach.format import format_time_loss
    out = format_time_loss(_buckets(), "your session best")
    assert "Net -0.38s vs your session best." in out        # 0.01+0.31+0.18-0.12
    assert "-0.31s" in out and "-0.18s" in out
    assert out.index("T1 Left Hairpin") < out.index("T1 exit → T2 entry")
    # the 0.01s straight is integration noise, not a coachable loss
    assert "lap start" not in out
    # a real gain is named so the coach can reinforce it
    assert "Biggest gain" in out and "T2 Right Sweeper +0.12s" in out


def test_format_time_loss_says_so_when_nothing_stands_out():
    from simcoach.format import format_time_loss
    flat = [{"label": "T1", "kind": "corner", "delta": 0.001,
             "start_dist": 0.0, "end_dist": 10.0}]
    out = format_time_loss(flat, "your best lap")
    assert "Net" in out and "stands out" in out


def test_format_time_loss_is_honest_with_no_buckets():
    from simcoach.format import format_time_loss
    assert "reference" in format_time_loss(None, "your best lap").lower()


def test_format_corner_line_reports_coasting_and_the_brake_to_throttle_gap():
    from simcoach.analysis import CornerMetrics
    from simcoach.format import format_corner_line
    cm = CornerMetrics(corner_no=1, name="Village", direction="left", apex_dist=1000.0,
                       entry_spd=120.0, apex_spd=60.0, min_spd=58.0, exit_spd=90.0,
                       brake_point=900.0, throttle_point=1050.0, trailbrake_overlap=0.3,
                       coast_time=0.42, coast_dist=18.0, brake_to_throttle_dist=18.0)
    line = format_corner_line(cm)
    assert "coasting 0.4s (18 m)" in line
    assert "18 m brake\N{RIGHTWARDS ARROW}throttle gap" in line


def test_format_corner_line_silent_when_coasting_was_not_measured():
    from simcoach.analysis import CornerMetrics
    from simcoach.format import format_corner_line
    # nan = the channel wasn't there (older capture / degenerate window), not zero.
    cm = CornerMetrics(corner_no=1, name="Village", direction="left", apex_dist=1000.0,
                       entry_spd=120.0, apex_spd=60.0, min_spd=58.0, exit_spd=90.0,
                       brake_point=900.0, throttle_point=1050.0, trailbrake_overlap=0.3)
    line = format_corner_line(cm)
    assert "coasting" not in line and "gap" not in line
