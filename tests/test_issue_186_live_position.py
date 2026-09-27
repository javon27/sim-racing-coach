"""#186 (rig, Sarthe #276, mid race lap 4): the driver had just passed for P2 and the
coach said "As of lap 3, you're actually P3 in class". The race tools overlay the LIVE
position, read from one scoring frame as the tool answers, onto the per-lap snapshot."""
import pytest

from simcoach.coach_tools import CoachContext
from simcoach.lmu.live import empty_snapshot, read_field
from simcoach.lmu.rf2structs import VehicleScoringInfoV01, rF2Scoring
from simcoach.model_types import RaceSample
from simcoach.race import LapFacts, build_snapshot
from simcoach.store import LapStore


def _veh(**kw) -> VehicleScoringInfoV01:
    v = VehicleScoringInfoV01()
    for k, val in kw.items():
        setattr(v, k, val.encode() if isinstance(val, str) else val)
    return v


def _scoring(player_place, *, total_laps=3, session=12):
    """A multiclass race frame as smm reads it: two Hypercars lead overall, then GT3s.
    The player is a GT3 at overall `player_place`; the other GT3s fill the places
    around him, so his class place is his overall place minus the two Hypercars."""
    cars = [_veh(mID=1, mPlace=1, mDriverName="H. One", mVehicleName="Ferrari 499P",
                 mVehicleClass="Hypercar", mTotalLaps=total_laps),
            _veh(mID=2, mPlace=2, mDriverName="H. Two", mVehicleName="Toyota GR010",
                 mVehicleClass="Hypercar", mTotalLaps=total_laps, mTimeBehindNext=1.0)]
    others = [p for p in range(3, 8) if p != player_place]
    for cid, place in enumerate(others, start=10):
        cars.append(_veh(mID=cid, mPlace=place, mDriverName=f"G. {place}",
                         mVehicleName=f"GT3 Entry {place}", mVehicleClass="GT3",
                         mTotalLaps=total_laps, mTimeBehindNext=1.5))
    cars.append(_veh(mID=5, mPlace=player_place, mDriverName="You",
                     mVehicleName="Corvette Z06 GT3.R", mVehicleClass="GT3",
                     mTotalLaps=total_laps, mIsPlayer=True, mTimeBehindNext=0.8))
    s = rF2Scoring()
    s.mScoringInfo.mSession = session
    s.mScoringInfo.mMaxLaps = 24
    s.mScoringInfo.mNumVehicles = len(cars)
    for i, v in enumerate(cars):
        s.mVehicles[i] = v
    return s


def _snap_lap3():
    """Snapshot as of lap 3: P5 overall, P3 in class."""
    rs = RaceSample(class_position=3, class_ahead="G. 4 (GT3 Entry 4)", class_gap_ahead=1.5,
                    class_behind="G. 6 (GT3 Entry 6)", class_gap_behind=1.5, max_laps=24)
    stint = [LapFacts(lap_no=n, lap_time=100.0, clean=True, position=5, race=rs)
             for n in (2, 3)]
    return build_snapshot(stint, "s")


def _ctx(tmp_path, reader):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    return CoachContext(store, on_track=True, race=_snap_lap3(), live_field=reader)


def test_the_live_class_position_is_said_not_the_lap_old_one(tmp_path):
    """The issue case: snapshot at lap 3 = P3 in class; mid lap 4 he is P2 in class
    (overall mPlace 4 -- two Hypercars ahead)."""
    frame = _scoring(player_place=4)
    field = read_field(frame)
    assert next(c for c in field.cars if c.is_player).place == 4   # overall != class
    out = _ctx(tmp_path, lambda: read_field(frame)).dispatch("get_race_state", {})
    assert "Position now (live): P4 overall, P2 in class." in out
    assert "P3 in class" not in out and "P5 overall" not in out
    # The neighbours and their gap trends are the lap's, and say so.
    assert "Car ahead in class at the end of lap 3: G. 4 (GT3 Entry 4), 1.5 s ahead" in out
    assert out.startswith("As of lap 3 (race).")


def test_live_equal_to_the_snapshot_leaves_the_answer_unchanged(tmp_path):
    frame = _scoring(player_place=5)          # P5 overall, P3 in class: as at lap 3
    plain = _ctx(tmp_path, None).dispatch("get_race_state", {})
    assert _ctx(tmp_path, lambda: read_field(frame)).dispatch("get_race_state", {}) == plain
    assert "Position: P5 overall, P3 in class." in plain


def _raise():
    raise OSError("shared memory mapping is stale: LMU is not running")


@pytest.mark.parametrize("reader", [
    _raise,                                             # the read fails
    empty_snapshot,                                     # LMU gone / garage (#147): not live
    lambda: read_field(_scoring(4, total_laps=7)),      # another lap: not this snapshot's race
    lambda: read_field(_scoring(4, session=5)),         # qualifying, not a race
    lambda: read_field(_scoring(0)),                    # player unplaced (mPlace 0)
], ids=["raises", "not-live", "other-lap", "not-race", "unplaced"])
def test_no_usable_live_frame_falls_back_to_the_snapshot_as_of_its_lap(tmp_path, reader):
    plain = _ctx(tmp_path, None).dispatch("get_race_state", {})
    out = _ctx(tmp_path, reader).dispatch("get_race_state", {})
    assert out == plain
    assert out.startswith("As of lap 3 (race).\nPosition: P5 overall, P3 in class.")


def test_a_commit_in_flight_still_reads_live(tmp_path):
    """Lap 5 under way while lap 4's commit is still on the race pool: the snapshot is
    lap 3's and the live frame is still this race (race.live_race's rule)."""
    frame = _scoring(player_place=4, total_laps=4)
    out = _ctx(tmp_path, lambda: read_field(frame)).dispatch("get_race_state", {})
    assert "Position now (live): P4 overall, P2 in class." in out


def test_a_class_lead_taken_since_the_lap_is_not_said_as_leading_then(tmp_path):
    rs = RaceSample(class_position=1, class_behind="G. 4 (GT3 Entry 4)",
                    class_gap_behind=1.5, max_laps=24)
    snap = build_snapshot([LapFacts(lap_no=3, lap_time=100.0, clean=True, position=3,
                                    race=rs)], "s")
    frame = _scoring(player_place=4)            # now P2 in class
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    out = CoachContext(store, on_track=True, race=snap,
                       live_field=lambda: read_field(frame)).dispatch("get_race_state", {})
    assert "Car ahead in class at the end of lap 3: none — you led your class." in out
    assert "you lead your class" not in out


def test_get_battle_says_a_place_changed_since_the_lap_it_compares(tmp_path):
    """#185's read stays the end-of-lap field; one line says he has moved since."""
    from simcoach.battle import CarLap, build_battle
    from simcoach.corners import Corner
    t1 = Corner(1, "La Source", "right", 200.0, 250.0, 300.0, label="1")
    me = CarLap(5, "You", "Corvette Z06 GT3.R", 3, True, entry={1: 104.0}, exit={1: 106.0})
    ahead = CarLap(11, "G. 4", "GT3 Entry 4", 2, False, entry={1: 103.0}, exit={1: 105.3})
    snap = _snap_lap3()
    snap.battle = build_battle([me, ahead], [t1])
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, on_track=True, race=snap)
    plain = ctx.dispatch("get_battle", {"direction": "ahead"})
    frame = _scoring(player_place=4)
    ctx.live_field = lambda: read_field(frame)
    moved = ctx.dispatch("get_battle", {"direction": "ahead"})
    assert moved == plain + ("\nYou're P2 in class now (live); this read compares the field "
                             "as it stood at the end of lap 3.")
    same = _scoring(player_place=5)
    ctx.live_field = lambda: read_field(same)
    assert ctx.dispatch("get_battle", {"direction": "ahead"}) == plain
