import math

from simcoach.analysis import CornerMetrics, analyze_lap
from simcoach.corners import CornerMap
from simcoach.format import corner_descriptor, format_corner_line, format_events, format_laptime


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


# --- tire-deg trend (#38 P1.6) --------------------------------------------------

def _trend_result(**overrides) -> dict:
    result = {
        "ok": True, "n": 8,
        "temp_slope": {"rl": 2.0}, "temp_mean": {"rl": 96.0},
        "press_slope": {}, "wear_pct_per_lap": {"rl": 2.1},
        "pace_slope_s": 0.15, "hottest": "rl", "most_worn": "rl",
    }
    result.update(overrides)
    return result


def test_format_tire_trend_house_style_and_deg_limited():
    from simcoach.format import format_tire_trend
    out = format_tire_trend(_trend_result())
    assert out.startswith("Over 8 laps: pace +0.15 s/lap; ")
    assert "RL wear +2.1%/lap (most worn)" in out
    assert "RL running hottest (96 °C mean)" in out
    assert "pressures steady" in out
    assert out.endswith("deg-limited")


def test_format_tire_trend_not_tire_limited_when_wear_steady():
    from simcoach.format import format_tire_trend
    out = format_tire_trend(_trend_result(
        temp_slope={}, temp_mean={}, wear_pct_per_lap={}, hottest=None, most_worn=None))
    assert "temps and wear steady" in out
    assert out.endswith("not tire-limited")


def test_format_tire_trend_silent_suffix_when_pace_steady():
    from simcoach.format import format_tire_trend
    out = format_tire_trend(_trend_result(pace_slope_s=0.01))
    assert "pace steady" in out
    assert "deg-limited" not in out and "not tire-limited" not in out


def test_format_tire_trend_respects_imperial_units():
    from simcoach.format import format_tire_trend
    out = format_tire_trend(_trend_result(), units="imperial")
    assert "205 °F mean" in out   # 96 C -> 204.8 F, rounds to 205


def test_format_tire_trend_honest_with_fewer_than_three_laps():
    from simcoach.format import format_tire_trend
    out = format_tire_trend({"ok": False, "n": 2})
    assert "2 laps" in out and "tire trend" in out.lower()


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


# --- traction-circle (G-G) utilization (#38 P1.4) ----------------------------

def test_format_corner_line_shows_grip_envelope_pct():
    cm = _cm(1.0)
    cm.gg_used = 0.92
    line = format_corner_line(cm)
    assert "grip 92% of envelope" in line


def test_format_corner_line_shows_dead_middle_over_the_report_floor():
    cm = _cm(1.0)
    cm.gg_used = 0.8
    cm.gg_dead_middle = 0.6
    line = format_corner_line(cm)
    assert "60% of corner under half grip (inputs not combined)" in line


def test_format_corner_line_silent_on_dead_middle_under_the_report_floor():
    cm = _cm(1.0)
    cm.gg_used = 0.8
    cm.gg_dead_middle = 0.1
    line = format_corner_line(cm)
    assert "grip 80% of envelope" in line
    assert "under half grip" not in line


def test_format_corner_line_silent_without_gg_fields():
    line = format_corner_line(_cm(1.0))
    assert "envelope" not in line and "grip" not in line


def test_format_lap_facts_shows_grip_envelope_line():
    from simcoach.analysis import LapAnalysis
    from simcoach.format import format_lap_facts
    a = LapAnalysis(lap_time=90.0, corners=[], has_reference=False,
                    gg_envelope=1.85, gg_used=0.72)
    out = format_lap_facts(a)
    assert "Grip envelope ~1.9 g (observed); this lap used 72% of it in the corners." in out


def test_format_lap_facts_silent_without_gg_fields():
    from simcoach.analysis import LapAnalysis
    from simcoach.format import format_lap_facts
    a = LapAnalysis(lap_time=90.0, corners=[], has_reference=False)
    out = format_lap_facts(a)
    assert "envelope" not in out


# --- gear/RPM sanity (#38 P1.7) ----------------------------------------------

def test_format_corner_line_appends_gear_min():
    cm = _cm(1.0)
    cm.gear_min = 2
    assert "2nd gear at min speed" in format_corner_line(cm)


def test_format_corner_line_appends_bog_and_short_shift():
    cm = _cm(1.0)
    cm.bog = True
    cm.short_shift = True
    line = format_corner_line(cm)
    assert ", bogged on exit" in line and ", short-shifted" in line


def test_format_corner_line_silent_without_gear_fields():
    line = format_corner_line(_cm(1.0))
    assert "gear at min speed" not in line
    assert "bogged" not in line and "short-shifted" not in line


# --- input-shape facts (#38 P1.8) ---------------------------------------

def test_format_corner_line_renders_brake_release_distance():
    cm = _cm(1.0)
    cm.brake_release_m = 45.0
    assert "released brake over 45 m" in format_corner_line(cm)


def test_format_corner_line_renders_brake_release_delta_over_floor():
    cm = _cm(1.0)
    cm.brake_release_m = 45.0
    cm.brake_release_m_delta = 25.0   # clears _DELTA_DIST_M (3.0)
    assert "released brake over 45 m (+25 m vs best)" in format_corner_line(cm)


def test_format_corner_line_silent_on_brake_release_delta_under_floor():
    cm = _cm(1.0)
    cm.brake_release_m = 45.0
    cm.brake_release_m_delta = 1.0   # under _DELTA_DIST_M
    line = format_corner_line(cm)
    assert "released brake over 45 m" in line
    assert "vs best" not in line


def test_format_corner_line_silent_without_brake_release():
    line = format_corner_line(_cm(1.0))
    assert "released brake" not in line


def test_format_corner_line_shows_brake_dumped_at_the_floor():
    cm = _cm(1.0)
    cm.brake_release_m = 20.0
    cm.brake_release_steep_frac = 0.7
    assert ", brake dumped" in format_corner_line(cm)


def test_format_corner_line_silent_on_dumped_under_the_floor():
    cm = _cm(1.0)
    cm.brake_release_m = 20.0
    cm.brake_release_steep_frac = 0.5
    assert "dumped" not in format_corner_line(cm)


def test_format_corner_line_renders_brake_reversals_at_the_floor():
    cm = _cm(1.0)
    cm.brake_reversals = 2
    assert ", 2 brake reversals" in format_corner_line(cm)


def test_format_corner_line_silent_on_one_brake_reversal():
    cm = _cm(1.0)
    cm.brake_reversals = 1
    assert "brake reversals" not in format_corner_line(cm)


def test_format_corner_line_renders_brake_reversals_best_with_delta():
    cm = _cm(1.0)
    cm.brake_reversals = 3
    cm.brake_reversals_delta = 3   # 3 more than the reference -> ref had 0
    assert ", 3 brake reversals (best 0)" in format_corner_line(cm)


def test_format_corner_line_renders_throttle_stabs_at_the_floor():
    cm = _cm(1.0)
    cm.throttle_reversals = 2
    assert ", 2 throttle stabs" in format_corner_line(cm)


def test_format_corner_line_silent_on_one_throttle_stab():
    cm = _cm(1.0)
    cm.throttle_reversals = 1
    assert "throttle stabs" not in format_corner_line(cm)


def test_format_corner_line_renders_steering_sawing_at_the_floor():
    cm = _cm(1.0)
    cm.steer_reversals = 3
    assert ", steering sawing (3)" in format_corner_line(cm)


def test_format_corner_line_silent_on_two_steer_reversals():
    cm = _cm(1.0)
    cm.steer_reversals = 2
    assert "sawing" not in format_corner_line(cm)


# --- event lines (#108 items 3+4) -------------------------------------------

_CMAP = CornerMap.load("simcoach/tracks/test_track.json")


def _ev(kind, corner_no, **detail):
    return {"kind": kind, "corner_no": corner_no, "lap_distance": 1000.0, "detail": detail}


def test_format_events_spin_line():
    ev = _ev("spin", 2, speed_from_kmh=178.0, speed_min_kmh=32.0, turned_deg=195.0)
    assert format_events([ev], _CMAP) == [
        "T2 Right Sweeper: spin — 178 → 32 km/h, turned 195°"]


def test_format_events_spin_line_imperial():
    ev = _ev("spin", 2, speed_from_kmh=178.0, speed_min_kmh=32.0, turned_deg=195.0)
    assert format_events([ev], _CMAP, "imperial") == [
        "T2 Right Sweeper: spin — 111 → 20 mph, turned 195°"]


def test_format_events_spin_off_the_map_has_no_corner_tag():
    ev = _ev("spin", None, speed_from_kmh=178.0, speed_min_kmh=32.0, turned_deg=195.0)
    assert format_events([ev], _CMAP) == [
        "spin — 178 → 32 km/h, turned 195°"]


def test_format_events_lockup_and_wheelspin_lines():
    assert format_events([_ev("lockup", 1), _ev("wheelspin", 2)], _CMAP) == [
        "T1 Left Hairpin: lockup", "T2 Right Sweeper: wheelspin"]


def test_format_events_off_line():
    assert format_events([_ev("off", 1, past_edge_m=1.4)], _CMAP) == [
        "T1 Left Hairpin: off track"]


def test_format_events_wear_step_line():
    ev = _ev("wear_step", 1, wheel="fr", step_pct=1.2, baseline_pct=0.3)
    assert format_events([ev], _CMAP) == [
        "FR wear step 1.2% at T1 Left Hairpin (stint median 0.3%/lap)"]


# --- corner_descriptor (#108 item 12: spoken corner locator) ----------------

# test_track.json (loaded above as _CMAP) carries no `length` -- the shipped file
# omits it -- but the synthetic lap it's paired with (conftest.synthetic_lap) is a
# real 3000 m circuit. Build the same two corners WITH a length so thirds/wrap
# math has something to work with.
_CMAP3000 = CornerMap.from_data({
    "length": 3000,
    "corners": [
        {"number": 1, "name": "Left Hairpin", "direction": "left",
         "start_dist": 800, "apex_dist": 1000, "end_dist": 1200},
        {"number": 2, "name": "Right Sweeper", "direction": "right",
         "start_dist": 2000, "apex_dist": 2200, "end_dist": 2400},
    ],
})


def test_corner_descriptor_first_corner_full_sentence(synthetic_lap):
    # apex 1000/3000 = exactly a third -> "early"; first in the map -> ordinal
    # phrase too; hairpin direction reads "the left-hand hairpin"; gap wraps
    # past the S/F line from corner 2's end (2400) to corner 1's start (800)
    # = 1400 m -> "the long straight".
    analysis = analyze_lap(synthetic_lap, _CMAP3000)
    out = corner_descriptor(_CMAP3000, 1, analysis=analysis)
    assert out == ("Turn 1, early in the lap, the first corner, "
                    "the left-hand hairpin after the long straight")


def test_corner_descriptor_last_corner_contains_expected_parts(synthetic_lap):
    analysis = analyze_lap(synthetic_lap, _CMAP3000)
    out = corner_descriptor(_CMAP3000, 2, analysis=analysis)
    assert "Turn 2" in out
    assert "the last corner" in out
    assert "the slow right" in out          # min ~94 km/h -> "slow" (<110)
    assert "after the long straight" in out  # 2000 - 1200 = 800 m gap


def test_corner_descriptor_sector_bounds_pins_apex_not_start(synthetic_lap):
    # Splits chosen so corner 1's apex (1000) sits AT the S1/S2 split (sector 1,
    # by apex) while its start_dist (800) would read sector 1 either way -- the
    # split that actually distinguishes apex-based from start-based sectoring is
    # corner 2 (apex 2200 > both splits -> sector 3).
    analysis = analyze_lap(synthetic_lap, _CMAP3000)
    out1 = corner_descriptor(_CMAP3000, 1, analysis=analysis, sector_bounds=[1000, 2000])
    out2 = corner_descriptor(_CMAP3000, 2, analysis=analysis, sector_bounds=[1000, 2000])
    assert "sector 1" in out1
    assert "sector 3" in out2


def test_corner_descriptor_no_analysis_has_no_speed_class():
    out = corner_descriptor(_CMAP3000, 1, analysis=None)
    # Spoken, a bare "left after the straight" means "immediately after": the
    # geometry-only form names the corner as a left-hander instead.
    assert "the left-hander" in out
    assert "hairpin" not in out
    assert not any(w in out for w in ("slow", "medium", "fast", "flat-out"))


def test_corner_descriptor_close_gap_reads_straight_after():
    cmap = CornerMap.from_data({"corners": [
        {"number": 1, "name": "A", "direction": "left",
         "start_dist": 0, "apex_dist": 20, "end_dist": 40},
        {"number": 2, "name": "B", "direction": "right",
         "start_dist": 90, "apex_dist": 110, "end_dist": 130},
    ]})
    assert "straight after Turn 1" in corner_descriptor(cmap, 2)


def test_corner_descriptor_mid_gap_reads_after_the_straight():
    cmap = CornerMap.from_data({"corners": [
        {"number": 1, "name": "A", "direction": "left",
         "start_dist": 0, "apex_dist": 20, "end_dist": 40},
        {"number": 2, "name": "B", "direction": "right",
         "start_dist": 340, "apex_dist": 360, "end_dist": 380},
    ]})
    assert "after the straight" in corner_descriptor(cmap, 2)
