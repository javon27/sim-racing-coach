"""#186 (rig, Sarthe #276, mid race lap 4): the driver had just passed for P2 and the
coach said "As of lap 3, you're actually P3 in class". The race tools overlay the LIVE
position, read from one scoring frame as the tool answers, onto the per-lap snapshot."""
import pytest

from simcoach.battle import CarLap, build_battle
from simcoach.coach_tools import CoachContext
from simcoach.corners import Corner
from simcoach.lmu.live import empty_snapshot, read_field
from simcoach.lmu.rf2structs import VehicleScoringInfoV01, rF2Scoring
from simcoach.model_types import RaceSample
from simcoach.race import LapFacts, build_snapshot
from simcoach.store import LapStore

# Stable mIDs: a CarLap's car_id and a VehicleState's id are both the game's mID.
_ID = {"You": 5, "A": 10, "B": 11, "C": 12, "D": 13,
       "E": 10}     # E: a new car in A's reused mID slot


def _veh(**kw) -> VehicleScoringInfoV01:
    v = VehicleScoringInfoV01()
    for k, val in kw.items():
        setattr(v, k, val.encode() if isinstance(val, str) else val)
    return v


def _scoring(gt3_order, *, total_laps=3, session=12, unplaced=False):
    """A multiclass race frame as smm reads it: two Hypercars lead overall, then the GT3s
    in `gt3_order` (driver names; "You" is the player). So the player's overall mPlace
    is his class place + 2."""
    cars = [_veh(mID=1, mPlace=1, mDriverName="H. One", mVehicleName="Ferrari 499P",
                 mVehicleClass="Hypercar", mTotalLaps=total_laps),
            _veh(mID=2, mPlace=2, mDriverName="H. Two", mVehicleName="Toyota GR010",
                 mVehicleClass="Hypercar", mTotalLaps=total_laps, mTimeBehindNext=1.0)]
    for place, name in enumerate(gt3_order, start=3):
        me = name == "You"
        cars.append(_veh(mID=_ID[name], mPlace=0 if me and unplaced else place,
                         mDriverName=name,
                         mVehicleName="Corvette Z06 GT3.R" if me else f"{name} Entry",
                         mVehicleClass="GT3", mTotalLaps=total_laps, mIsPlayer=me,
                         mTimeBehindNext=1.5))
    s = rF2Scoring()
    s.mScoringInfo.mSession = session
    s.mScoringInfo.mMaxLaps = 24
    s.mScoringInfo.mNumVehicles = len(cars)
    for i, v in enumerate(cars):
        s.mVehicles[i] = v
    return s


AT_LAP_3 = ["A", "B", "You", "C"]        # P5 overall, P3 in class at the end of lap 3
PASSED_B = ["A", "You", "B", "C"]        # mid lap 4: he has just passed B for P2


def _snap_lap3(battle=None):
    """As of lap 3: P5 overall, P3 in class, B ahead and closing (1.75 -> 1.5 s), C
    behind."""
    def rs(gap):
        return RaceSample(class_position=3, class_ahead="B (B Entry)", class_gap_ahead=gap,
                          class_behind="C (C Entry)", class_gap_behind=1.5, max_laps=24)
    stint = [LapFacts(lap_no=n, lap_time=100.0, clean=True, position=5, race=rs(g))
             for n, g in ((2, 1.75), (3, 1.5))]
    return build_snapshot(stint, "s", battle=battle)


def _ctx(tmp_path, reader, snap=None):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    return CoachContext(store, on_track=True, race=snap or _snap_lap3(), live_field=reader)


def _live(order, **kw):
    frame = _scoring(order, **kw)
    return lambda: read_field(frame)


def test_the_live_place_leads_and_a_passed_cars_stale_gap_is_cut(tmp_path):
    """The issue case: at lap 3 he was P3 in class; mid lap 4 he is P2 in class (mPlace
    4 overall -- two Hypercars ahead). The class place is the class rank, not mPlace."""
    field = read_field(_scoring(PASSED_B))
    assert next(c for c in field.cars if c.is_player).place == 4   # overall != class
    out = _ctx(tmp_path, _live(PASSED_B)).dispatch("get_race_state", {})
    assert out.startswith("Position now (live): P4 overall, P2 in class.\n"
                          "As of lap 3 (race).")
    assert ("Car ahead in class now (live): A (A Entry); no gap to him yet — it's read at "
            "the line, and a trend needs 2 clean laps with him there.") in out
    assert ("Car behind in class now (live): B (B Entry); no gap to him yet — it's read at "
            "the line, and a trend needs 2 clean laps with him there.") in out
    # B's lap-3 gap, closing trend and catch-lap are about a car he has passed: cut.
    for stale in ("P3 in class", "P5 overall", "1.5 s", "closing", "reach", "lap 9"):
        assert stale not in out, stale


def test_a_neighbour_swap_ahead_is_live_and_the_same_car_behind_keeps_its_trend(tmp_path):
    """MINOR 4: his place is unchanged, but the two cars ahead swapped -- a different car
    is ahead of him now, the same car is behind."""
    out = _ctx(tmp_path, _live(["B", "A", "You", "C"])).dispatch("get_race_state", {})
    assert out.startswith("Position now (live): P5 overall, P3 in class.\nAs of lap 3")
    assert "Car ahead in class now (live): A (A Entry); no gap to him yet" in out
    assert "Car behind in class at the end of lap 3: C (C Entry), 1.5 s behind" in out


def test_a_class_lead_taken_since_the_lap_is_said_in_the_present(tmp_path):
    out = _ctx(tmp_path, _live(["You", "A", "B", "C"])).dispatch("get_race_state", {})
    assert out.startswith("Position now (live): P3 overall, P1 in class.")
    assert "Car ahead in class now (live): none — you lead your class." in out
    assert "B (B Entry)" not in out


def test_live_equal_to_the_snapshot_leaves_the_answer_unchanged(tmp_path):
    plain = _ctx(tmp_path, None).dispatch("get_race_state", {})
    assert _ctx(tmp_path, _live(AT_LAP_3)).dispatch("get_race_state", {}) == plain
    assert plain.startswith("As of lap 3 (race).\nPosition: P5 overall, P3 in class.")


def _raise():
    raise OSError("shared memory mapping is stale: LMU is not running")


@pytest.mark.parametrize("reader", [
    _raise,                                      # the read fails
    empty_snapshot,                              # LMU gone / garage (#147): not live
    _live(PASSED_B, total_laps=2),               # lap 3 in progress: before the snapshot
    _live(PASSED_B, total_laps=5),               # lap 6: not this snapshot's race
    _live(PASSED_B, session=5),                  # qualifying, not a race
    _live(PASSED_B, unplaced=True),              # player unplaced (mPlace 0)
], ids=["raises", "not-live", "lap-before", "lap-after", "not-race", "unplaced"])
def test_no_usable_live_frame_falls_back_to_the_snapshot_as_of_its_lap(tmp_path, reader):
    plain = _ctx(tmp_path, None).dispatch("get_race_state", {})
    out = _ctx(tmp_path, reader).dispatch("get_race_state", {})
    assert out == plain
    assert out.startswith("As of lap 3 (race).\nPosition: P5 overall, P3 in class.")


@pytest.mark.parametrize("total_laps", [3, 4])
def test_the_lap_after_the_snapshot_or_a_commit_in_flight_reads_live(tmp_path, total_laps):
    """Lap 4 under way, or lap 5 while lap 4's commit is still on the race pool
    (race.live_race's rule)."""
    out = _ctx(tmp_path, _live(PASSED_B, total_laps=total_laps)).dispatch(
        "get_race_state", {})
    assert out.startswith("Position now (live): P4 overall, P2 in class.")


# --- get_battle: #185's read is the lap's field; the car asked for is the live one ----

T1 = Corner(1, "La Source", "right", 200.0, 250.0, 300.0, label="1")


def _lap(name, cls_pos, entry, exit_):
    return CarLap(_ID[name], name, "Corvette Z06 GT3.R" if name == "You" else f"{name} Entry",
                  cls_pos, name == "You", entry={1: entry}, exit={1: exit_})


def _battle(*, without=("D",)):
    """Lap 3's timing, class places as at the end of lap 3 (A, B, You, C, D)."""
    laps = [_lap("A", 1, 102.0, 104.0), _lap("B", 2, 103.0, 105.3),
            _lap("You", 3, 104.0, 106.0), _lap("C", 4, 104.5, 106.9),
            _lap("D", 5, 105.0, 107.4)]
    return build_battle([c for c in laps if c.driver not in without], [T1])


MOVED = ("\nYou're P2 in class now (live); this read compares the field as it stood at "
         "the end of lap 3.")


def test_after_a_pass_the_car_ahead_is_the_live_one_not_the_car_just_passed(tmp_path):
    ctx = _ctx(tmp_path, _live(PASSED_B), _snap_lap3(_battle()))
    out = ctx.dispatch("get_battle", {"direction": "ahead"})
    assert ("Rival (directly ahead of you in class now (live), you're P2): A (A Entry), "
            "P1 in class.") in out
    assert "B (B Entry)" not in out and out.endswith(MOVED)
    behind = ctx.dispatch("get_battle", {"direction": "behind"})
    assert ("Rival (directly behind you in class now (live), you're P2): B (B Entry), "
            "P3 in class.") in behind
    # A class position, never an overall one: the Hypercar P1 overall is not his P1.
    assert "Rival (P1 in class now (live)): A (A Entry), P1 in class." in ctx.dispatch(
        "get_battle", {"position": 1})


def test_after_a_pass_his_old_place_is_the_car_now_there_not_him(tmp_path):
    ctx = _ctx(tmp_path, _live(PASSED_B), _snap_lap3(_battle()))
    for args in ({"position": 3}, {"rival": "P3"}):
        out = ctx.dispatch("get_battle", args)
        assert "is you" not in out, args
        assert "Rival (P3 in class now (live)): B (B Entry), P3 in class." in out, args


def test_after_a_pass_his_new_place_is_him(tmp_path):
    ctx = _ctx(tmp_path, _live(PASSED_B), _snap_lap3(_battle()))
    assert ctx.dispatch("get_battle", {"position": 2}) == (
        "As of lap 3. P2 is you now (live), not a rival." + MOVED)


def test_a_live_car_the_lap_never_timed_is_said_so(tmp_path):
    ctx = _ctx(tmp_path, _live(PASSED_B), _snap_lap3(_battle(without=("A",))))
    assert ctx.dispatch("get_battle", {"direction": "ahead"}) == (
        "As of lap 3. Rival (directly ahead of you in class now (live), you're P2): "
        "A (A Entry), P1 in class, was not timed on lap 3: it is not in that lap's field."
        + MOVED)


def test_a_class_leader_now_has_no_car_ahead(tmp_path):
    ctx = _ctx(tmp_path, _live(["You", "A", "B", "C"]), _snap_lap3(_battle()))
    assert ctx.dispatch("get_battle", {"direction": "ahead"}).startswith(
        "As of lap 3. You're P1 in class now (live): nobody in your class is ahead of you.")


def test_get_battle_is_unchanged_when_his_place_has_not_moved(tmp_path):
    plain_ctx = _ctx(tmp_path, None, _snap_lap3(_battle()))
    live_ctx = _ctx(tmp_path, _live(AT_LAP_3), _snap_lap3(_battle()))
    for args in ({"direction": "ahead"}, {"position": 3}, {"rival": "P2"}, {}):
        assert live_ctx.dispatch("get_battle", args) == plain_ctx.dispatch("get_battle",
                                                                             args), args
    assert "Rival (directly ahead of you in class at the end of lap 3, when you were P3): " \
           "B (B Entry)" in live_ctx.dispatch("get_battle", {"direction": "ahead"})


def test_after_a_pass_a_named_rival_is_stated_at_his_live_place(tmp_path):
    """Round 2: 'B, P2 in class' beside 'You're P2 in class now' put two cars at P2."""
    ctx = _ctx(tmp_path, _live(PASSED_B), _snap_lap3(_battle()))
    out = ctx.dispatch("get_battle", {"rival": "B"})
    assert "Rival (the car you named): B (B Entry), P3 in class now (live)." in out
    assert "P2 in class." not in out.replace("You're P2 in class now", "")
    # No match: the field it lists is the lap's, and says so.
    assert ctx.dispatch("get_battle", {"rival": "Zed"}).startswith(
        "As of lap 3. No car in your class matches 'Zed'. In your class: A (A Entry), P1 "
        "in class; B (B Entry), P2 in class; C (C Entry), P4 in class. Those are the "
        "places at the end of lap 3.")


def test_after_a_pass_the_default_nearest_rival_is_stated_at_his_live_place(tmp_path):
    ctx = _ctx(tmp_path, _live(PASSED_B), _snap_lap3(_battle(without=("C", "D"))))
    out = ctx.dispatch("get_battle", {})
    assert "Rival (nearest to you on lap 3): B (B Entry), P3 in class now (live)." in out
    assert "P2 in class." not in out.replace("You're P2 in class now", "")


def test_after_a_pass_a_rival_gone_from_the_live_field_keeps_the_lap_label(tmp_path):
    ctx = _ctx(tmp_path, _live(PASSED_B), _snap_lap3(_battle(without=())))
    assert ("Rival (the car you named): D (D Entry), P5 in class at the end of lap 3."
            in ctx.dispatch("get_battle", {"rival": "D"}))


def test_a_swap_behind_with_his_place_held_compares_the_car_behind_now(tmp_path):
    """Round 2 MINOR 1: D passed C behind him; get_race_state names D, so get_battle
    'behind' compares D -- and 'ahead' (still B) is #185's answer, byte for byte."""
    swapped = ["A", "B", "You", "D", "C"]
    battle = _battle(without=())
    live_ctx = _ctx(tmp_path, _live(swapped), _snap_lap3(battle))
    plain_ctx = _ctx(tmp_path, None, _snap_lap3(battle))
    behind = live_ctx.dispatch("get_battle", {"direction": "behind"})
    assert ("Rival (directly behind you in class now (live), you're P3): D (D Entry), "
            "P4 in class.") in behind
    assert "C (C Entry)" not in behind and "now (live);" not in behind
    for args in ({"direction": "ahead"}, {"position": 2}, {"position": 3}):
        assert live_ctx.dispatch("get_battle", args) == plain_ctx.dispatch("get_battle",
                                                                             args), args


def test_his_place_held_a_named_rival_who_swapped_is_stated_at_his_live_place(tmp_path):
    """Round 3: B passed A ahead of him; his own P3 held. A is P2 now, never 'P1'."""
    battle = _battle(without=())
    ctx = _ctx(tmp_path, _live(["B", "A", "You", "C", "D"]), _snap_lap3(battle))
    assert "Rival (the car you named): A (A Entry), P2 in class now (live)." in ctx.dispatch(
        "get_battle", {"rival": "A"})
    assert "Rival (the car you named): B (B Entry), P1 in class now (live)." in ctx.dispatch(
        "get_battle", {"rival": "B"})
    assert ctx.dispatch("get_battle", {"rival": "Zed"}).endswith(
        "Those are the places at the end of lap 3.")


def test_nothing_moved_every_battle_answer_is_byte_identical(tmp_path):
    battle = _battle(without=())
    live_ctx = _ctx(tmp_path, _live(["A", "B", "You", "C", "D"]), _snap_lap3(battle))
    plain_ctx = _ctx(tmp_path, None, _snap_lap3(battle))
    for args in ({}, {"rival": "A"}, {"rival": "B"}, {"rival": "D"}, {"rival": "Zed"},
                 {"rival": "P4"}, {"direction": "ahead"}, {"direction": "behind"},
                 {"position": 1}, {"position": 3}, {"position": 9}):
        assert live_ctx.dispatch("get_battle", args) == plain_ctx.dispatch("get_battle",
                                                                             args), args
    assert "Those are the places" not in live_ctx.dispatch("get_battle", {"rival": "Zed"})


def test_a_reused_mid_slot_is_not_the_laps_car(tmp_path):
    """Round 2 MINOR 3: A left and a new car E took A's mID slot -- A's lap timing is
    never stated as E's."""
    ctx = _ctx(tmp_path, _live(["E", "You", "B", "C"]), _snap_lap3(_battle()))
    out = ctx.dispatch("get_battle", {"direction": "ahead"})
    assert out.startswith("As of lap 3. Rival (directly ahead of you in class now (live), "
                          "you're P2): E (E Entry), P1 in class, was not timed on lap 3: it "
                          "is not in that lap's field.")
