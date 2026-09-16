"""Agentic tool layer: the tools return the right on-demand summaries, and the
Coach agentic loop dispatches the model's tool calls (via FakeModel scripting)."""
import re

import pytest

from simcoach.coach import Coach
from simcoach.coach_tools import TOOLS, CoachContext, scrub_stored_answer
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel
from simcoach.store import LapStore


def _ctx(tmp_path, lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    lap_id = store.save(lap)
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    return CoachContext(store, record=lap, lap_id=lap_id, cmap=cmap,
                        track=lap.track, car=lap.car, on_track=True), store, lap_id


def test_get_corner_returns_one_corner(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    out = ctx.get_corner("1")
    assert out.startswith("Lap: ")   # #108 item 1: lap identity leads every result
    assert "\nT1 " in out
    assert "\nT2" not in out  # just the one corner, not the whole lap
    # name lookup works too
    assert "\nT1 " in ctx.get_corner("Left Hairpin")
    # unknown corner lists the options instead of crashing
    assert "No corner matching" in ctx.get_corner("Nonexistent")


def test_get_corner_targets_a_specific_lap(tmp_path, synthetic_lap):
    # A corner question during a lap-2 discussion must read THAT lap, not always latest.
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    lap = LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "race"})
    store.save(lap)
    ctx = CoachContext(store, cmap=cmap, track=lap.track, car=lap.car,
                       session_id="s1", on_track=True)
    assert "\nT1 " in ctx.get_corner("1", which="race lap 1")
    assert "\nT1 " in ctx.dispatch("get_corner", {"corner": "1", "which": "race lap 1"})
    # a lap that doesn't exist is honest, not silently the latest lap
    assert "no lap" in ctx.get_corner("1", which="race lap 9").lower()


# --- get_corner "usually" gear comparison (#38 P1.7) ------------------------

def _gear_session(tmp_path, synthetic_lap, gears, phase="practice"):
    """A stored session of len(gears) valid laps, each a clone of synthetic_lap
    with a constant gear (and a constant rpm, since gear_read needs both) --
    so a lap's gear_min reads that lap's own constant everywhere, T1's
    min-speed sample included."""
    import dataclasses

    import numpy as np
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    session_id = "s1--online"
    n = len(synthetic_lap.channels["lap_distance"])
    ids = []
    for i, gear in enumerate(gears):
        ch = {**synthetic_lap.channels, "gear": np.full(n, float(gear)), "rpm": np.full(n, 6000.0)}
        lap = dataclasses.replace(synthetic_lap, channels=ch, lap_time=90.0 + i,
                                  session_id=session_id, phase=phase, timestamp=90.0 + i)
        ids.append(store.save(lap))
    cur = store.load(ids[-1])
    return CoachContext(store, record=cur, lap_id=ids[-1], cmap=cmap, track=cur.track,
                        car=cur.car, session_id=session_id, on_track=True)


def test_get_corner_usually_line_when_gear_differs_from_stint_mode(tmp_path, synthetic_lap):
    # 4 laps take T1 in 2nd, the queried (latest) lap in 3rd.
    ctx = _gear_session(tmp_path, synthetic_lap, [2, 2, 2, 2, 3])
    out = ctx.dispatch("get_corner", {"corner": "T1"})
    assert "You took T1 in 3rd (usually 2nd)." in out


def test_get_corner_silent_when_all_laps_agree(tmp_path, synthetic_lap):
    ctx = _gear_session(tmp_path, synthetic_lap, [2, 2, 2, 2, 2])
    out = ctx.dispatch("get_corner", {"corner": "T1"})
    assert "usually" not in out


def test_get_corner_silent_with_fewer_than_three_laps(tmp_path, synthetic_lap):
    ctx = _gear_session(tmp_path, synthetic_lap, [2, 3])
    out = ctx.dispatch("get_corner", {"corner": "T1"})
    assert "usually" not in out


def test_get_lap_summary_and_tires(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    summary = ctx.get_lap_summary("latest")
    assert "Corner facts" in summary and "T1 " in summary
    assert "Tire temps" in ctx.get_tire_report()


def test_get_lap_summary_shows_grip_envelope(tmp_path, synthetic_lap):
    # 3 stored laps give `grip_envelope` real samples to read (the fixture's
    # own lat_g/long_g clear the load gate in both corners).
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for _ in range(3):
        store.save(synthetic_lap)
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       on_track=True)
    summary = ctx.get_lap_summary("latest")
    assert "Grip envelope" in summary
    assert "% of it in the corners" in summary


def test_get_lap_summary_envelope_computed_once_per_context(tmp_path, synthetic_lap, monkeypatch):
    # Two DIFFERENT laps (distinct analysis-cache keys) in one context must still
    # only trigger one `grip_envelope` call between them (issue #38 P1.4).
    import simcoach.coach_tools as coach_tools_mod
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    lap_a = LapRecord(**{**synthetic_lap.__dict__, "session_id": None, "phase": None})
    lap_b = LapRecord(**{**synthetic_lap.__dict__, "session_id": None, "phase": None,
                         "lap_time": 91.0})
    id_a = store.save(lap_a)
    id_b = store.save(lap_b)
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(store, cmap=cmap, track=lap_a.track, car=lap_a.car, on_track=True)

    calls = []
    real = coach_tools_mod.grip_envelope

    def spy(records):
        calls.append(records)
        return real(records)
    monkeypatch.setattr(coach_tools_mod, "grip_envelope", spy)

    ctx.get_lap_summary(which=str(id_a))
    ctx.get_corner("1", which=str(id_b))
    assert len(calls) == 1  # not re-fetched for the second, different lap this turn


def test_get_wheel_report(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    rep = ctx.dispatch("get_wheel_report", {})
    assert "Brake temps" in rep and "wear" in rep.lower()


def test_get_energy_report(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    rep = ctx.dispatch("get_energy_report", {})
    # synthetic fuel channel burns 2.6 L over the lap
    assert "Fuel: 2.6" in rep and "laps left" in rep


def test_session_status_and_recent_laps(tmp_path, synthetic_lap):
    ctx, store, lap_id = _ctx(tmp_path, synthetic_lap)
    status = ctx.get_session_status()
    assert "test_track" in status and "Laps" in status
    listing = ctx.list_recent_laps()
    assert f"lap {lap_id}:" in listing


def test_session_status_uses_current_phase_lap_count(tmp_path, synthetic_lap):
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    laps = [("qualifying", 1.0), ("qualifying", 2.0)] + [("race", 3.0 + i) for i in range(6)]
    for ph, t in laps:
        store.save(LapRecord(**{**synthetic_lap.__dict__,
                                "session_id": "s1", "phase": ph, "timestamp": t}))
    ctx = CoachContext(
        store,
        record=LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "race"}),
        session_id="s1", track=synthetic_lap.track, car=synthetic_lap.car,
    )
    status = ctx.get_session_status()
    assert "this race: 6" in status          # phase count, not the 8 session total
    assert "session total: 8" in status


def test_recent_laps_use_in_session_phase_numbering(tmp_path, synthetic_lap):
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ids = []
    for ph, t in [("practice", 1.0), ("practice", 2.0), ("race", 3.0), ("race", 4.0)]:
        ids.append(store.save(LapRecord(**{**synthetic_lap.__dict__,
                                           "session_id": "s1", "phase": ph, "timestamp": t})))
    ctx = CoachContext(
        store,
        record=LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "race"}),
        lap_id=ids[-1], session_id="s1",
    )
    listing = ctx.list_recent_laps().lower()
    # Named by per-phase in-session number, not the raw DB id.
    for lbl in ("practice lap 1", "practice lap 2", "race lap 1", "race lap 2"):
        assert lbl in listing
    # No raw DB ids leak: ids 3 & 4 would show as "lap 3"/"lap 4" if raw ids were
    # used, but per-phase numbering here only ever reaches "lap 2".
    assert "lap 3" not in listing and "lap 4" not in listing

    # Resolution maps those references back to the right lap.
    assert ctx._resolve_lap("practice lap 1")[1] == ids[0]
    assert ctx._resolve_lap("race 1")[1] == ids[2]
    assert ctx._resolve_lap("2")[1] == ids[3]  # bare number -> current (race) phase


def test_lap_numbering_matches_ui_contiguous_phase_grouping(tmp_path, synthetic_lap):
    # Issue #34: the UI (LapList.svelte) groups laps into a NEW block -- restarting
    # "Lap 1" -- every time the phase value changes, even if that same phase
    # recurs later (e.g. practice -> race -> practice again). The coach's numbering
    # must follow the same grouping, not a cumulative per-phase count across the
    # whole session (which would keep climbing past the interruption).
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ids = []
    for ph, t in [("practice", 1.0), ("practice", 2.0), ("race", 3.0), ("practice", 4.0)]:
        ids.append(store.save(LapRecord(**{**synthetic_lap.__dict__,
                                           "session_id": "s1", "phase": ph, "timestamp": t})))
    ctx = CoachContext(
        store,
        record=LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "practice"}),
        lap_id=ids[-1], session_id="s1",
    )
    listing = ctx.list_recent_laps().lower()
    # The UI shows this lap as the FIRST lap of the second (new) practice block,
    # not the third practice lap overall.
    assert "practice lap 1" in listing
    assert ctx._resolve_lap("practice lap 1")[1] == ids[-1]


def test_lap_numbering_prefers_game_lap_no_when_every_lap_in_run_has_one(tmp_path, synthetic_lap):
    # Issue #77: a session with a garage return or an out-lap in between should
    # show the game's real lap numbers (2,3,4,8,9), not a plain 1..N re-count,
    # once every lap in the run captured mTotalLaps at the wrap.
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ids = []
    for t, n in [(1.0, 2), (2.0, 3), (3.0, 4), (4.0, 8), (5.0, 9)]:
        ids.append(store.save(LapRecord(**{**synthetic_lap.__dict__,
                                           "session_id": "s1", "phase": "practice",
                                           "timestamp": t, "lap_no": n})))
    ctx = CoachContext(
        store,
        record=LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "practice"}),
        lap_id=ids[-1], session_id="s1",
    )
    listing = ctx.list_recent_laps().lower()
    for lbl in ("practice lap 2", "practice lap 3", "practice lap 4",
                "practice lap 8", "practice lap 9"):
        assert lbl in listing
    assert "practice lap 1" not in listing
    assert "practice lap 5" not in listing
    assert ctx._resolve_lap("practice lap 8")[1] == ids[3]
    assert ctx._resolve_lap("9")[1] == ids[4]  # bare number, current phase


def test_lap_numbering_falls_back_to_counting_when_any_lap_in_run_lacks_lap_no(
    tmp_path, synthetic_lap
):
    # Legacy session (or a run where even one lap never got a lap_no): number
    # 1..N within the run, same as today.
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ids = []
    for t, n in [(1.0, 2), (2.0, None), (3.0, 4)]:
        ids.append(store.save(LapRecord(**{**synthetic_lap.__dict__,
                                           "session_id": "s1", "phase": "practice",
                                           "timestamp": t, "lap_no": n})))
    ctx = CoachContext(
        store,
        record=LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "practice"}),
        lap_id=ids[-1], session_id="s1",
    )
    listing = ctx.list_recent_laps().lower()
    for lbl in ("practice lap 1", "practice lap 2", "practice lap 3"):
        assert lbl in listing


def test_resolve_lap_never_falls_back_to_raw_db_id(tmp_path, synthetic_lap):
    # Issue #34: a bare number the driver gives must only ever resolve through the
    # in-session lap numbering (what they actually see in the UI) -- never be
    # silently reinterpreted as a raw database id, which can point at a
    # completely unrelated lap (here, one from a different track/car entirely).
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    unrelated = LapRecord(**{**synthetic_lap.__dict__, "track": "other_track", "car": "other_car",
                             "session_id": None, "phase": None})
    for _ in range(5):
        store.save(unrelated)  # bump the autoincrement so later ids are unrelated laps
    ids = []
    for t in (10.0, 11.0):
        ids.append(store.save(LapRecord(**{**synthetic_lap.__dict__,
                                           "session_id": "s1", "phase": "race", "timestamp": t})))
    ctx = CoachContext(
        store,
        record=LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "race"}),
        lap_id=ids[-1], session_id="s1",
    )
    # "5" isn't a valid in-session reference (only race laps 1-2 exist) and happens
    # to collide with an unrelated lap's raw id (ids 1-5) -- must not resolve to it.
    record, lap_id = ctx._resolve_lap("5")
    assert record is None and lap_id is None


def _practice_session(tmp_path, synthetic_lap, n=35):
    """A practice session of `n` laps, like the one in issue #34's chat."""
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ids = [store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1",
                                   "phase": "practice", "timestamp": float(i)}))
           for i in range(n)]
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(
        store, cmap=cmap,
        record=LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "practice"}),
        lap_id=ids[-1], session_id="s1",
    )
    return ctx, store, ids


def test_unprefixed_lap_reference_resolves(tmp_path, synthetic_lap):
    # Issue #34 (real root cause): the driver says "lap 35 on this list" using the
    # UI's own unprefixed wording, so the model asks for "lap 35". Only
    # "practice lap 35"/"practice 35"/"35" were registered, so the UI's own phrasing
    # never resolved and the driver saw the tool's failure text.
    ctx, _, ids = _practice_session(tmp_path, synthetic_lap)
    assert ctx._resolve_lap("lap 35")[1] == ids[34]
    assert ctx._resolve_lap("practice lap 35")[1] == ids[34]  # still works
    assert ctx._resolve_lap("35")[1] == ids[34]


def test_unknown_lap_reference_names_the_phase_and_range(tmp_path, synthetic_lap):
    # The failure must be self-correcting: our own tool schema primed "race lap N",
    # and the old message named neither the session's phase nor the valid range, so
    # the model had nothing to retry against.
    ctx, _, _ = _practice_session(tmp_path, synthetic_lap)
    msg = ctx.get_lap_summary("race lap 35")
    assert "practice" in msg and "1-35" in msg and "race lap 35" in msg
    assert "practice" in ctx.get_corner("1", which="race lap 35")


def test_unknown_lap_reference_still_fails(tmp_path, synthetic_lap):
    # Making the UI's wording resolve must not make everything resolve.
    ctx, _, _ = _practice_session(tmp_path, synthetic_lap, n=3)
    for ref in ("race lap 2", "lap 99", "qualifying lap 1", "lap banana"):
        assert ctx._resolve_lap(ref) == (None, None)


def test_raw_id_resolves_only_when_laps_are_labelled_by_id(tmp_path, synthetic_lap):
    # With no session numbering (the REPL asks questions before this run's first lap)
    # list_recent_laps labels laps by their raw DB id, so that is the only reference
    # the model can give back. It must resolve -- but only after validating the id
    # belongs to THIS track/car, never an unrelated lap (issue #34).
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    other = [store.save(LapRecord(**{**synthetic_lap.__dict__, "track": "other_track",
                                     "car": "other_car", "session_id": None, "phase": None}))
             for _ in range(3)]
    mine = store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": None,
                                   "phase": "practice"}))
    ctx = CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                       track=synthetic_lap.track, car=synthetic_lap.car)
    listing = ctx.list_recent_laps()
    assert f"lap {mine}" in listing          # labelled by raw id, so it must resolve
    assert ctx._resolve_lap(str(mine))[1] == mine
    assert ctx._resolve_lap(f"lap {mine}")[1] == mine
    # An id from a different track/car stays unresolvable.
    assert ctx._resolve_lap(str(other[0])) == (None, None)
    assert ctx._resolve_lap(f"lap {other[-1]}") == (None, None)


def test_get_race_result_formats_and_falls_back(tmp_path, synthetic_lap):
    from simcoach.lmu.results import ResultLap, ResultSession
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    ctx.session_id = "race-1"
    rs = ResultSession(
        sim="lmu", track=synthetic_lap.track, car=synthetic_lap.car, car_class="GT3",
        date_ts=1, session_type="race", position=3, class_position=2, best_lap=90.0,
        finish_status="Finished",
        laps=[ResultLap(lap_no=1, lap_time=90.0, sectors=[], valid=True)],
        incidents=["Off track T7", "Contact T3"], penalties=[{"text": "Track limits DT"}],
    )
    store.save_result_session(rs, session_id="race-1")

    out = ctx.dispatch("get_race_result", {})
    assert "P3 overall" in out and "P2 in class" in out and "Finished" in out
    assert "Incidents (2)" in out and "Off track T7" in out
    assert "Penalties (1)" in out and "Track limits DT" in out

    # No race row for this session -> honest fallback.
    ctx.session_id = "practice-1"
    assert "No race result recorded" in ctx.get_race_result()


def test_get_race_result_arc_is_race_phase_only(tmp_path, synthetic_lap):
    from simcoach.model_types import LapRecord
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    ctx.session_id = "arc-1"
    # An earlier practice lap (P14) in the SAME session must not pollute the race arc.
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "arc-1",
                            "timestamp": -1.0, "position": 14, "phase": "practice"}))
    # A live race with no result log yet: P5 -> P8 -> P10 -> P3.
    for i, place in enumerate((5, 8, 10, 3)):
        store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "arc-1",
                                "timestamp": float(i), "position": place, "phase": "race"}))
    out = ctx.get_race_result()
    assert "P5 on the first captured lap" in out and "as low as P10" in out and "P3" in out
    assert "P14" not in out  # practice position excluded
    # With no result log there is no grid slot, and the first lap capture happened to
    # record is NOT one -- the tool must not offer it as a start (issue #67).
    assert "started p" not in out.lower()
    assert "grid position was not captured" in out


def test_get_race_result_for_a_live_session_reports_the_real_arc(tmp_path, synthetic_lap):
    """The 2026-09-10 Silverstone race (issue #67): the coach said 'you started P18 and
    crossed the line P18. No incidents or penalties logged' for a P9 -> P18 race with a
    logged contact, because the live session id never joined the result-log row."""
    from simcoach.lmu.results import ResultLap, ResultSession
    from simcoach.model_types import LapRecord
    track, car = "Silverstone National Circuit", "Team WRT 2026 #32:WEC"
    live_sid = "silverstone-national-circuit--team-wrt-2026-32-wec--offline"
    result_sid = "2026_09_10_00_39_07-16R1"   # the XML stem: NOT the live id
    contact = "Jayson G(0) reported contact (948.15) with another vehicle"
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    # Live capture got every timed lap the XML lists, at the sim's full float precision,
    # the first of them already running P18. So the dedupe leaves NO result lap row and
    # the link has to hold without one.
    live_times = [70.40680694580078, 88.20706176757812, 92.79763793945312,
                  66.87100219726562, 62.2532958984375, 60.8125, 62.413330078125,
                  63.27410888671875, 76.2119140625]
    for i, (lt, place) in enumerate(zip(live_times, [18, 2, 18, 18, 18, 18, 18, 18, 18])):
        store.save(LapRecord(**{**synthetic_lap.__dict__, "track": track, "car": car,
                                "lap_time": lt, "phase": "race",
                                "timestamp": 1789017974.0 + 63.0 * i,
                                "session_id": live_sid, "position": place}))
    store.save_result_session(ResultSession(
        sim="lmu", track=track, car=car, car_class="GT3", date_ts=1789017807.0,
        session_type="race", position=18, class_position=18, best_lap=60.8125,
        finish_status="Finished Normally", grid_position=9, class_grid_position=9,
        online=False,
        laps=[ResultLap(lap_no=i + 1, lap_time=t, sectors=[], valid=True) for i, t in
              enumerate([70.4068, 88.2071, 92.7976, 66.8710, 62.2533, 60.8125,
                         62.4133, 63.2741, 76.2119])],
        incidents=[contact], penalties=[]), session_id=result_sid)
    assert store.session_lap_ids(result_sid) == [], "fixture must leave no result laps"

    ctx = CoachContext(store, track=track, car=car, session_id=live_sid, on_track=True)
    out = ctx.get_race_result()

    assert "Started P9 on the grid" in out
    assert "P18 overall" in out and "Finished Normally" in out
    assert "Incidents (1)" in out and "contact" in out
    # The first captured lap ran P18; that is not a start position and must not be sold
    # as one -- reporting it as the start is what inverted the whole race narrative.
    assert "started P18" not in out and "started p18" not in out.lower()
    # With a known grid slot the disclaimer is unnecessary and must not appear.
    assert "grid position was not captured" not in out


def test_get_race_result_is_honest_while_a_second_race_is_running(tmp_path, synthetic_lap):
    """Mid-race-2 the only result log on disk is race 1's, because a race's log is not
    written until after it finishes. Reporting from it asserted a grid slot and
    'Finished Normally' for a race still being driven, and merged both races' laps into
    one arc -- a confident wrong where the tool used to say nothing."""
    from simcoach.lmu.results import ResultLap, ResultSession
    from simcoach.model_types import LapRecord
    track, car = "Silverstone National Circuit", "Team WRT 2026 #32:WEC"
    live_sid = "silverstone-national-circuit--team-wrt-2026-32-wec--offline"
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))

    def _laps(times, places, t0):
        for i, (lt, place) in enumerate(zip(times, places)):
            store.save(LapRecord(**{**synthetic_lap.__dict__, "track": track, "car": car,
                                    "lap_time": lt, "phase": "race", "position": place,
                                    "timestamp": t0 + 63.0 * i, "session_id": live_sid}))

    race1 = [70.4068, 88.2071, 92.7976, 66.8710, 62.2533, 60.8125, 62.4133, 63.2741]
    _laps(race1, [14, 12, 10, 9, 8, 7, 6, 6], 1789017974.0)
    store.save_result_session(ResultSession(
        sim="lmu", track=track, car=car, car_class="GT3", date_ts=1789017807.0,
        session_type="race", position=6, class_position=6, best_lap=60.8125,
        finish_status="Finished Normally", grid_position=9, class_grid_position=9,
        online=False, incidents=[], penalties=[],
        laps=[ResultLap(lap_no=i + 1, lap_time=t, sectors=[], valid=True)
              for i, t in enumerate(race1)]), session_id="2026_09_10_00_39_07-16R1",
        logged_ts=1789018800.0)
    # Race 2, two hours later, three laps in. Its log does not exist yet.
    _laps([64.1, 64.2, 64.3], [18, 17, 16], 1789017807.0 + 7200.0)

    ctx = CoachContext(store, track=track, car=car, session_id=live_sid, on_track=True)
    out = ctx.get_race_result()

    # No grid slot, no finish, no status invented for a race still running.
    assert "Started P9" not in out and "on the grid" not in out
    assert "Finished P6" not in out and "Finished Normally" not in out
    # And the arc covers race 2's three laps only, not all eleven.
    assert "over 3 captured lap(s)" in out
    assert "P18 on the first captured lap" in out and "P16 on the most recent" in out
    assert "grid position was not captured" in out


def test_no_lap_context_is_honest(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, track="spa", car="car", on_track=False)
    assert "no lap captured yet" in ctx.get_session_status().lower()
    assert "no lap" in ctx.get_corner("1").lower()


def test_list_track_corners_works_without_a_lap(tmp_path):
    # With a saved map but NO laps, the coach must still get corners FROM THE MAP
    # (authoritative), not recall them from memory.
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(store, track="test_track", car="car", cmap=cmap, on_track=True)
    out = ctx.list_track_corners()
    assert "T1 " in out
    assert cmap.corners[0].name in out  # the map's real corner name, not invented
    assert ctx.dispatch("list_track_corners", {}).startswith("Corners for")
    # session status advertises the map so the model knows the tool is worth calling
    assert "Corner map: yes" in ctx.get_session_status()


def _ctx_with_notes(notes):
    from simcoach.corners import Corner, CornerMap
    cm = CornerMap([Corner(number=1, name="Primeira", direction="left",
                           apex_dist=50.0, start_dist=10.0, end_dist=90.0, label="1")],
                   notes=notes)
    return CoachContext(store=None, cmap=cm, track="Algarve", car="GT3")


def test_get_track_notes_all():
    ctx = _ctx_with_notes({"overview": "rhythm track",
                           "corners": {"1": "brake earlier than it looks"}})
    out = ctx.get_track_notes()
    assert "rhythm track" in out
    assert "brake earlier" in out
    assert "T1" in out


def test_get_track_notes_single_corner():
    ctx = _ctx_with_notes({"overview": "rhythm track",
                           "corners": {"1": "brake earlier", "2": "flat out"}})
    out = ctx.get_track_notes(corner="1")
    assert "brake earlier" in out
    assert "flat out" not in out          # other corners excluded


def test_get_track_notes_empty():
    ctx = _ctx_with_notes({})
    assert "no authored notes" in ctx.get_track_notes().lower()


def test_get_track_notes_includes_reference_times():
    ctx = _ctx_with_notes({"overview": "rhythm track", "corners": {},
                           "reference_times": {"LMGT3": "1:41.500", "Hypercar": "1:35.200"}})
    out = ctx.get_track_notes()
    assert "1:41.500" in out and "LMGT3" in out
    assert "1:35.200" in out and "Hypercar" in out
    assert "reference" in out.lower()


def test_get_track_notes_no_reference_times_omits_line():
    ctx = _ctx_with_notes({"overview": "rhythm track", "corners": {"1": "brake late"}})
    assert "reference" not in ctx.get_track_notes().lower()


def test_reference_times_not_in_single_corner_view():
    ctx = _ctx_with_notes({"overview": "x", "corners": {"1": "brake late"},
                           "reference_times": {"LMGT3": "1:41.500"}})
    assert "1:41.500" not in ctx.get_track_notes(corner="1")


def _knowledge(slug):
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "simcoach" / "knowledge"
            / f"{slug}.md").read_text(encoding="utf-8")


def test_get_track_notes_falls_back_to_knowledge_file_when_no_map():
    # COTA has an authored note but NO corner-map JSON, so cmap is None. Before the
    # fallback the coach could not reach a word of it.
    ctx = CoachContext(store=None, cmap=None, track="Circuit of the Americas", car="GT3")
    out = ctx.get_track_notes()
    assert out == _knowledge("circuit_of_the_americas")   # verbatim, not reformatted
    assert "Bobby Pin" in out


def test_get_track_notes_falls_back_when_map_notes_are_empty():
    # A map file exists but carries no `notes` — the authored .md still gets through.
    ctx = _ctx_with_notes({})
    ctx.track = "Fuji Speedway"
    out = ctx.get_track_notes()
    assert out == _knowledge("fuji_speedway")
    assert "300R" in out


def test_map_notes_win_over_knowledge_file():
    # Guard (green before and after): the map file stays authoritative where it has
    # notes — the fallback must never shadow it.
    ctx = _ctx_with_notes({"overview": "rhythm track", "corners": {"1": "brake earlier"}})
    ctx.track = "Silverstone Grand Prix Circuit"   # has a knowledge/*.md too
    out = ctx.get_track_notes()
    assert "rhythm track" in out and "brake earlier" in out
    assert "Silverstone (Grand Prix layout) notes" not in out


def test_get_track_notes_with_neither_map_nor_knowledge_file():
    # Guard (green before and after): no map, no note -> the plain "none yet" answer.
    ctx = CoachContext(store=None, cmap=None, track="Nowhere Circuit", car="GT3")
    assert "no authored notes" in ctx.get_track_notes().lower()


def test_record_driver_learning_writes_pending(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    out = ctx.record_driver_learning("consistently late on throttle")
    assert "review" in out.lower()
    rows = store.list_learnings(status="pending")
    assert any(r["text"] == "consistently late on throttle" and r["source"] == "coach_tool"
               for r in rows)
    # not active -> not returned by get_driver_memories
    assert "consistently late on throttle" not in ctx.get_driver_memories()


def test_get_driver_memories_active_and_topic(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    store.add_learning("brakes early in the wet", status="active", source="manual")
    store.add_learning("good tyre management", status="active", source="manual")
    out = ctx.get_driver_memories()
    assert "brakes early in the wet" in out and "good tyre management" in out
    assert "tyre" in ctx.get_driver_memories(topic="tyre")
    assert "brakes early" not in ctx.get_driver_memories(topic="tyre")   # topic filters
    assert "no saved memories" in ctx.get_driver_memories(topic="nonexistent").lower()


def test_get_corner_appends_authored_note():
    ctx = _ctx_with_notes({"corners": {"1": "brake earlier than it looks"}})
    note = ctx._corner_note("1")
    assert note == "brake earlier than it looks"
    assert ctx._corner_note("99") is None


def test_corner_note_resolves_ranged_key():
    # A wiki note can cover a span of corners ("10-11"), but shipped corner
    # maps label corners as individual integers ("10", "11"). Both ends of
    # the range must resolve to the same authored note.
    ctx = _ctx_with_notes({"corners": {"10-11": "double-apex, brake early"}})
    assert ctx._corner_note("10") == "double-apex, brake early"
    assert ctx._corner_note("11") == "double-apex, brake early"
    assert ctx._corner_note("12") is None
    assert ctx._corner_note("8A") is None  # non-numeric label: exact match only, no crash


def test_corner_note_resolves_ranged_key_on_real_track():
    # Regression guard: synthetic single-corner maps in this file can't catch
    # a range-vs-integer-label mismatch. Algarve's real shipped notes have a
    # "10-11" range key while its real corner map labels corners "10"/"11".
    cmap = CornerMap.load("simcoach/tracks/algarve_international_circuit.json")
    assert "10-11" in (cmap.notes.get("corners") or {})
    labels = {c.label or str(c.number) for c in cmap.corners}
    assert {"10", "11"} <= labels
    ctx = CoachContext(store=None, cmap=cmap, track="Algarve", car="GT3")
    assert "brake earlier than it looks" in ctx.get_track_notes(corner="10")
    assert ctx._corner_note("10") == ctx._corner_note("11")


def test_corner_note_survives_labels_int_cannot_parse():
    # The label reaching the read path comes from the map, and the editor's label box
    # is free text -- '²' U+00B2, '₂' U+2082, '⑥' U+2465 all satisfy str.isdigit() and
    # RAISE on int(). Parsing the label inside the range check made _corner_note (and
    # so the LLM-callable get_track_notes) throw for any map carrying one.
    ctx = _ctx_with_notes({"overview": "rhythm track",
                           "corners": {"1-3": "the opening complex", "²": "the squared one"}})
    assert ctx._corner_note("²") == "the squared one"        # its own exact key wins
    assert "the squared one" in ctx.get_track_notes(corner="²")
    for odd in ("₂", "⑥"):                                   # no exact key, no bind
        assert ctx._corner_note(odd) is None
        assert f"No authored note for corner {odd}." in ctx.get_track_notes(corner=odd)
    assert ctx._corner_note("2") == "the opening complex"    # the range still resolves
    assert "the squared one" in ctx.get_track_notes()        # listing all notes is fine


def test_corner_note_survives_an_overlong_range_key():
    # A note key of 5000 digits is persistable (`PUT /notes` validates nothing) and
    # made the int() parse raise -- for EVERY label, including ones the map does not
    # have, because the poison key is reached before any comparison. One such key on
    # disk took out the whole read path, while GET .../notes still returned 200 so the
    # editor showed nothing wrong.
    poison = "1-" + "9" * 5000
    ctx = _ctx_with_notes({"corners": {poison: "nonsense", "1": "brake late"}})
    assert ctx._corner_note("1") == "brake late"     # ordinary labels keep working
    assert ctx._corner_note("99") is None
    assert ctx._corner_note("2") is None             # the poison key names nothing
    assert "brake late" in ctx.get_track_notes(corner="1")


def test_corner_note_exact_label_wins_in_either_dict_order():
    # GUARD (passes before this commit too). Pins the precedence rule that lives in
    # _corner_note rather than in the shared grammar, so a later "simplification" that
    # folds the exact-label lookup into the range scan cannot silently lose it.
    # An exact '12' note must beat a '11-13' range covering it whichever order the
    # authored dict happens to carry them in -- the read path looks the label up
    # before it scans for a covering range, so this cannot depend on insertion order.
    for corners in ({"12": "the exact one", "11-13": "the complex"},
                    {"11-13": "the complex", "12": "the exact one"}):
        ctx = _ctx_with_notes({"corners": corners})
        assert ctx._corner_note("12") == "the exact one", corners
        assert ctx._corner_note("11") == "the complex", corners


def test_get_corner_output_includes_authored_note(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    ctx.cmap.notes = {"corners": {"1": "brake earlier than it looks"}}
    out = ctx.get_corner("1")
    assert "Authored note: brake earlier than it looks" in out


def test_get_corner_output_includes_baked_geometry(tmp_path, synthetic_lap):
    # Issue #35: elevation/banking baked into the map must reach the coach
    # through get_corner, not be left stranded at map-build time. radius_m is
    # present but must NOT reach the coach (#99: the shipped bakes are stale).
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    ctx.cmap.geometry = {"1": {"radius_m": 66.0, "elev_delta_m": -11.0, "banking_deg": 2.0}}
    out = ctx.get_corner("1")
    # T1 is a LEFT hairpin and +2.0 favours right (9756b65), so this reads
    # off-camber -- the corner's computed direction must reach the clause (#35).
    assert ("Geometry (baked from the track map): "
            "downhill 11m, 2\N{DEGREE SIGN} off-camber") in out
    assert "radius" not in out
    # same magnitude, sign flipped to favour the left-hander -> banked
    ctx.cmap.geometry = {"1": {"radius_m": 66.0, "banking_deg": -2.0}}
    assert "2\N{DEGREE SIGN} banked" in ctx.get_corner("1")


def test_get_corner_output_omits_geometry_line_when_none_baked(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    out = ctx.get_corner("1")
    assert "Geometry" not in out


def test_corner_geometry_clause_empty_for_unknown_corner():
    ctx = _ctx_with_notes({})
    assert ctx._corner_geometry_clause("99", None) == ""


def test_get_setup_surfaces_step_position(tmp_path, synthetic_lap):
    # Issue #32: minValue/maxValue are ORDINAL option indices, not physical bounds.
    # Real captured shape: VM_BRAKE_BALANCE = stringValue "51.5:48.5", value 22,
    # minValue 0, maxValue 57 -- bias *position* 22 of 0-57, NOT "max 57".
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    ctx.setup = {
        "VM_BRAKE_BALANCE": {"stringValue": "51.5:48.5", "value": 22, "isFreeSetting": True,
                             "minValue": 0, "maxValue": 57},
    }
    out = ctx.get_setup()
    assert "Brake Balance: 51.5:48.5 (step 22 of 0-57)" in out
    assert "(range" not in out


def test_get_setup_surfaces_live_electronics_over_garage(tmp_path, synthetic_lap):
    """Issue #33: get_setup must state the LIVE on-track dial read as the truth for
    an electronic the latest lap captured, tagging the garage value for context --
    and leave the garage caveat only where no live value exists (mechanical params
    unaffected). A past session's stored setup (get_last_setup) has no live lap
    behind it, so it must never carry a live tag."""
    import dataclasses

    import numpy as np
    n = len(synthetic_lap.channels["lap_distance"])
    ch = {**synthetic_lap.channels,
          "tc_map": np.full(n, np.nan), "tc_map_max": np.full(n, np.nan),
          "rear_brake_bias": np.full(n, np.nan)}
    ch["tc_map"][-1] = 3
    ch["tc_map_max"][-1] = 11
    ch["rear_brake_bias"][-1] = 0.495
    lap = dataclasses.replace(synthetic_lap, channels=ch)
    ctx, _, _ = _ctx(tmp_path, lap)
    ctx.setup = {
        "VM_TRACTIONCONTROLMAP": {"stringValue": "6", "value": 6, "isFreeSetting": True,
                                  "minValue": 0, "maxValue": 11},
        "VM_BRAKE_BALANCE": {"stringValue": "51.5:48.5", "value": 22, "isFreeSetting": True,
                             "minValue": 0, "maxValue": 57},
        "VM_REAR_WING": {"stringValue": "6.5 deg", "value": 6, "isFreeSetting": True,
                         "minValue": 0, "maxValue": 9},
    }
    out = ctx.get_setup()
    assert "Tractioncontrolmap: 3 of 0-11 [live on-track; garage 6]" in out
    assert "step 6" not in out.split("Tractioncontrolmap")[1].split("\n")[0]
    assert "Brake Balance: 50.5:49.5 [live on-track; garage 51.5:48.5]" in out
    assert "Live values are from the latest captured lap." in out
    assert "Rear Wing: 6.5 deg (step 6 of 0-9)" in out

    store = ctx.store
    store.save_session_setup("prior--offline", synthetic_lap.track, synthetic_lap.car, {
        "VM_TRACTIONCONTROLMAP": {"stringValue": "6", "value": 6, "isFreeSetting": True,
                                  "minValue": 0, "maxValue": 11},
    })
    last = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car).get_last_setup()
    assert "live on-track" not in last


def test_get_setup_omits_step_without_ordinal(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    ctx.setup = {
        "VM_BRAKE_BALANCE": {"stringValue": "51.5:48.5", "isFreeSetting": True,
                             "minValue": 0, "maxValue": 57},
    }
    out = ctx.get_setup()
    assert "Brake Balance: 51.5:48.5" in out
    assert "0-57" not in out


def test_get_last_setup_renders_step_position(tmp_path):
    # The third _setup_lines caller must render identically -- and must not
    # double-format a line that already carries its step suffix.
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save_session_setup("s1--online", "spa", "gt3", {
        "VM_REAR_WING": {"stringValue": "6.5 deg", "value": 6, "isFreeSetting": True,
                         "minValue": 0, "maxValue": 9},
    })
    ctx = CoachContext(store, track="spa", car="gt3")
    out = ctx.dispatch("get_last_setup", {})
    assert "Rear Wing: 6.5 deg (step 6 of 0-9)" in out
    assert out.count("step 6 of 0-9") == 1


def test_dispatch_routes_and_handles_unknown(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    assert "\nT1 " in ctx.dispatch("get_corner", {"corner": "1"})
    assert "unknown tool" in ctx.dispatch("bogus", {})


def test_agentic_loop_dispatches_tool_calls(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    # Script the fake model to call get_corner, then answer.
    model = FakeModel(reply="Turn 1 looks good.",
                      tool_script=[("get_corner", {"corner": "1"})])
    coach = Coach(model)
    res = coach.ask_agentic(ctx, "how's turn 1?", "test_car")
    assert res.text == "Turn 1 looks good."
    assert res.usage.total > 0          # usage rides back out with the answer
    # the tool actually ran and produced the corner line
    assert model.dispatched[0][0] == "get_corner"
    assert "\nT1 " in model.dispatched[0][2]
    # tools were advertised to the model
    assert model.last_tools == TOOLS


def test_agentic_system_prompt_has_units_and_tools_guidance(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    model = FakeModel(reply="ok")
    Coach(model).ask_agentic(ctx, "q", "test_car", units="imperial")
    assert "mph" in model.last_system and "°F" in model.last_system
    assert "tool" in model.last_system.lower()


def test_get_personal_best_all_time_and_session_contrast(tmp_path, synthetic_lap):
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    # Older session: a 91.0s lap. Current session: a 93.0s lap (slower).
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1--online",
                            "timestamp": 1000.0, "lap_time": 91.0}))
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s2--online",
                            "timestamp": 2000.0, "lap_time": 93.0}))
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s2--online")
    out = ctx.dispatch("get_personal_best", {})
    assert "1:31.000" in out  # all-time best from the older session
    assert "1:33.000" in out  # this session's best, contrasted


def test_get_personal_best_no_laps(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, track="nowhere", car="nocar")
    assert "no recorded laps" in ctx.get_personal_best().lower()


def test_get_personal_best_finds_laps_under_raw_entry_name(tmp_path, synthetic_lap, monkeypatch):
    # ctx.car is the RESOLVED model ("BMW M4 LMGT3"); laps are stored under the raw
    # team entry name ("test_car"). get_personal_best must still find them (matches
    # how the other history tools already resolve this via store.session_summary).
    import simcoach.store as store_mod
    monkeypatch.setattr(store_mod, "resolve_car",
                        lambda c: "BMW M4 LMGT3" if c == "test_car" else c)
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    from simcoach.model_types import LapRecord
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1--online"}))
    ctx = CoachContext(store, track=synthetic_lap.track, car="BMW M4 LMGT3")
    out = ctx.dispatch("get_personal_best", {})
    assert "1:32.500" in out


def test_get_progress_lists_sessions_in_order(tmp_path, synthetic_lap):
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1--online",
                            "timestamp": 1000.0, "lap_time": 95.0}))
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s2--online",
                            "timestamp": 2000.0, "lap_time": 92.0}))
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car)
    out = ctx.dispatch("get_progress", {})
    assert out.index("1:35.000") < out.index("1:32.000")  # chronological order
    assert "faster" in out.lower()


def test_get_progress_no_history(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, track="nowhere", car="nocar")
    assert "no session history" in ctx.get_progress().lower()


def test_list_past_sessions_excludes_current_and_shows_result(tmp_path, synthetic_lap):
    from simcoach.lmu.results import ResultSession
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "past--online",
                            "timestamp": 1000.0, "lap_time": 95.0}))
    rs = ResultSession(sim="lmu", track=synthetic_lap.track, car=synthetic_lap.car,
                       car_class="GT3", date_ts=1, session_type="race", position=4,
                       class_position=1, best_lap=95.0, finish_status="Finished",
                       laps=[], incidents=[], penalties=[])
    store.save_result_session(rs, session_id="past--online")
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "current--online",
                            "timestamp": 2000.0, "lap_time": 92.0}))
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="current--online")
    out = ctx.dispatch("list_past_sessions", {})
    assert "P4" in out
    assert "1:35.000" in out
    assert "1:32.000" not in out  # current session excluded


def test_list_past_sessions_none_recorded(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, track="nowhere", car="nocar")
    assert "no past sessions" in ctx.list_past_sessions().lower()


def test_get_last_setup_returns_stored_setup(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    setup = {"VM_WING_REAR": {"stringValue": "6", "isFreeSetting": True}}
    store.save_session_setup("s1--online", "spa", "gt3", setup)
    ctx = CoachContext(store, track="spa", car="gt3")
    out = ctx.dispatch("get_last_setup", {})
    assert "Wing Rear: 6" in out


def test_get_last_setup_none_stored(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, track="spa", car="gt3")
    assert "no stored setup" in ctx.get_last_setup().lower()


def test_get_last_setup_excludes_current_session(tmp_path):
    # "what did we run here LAST time?" must return the PRIOR session's setup,
    # not the one the current session already autosaved.
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save_session_setup("prev--online", "spa", "gt3",
                             {"VM_WING_REAR": {"stringValue": "6", "isFreeSetting": True}})
    store.save_session_setup("cur--online", "spa", "gt3",
                             {"VM_WING_REAR": {"stringValue": "9", "isFreeSetting": True}})
    ctx = CoachContext(store, track="spa", car="gt3", session_id="cur--online")
    out = ctx.get_last_setup()
    assert "Wing Rear: 6" in out          # prior session
    assert "Wing Rear: 9" not in out       # current session excluded
    # Only the current session on record -> nothing prior to report.
    solo = CoachContext(store, track="spa", car="gt3", session_id="prev--online")
    # (prev is now the "current" here; cur--online is newer but different combo? no —
    #  both same combo, so prev-as-current excludes prev, leaving cur as the prior)
    assert "Wing Rear: 9" in solo.get_last_setup()


def test_current_lap_resolved_from_store_when_not_pinned(tmp_path, synthetic_lap):
    # The coach is no longer handed a lap (record=None). 'latest' and the corner
    # tools resolve the most-recent stored lap on demand — the fix for the coach
    # fixating on a UI-selected lap.
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save(synthetic_lap)
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(store, record=None, lap_id=None, cmap=cmap,
                       track=synthetic_lap.track, car=synthetic_lap.car, on_track=False)
    summary = ctx.get_lap_summary("latest")
    assert "Corner facts" in summary and "T1 " in summary
    assert "\nT1 " in ctx.get_corner("1")
    assert "Current lap: yes" in ctx.get_session_status()


# --- get_time_loss (issue #38 slice 1) ----------------------------------------

def _time_loss_ctx(tmp_path, synthetic_lap):
    """A store holding a clean reference lap plus a slower current lap that loses
    time ONLY inside T1's 800-1200 m window. Returns (ctx, cur_lap_id)."""
    import dataclasses

    import numpy as np
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")

    def clone(lap_time, slow_in_t1=False):
        ch = {k: v.copy() for k, v in synthetic_lap.channels.items()}
        if slow_in_t1:
            d = ch["lap_distance"]
            ch["speed"][(d >= 900) & (d <= 1100)] = 15.0   # half the corner speed
        return dataclasses.replace(synthetic_lap, lap_time=lap_time, channels=ch,
                                   session_id="s1--online", phase="race",
                                   timestamp=lap_time)

    store.save(clone(90.0))                              # the reference (faster)
    cur = clone(95.0, slow_in_t1=True)
    cur_id = store.save(cur)
    ctx = CoachContext(store, record=cur, lap_id=cur_id, cmap=cmap,
                       track=cur.track, car=cur.car, session_id="s1--online",
                       on_track=True)
    assert np.isfinite(cur.channels["speed"]).all()
    return ctx, cur_id


def test_get_time_loss_names_the_worst_corner(tmp_path, synthetic_lap):
    ctx, _ = _time_loss_ctx(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_time_loss", {})
    assert "Net" in out and "-" in out          # the loss is reported and signed
    assert "T1 Left Hairpin" in out             # attributed to the owned corner name
    assert out.index("T1 Left Hairpin") > out.index("Biggest losses")


def test_get_time_loss_is_honest_without_a_reference(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)    # only one lap stored
    assert "reference" in ctx.get_time_loss().lower()


# --- get_lap_summary leads with the time-loss ranking (#102) ------------------

def test_get_lap_summary_leads_with_the_ranking_and_the_worst_corner_first(tmp_path, synthetic_lap):
    ctx, _ = _time_loss_ctx(tmp_path, synthetic_lap)   # loses time only in T1
    out = ctx.get_lap_summary("latest")
    assert out.splitlines()[0].startswith("Lap: ")   # #108 item 1: lap identity first
    assert out.splitlines()[1].startswith("Events this lap: ")   # #108 items 3+4
    ranking_line = out.splitlines()[2]
    assert "Net" in ranking_line and "-" in ranking_line
    assert out.index("T1 Left Hairpin") < out.index("T2 Right Sweeper")
    assert "\N{BLACK UP-POINTING TRIANGLE}" in out   # the top-loss corner is marked


def test_get_lap_summary_reorders_the_worst_corner_ahead_of_track_order(tmp_path, synthetic_lap):
    # T1 is first in track order; make T2 -- not T1 -- the one losing time, so
    # reordering (not track order) is what puts it first.
    import dataclasses
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")

    def clone(lap_time, slow_in_t2=False):
        ch = {k: v.copy() for k, v in synthetic_lap.channels.items()}
        if slow_in_t2:
            d = ch["lap_distance"]
            ch["speed"][(d >= 2100) & (d <= 2300)] = 15.0
        return dataclasses.replace(synthetic_lap, lap_time=lap_time, channels=ch,
                                   session_id="s2--online", phase="race", timestamp=lap_time)

    store.save(clone(90.0))
    cur = clone(95.0, slow_in_t2=True)
    cur_id = store.save(cur)
    ctx = CoachContext(store, record=cur, lap_id=cur_id, cmap=cmap, track=cur.track,
                       car=cur.car, session_id="s2--online", on_track=True)

    out = ctx.get_lap_summary("latest")
    assert out.splitlines()[0].startswith("Lap: ")   # #108 item 1: lap identity first
    assert out.splitlines()[1].startswith("Events this lap: ")   # #108 items 3+4
    assert out.splitlines()[2].startswith("Net")
    assert out.index("T2 Right Sweeper") < out.index("T1 Left Hairpin")


def test_get_lap_summary_unchanged_without_a_reference(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)    # only one lap stored -- no reference
    out = ctx.get_lap_summary("latest")
    assert out.splitlines()[0].startswith("Lap: ")   # #108 item 1: lap identity first
    assert out.splitlines()[1].startswith("Events this lap: ")   # #108 items 3+4
    assert out.splitlines()[2] == "Corner facts (authoritative, from telemetry):"


def test_get_time_loss_refuses_to_compare_a_lap_to_itself(tmp_path, synthetic_lap):
    ctx, _ = _time_loss_ctx(tmp_path, synthetic_lap)
    # 'best' as the subject lap would otherwise be compared against itself.
    assert "itself" in ctx.get_time_loss(which="best").lower()


def test_get_time_loss_registered_in_tools_and_dispatch(tmp_path, synthetic_lap):
    assert any(t["name"] == "get_time_loss" for t in TOOLS)
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    assert not ctx.dispatch("get_time_loss", {}).startswith("error:")


def test_every_advertised_tool_is_dispatchable(tmp_path, synthetic_lap):
    # A TOOLS entry with no dispatch wiring is a tool the model can call and always
    # get "error: unknown tool" from.
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    for spec in TOOLS:
        assert not ctx.dispatch(spec["name"], {}).startswith("error: unknown tool")


def test_agentic_loop_can_call_get_time_loss(tmp_path, synthetic_lap):
    ctx, _ = _time_loss_ctx(tmp_path, synthetic_lap)
    model = FakeModel(reply="You're losing it at Turn 1.",
                      tool_script=[("get_time_loss", {})])
    answer = Coach(model).ask_agentic(ctx, "where am I losing time?", "test_car")
    assert answer.text == "You're losing it at Turn 1."
    assert model.dispatched[0][0] == "get_time_loss"
    assert "Net" in model.dispatched[0][2]


def test_agentic_prompt_points_at_get_time_loss(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    model = FakeModel(reply="ok")
    Coach(model).ask_agentic(ctx, "where am I losing time?", "test_car")
    assert "get_time_loss" in model.last_system


# --- get_consistency (#38) ------------------------------------------------------

def _consistency_session(tmp_path, synthetic_lap, scales, phase="practice"):
    """A stored session of len(scales) valid laps, each a clone of synthetic_lap
    with T1's min-speed window (900-1100, minimum at 1015 -- see conftest) scaled
    by that lap's factor -- mirrors test_analysis.py::_consistency_stint but
    through the store, so get_consistency exercises session_laps/load/
    _analysis_for like the real tool does."""
    import dataclasses
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    session_id = "s1--online"
    ids = []
    for i, scale in enumerate(scales):
        ch = {k: v.copy() for k, v in synthetic_lap.channels.items()}
        d = ch["lap_distance"]
        win = (d >= 900) & (d <= 1100)
        ch["speed"][win] = ch["speed"][win] * scale
        lap = dataclasses.replace(synthetic_lap, channels=ch, lap_time=90.0 + i,
                                  session_id=session_id, phase=phase, timestamp=90.0 + i)
        ids.append(store.save(lap))
    cur = store.load(ids[-1])
    return CoachContext(store, record=cur, lap_id=ids[-1], cmap=cmap, track=cur.track,
                        car=cur.car, session_id=session_id, on_track=True)


def test_get_consistency_names_the_noisy_corner(tmp_path, synthetic_lap):
    ctx = _consistency_session(tmp_path, synthetic_lap, [1.0, 0.95, 1.05, 0.90, 1.10])
    out = ctx.dispatch("get_consistency", {})
    assert "Lap time" in out
    assert "T1 Left Hairpin" in out


def test_get_consistency_honest_with_two_laps(tmp_path, synthetic_lap):
    ctx = _consistency_session(tmp_path, synthetic_lap, [1.0, 1.0])
    out = ctx.get_consistency()
    assert "2 laps" in out and "consistency" in out.lower()


def test_get_consistency_registered_in_tools_and_dispatch(tmp_path, synthetic_lap):
    assert any(t["name"] == "get_consistency" for t in TOOLS)
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)   # no active session -- still dispatchable
    assert not ctx.dispatch("get_consistency", {}).startswith("error:")


def test_agentic_prompt_points_at_get_consistency(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    model = FakeModel(reply="ok")
    Coach(model).ask_agentic(ctx, "am I consistent?", "test_car")
    assert "get_consistency" in model.last_system


# --- get_tire_trend (#38 P1.6) ----------------------------------------------

def _tire_trend_session(tmp_path, synthetic_lap, n, phase="practice"):
    """A stored session of n valid laps -- same shape as `_consistency_session`,
    but without the T1 speed-window scaling (get_tire_trend reads tire/wheel
    channels, which synthetic_lap already carries on every lap)."""
    import dataclasses
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    session_id = "s1--online"
    ids = []
    for i in range(n):
        lap = dataclasses.replace(synthetic_lap, lap_time=90.0 + i,
                                  session_id=session_id, phase=phase, timestamp=90.0 + i)
        ids.append(store.save(lap))
    cur = store.load(ids[-1])
    return CoachContext(store, record=cur, lap_id=ids[-1], cmap=cmap, track=cur.track,
                        car=cur.car, session_id=session_id, on_track=True)


def test_get_tire_trend_five_laps_reports_pace(tmp_path, synthetic_lap):
    ctx = _tire_trend_session(tmp_path, synthetic_lap, n=5)
    out = ctx.dispatch("get_tire_trend", {})
    assert "s/lap" in out


def test_get_tire_trend_honest_with_two_laps(tmp_path, synthetic_lap):
    ctx = _tire_trend_session(tmp_path, synthetic_lap, n=2)
    out = ctx.get_tire_trend()
    assert "2 laps" in out and "tire trend" in out.lower()


def test_get_tire_trend_registered_in_tools_and_dispatch(tmp_path, synthetic_lap):
    assert any(t["name"] == "get_tire_trend" for t in TOOLS)
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)   # no active session -- still dispatchable
    assert not ctx.dispatch("get_tire_trend", {}).startswith("error:")


def test_agentic_prompt_points_at_get_tire_trend(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    model = FakeModel(reply="ok")
    Coach(model).ask_agentic(ctx, "are my tires going off?", "test_car")
    assert "get_tire_trend" in model.last_system


# --- get_handling (#38) ----------------------------------------------------

def _understeer_lap(synthetic_lap):
    """A clone of synthetic_lap with T1 (800-1200, min speed at 1015) forced
    into an understeer signature: steering keeps winding to the min-speed
    sample while lat_g sits flat -- see analysis.handling_read."""
    import dataclasses

    import numpy as np
    ch = {k: v.copy() for k, v in synthetic_lap.channels.items()}
    d = ch["lap_distance"]
    winding = (d >= 800) & (d <= 1015)
    ch["steering"][winding] = np.linspace(0.1, 0.6, int(winding.sum()))
    ch["lat_g"][(d >= 800) & (d <= 1200)] = 1.5
    return dataclasses.replace(synthetic_lap, channels=ch)


def test_get_handling_names_the_understeer_corner(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, _understeer_lap(synthetic_lap))
    out = ctx.dispatch("get_handling", {})
    assert "T1 Left Hairpin" in out
    assert "understeer" in out.lower()


def test_get_handling_honest_on_a_clean_lap(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_handling", {})
    assert "no handling flags" in out.lower() and "neutral" in out.lower()


def test_get_handling_registered_in_tools_and_dispatch(tmp_path, synthetic_lap):
    assert any(t["name"] == "get_handling" for t in TOOLS)
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    assert not ctx.dispatch("get_handling", {}).startswith("error:")


def test_agentic_prompt_points_at_get_handling(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    model = FakeModel(reply="ok")
    Coach(model).ask_agentic(ctx, "is the car understeering?", "test_car")
    assert "get_handling" in model.last_system

# Either side of the #67 fix merging (56aa319). BEFORE is the stored timestamp of the
# answer that told a P9 -> P18 driver "you started P18 ... No incidents".
_BEFORE_67_FIX = 1789018942.56
_AFTER_67_FIX = 1789057327.0


@pytest.mark.parametrize("excerpt", [
    # Every grip shape among the driver's stored answers (#72)...
    "with min grip reading zero you're asking for more than the rear can give",
    "min grip reading 0.00 there confirms the tires weren't gripping at all",
    "- **Min grip 0.00** logged mid-corner",
    "**TC hammers in at 48%** and grip drops to **0.00** -- the rear letting go completely",
    # ...and a capital that only a case-insensitive match catches.
    "Min Grip reading zero mid-corner",
])
def test_scrub_retracts_a_fabricated_grip_reading_whenever_written(excerpt):
    # The channel never carried data, so there is no cutoff: a row from any time is void.
    for ts in (_BEFORE_67_FIX, _AFTER_67_FIX):
        out = scrub_stored_answer(excerpt, ts)
        assert out.startswith(excerpt + "\n\n[Correction") and "mGripFract" in out
        assert "never the grid slot" not in out


@pytest.mark.parametrize("excerpt", [
    # Every start / incident shape among the pre-fix stored answers (#67)...
    "you started P3, had a rough patch that dropped you as low as P15",
    "you started **P18** and crossed the line **P18**",
    "I shouldn't have assumed that P1 starting spot",
    "that P1→P34 overall swing is almost certainly the prototypes",
    "**P14 to P9** is a solid haul through the field",
    "it explains the drop from P4 down toward P11",
    # ...where only a case-insensitive match catches the capital.
    "No incidents or penalties logged.",
])
def test_scrub_retracts_a_race_start_claim_only_before_the_fix(excerpt):
    out = scrub_stored_answer(excerpt, _BEFORE_67_FIX)
    assert out.startswith(excerpt + "\n\n[Correction") and "never the grid slot" in out
    # After the fix the same words are the coach reading a real result log.
    assert scrub_stored_answer(excerpt, _AFTER_67_FIX) == excerpt


def test_race_retraction_lets_get_race_result_speak_only_for_the_same_race():
    # Resuming from the Sessions screen re-opens a chat in its OWN session, where
    # get_race_result is the true result for that race; continuing an old chat while a
    # different session is selected is where it is not. The retraction must allow the
    # first and refuse the second -- not blanket-forbid the tool (which discards the
    # correct result on the common path) nor blanket-direct the model to it.
    claim = "you started **P18** and crossed the line **P18**"
    out = scrub_stored_answer(claim, _BEFORE_67_FIX, "Silverstone National Circuit")
    assert "only if the track it names is Silverstone National Circuit" in out
    assert "do not attach its figures" not in out
    # A row with no stored track still gets the condition, just without a name to match.
    assert "only if it is that same race" in scrub_stored_answer(claim, _BEFORE_67_FIX)


def test_get_race_result_names_the_track_of_the_session_it_reports(tmp_path, synthetic_lap):
    # The label must come from the session the figures describe, not ctx.track: that
    # follows the live game or the last capture, so reviewing one session while the game
    # sits at another track labelled one track's result with the other's name -- a false
    # fact stated as fact, the #67 class.
    from simcoach.model_types import LapRecord
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "sel-1", "phase": "race",
                            "timestamp": 5.0, "position": 4}))
    ctx.session_id = "sel-1"
    ctx.track = "Fuji Speedway"   # the live game, at a different track entirely
    out = ctx.get_race_result()
    assert out.startswith(f"Session track: {synthetic_lap.track}.")
    assert "Fuji" not in out


@pytest.mark.parametrize("excerpt", [
    # Authored-style grip prose: a blanket "grip" filter would gut knowledge/ and cars/.
    "You ran out of front grip on entry; the mechanical grip balance is rearward.",
    # Pre-fix stored answers that name a position without claiming a start.
    "that P1 driver was almost **1.7 seconds** faster than you",
    "your last outing dipped to 1:28.602 (P32)",
])
def test_scrub_leaves_every_other_answer_untouched(excerpt):
    assert scrub_stored_answer(excerpt, _BEFORE_67_FIX) == excerpt


def test_get_tire_trend_works_without_a_corner_map(tmp_path, synthetic_lap):
    # Tire trend is a lap-level read: a track with no corner map (26% of stored
    # telemetry laps) must still get its stint trend, not a "no corner map" refusal.
    ctx = _tire_trend_session(tmp_path, synthetic_lap, n=5)
    ctx.cmap = None
    out = ctx.dispatch("get_tire_trend", {})
    assert "s/lap" in out and "corner map" not in out


# --- lap identity on every per-lap tool result (#108 item 1) ---------------
# La Source #108: "no lockup" then a minute later "IS showing a lockup" -- both
# true, `which="latest"` had moved to a newer lap, and no tool result said which
# lap it described. Every single-lap tool must now lead with `Lap: ...`, with
# game-lap-number and wall-clock time distinct enough that a wrong lap fails.

def _lap_identity_session(tmp_path, synthetic_lap):
    """Two race laps in one session with distinct lap_no/timestamp/lap_time, so
    an identity line naming the wrong lap fails the assertions."""
    import dataclasses
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    session_id = "s1--offline"
    lap2 = dataclasses.replace(synthetic_lap, session_id=session_id, phase="race",
                               lap_no=2, timestamp=1_700_000_000.0, lap_time=91.0)
    lap3 = dataclasses.replace(synthetic_lap, session_id=session_id, phase="race",
                               lap_no=3, timestamp=1_700_000_060.0, lap_time=90.0)
    id2 = store.save(lap2)
    id3 = store.save(lap3)
    ctx = CoachContext(store, record=lap3, lap_id=id3, cmap=cmap, track=lap3.track,
                       car=lap3.car, session_id=session_id, on_track=True)
    return ctx, store, id2, id3


def test_get_lap_summary_starts_with_lap_identity(tmp_path, synthetic_lap):
    ctx, *_ = _lap_identity_session(tmp_path, synthetic_lap)
    out = ctx.get_lap_summary("latest")
    assert out.startswith("Lap: ")
    assert "race lap 3" in out
    assert "game lap 3" in out
    assert re.search(r"\d{2}:\d{2}:\d{2}", out)


def test_get_handling_starts_with_lap_identity(tmp_path, synthetic_lap):
    ctx, *_ = _lap_identity_session(tmp_path, synthetic_lap)
    out = ctx.get_handling("latest")
    assert out.startswith("Lap: ")
    assert "race lap 3" in out
    assert "game lap 3" in out
    assert re.search(r"\d{2}:\d{2}:\d{2}", out)


def test_get_corner_starts_with_lap_identity(tmp_path, synthetic_lap):
    ctx, *_ = _lap_identity_session(tmp_path, synthetic_lap)
    out = ctx.get_corner("1", "latest")
    assert out.startswith("Lap: ")
    assert "race lap 3" in out
    assert "game lap 3" in out
    assert re.search(r"\d{2}:\d{2}:\d{2}", out)


def test_get_time_loss_identity_names_subject_and_reference_laps(tmp_path, synthetic_lap):
    ctx, *_ = _lap_identity_session(tmp_path, synthetic_lap)
    out = ctx.get_time_loss("latest")
    assert out.startswith("Lap: ")
    assert "race lap 3" in out          # the subject lap (latest, faster of the two)
    assert "game lap 3" in out
    assert " vs best: " in out
    assert "game lap 2" in out          # the REFERENCE lap's identity, not the subject's


def test_get_tire_report_and_wheel_report_start_with_lap_identity(tmp_path, synthetic_lap):
    ctx, *_ = _lap_identity_session(tmp_path, synthetic_lap)
    for out in (ctx.get_tire_report(), ctx.get_wheel_report()):
        assert out.startswith("Lap: ")
        assert "race lap 3" in out
        assert "game lap 3" in out


def test_lap_identity_omits_game_lap_fragment_when_not_stored(tmp_path, synthetic_lap):
    # synthetic_lap has no lap_no and timestamp=0.0 -- no fragment must be invented.
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    out = ctx.get_lap_summary("latest")
    assert out.startswith("Lap: ")
    assert "game lap" not in out


def test_latest_lap_identity_is_cached_once_per_turn(tmp_path, synthetic_lap):
    # #108: "latest" must resolve once per CoachContext (one per coach turn) so two
    # tool calls in the same turn describe the same lap, even if a newer lap lands
    # mid-turn. A fresh context picks up the newer lap.
    import dataclasses
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    lap1 = dataclasses.replace(synthetic_lap, lap_no=1, timestamp=100.0, lap_time=95.0)
    lap2 = dataclasses.replace(synthetic_lap, lap_no=2, timestamp=200.0, lap_time=94.0)
    store.save(lap1)
    store.save(lap2)
    ctx = CoachContext(store, cmap=cmap, track=lap1.track, car=lap1.car, on_track=True)

    first = ctx.get_lap_summary("latest")
    assert "game lap 2" in first

    lap3 = dataclasses.replace(synthetic_lap, lap_no=3, timestamp=300.0, lap_time=93.0)
    store.save(lap3)

    second = ctx.get_lap_summary("latest")
    assert "game lap 2" in second
    assert "game lap 3" not in second   # cached -- the new lap doesn't leak into this turn

    ctx2 = CoachContext(store, cmap=cmap, track=lap1.track, car=lap1.car, on_track=True)
    fresh = ctx2.get_lap_summary("latest")
    assert "game lap 3" in fresh


# --- stint event log (#108 items 3+4) ---------------------------------------

def _events_session(tmp_path, synthetic_lap):
    """4 race laps: lap 2 spins at T2, lap 3 takes a 4x FR wear step at T1."""
    import dataclasses

    import numpy as np

    from simcoach.analysis import _SPEED_KMH_TO_MS

    def spin_at(base, lo=2190.0, hi=2230.0, turn=200.0, v_from=180.0, v_min=30.0):
        ch = dict(base.channels)
        d = np.asarray(ch["lap_distance"], float)
        h = np.radians(np.clip((d - lo) / (hi - lo), 0.0, 1.0) * turn)
        ds = np.r_[0.0, np.diff(d)]
        ch["pos_x"] = np.cumsum(ds * np.cos(h))
        ch["pos_z"] = np.cumsum(ds * np.sin(h))
        v = np.asarray(ch["speed"], float).copy()
        v[(d >= lo - 60) & (d < lo)] = v_from
        seg = (d >= lo) & (d <= hi)
        v[seg] = np.interp(d[seg], [lo, hi], [v_from, v_min])
        v[(d > hi) & (d <= hi + 60)] = v_min
        ch["speed"] = v
        ch["time"] = np.cumsum(np.r_[0.0, np.diff(d) / np.maximum(
            v[1:] * _SPEED_KMH_TO_MS, 1.0)])
        return dataclasses.replace(base, channels=ch)

    def wear_step_at(base, wheel="fr", drop=0.09, at_dist=1000.0):
        ch = dict(base.channels)
        d = np.asarray(ch["lap_distance"], float)
        wear = np.asarray(ch[f"tire_wear_{wheel}"], float).copy()
        wear[int(np.argmin(np.abs(d - at_dist))):] -= drop
        ch[f"tire_wear_{wheel}"] = wear
        return dataclasses.replace(base, channels=ch)

    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    session_id = "s1--online"
    laps = [synthetic_lap, spin_at(synthetic_lap), wear_step_at(synthetic_lap), synthetic_lap]
    ids = [store.save(dataclasses.replace(lap, session_id=session_id, phase="race",
                                          lap_time=90.0 + i, timestamp=90.0 + i))
           for i, lap in enumerate(laps)]
    cur = store.load(ids[-1])
    ctx = CoachContext(store, cmap=cmap, track=cur.track, car=cur.car,
                       session_id=session_id, on_track=True)
    return ctx, ids


def test_get_stint_events_names_the_lap_and_the_corner(tmp_path, synthetic_lap):
    ctx, _ = _events_session(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_stint_events", {})
    assert "race lap 2" in out and "race lap 3" in out
    assert "T2 Right Sweeper: spin" in out
    assert "FR wear step" in out and "T1 Left Hairpin" in out
    # the two clean laps say so rather than going unmentioned
    assert out.count("none") == 2


def test_get_stint_events_scopes_to_the_last_n_laps(tmp_path, synthetic_lap):
    ctx, _ = _events_session(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_stint_events", {"laps": 2})
    assert "race lap 1" not in out and "race lap 2" not in out
    assert "race lap 3" in out and "race lap 4" in out


def test_get_stint_events_is_advertised_to_the_model():
    spec = next(t for t in TOOLS if t["name"] == "get_stint_events")
    assert "spin" in spec["description"]
    assert "laps" in spec["input_schema"]["properties"]


def test_get_lap_summary_leads_with_events_this_lap(tmp_path, synthetic_lap):
    ctx, _ = _events_session(tmp_path, synthetic_lap)
    out = ctx.get_lap_summary("race lap 2")
    lines = out.splitlines()
    assert lines[0].startswith("Lap: ")
    assert lines[1].startswith("Events this lap: ")
    assert "spin" in lines[1]
    # a clean lap says so rather than dropping the line
    assert ctx.get_lap_summary("race lap 1").splitlines()[1] == "Events this lap: none"


def test_get_wheel_report_appends_the_wear_step(tmp_path, synthetic_lap):
    ctx, ids = _events_session(tmp_path, synthetic_lap)
    ctx.record, ctx.lap_id = ctx.store.load(ids[2]), ids[2]
    out = ctx.get_wheel_report()
    assert "FR wear step" in out and "T1 Left Hairpin" in out
