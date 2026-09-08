import pytest
from simcoach.lmu.rf2structs import rF2Scoring, VehicleScoringInfoV01
from simcoach.lmu.live import read_field, empty_snapshot, FieldSnapshot


def _veh(**kw) -> VehicleScoringInfoV01:
    v = VehicleScoringInfoV01()
    for k, val in kw.items():
        if k in ("mDriverName", "mVehicleName", "mVehicleClass"):
            setattr(v, k, val.encode())
        else:
            setattr(v, k, val)
    return v


def _scoring(vehicles, *, session=12, current_et=100.0, end_et=0.0, max_laps=24) -> rF2Scoring:
    s = rF2Scoring()
    s.mScoringInfo.mSession = session
    s.mScoringInfo.mCurrentET = current_et
    s.mScoringInfo.mEndET = end_et
    s.mScoringInfo.mMaxLaps = max_laps
    s.mScoringInfo.mNumVehicles = len(vehicles)
    for i, v in enumerate(vehicles):
        s.mVehicles[i] = v
    return s


def test_read_field_maps_basic_fields_and_player():
    s = _scoring([
        _veh(mID=1, mPlace=1, mDriverName="A. Lead", mVehicleName="Ferrari 499P",
             mVehicleClass="Hypercar", mLapDist=100.0, mSector=0, mTotalLaps=8,
             mTimeBehindLeader=0.0, mLapsBehindLeader=0),
        _veh(mID=2, mPlace=9, mDriverName="You", mVehicleName="Corvette Z06 GT3.R",
             mVehicleClass="GT3", mLapDist=500.0, mSector=1, mTotalLaps=8, mIsPlayer=True,
             mTimeBehindNext=2.4, mLapsBehindNext=0, mTimeBehindLeader=31.5, mLapsBehindLeader=0,
             mCurSector1=30.1, mBestSector1=29.8, mLastLapTime=92.5, mBestLapTime=91.9),
    ])
    snap = read_field(s)
    assert snap.session.live is True
    assert snap.session.current_lap == 9          # player mTotalLaps + 1
    assert snap.session.max_laps == 24
    assert snap.session.best_s1 == pytest.approx(29.8)
    assert snap.session.last_lap == pytest.approx(92.5)
    you = next(c for c in snap.cars if c.is_player)
    assert you.place == 9
    assert you.car == "Corvette Z06 GT3.R"
    assert you.cls == "GT3"
    assert you.gap_next == pytest.approx(2.4)


def test_class_position_ranks_within_class():
    s = _scoring([
        _veh(mID=1, mPlace=1, mVehicleClass="Hypercar", mVehicleName="H1"),
        _veh(mID=2, mPlace=2, mVehicleClass="GT3", mVehicleName="G1"),
        _veh(mID=3, mPlace=3, mVehicleClass="GT3", mVehicleName="G2", mIsPlayer=True),
    ])
    snap = read_field(s)
    by_id = {c.id: c for c in snap.cars}
    assert by_id[1].class_position == 1
    assert by_id[2].class_position == 1
    assert by_id[3].class_position == 2


def test_laps_down_zeroes_the_seconds_gap():
    s = _scoring([
        _veh(mID=1, mPlace=1, mVehicleClass="GT3", mVehicleName="G1"),
        _veh(mID=2, mPlace=2, mVehicleClass="GT3", mVehicleName="G2", mIsPlayer=True,
             mTimeBehindLeader=45.0, mLapsBehindLeader=1),
    ])
    you = next(c for c in read_field(s).cars if c.is_player)
    assert you.gap_leader is None
    assert you.laps_behind_leader == 1


def test_unset_sector_time_is_none():
    s = _scoring([_veh(mID=2, mPlace=1, mVehicleClass="GT3", mVehicleName="G1",
                       mIsPlayer=True, mCurSector1=0.0, mBestSector1=-1.0)])
    snap = read_field(s)
    assert snap.session.cur_s1 is None
    assert snap.session.best_s1 is None


def test_no_player_is_not_live():
    s = _scoring([_veh(mID=1, mPlace=1, mVehicleClass="GT3", mVehicleName="G1")])
    snap = read_field(s)
    assert snap.session.live is False


def test_player_in_garage_is_not_live():
    # In the garage (or menus with stale scoring) the Pit Wall must NOT show a live
    # field — matches the GARAGE header from read_live_status.
    s = _scoring([_veh(mID=2, mPlace=1, mVehicleClass="GT3", mVehicleName="G1",
                       mIsPlayer=True, mInGarageStall=1)])
    snap = read_field(s)
    assert snap.session.live is False


def test_clamps_to_num_vehicles():
    s = _scoring([_veh(mID=1, mPlace=1, mVehicleClass="GT3", mVehicleName="G1", mIsPlayer=True)])
    s.mScoringInfo.mNumVehicles = 1
    # a stray struct past the count must be ignored
    s.mVehicles[5] = _veh(mID=99, mPlace=2, mVehicleClass="GT3", mVehicleName="ghost")
    snap = read_field(s)
    assert [c.id for c in snap.cars] == [1]


def test_unclassified_ranks_last_in_class():
    s = _scoring([
        _veh(mID=1, mPlace=1, mVehicleClass="GT3", mVehicleName="G1"),
        _veh(mID=2, mPlace=0, mVehicleClass="GT3", mVehicleName="G2", mIsPlayer=True),
    ])
    snap = read_field(s)
    by_id = {c.id: c for c in snap.cars}
    assert by_id[1].class_position == 1
    assert by_id[2].class_position == 2


def test_empty_snapshot_is_not_live():
    snap = empty_snapshot()
    assert isinstance(snap, FieldSnapshot)
    assert snap.session.live is False
    assert snap.cars == []
