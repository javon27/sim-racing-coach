"""#192 (rig, Laguna 2026-10-02, 35 cars, the driver in GT3): scoring mTimeBehindNext
updates every tick and is the gap to the car one place up OVERALL, whatever its class;
the class gap is the sum of links to the nearest same-class car (race.class_neighbours).
So the race tools state the live class gap, labelled live, and keep the lap's gap and
trend labelled with its lap."""
from simcoach.coach_tools import CoachContext
from simcoach.lmu.live import read_field
from simcoach.lmu.rf2structs import VehicleScoringInfoV01, rF2Scoring
from simcoach.model_types import RaceSample
from simcoach.race import LapFacts, build_snapshot
from simcoach.store import LapStore

_ID = {"H1": 1, "H2": 2, "H3": 3, "A": 10, "B": 11, "You": 5, "C": 12}


def _frame(order, lapped=()):
    """`order`: (name, mTimeBehindNext) in overall order. H* are Hypercars, the rest
    GT3. A name in `lapped` is a lap down on the car one place up."""
    s = rF2Scoring()
    s.mScoringInfo.mSession = 12
    s.mScoringInfo.mMaxLaps = 24
    s.mScoringInfo.mNumVehicles = len(order)
    for i, (name, gap) in enumerate(order):
        v = VehicleScoringInfoV01()
        hyper = name.startswith("H")
        for k, val in {"mID": _ID[name], "mPlace": i + 1, "mDriverName": name,
                       "mVehicleName": "Corvette Z06 GT3.R" if name == "You" else
                       f"{name} Entry", "mVehicleClass": "Hypercar" if hyper else "GT3",
                       "mTotalLaps": 3, "mIsPlayer": name == "You", "mTimeBehindNext": gap,
                       "mLapsBehindNext": 1 if name in lapped else 0}.items():
            setattr(v, k, val.encode() if isinstance(val, str) else val)
        s.mVehicles[i] = v
    return s


# End of lap 3: P5 overall, P3 in class; B ahead (closing 3.0 -> 2.5 s), C behind.
AT_LAP_3 = [("H1", 0.0), ("A", 2.0), ("B", 1.1), ("H2", 0.6), ("You", 0.8), ("H3", 0.5),
            ("C", 0.4)]
# Mid lap 4: he has passed B. A Hypercar sits one place up overall both times, so his own
# gap_next (0.8) is never the class gap (H2's 0.6 + his 0.8 = 1.4).
PASSED_B = [("H1", 0.0), ("A", 2.0), ("H2", 0.6), ("You", 0.8), ("H3", 0.5), ("B", 0.3),
            ("C", 0.4)]


def _ctx(tmp_path, frame):
    def rs(gap):
        return RaceSample(class_position=3, class_ahead="B (B Entry)", class_gap_ahead=gap,
                          class_behind="C (C Entry)", class_gap_behind=1.0, max_laps=24)
    snap = build_snapshot([LapFacts(lap_no=n, lap_time=100.0, clean=True, position=5,
                                    race=rs(g)) for n, g in ((2, 3.0), (3, 2.5))], "s")
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    return CoachContext(store, on_track=True, race=snap,
                        live_field=lambda: read_field(frame))


def test_a_passed_car_gets_the_live_class_gap_not_a_cut(tmp_path):
    field = read_field(_frame(PASSED_B))
    assert next(c for c in field.cars if c.is_player).gap_next == 0.8   # not the class gap
    out = _ctx(tmp_path, _frame(PASSED_B)).dispatch("get_race_state", {})
    assert out.startswith("Position now (live): P4 overall, P2 in class.")
    assert ("Car ahead in class now (live, P1): A (A Entry), 1.4 s ahead; no trend yet — a "
            "trend needs 2 clean laps with him there.") in out
    assert ("Car behind in class now (live, P3): B (B Entry), 0.8 s behind; no trend yet — a "
            "trend needs 2 clean laps with him there.") in out
    # B's lap-3 gap and closing trend are about a car he has passed: still cut.
    for stale in ("2.5 s", "closing", "reach him", "at the end of lap 3"):
        assert stale not in out, stale


def test_the_same_car_gets_the_live_gap_and_the_laps_trend_as_of_its_lap(tmp_path):
    out = _ctx(tmp_path, _frame(AT_LAP_3)).dispatch("get_race_state", {})
    assert out.startswith("As of lap 3 (race).\nPosition: P5 overall, P3 in class.")
    assert ("Car ahead in class now (live, P2): B (B Entry), 1.4 s ahead.\n"
            "Car ahead in class (P2) at the end of lap 3: B (B Entry), 2.5 s ahead; you are "
            "closing 0.50 s/lap") in out
    assert ("Car behind in class now (live, P4): C (C Entry), 0.9 s behind.\n"
            "Car behind in class (P4) at the end of lap 3: C (C Entry), 1.0 s behind") in out


def test_a_lapped_link_has_no_seconds_figure(tmp_path):
    """H2 is a lap down on B: the link between B and him is lapped, so the class gap is
    None -- never H2's seconds plus his."""
    out = _ctx(tmp_path, _frame(AT_LAP_3, lapped=("H2",))).dispatch("get_race_state", {})
    assert ("Car ahead in class now (live, P2): B (B Entry), a lap or more away (no seconds "
            "figure).") in out
    assert "1.4 s" not in out
