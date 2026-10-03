"""#215 [RIG 2026-10-02, Laguna Seca]: LMU's mTrackLimitsSteps is a session running
total that steps only on a time-gaining off (+1/+2/+3 per off seen), holds across the
line and drops to 0 at a session change. A RISE versus the previous sample is a
game-confirmed strike, placed where it rose; never a count of offs, never points.

Placement runs on the SHIPPED Laguna map: LMU steps the counter 130-280 m after the
off, so both rig strikes (1819 m, 3179 m) land between corner windows."""
import dataclasses

import numpy as np

from simcoach.analysis import is_incident_lap, lap_events, optimal_sectors
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.format import format_events
from simcoach.lmu.smm import sample_from_telem
from simcoach.model_types import LapRecord
from simcoach.store import LapStore
from tests.test_lmu_smm import make_telem, roundtrip

_CMAP = CornerMap.load("simcoach/tracks/weathertech_raceway_laguna_seca.json")
_N = 1801
_D = np.linspace(0.0, _CMAP.length, _N)
_STAIR = _D[(np.arange(_N) // 10) * 10]       # lap_distance: LMU's ~5 Hz staircase
# Rig sample indices: each the FIRST sample of a new staircase step, so the frame it
# rose on (d[i]) and the one before it (d[i-1]) carry different distances.
_T5_OFF, _T11_OFF = 910, 1590                  # ~1813 m, ~3168 m


def _lap(steps=None) -> LapRecord:
    """`steps`: [(sample index, value)] -- the counter holds `value` from that sample."""
    ch = {"lap_distance": _STAIR.copy(), "speed": np.full(_N, 150.0)}
    if steps is not None:
        tl = np.zeros(_N)
        for i, value in steps:
            tl[i:] = value
        ch["track_limits_steps"] = tl
    return LapRecord("lmu", "laguna", "car", 90.0, True, 0.0, ch)


def _strikes(rec):
    return [(e["corner_no"], e["lap_distance"], e["detail"]["steps"])
            for e in lap_events(rec, _CMAP, None) if e["kind"] == "track_limits"]


def test_sample_from_telem_maps_track_limits_steps():
    t = make_telem()
    t.mTrackLimitsSteps = 3
    assert sample_from_telem(roundtrip(t), lap_dist=0.0)["track_limits_steps"] == 3.0


def test_each_rise_is_a_strike_placed_on_the_frame_it_rose_carrying_the_rise():
    # Rig lap 1: 0 -> 1 after T5, 1 -> 4 after T11.
    got = _strikes(_lap([(_T5_OFF, 1), (_T11_OFF, 4)]))
    assert got == [(None, _STAIR[_T5_OFF], 1), (None, _STAIR[_T11_OFF], 3)]
    assert _STAIR[_T5_OFF] != _STAIR[_T5_OFF - 1]          # the test can tell them apart


def test_a_lap_that_starts_at_4_and_stays_at_4_has_no_strike():
    # Rig lap 2: the session total held at 4 across the line -- not a per-lap zero.
    assert _strikes(_lap([(0, 4)])) == []
    # and a later rise on such a lap is its own size, not the total
    assert [s for _, _, s in _strikes(_lap([(0, 4), (_T5_OFF, 5)]))] == [1]


def test_a_drop_to_zero_at_a_session_change_is_no_strike():
    assert _strikes(_lap([(0, 4), (600, 0)])) == []
    # ...but a rise after it still is (race lap 2: 0 -> 2 at 1287 m)
    assert [s for _, _, s in _strikes(_lap([(0, 4), (450, 0), (650, 2)]))] == [2]


def test_an_old_lap_without_the_channel_says_nothing_about_strikes():
    rec = _lap()
    assert _strikes(rec) == []
    assert not any("track-limits" in line
                   for line in format_events(lap_events(rec, _CMAP, None), _CMAP))
    # control: the same lap carrying the channel does get a line
    assert any("track-limits" in line for line in format_events(
        lap_events(_lap([(_T5_OFF, 1)]), _CMAP, None), _CMAP))


def test_strike_wording_is_measured_steps_never_points_or_an_off_count():
    lines = format_events(lap_events(_lap([(_T5_OFF, 1), (_T11_OFF, 4)]), _CMAP, None),
                          _CMAP)
    assert lines == [
        ("LMU counted a track-limits strike at 1,813 m, between T5 and T6 "
         "(its session track-limits counter rose 1 steps; a step's points value isn't known)"),
        ("LMU counted a track-limits strike at 3,168 m, between T11 and T12 "
         "(its session track-limits counter rose 3 steps; a step's points value isn't known)"),
    ]


def test_a_strike_only_lap_is_an_incident_lap():
    assert is_incident_lap(_lap([(_T5_OFF, 1)]), _CMAP, None) is True
    assert is_incident_lap(_lap([(0, 4)]), _CMAP, None) is False


def test_a_strike_keeps_its_sector_out_of_the_optimal(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                       track="test_track", car="test_car", session_id="s",
                       on_track=False)
    n = len(synthetic_lap.channels["lap_distance"])
    tl = np.zeros(n)
    tl[n // 6:] = 2                                       # rises at ~500 m: S1
    struck = dataclasses.replace(synthetic_lap, channels={**synthetic_lap.channels,
                                                          "track_limits_steps": tl})
    clean = dataclasses.replace(synthetic_lap, channels={**synthetic_lap.channels,
                                                         "track_limits_steps": tl * 0})
    laps = []
    for key, rec, sectors in (("struck", struck, [25.0, 30.0, 26.0]),
                              ("clean", clean, [26.0, 30.5, 26.5])):
        lap_id = store.save(dataclasses.replace(rec, session_id="s", lap_no=len(laps) + 2))
        laps.append({"key": key, "sectors": sectors,
                     "incidents": ctx._incidents(rec, lap_id)})
    opt = optimal_sectors(laps, [1000.0, 2000.0])
    assert opt["best"][0] == (26.0, "clean")
    assert opt["dropped"] == [(0, 25.0, "struck")]
    assert opt["best"][1] == (30.0, "struck")             # only its own sector is out
