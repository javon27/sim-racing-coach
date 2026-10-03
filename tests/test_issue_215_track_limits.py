"""#215 [RIG 2026-10-02, Laguna Seca]: LMU's mTrackLimitsSteps is a session running
total that steps only on a time-gaining off (+1/+2/+3 per off seen), holds across the
line and drops to 0 at a session change. A RISE versus the previous sample is a
game-confirmed strike, placed where it rose; never a count of offs, never points."""
import dataclasses

import numpy as np

from simcoach.analysis import lap_events
from simcoach.coach_tools import CoachContext
from simcoach.corners import Corner, CornerMap
from simcoach.format import format_events
from simcoach.lmu.smm import sample_from_telem
from simcoach.model_types import LapRecord
from simcoach.store import LapStore
from tests.test_lmu_smm import make_telem, roundtrip

# Laguna Seca, 3602 m: T5 and T10 windows around the rig's two strikes.
_CMAP = CornerMap([Corner(5, "T5", "left", 1700.0, 1800.0, 1900.0),
                   Corner(10, "T10", "right", 3100.0, 3180.0, 3250.0)], length=3602.0)
_N = 1801


def _lap(steps=None) -> LapRecord:
    """A lap 0..3602 m with lap_distance as LMU's ~5 Hz staircase; `steps` is
    [(from_m, value)] -- the counter holds `value` from the first sample at from_m."""
    d = np.linspace(0.0, 3602.0, _N)
    ch = {"lap_distance": d[(np.arange(_N) // 10) * 10], "speed": np.full(_N, 150.0)}
    if steps is not None:
        tl = np.zeros(_N)
        for at, value in steps:
            tl[d >= at] = value
        ch["track_limits_steps"] = tl
    return LapRecord("lmu", "laguna", "car", 90.0, True, 0.0, ch)


def _strikes(rec):
    return [(e["corner_no"], round(e["lap_distance"]), e["detail"]["steps"])
            for e in lap_events(rec, _CMAP, None) if e["kind"] == "track_limits"]


def test_sample_from_telem_maps_track_limits_steps():
    t = make_telem()
    t.mTrackLimitsSteps = 3
    assert sample_from_telem(roundtrip(t), lap_dist=0.0)["track_limits_steps"] == 3.0


def test_each_rise_is_a_strike_placed_where_it_rose_carrying_the_rise():
    # Rig lap 1: 0 -> 1 at 1819 m (T5), 1 -> 4 at 3179 m (T10).
    rec = _lap([(1819.0, 1), (3179.0, 4)])
    got = _strikes(rec)
    assert [(c, s) for c, _, s in got] == [(5, 1), (10, 3)]
    # placed on the frame it rose: within one ~5 Hz scoring step (~20 m here)
    assert abs(got[0][1] - 1819) <= 20 and abs(got[1][1] - 3179) <= 20


def test_a_lap_that_starts_at_4_and_stays_at_4_has_no_strike():
    # Rig lap 2: the session total held at 4 across the line -- not a per-lap zero.
    assert _strikes(_lap([(0.0, 4)])) == []
    # and a later rise on such a lap is its own size, not the total
    assert [s for _, _, s in _strikes(_lap([(0.0, 4), (1819.0, 5)]))] == [1]


def test_a_drop_to_zero_at_a_session_change_is_no_strike():
    assert _strikes(_lap([(0.0, 4), (1200.0, 0)])) == []
    # ...but a rise after it still is (race lap 2: 0 -> 2 at 1287 m)
    assert [(c, s) for c, _, s in _strikes(_lap([(0.0, 4), (900.0, 0), (1287.0, 2)]))] \
        == [(None, 2)]


def test_an_old_lap_without_the_channel_says_nothing_about_strikes():
    rec = _lap()
    assert _strikes(rec) == []
    assert not any("track-limits" in line
                   for line in format_events(lap_events(rec, _CMAP, None), _CMAP))
    # control: the same lap carrying the channel does get a line
    assert any("track-limits" in line for line in format_events(
        lap_events(_lap([(1819.0, 1)]), _CMAP, None), _CMAP))


def test_strike_wording_is_game_confirmed_with_no_points_and_no_off_count():
    lines = format_events(lap_events(_lap([(1819.0, 1), (3179.0, 4)]), _CMAP, None), _CMAP)
    assert len(lines) == 2
    assert lines[1].startswith("LMU counted a track-limits strike at 3,1")
    assert "(T10)" in lines[1] and "rose 3" in lines[1]
    assert not any("point" in line or "penalt" in line for line in lines)


def test_a_strike_in_the_same_corner_corroborates_a_surface_off():
    off = {"kind": "off", "corner_no": 5, "lap_distance": 1800.0, "detail": {}}
    strike = {"kind": "track_limits", "corner_no": 5, "lap_distance": 1819.0,
              "detail": {"steps": 1}}
    elsewhere = dict(strike, corner_no=10, lap_distance=3179.0)
    assert format_events([off, strike], _CMAP)[0] == (
        "T5: off track, and LMU counted a track-limits strike there")
    assert format_events([off, elsewhere], _CMAP)[0] == "T5: off track"
    # a strike with no surface off (painted asphalt run-off) is still reported
    assert format_events([elsewhere], _CMAP)[0].startswith("LMU counted a track-limits")


def test_a_strike_keeps_its_sector_out_of_the_optimal(tmp_path, synthetic_lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                       track="test_track", car="test_car", session_id="s",
                       on_track=False)
    n = len(synthetic_lap.channels["lap_distance"])
    tl = np.zeros(n)
    tl[n // 3:] = 2
    rec = dataclasses.replace(synthetic_lap, channels={**synthetic_lap.channels,
                                                       "track_limits_steps": tl})
    lap_id = store.save(dataclasses.replace(rec, session_id="s", lap_no=2))
    assert len(ctx._incidents(rec, lap_id)) == 1
