"""#181: a whole racing lap LMU invalidated (Sarthe race lap 5, lap id 7640 -- forced off
at 7A/7B, no time in the result log) is a RACING lap: counted, and described as
invalidated -- never as the cool-down after the flag. Only an in-lap is that."""
import dataclasses

from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.store import LapStore

SID = "sarthe--online"
T0 = 1_790_000_000.0


def _race(tmp_path, synthetic_lap, cool_down=True):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    # (lap_no, partial, position, time, valid) -- the stored shape after the #181 fix.
    shape = [(1, None, 5, 251.0, True), (2, None, 4, 243.6, True), (3, None, 3, 243.0, True),
             (4, None, 2, 242.4, True),
             (5, None, 2, None, False)]            # the invalidated whole lap: no time
    if cool_down:
        shape.append((6, "in", 2, None, False))    # the real cool-down: into the pits
    for k, (lap_no, partial, pos, t, valid) in enumerate(shape):
        store.save(dataclasses.replace(
            synthetic_lap, session_id=SID, phase="race", lap_no=lap_no, partial=partial,
            position=pos, lap_time=t, valid=valid, timestamp=T0 + 240 * k))
    return CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track=synthetic_lap.track, car=synthetic_lap.car, session_id=SID)


def test_race_result_names_the_invalidated_lap_as_raced(tmp_path, synthetic_lap):
    out = _race(tmp_path, synthetic_lap).get_race_result()
    assert ("race lap 4 P2, race lap 5 P2 (invalidated), race lap 6 P2.") in out
    assert "An invalidated lap was RACED" in out and "never the cool-down" in out
    # The cool-down rule keys on the lap BEGUN after the flag, never on a missing time.
    assert "the lap begun after the chequered flag is the cool-down" in out
    assert "a lap marked invalidated is never the cool-down" in out


def test_result_log_untimed_rows_are_not_called_raced(tmp_path, synthetic_lap):
    """A result-import row with no time may be an out-lap: no 'invalidated'/'RACED'."""
    ctx = _race(tmp_path, synthetic_lap)
    ctx.store.conn.execute("UPDATE laps SET source = 'result' WHERE lap_no = 5")
    out = ctx.get_race_result()
    assert "(invalidated)" not in out and "RACED" not in out


def test_no_invalidated_note_without_one(tmp_path, synthetic_lap):
    ctx = _race(tmp_path, synthetic_lap)
    ctx.store.conn.execute("UPDATE laps SET lap_time = 241.6, valid = 1 WHERE lap_no = 5")
    out = ctx.get_race_result()
    assert "(invalidated)" not in out and "RACED" not in out


def test_the_invalidated_lap_counts_as_a_racing_lap(tmp_path, synthetic_lap):
    ctx = _race(tmp_path, synthetic_lap)
    win = ctx.store.race_windows(SID)[-1]
    assert win["laps"] == 5                        # laps 1-5; the in-lap is not one
    assert "this race: 5" in ctx.get_session_status()


def test_lap_reads_describe_it_as_invalidated_not_cool_down(tmp_path, synthetic_lap):
    ctx = _race(tmp_path, synthetic_lap)
    listing = ctx.list_recent_laps(limit=20)
    assert "race lap 5: no time — LMU didn't time it (invalidated) P2 overall" in listing
    assert "race lap 6: in lap (no time)" in listing
    lap5 = ctx.get_lap_summary("race lap 5").splitlines()[0]
    assert "no time — LMU didn't time it (invalidated)" in lap5
    assert "racing" not in lap5 and "racing" not in listing   # neutral: any phase, any source
    lap6 = ctx.get_lap_summary("race lap 6").splitlines()[0]
    assert "in-lap" in lap6 and "invalidated" not in lap6


def test_time_loss_refuses_an_untimed_lap(tmp_path, synthetic_lap):
    out = _race(tmp_path, synthetic_lap).get_time_loss("race lap 5")
    assert "no lap time" in out and "no time loss to measure" in out
    assert "vs best" not in out
