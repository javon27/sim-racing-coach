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


# --- #101: noise floor keyed to which time axis produced the buckets ---------

def test_format_time_loss_treats_small_delta_as_loss_when_exact():
    from simcoach.format import format_time_loss
    buckets = [{"label": "T1", "kind": "corner", "delta": 0.01, "exact": True,
                "start_dist": 0.0, "end_dist": 100.0}]
    out = format_time_loss(buckets, "your best lap")
    assert "T1" in out and "Biggest losses" in out


def test_format_time_loss_treats_same_delta_as_noise_when_not_exact():
    from simcoach.format import format_time_loss
    buckets = [{"label": "T1", "kind": "corner", "delta": 0.01,
                "start_dist": 0.0, "end_dist": 100.0}]
    out = format_time_loss(buckets, "your best lap")
    assert "stands out" in out


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

def test_track_edge_never_renders_until_rig_verified():
    # mTrackEdge/mPathLateral units + sign are PENDING LIVE VALIDATION; an
    # unverified figure is cut from the prompt, not hedged (#99).
    for v in (-9.75, -2.0, -0.5, 0.24, 3.0):
        line = format_corner_line(_cm(v))
        assert "edge" not in line, line


# --- #102: per-corner deltas vs the reference, relative and driver-facing --------

def _cm_deltas(**deltas) -> CornerMetrics:
    return CornerMetrics(
        corner_no=1, name="Turn 1", direction="right", apex_dist=1000.0,
        entry_spd=200.0, apex_spd=150.0, min_spd=150.0, exit_spd=180.0,
        brake_point=900.0, throttle_point=1050.0, trailbrake_overlap=0.2,
        **deltas,
    )


def test_format_corner_line_renders_brake_and_throttle_point_deltas():
    cm = _cm_deltas(brake_point_delta=10.0, throttle_point_delta=15.0)
    line = format_corner_line(cm)
    assert "brake 10 m later than best" in line
    assert "throttle 15 m later than best" in line


def test_format_corner_line_renders_earlier_wording_for_negative_deltas():
    cm = _cm_deltas(brake_point_delta=-8.0)
    line = format_corner_line(cm)
    assert "brake 8 m earlier than best" in line


def test_format_corner_line_renders_coast_trail_and_abs_tc_deltas():
    cm = _cm_deltas(coast_time_delta=0.3, trailbrake_overlap_delta=-0.08,
                    abs_pct_delta=0.15)
    line = format_corner_line(cm)
    assert "+0.3s coasting vs best" in line
    assert "trail -8 pts" in line
    assert "ABS +15 pts" in line


def test_format_corner_line_silent_with_no_reference():
    # No deltas supplied (all None, the no-reference default) -> no delta text.
    cm = _cm_deltas()
    line = format_corner_line(cm)
    assert "vs best" not in line and "than best" not in line and "pts" not in line


def test_format_corner_line_silent_for_deltas_under_the_floor():
    cm = _cm_deltas(brake_point_delta=1.0, throttle_point_delta=-2.0,
                    coast_time_delta=0.02, trailbrake_overlap_delta=0.01,
                    abs_pct_delta=-0.03, peak_lat_g_delta=0.05,
                    peak_brake_g_delta=-0.1, entry_spd_delta=1.5, exit_spd_delta=-2.0)
    line = format_corner_line(cm)
    assert "vs best" not in line and "than best" not in line and "pts" not in line


# --- consistency (#38) ---------------------------------------------------------

def _consistency_result(**overrides) -> dict:
    result = {
        "ok": True, "n": 8, "lap_time_sigma": 0.4,
        "lap_time_best": 91.2, "lap_time_mean": 91.8,
        "corners": {
            "T4 Village": {"min_spd": {"sigma": 6.0, "n": 8},
                          "brake_point": {"sigma": 9.0, "n": 8}},
            "T7 Anchor": {"min_spd": {"sigma": 1.0, "n": 2}},  # n < 3 -> silent
        },
        "ranking": ["T4 Village", "T7 Anchor"],
    }
    result.update(overrides)
    return result


def test_format_consistency_house_style():
    from simcoach.format import format_consistency
    out = format_consistency(_consistency_result())
    assert out.startswith("Lap time \N{GREEK SMALL LETTER SIGMA} 0.4s over 8 laps "
                          "(best 1:31.200, mean 1:31.800).")
    assert "Least consistent: T4 Village — min speed ±6 km/h, brake point ±9 m" in out
    # T7's only field has n=2 (< 3): the corner is ranked but contributes no clause.
    assert "T7 Anchor" not in out


def test_format_consistency_respects_imperial_units():
    from simcoach.format import format_consistency
    out = format_consistency(_consistency_result(), units="imperial")
    assert "min speed ±4 mph" in out   # 6 km/h * 0.621371 rounds to 4


def test_format_consistency_honest_with_fewer_than_three_laps():
    from simcoach.format import format_consistency
    out = format_consistency({"ok": False, "n": 2})
    assert "2 laps" in out and "consistency" in out.lower()


def test_format_consistency_silent_when_no_corner_clears_the_sample_floor():
    from simcoach.format import format_consistency
    out = format_consistency(_consistency_result(
        corners={"T7 Anchor": {"min_spd": {"sigma": 1.0, "n": 2}}},
        ranking=["T7 Anchor"]))
    assert "Least consistent" not in out
    assert out.startswith("Lap time")


# --- handling / lockup / wheelspin suffixes (#38) ---------------------------

def test_format_corner_line_appends_understeer_suffix():
    cm = _cm(1.0)
    cm.handling = "understeer"
    assert "understeer (steering still winding, lat g flat)" in format_corner_line(cm)


def test_format_corner_line_appends_oversteer_suffix():
    cm = _cm(1.0)
    cm.handling = "oversteer"
    assert "oversteer (countersteer at exit)" in format_corner_line(cm)


def test_format_corner_line_neutral_handling_adds_nothing():
    cm = _cm(1.0)
    cm.handling = "neutral"
    line = format_corner_line(cm)
    assert "understeer" not in line and "oversteer" not in line
    assert line == format_corner_line(_cm(1.0))   # default (None) reads the same


def test_format_corner_line_appends_lockup_and_wheelspin():
    cm = _cm(1.0)
    cm.lockup = True
    cm.wheelspin = True
    line = format_corner_line(cm)
    assert ", lockup" in line and ", wheelspin" in line
