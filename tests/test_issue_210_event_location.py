"""#210 (Long Beach rig, 2026-09-27): every spin, stop and hit carries its own place;
a car stopped on track is an event; a scan still behind the line prints no distance.

Laps are built the way LMU stores them: lap_distance a ~5 Hz staircase under ~50 Hz
channels, elapsed_time/lap_start_et with lap_start_et rolling over a few samples
before the end (rig lap 7744), and the #143 impact channels."""
import dataclasses

import numpy as np
import pytest

from simcoach.analysis import _SPEED_KMH_TO_MS, damage_events, lap_events
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.format import format_damage_line, format_events
from simcoach.livelap import lap_incidents
from simcoach.model_types import LapRecord

# T1 Left Hairpin 800-1200 m, T2 Right Sweeper 2000-2400 m
_CMAP = CornerMap.load("simcoach/tracks/test_track.json")
_HZ = 50.0


def _lap(segments, d0=0.0, hit_at_m=None, **kw):
    """segments: (duration_s, path turn deg, v_start_kmh, v_end_kmh). A hit (front-left,
    magnitude 900) lands at the first sample past `hit_at_m`."""
    v_parts, h_parts, heading = [], [], 0.0
    for dur, turn, v0, v1 in segments:
        k = max(2, round(dur * _HZ))
        v_parts.append(np.linspace(v0, v1, k))
        h_parts.append(heading + np.linspace(0.0, turn, k))
        heading += turn
    v = np.concatenate(v_parts)
    h = np.radians(np.concatenate(h_parts))
    n = v.size
    ds = np.r_[0.0, v[1:] * _SPEED_KMH_TO_MS / _HZ]
    d = d0 + np.cumsum(ds)
    et = 400.0 + np.arange(n) / _HZ
    lset = np.full(n, 400.0)
    lset[-3:] = et[-3] - 0.02            # LMU rolls the lap over before the wrap
    impact_et = np.zeros(n)
    if hit_at_m is not None:
        impact_et[int(np.argmax(d >= hit_at_m)):] = 450.0
    ch = {
        "lap_distance": d[(np.arange(n) // 10) * 10],     # scoring-rate staircase
        "speed": v,
        "pos_x": np.cumsum(ds * np.cos(h)), "pos_z": np.cumsum(ds * np.sin(h)),
        "elapsed_time": et, "lap_start_et": lset,
        "last_impact_et": impact_et, "last_impact_mag": np.full(n, 900.0),
        "last_impact_x": np.full(n, 1.0), "last_impact_z": np.full(n, -1.5),
    }
    fields = {"lap_time": float(n / _HZ), "valid": True, "phase": "race", "lap_no": 3,
              "pit": False, **kw}
    return LapRecord(sim="lmu", track="test_track", car="test_car", timestamp=0.0,
                     channels=ch, **fields)


_SPIN = (2.0, 200.0, 180.0, 30.0)                          # test_analysis' spin shape
_STOP = [(40.0, 0.0, 132.0, 132.0), (3.0, 0.0, 132.0, 0.0),  # 132 -> 0 on the brake,
         (7.0, 0.0, 0.0, 0.0), (5.0, 0.0, 0.0, 100.0)]       # 7 s parked, then away


def _lines(rec):
    return format_events(lap_events(rec, _CMAP, None), _CMAP)


def test_a_spin_between_corners_is_placed_between_them():
    """Long Beach race lap 3: the spin at 2821 m (T11 exit) had no tag and was read
    as the T1 incident listed above it."""
    rec = _lap([(30.0, 0.0, 180.0, 180.0), _SPIN, (4.0, 0.0, 30.0, 30.0)])
    spins = [line for line in _lines(rec) if line.startswith("spin")]
    assert len(spins) == 1
    assert spins[0].startswith("spin at 1,5")
    assert ", between T1 Left Hairpin and T2 Right Sweeper \N{EM DASH} 180 " in spins[0]


def test_a_spin_inside_a_corner_window_names_that_corner_and_its_distance():
    rec = _lap([(20.0, 0.0, 180.0, 180.0), _SPIN, (4.0, 0.0, 30.0, 30.0)])
    [line] = [x for x in _lines(rec) if x.startswith("spin")]
    assert line.startswith("spin at 1,0") and " m (T1 Left Hairpin) \N{EM DASH} " in line


def test_an_impact_is_placed_on_the_corner_map():
    rec = _lap([(45.0, 0.0, 135.0, 135.0)], hit_at_m=1500.0)
    events = damage_events(rec.channels)
    assert len(events) == 1
    line = format_damage_line(events, {}, _CMAP)
    assert line.startswith("Contact: 1 impact at 1,5")
    assert " m, between T1 Left Hairpin and T2 Right Sweeper (front-left), magnitude 900." \
        in line


def test_a_stop_on_track_is_an_event_with_place_duration_and_speed():
    """Long Beach race lap 4: 132 -> 0 km/h on the brake at ~3047 m, ~7 s, no rotation,
    no hit -- no event kind said so."""
    rec = _lap(_STOP)
    [stop] = [e for e in lap_events(rec, _CMAP, None) if e["kind"] == "stop"]
    assert stop["lap_distance"] == pytest.approx(1520.0, abs=15.0)
    assert stop["detail"]["speed_from_kmh"] == pytest.approx(132.0, abs=1.0)
    assert stop["detail"]["speed_min_kmh"] == 0.0
    # timed off the game clock: ~0.2 s braking under 10 + 7 s parked + ~0.5 s away
    assert 7.0 <= stop["detail"]["stopped_s"] <= 8.0
    assert stop["detail"]["after_contact"] is False
    [line] = [x for x in _lines(rec) if x.startswith("stopped")]
    assert line.startswith("stopped at 1,5")
    assert line.endswith(", between T1 Left Hairpin and T2 Right Sweeper \N{EM DASH} "
                         "132 \N{RIGHTWARDS ARROW} 0 km/h, 7.7 s under 10 km/h")
    imperial = format_events(lap_events(rec, _CMAP, None), _CMAP, "imperial")
    assert "82 \N{RIGHTWARDS ARROW} 0 mph, 7.7 s under 6 mph" in imperial[0]


def test_pit_lane_grid_and_short_stops_are_not_stops():
    def stops(rec):
        return [e for e in lap_events(rec, _CMAP, None) if e["kind"] == "stop"]
    assert len(stops(_lap(_STOP))) == 1                        # the control
    assert stops(_lap(_STOP, pit=True)) == []                  # a pit lap
    for kind in ("out", "in", "partial"):                      # box, grid, cool-down
        assert stops(_lap(_STOP, partial=kind, lap_time=None, valid=False)) == []
    # a lap whose buffer began standing (a standing start from the grid)
    assert stops(_lap([(6.0, 0.0, 0.0, 0.0), (8.0, 0.0, 0.0, 150.0)])) == []
    # ~1.2 s under 10 km/h is a crawl through a slow corner, not a stop
    assert stops(_lap([(20.0, 0.0, 60.0, 60.0), (1.0, 0.0, 60.0, 0.0),
                       (0.5, 0.0, 0.0, 0.0), (3.0, 0.0, 0.0, 60.0)])) == []


def test_a_stop_after_a_spin_folds_into_it_and_one_after_a_hit_says_so():
    """Long Beach quali lap 2: spin at 1921 m, then 1.6 s under 10 km/h at the same
    spot -- one incident, not a spin plus a 'stop' listed ahead of it."""
    spun = _lap([(30.0, 0.0, 180.0, 180.0), _SPIN, (1.0, 0.0, 30.0, 0.0),
                 (5.0, 0.0, 0.0, 0.0), (3.0, 0.0, 0.0, 80.0)])
    [line] = _lines(spun)
    assert line.startswith("spin at 1,5")
    assert line.endswith(", turned 200°, then stopped 5.7 s under 10 km/h")
    hit = _lap(_STOP, hit_at_m=1480.0)
    [line] = _lines(hit)
    assert line.startswith("stopped at 1,5")
    assert line.endswith("7.7 s under 10 km/h, right after contact there")


def test_a_scan_still_behind_the_line_prints_no_negative_distance():
    """An out-lap's buffer before the line reads lap_distance -400 -> -241: 'whole lap
    scanned to -241 m' is no measured reach."""
    rec = _lap([(4.0, 0.0, 144.0, 144.0)], d0=-400.0, partial="out")
    scan = lap_incidents(rec.channels, _CMAP)
    assert scan["scanned_to"] < 0                              # the fixture's premise
    ctx = CoachContext(None, cmap=_CMAP, on_track=True)
    ctx._provisional = lambda: ({"incidents": scan}, [], "Lap: game lap 1 so far")
    out = ctx._this_lap_events()
    assert "scanned to" not in out and "-2" not in out
    # the same scan once past the line still says how far it reached
    fwd = dataclasses.replace(rec, channels=dict(
        rec.channels, lap_distance=rec.channels["lap_distance"] + 500.0))
    ctx._provisional = lambda: ({"incidents": lap_incidents(fwd.channels, _CMAP)}, [],
                                "Lap: game lap 1 so far")
    assert "no incidents so far (whole lap scanned to 2" in ctx._this_lap_events()
