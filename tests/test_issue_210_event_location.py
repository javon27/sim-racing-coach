"""#210 (Long Beach rig, 2026-09-27): every spin, stop and hit carries its own place;
a car stopped on track is an event; a scan still behind the line prints no distance.

Laps are built the way LMU stores them: lap_distance a ~5 Hz staircase under ~50 Hz
channels, elapsed_time/lap_start_et with lap_start_et rolling over a few samples
before the end (rig lap 7744), and the #143 impact channels."""
import dataclasses
import math

import numpy as np
import pytest

from simcoach.analysis import (
    _SPEED_KMH_TO_MS,
    _spin_events,
    damage_events,
    is_incident_lap,
    lap_events,
)
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.format import format_damage_line, format_events, place_text
from simcoach.livelap import incident_lines, lap_incidents
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
    if hit_at_m is not None:             # mLastImpactET: the game clock of the hit
        i = int(np.argmax(d >= hit_at_m))
        impact_et[i:] = et[i]
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
# Long Beach 7658 (race lap 11), after the spin at 782 m: four runs under 10 km/h
# (~2.1, 1.9, 2.3, 2.7 s) over a 15.6 s span, rocking back and forth off the wall
# at up to ~40 km/h in between -- gaps over 3 s, but all within ~50 m.
_ROCK = [(1.0, 0.0, 30.0, 0.0), (1.8, 0.0, 0.0, 0.0),
         (2.0, 0.0, 0.0, 40.0), (2.0, 0.0, 40.0, 0.0), (1.6, 0.0, 0.0, 0.0),
         (1.6, 0.0, 0.0, 27.0), (1.6, 0.0, 27.0, 0.0), (2.0, 0.0, 0.0, 0.0),
         (1.0, 0.0, 0.0, 15.0), (1.0, 0.0, 15.0, 0.0), (2.4, 0.0, 0.0, 0.0),
         (4.0, 0.0, 0.0, 100.0)]
_PIT_BOX = [(20.0, 0.0, 60.0, 60.0), (2.0, 0.0, 60.0, 0.0),    # limiter -> the box,
            (25.0, 0.0, 0.0, 0.0), (5.0, 0.0, 0.0, 60.0)]      # 25 s service


def _lines(rec):
    return format_events(lap_events(rec, _CMAP, None), _CMAP)


def _stops(rec):
    return [e for e in lap_events(rec, _CMAP, None) if e["kind"] == "stop"]


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


def test_place_text_edges_past_the_last_corner_before_t1_behind_the_line_and_nan():
    wrap = ", between T2 Right Sweeper and T1 Left Hairpin"
    assert place_text(_CMAP, 3100.0) == "3,100 m" + wrap           # past the last corner
    assert place_text(_CMAP, 5.0) == "5 m" + wrap                  # before T1
    assert place_text(_CMAP, -241.0) == "241 m before the line" + wrap
    assert place_text(_CMAP, math.nan) == "an unknown point"
    assert place_text(None, -241.0) == "241 m before the line"     # no map: distance only


def test_a_spin_with_no_lap_distance_never_prints_nan():
    """Long Beach in-laps 7573/7583: the tail's lap distance is NaN (past the last knot
    `_interp_lap_distance` trusts) and the line read 'spin at nan m'."""
    ev = {"kind": "spin", "corner_no": None, "lap_distance": math.nan,
          "detail": {"speed_from_kmh": 219.0, "speed_min_kmh": 5.0, "turned_deg": 162.0}}
    [line] = format_events([ev], _CMAP)
    assert line.startswith("spin at an unknown point \N{EM DASH} 219 ") and "nan" not in line


def test_a_stop_on_track_is_an_event_with_place_duration_and_speed():
    """Long Beach race lap 4: 132 -> 0 km/h on the brake at ~3047 m, ~7 s, no rotation,
    no hit -- no event kind said so."""
    rec = _lap(_STOP)
    [stop] = _stops(rec)
    assert stop["lap_distance"] == pytest.approx(1520.0, abs=15.0)
    assert stop["detail"]["speed_from_kmh"] == pytest.approx(132.0, abs=1.0)
    assert stop["detail"]["speed_min_kmh"] == 0.0
    # timed off the game clock: ~0.2 s braking under 10 + 7 s parked + ~0.5 s away
    assert 7.0 <= stop["detail"]["stopped_s"] <= 8.0
    assert stop["detail"]["after_contact"] is False
    [line] = [x for x in _lines(rec) if x.startswith("stopped")]
    assert line.startswith("stopped at 1,5")
    assert line.endswith(", between T1 Left Hairpin and T2 Right Sweeper \N{EM DASH} "
                         "132 \N{RIGHTWARDS ARROW} 0 km/h, stuck 7.7 s (dipping under 10 km/h)")
    imperial = format_events(lap_events(rec, _CMAP, None), _CMAP, "imperial")
    assert "82 \N{RIGHTWARDS ARROW} 0 mph, stuck 7.7 s (dipping under 6 mph)" in imperial[0]


def test_rocking_off_a_wall_is_one_stop_timed_over_its_whole_span():
    """Long Beach 7658: four runs under 10 km/h at 780-783 m over 15.6 s printed
    'then stopped 2.7 s' -- only the last run, each fold overwriting the one before."""
    rec = _lap([(40.0, 0.0, 132.0, 132.0), (3.0, 0.0, 132.0, 30.0)] + _ROCK)
    [stop] = _stops(rec)
    # 0.3 s braking under 10 + 17.0 s of runs and rocks + 0.4 s pulling away
    assert stop["detail"]["stopped_s"] == pytest.approx(17.7, abs=0.1)
    assert stop["detail"]["speed_from_kmh"] == pytest.approx(132.0, abs=1.0)
    spun = _lap([(30.0, 0.0, 180.0, 180.0), _SPIN] + _ROCK)
    [line] = _lines(spun)
    assert line.startswith("spin at 1,5")
    assert line.endswith(", turned 200°, then stuck 17.7 s (dipping under 10 km/h)")


def test_the_speed_a_stop_came_down_from_is_the_peak_before_it():
    """A stop braked into with a lift in the middle (98 -> 20, back up to 23, -> 0): the
    first cut's walk back while speed was still falling stopped at the lift and
    printed '23 -> 0'. The peak over the 5 s before the stop is 98."""
    rec = _lap([(40.0, 0.0, 98.0, 98.0), (2.0, 0.0, 98.0, 20.0), (0.5, 0.0, 20.0, 23.0),
                (1.5, 0.0, 23.0, 0.0), (5.0, 0.0, 0.0, 0.0), (3.0, 0.0, 0.0, 60.0)])
    [stop] = _stops(rec)
    assert stop["detail"]["speed_from_kmh"] == pytest.approx(98.0, abs=1.0)


def test_pit_box_grid_and_crawl_are_not_stops_but_a_crash_on_an_in_lap_is():
    # the pit box at limiter speed, on a pit lap and on a lap with no pit record
    for kw in ({"pit": True}, {"pit": None},
               {"partial": "in", "lap_time": None, "valid": False},
               {"partial": "out", "lap_time": None, "valid": False}):
        assert _stops(_lap(_PIT_BOX, **kw)) == [], kw
    # a lap whose buffer began standing (the grid, a standing start)
    assert _stops(_lap([(6.0, 0.0, 0.0, 0.0), (8.0, 0.0, 0.0, 150.0)])) == []
    # ~1.2 s under 10 km/h is a crawl through a slow corner, not a stop
    assert _stops(_lap([(20.0, 0.0, 60.0, 60.0), (1.0, 0.0, 60.0, 0.0),
                        (0.5, 0.0, 0.0, 0.0), (3.0, 0.0, 0.0, 60.0)])) == []
    # braked in from racing speed: a stop even on an in-lap or a pit-flagged lap
    for kw in ({"pit": True}, {"pit": None},
               {"partial": "in", "lap_time": None, "valid": False}):
        assert len(_stops(_lap(_STOP, **kw))) == 1, kw


def test_a_stop_on_the_lap_in_progress_is_reported_not_no_incidents(tmp_path):
    """The driver's complaint: stop on track, ask, and hear nothing happened -- the
    live scan's record is a partial, which the first cut never looked at."""
    rec = _lap([(40.0, 0.0, 135.0, 135.0), (3.0, 0.0, 135.0, 0.0), (7.0, 0.0, 0.0, 0.0)])
    ctx = CoachContext(None, cmap=_CMAP, on_track=True)
    ctx._provisional = lambda: ({"incidents": lap_incidents(rec.channels, _CMAP)}, [],
                                "Lap: game lap 4 so far")
    out = ctx._this_lap_events()
    assert "stopped at 1,5" in out and "135 \N{RIGHTWARDS ARROW} 0 km/h, stuck 7." in out
    assert "no incidents" not in out
    # a pit-box stop on the live lap is not one
    box = _lap(_PIT_BOX)
    ctx._provisional = lambda: ({"incidents": lap_incidents(box.channels, _CMAP)}, [],
                                "Lap: game lap 4 so far")
    assert "stopped" not in ctx._this_lap_events()


def test_a_stop_after_a_spin_folds_into_it_and_one_after_a_hit_says_so():
    """Long Beach quali lap 2: spin at 1921 m, then 1.6 s under 10 km/h at the same
    spot -- one incident, not a spin plus a 'stop' listed ahead of it."""
    spun = _lap([(30.0, 0.0, 180.0, 180.0), _SPIN, (1.0, 0.0, 30.0, 0.0),
                 (5.0, 0.0, 0.0, 0.0), (3.0, 0.0, 0.0, 80.0)])
    [line] = _lines(spun)
    assert line.startswith("spin at 1,5")
    assert line.endswith(", turned 200°, then stuck 5.7 s (dipping under 10 km/h)")
    hit = _lap(_STOP, hit_at_m=1480.0)
    [line] = _lines(hit)
    assert line.startswith("stopped at 1,5")
    assert line.endswith("stuck 7.7 s (dipping under 10 km/h), right after contact there")


def test_a_crash_spin_in_an_in_lap_tail_folds_into_its_stop_at_the_stop_place():
    """Long Beach 7573/7583 (in-laps): wall at 2550 m, spin, stuck -- the spin's own
    slowest sample sat in the tail past the last distance knot (NaN), so it printed
    'spin at nan m' after a separate 'stopped at 2,553 m'."""
    rec = _lap([(30.0, 0.0, 180.0, 180.0), _SPIN, (1.0, 0.0, 30.0, 0.0),
                (5.0, 0.0, 0.0, 0.0)], partial="in", lap_time=None, valid=False)
    v = rec.channels["speed"]
    lo = int(np.flatnonzero(v < 10)[0])                    # the stop starts here
    slowest = lo + int(np.argmin(np.where(v[lo:] > 5.0, v[lo:], np.inf)))
    d = np.cumsum(np.r_[0.0, v[1:] * _SPEED_KMH_TO_MS / _HZ])
    d[slowest:] = np.nan                                  # the spin's slowest: no place
    rec = dataclasses.replace(rec, channels=dict(rec.channels, lap_distance=d))
    [spin] = _spin_events(rec, _CMAP)
    assert math.isnan(spin["lap_distance"])               # the fixture's premise
    [line] = _lines(rec)
    assert line.startswith("spin at 1,5") and "nan" not in line and "unknown" not in line
    assert line.endswith(", turned 200°, then stuck 5.3 s (dipping under 10 km/h)")


def test_a_spin_or_hit_after_the_stop_did_not_cause_it():
    """Cause is order, not only distance: a hit taken pulling away, or a spin on the
    restart, is its own line -- not 'right after contact', not folded."""
    hit = _lap(_STOP, hit_at_m=1530.0)                     # hit while pulling away
    [stop] = _stops(hit)
    assert stop["detail"]["after_contact"] is False
    assert damage_events(hit.channels)[0]["lap_distance"] > stop["lap_distance"]
    rec = _lap([(40.0, 0.0, 132.0, 132.0), (3.0, 0.0, 132.0, 0.0), (7.0, 0.0, 0.0, 0.0),
                (3.0, 0.0, 0.0, 60.0), (2.0, 200.0, 60.0, 5.0), (3.0, 0.0, 5.0, 40.0)])
    lines = _lines(rec)
    assert [x.split(" at ")[0] for x in lines] == ["stopped", "spin"]
    assert "then stuck" not in lines[1]


def _with(rec, **channels):
    return dataclasses.replace(rec, channels=dict(rec.channels, **channels))


def test_a_teleport_to_the_garage_is_no_stop_and_no_spin():
    """Algarve 7690 / Sarthe 7447 / 7416 (partial practice laps): ESC to the garage at
    speed -- 261 -> 0 km/h and a 358 m jump in one sample, then a 5,200 s gap on the
    game clock -- printed a 372-degree spin 'then stuck 5285.3 s'."""
    # the car leaves the garage facing the other way: across the jump the path "turns"
    # 180 degrees while speed "collapses" -- the shape of 7690's fake 372-degree spin
    garage = 125                                           # 2.5 s standing in it
    rec = _lap([(40.0, 0.0, 200.0, 200.0), (garage / _HZ, 180.0, 0.0, 0.0),
                (6.0, 0.0, 0.0, 60.0)], partial="partial", pit=True, lap_time=None,
               valid=False)
    v = rec.channels["speed"]
    i = int(np.flatnonzero(v < 10)[0])                     # the teleport sample
    assert v[i - 1] > 190 and v[i] == 0                    # 200 -> 0 in one sample
    x, z = rec.channels["pos_x"].copy(), rec.channels["pos_z"].copy()
    x[i:] += 300.0                                         # the garage is 300 m away
    z[i:] += 40.0
    et = rec.channels["elapsed_time"].copy()
    et[i + 100:] += 5200.0                                 # the garage wait
    # the spin detector's own clock where the standing car takes no time (7690: the
    # ds/v fallback), so a 3 s window reaches from before the jump to after the garage
    t = np.arange(v.size) / _HZ
    t[i:i + garage] = t[i]
    t[i + garage:] -= (garage - 1) / _HZ
    rec = _with(rec, pos_x=x, pos_z=z, elapsed_time=et, time=t)
    kinds = [e["kind"] for e in lap_events(rec, _CMAP, None)]
    assert "stop" not in kinds and "spin" not in kinds and "parked" not in kinds


def test_a_wall_hit_that_sheds_speed_in_one_sample_is_no_teleport():
    """Long Beach 7583: the wall took 66 -> 23 km/h in one sample with the car moving
    0.4 m, mid-spin -- a one-sample speed drop is a real hit, only a position jump
    teleports. Read as a break, it cut the spin's window and lost the spin."""
    rec = _lap([(30.0, 0.0, 180.0, 180.0), (1.5, 140.0, 180.0, 66.0),   # the wall:
                (0.5, 60.0, 23.0, 5.0), (0.5, 0.0, 5.0, 0.0),           # 66 -> 23
                (5.0, 0.0, 0.0, 0.0), (3.0, 0.0, 0.0, 80.0)])
    v = rec.channels["speed"]
    assert (v[:-1] - v[1:]).max() > 40                    # the fixture's premise
    [line] = _lines(rec)
    assert line.startswith("spin at 1,5") and line.endswith(", then stuck 6.0 s (dipping under 10 km/h)")


def test_a_clock_step_back_between_two_stretches_is_not_one_stop():
    """A session restart steps elapsed_time BACK (32 partial/pit laps): 'et[lo] -
    et[prev_hi] < 3 s' held for a negative step, so two stops merged into one."""
    rec = _lap([(40.0, 0.0, 132.0, 132.0), (3.0, 0.0, 132.0, 0.0), (2.0, 0.0, 0.0, 0.0),
                (0.5, 0.0, 0.0, 40.0), (0.5, 0.0, 40.0, 0.0), (2.0, 0.0, 0.0, 0.0),
                (4.0, 0.0, 0.0, 100.0)])
    v = rec.channels["speed"]
    fast = np.flatnonzero(v >= 10)
    gap = int(fast[np.searchsorted(fast, np.flatnonzero(v < 10)[0])]) + 5  # in the climb
    et = rec.channels["elapsed_time"].copy()
    et[gap:] -= 100.0
    stops = _stops(_with(rec, elapsed_time=et))
    assert len(stops) == 2
    # each 2 s standstill + its own ramps under 10 km/h; merged would be ~5.4 s
    assert [s["detail"]["stopped_s"] for s in stops] == [pytest.approx(2.35, abs=0.1),
                                                         pytest.approx(2.55, abs=0.1)]


def test_a_crawl_after_a_crash_does_not_hide_the_speed_or_the_stop():
    """Long Beach 7667: braked 157 -> 23, crawled ~5 s, stopped -- '23 -> 0'. On the
    live lap the crawl also failed the 80 km/h gate, so it said 'no incidents'."""
    rec = _lap([(40.0, 0.0, 150.0, 150.0), (0.5, 0.0, 150.0, 20.0), (6.0, 0.0, 20.0, 20.0),
                (1.0, 0.0, 20.0, 0.0), (5.0, 0.0, 0.0, 0.0)])
    ctx = CoachContext(None, cmap=_CMAP, on_track=True)
    ctx._provisional = lambda: ({"incidents": lap_incidents(rec.channels, _CMAP)}, [],
                                "Lap: game lap 4 so far")
    out = ctx._this_lap_events()
    assert "150 \N{RIGHTWARDS ARROW} 0 km/h, stuck 5." in out and "no incidents" not in out


def test_a_stored_in_lap_ending_parked_is_not_an_incident_but_the_live_lap_is_stuck():
    """Algarve 7669/7691/7607: braked 187 -> 0 at ~1.2 g and never moved again -- the
    driver parked and went to the garage. Not 'stuck', not an incident. The live lap's
    buffer also ends standing, but there the car is stopped NOW: stuck, an incident."""
    rec = _lap([(40.0, 0.0, 187.0, 187.0), (4.5, 0.0, 187.0, 0.0), (5.0, 0.0, 0.0, 0.0)],
               partial="in", lap_time=None, valid=False)
    [line] = _lines(rec)
    assert line.endswith("\N{EM DASH} 187 \N{RIGHTWARDS ARROW} 0 km/h; the lap ended there")
    assert not is_incident_lap(rec, _CMAP, None)
    # ... and the same after a teleport to the garage at the end (Algarve 7607)
    x = rec.channels["pos_x"].copy()
    x[-5:] += 500.0
    assert _lines(_with(rec, pos_x=x)) == [line]
    # the live lap: stuck, with its time so far
    live = incident_lines(lap_incidents(rec.channels, _CMAP), _CMAP)
    assert live and "187 \N{RIGHTWARDS ARROW} 0 km/h, stuck 5." in live[0]
    assert "lap ended" not in live[0]


def test_a_stop_ended_by_an_escape_is_parked_though_the_car_drives_on_after_it():
    """Sarthe 7449 / Long Beach 7733 (stored partials): parked, ESC to the garage, then
    drove out -- the driving AFTER the teleport made it read 'stuck 2.5 s'."""
    rec = _lap([(40.0, 0.0, 187.0, 187.0), (4.5, 0.0, 187.0, 0.0), (5.0, 0.0, 0.0, 0.0),
                (6.0, 0.0, 0.0, 80.0)],
               partial="partial", pit=True, lap_time=None, valid=False)
    i = int(np.flatnonzero(rec.channels["speed"] < 10)[0]) + 150    # still standing
    x = rec.channels["pos_x"].copy()
    x[i:] += 500.0                                                  # the teleport
    [line] = _lines(_with(rec, pos_x=x))
    assert line.endswith("\N{EM DASH} 187 \N{RIGHTWARDS ARROW} 0 km/h; the lap ended there")


def test_the_crawl_skipped_before_a_stop_is_capped_at_10_s():
    """A car limping home under 30 km/h for a minute came down from its limp, not from
    the 200 km/h it did before the damage, a minute earlier."""
    rec = _lap([(20.0, 0.0, 200.0, 200.0), (1.0, 0.0, 200.0, 25.0), (60.0, 0.0, 25.0, 25.0),
                (1.0, 0.0, 25.0, 0.0), (5.0, 0.0, 0.0, 0.0), (3.0, 0.0, 0.0, 60.0)])
    [stop] = _stops(rec)
    assert stop["detail"]["speed_from_kmh"] == pytest.approx(25.0, abs=0.5)


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


def test_a_spin_still_under_way_is_placed_where_it_last_had_a_distance():
    # #263 (rig lap 8206 replayed as the buffer grows): mid-incident the slowest sample
    # sits past the last lap-distance knot -- "spin at an unknown point" over PTT.
    rec = _lap([(20.0, 0.0, 180.0, 180.0), _SPIN])           # the buffer ends in the spin
    [spin] = [e for e in lap_events(rec, _CMAP, None, live=True) if e["kind"] == "spin"]
    assert math.isfinite(spin["lap_distance"]), spin
    d = np.asarray(rec.channels["lap_distance"], dtype=float)
    assert d[0] + 1000.0 < spin["lap_distance"] <= d.max()   # within the spin's own reach
    assert "unknown point" not in format_events([spin], _CMAP)[0]
