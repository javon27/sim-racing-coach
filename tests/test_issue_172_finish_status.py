"""#172 (rig 2026-10-02, Laguna Seca, offline race): the player's mFinishStatus goes
0 -> 1 on the frame he crosses the line at the flag (lap 10 -> 11) and stays 1 for the
whole cool-down lap, on track and out of the pits. On that lap -- on track, a fresh race
snapshot, no result row yet, mSession still race -- the race tools called it live."""
import pytest

from simcoach.coach_tools import CoachContext
from simcoach.lmu.live import read_field
from simcoach.lmu.results import ResultLap, ResultSession
from simcoach.lmu.rf2structs import VehicleScoringInfoV01, rF2Scoring
from simcoach.model_types import LapRecord, RaceSample
from simcoach.race import LapFacts, build_snapshot
from simcoach.store import LapStore


def _frame(finish_status, session=12):
    """The cool-down frame: race session, the player has 10 laps done (on lap 11)."""
    s = rF2Scoring()
    s.mScoringInfo.mSession = session
    s.mScoringInfo.mMaxLaps = 10
    s.mScoringInfo.mNumVehicles = 1
    v = VehicleScoringInfoV01()
    v.mID, v.mPlace, v.mTotalLaps, v.mIsPlayer = 1, 1, 10, True
    v.mDriverName, v.mVehicleName, v.mVehicleClass = b"You", b"You Entry", b"GT3"
    v.mFinishStatus = finish_status
    s.mVehicles[0] = v
    return s


def _ctx(tmp_path, synthetic_lap, finish_status, session=12, on_track=True,
         logged=False, reader=None):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    for i in range(3):   # race laps, no result row unless `logged`
        store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "lag",
                                "timestamp": float(i), "position": 1, "phase": "race"}))
    if logged:
        store.save_result_session(ResultSession(
            sim="lmu", track=synthetic_lap.track, car=synthetic_lap.car, car_class="GT3",
            date_ts=0.0, session_type="race", position=1, class_position=1, best_lap=82.0,
            finish_status="Finished Normally", incidents=[], penalties=[],
            laps=[ResultLap(lap_no=1, lap_time=82.0, sectors=[], valid=True)]),
            session_id="lag")
    rs = RaceSample(class_position=1, class_ahead=None, class_gap_ahead=None,
                    class_behind=None, class_gap_behind=None, max_laps=10)
    snap = build_snapshot([LapFacts(lap_no=n, lap_time=82.0, clean=True, position=1,
                                    race=rs) for n in (9, 10)], "lag")
    frame = _frame(finish_status, session)
    return CoachContext(store, track=synthetic_lap.track, car="C", session_id="lag",
                        on_track=on_track, race=snap,
                        live_field=reader or (lambda: read_field(frame)))


def test_the_frame_carries_the_finish_status():
    assert read_field(_frame(1)).session.finish_status == 1


def test_still_racing_is_not_called_over(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap, 0)
    assert "race is over" not in ctx.get_race_result()
    for tool in ("get_race_state", "get_strategy"):
        assert "race is over" not in ctx.dispatch(tool, {}), tool


@pytest.mark.parametrize("status,said,never", [
    (1, "You have finished (taken the chequered flag)", "retired"),
    (2, "You have retired (DNF)", "finished"),
])
def test_the_cool_down_lap_is_over(tmp_path, synthetic_lap, status, said, never):
    ctx = _ctx(tmp_path, synthetic_lap, status)
    out = ctx.get_race_result()
    assert said in out and "so this race is over" in out
    assert "not a pit stop" in out and "no pit-stop or strategy offers" in out
    assert never not in out
    for tool in ("get_race_state", "get_strategy"):
        out = ctx.dispatch(tool, {})
        assert out.startswith("This race is over: " + said[0].lower() + said[1:]), tool
        assert "No race state, pit stops or strategy to offer" in out
        assert never not in out


# --- review round 1: the gates and the precedence -----------------------------

def test_a_finish_status_outside_a_race_is_not_over(tmp_path, synthetic_lap):
    # mSession 5 is qualifying: a 1 there ends a qualifying run, not a race.
    ctx = _ctx(tmp_path, synthetic_lap, 1, session=5)
    for tool in ("get_race_state", "get_strategy"):
        assert "race is over" not in ctx.dispatch(tool, {}), tool


def test_off_track_falls_through_to_no_race_live(tmp_path, synthetic_lap):
    out = _ctx(tmp_path, synthetic_lap, 1, on_track=False).get_race_result()
    assert "No race is live now" in out and "You have" not in out


def test_the_result_row_leads_the_live_status(tmp_path, synthetic_lap):
    # #208: the result log leads the status.
    out = _ctx(tmp_path, synthetic_lap, 1, logged=True).get_race_result()
    assert "This race is FINISHED (the result log" in out
    assert "You have finished" not in out


def test_a_dq_is_disqualified_never_finished(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap, 3)
    out = ctx.get_race_result()
    assert "You have been disqualified (DQ)" in out and "finished" not in out
    for tool in ("get_race_state", "get_strategy"):
        out = ctx.dispatch(tool, {})
        assert out.startswith("This race is over: you have been disqualified (DQ)"), tool
        assert "finished" not in out


def test_an_unknown_finish_status_is_not_over(tmp_path, synthetic_lap):
    ctx = _ctx(tmp_path, synthetic_lap, 4)
    assert "race is over" not in ctx.get_race_result()
    for tool in ("get_race_state", "get_strategy"):
        assert "race is over" not in ctx.dispatch(tool, {}), tool


def test_a_failing_live_read_is_not_over(tmp_path, synthetic_lap):
    def boom():
        raise OSError("shared memory gone")
    ctx = _ctx(tmp_path, synthetic_lap, 1, reader=boom)
    assert "race is over" not in ctx.get_race_result()
    assert "race is over" not in ctx.get_race_state()
    assert "race is over" not in ctx.get_strategy()
