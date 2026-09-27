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


def test_get_corner_locates_the_corner(tmp_path, synthetic_lap):
    """#108 item 12: get_corner appends a spoken-ready locator, computed from the
    owned map + measured speed, not left for the model to invent."""
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    out = ctx.get_corner("1")
    assert "Locate it: Turn 1, " in out


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
    # 6 stored laps (>= _ENVELOPE_MIN_LAPS, #108 item 2) give `grip_envelope`
    # a formed read (the fixture's own lat_g/long_g clear the load gate).
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for _ in range(6):
        store.save(synthetic_lap)
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       on_track=True)
    summary = ctx.get_lap_summary("latest")
    assert "Grip envelope" in summary
    assert "% of it in the corners" in summary


def test_get_lap_summary_envelope_still_forming_under_5_laps(tmp_path, synthetic_lap):
    # #108 item 2: with 2 stored laps, the envelope hasn't formed -- report
    # peak g and say so, not a (possibly meaningless, >100%) percentage.
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for _ in range(2):
        store.save(synthetic_lap)
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       on_track=True)
    summary = ctx.get_lap_summary("latest")
    assert "still forming: 2 of 5 laps" in summary
    assert "Peak combined grip this lap" in summary
    for line in summary.splitlines():
        assert "of envelope" not in line


def test_get_lap_summary_clamps_grip_pct_at_100(tmp_path, synthetic_lap):
    # #108 item 2: an envelope formed from 6 mild laps, read against a lap whose
    # own g is amplified -- gg_used lands > 1.0 without a real crash spike. The
    # driver must never see a percentage over 100.
    import dataclasses

    import numpy as np
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for _ in range(6):
        store.save(synthetic_lap)
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    hot_ch = {**synthetic_lap.channels,
              "lat_g": np.asarray(synthetic_lap.channels["lat_g"]) * 2.0,
              "long_g": np.asarray(synthetic_lap.channels["long_g"]) * 2.0}
    hot_lap = dataclasses.replace(synthetic_lap, channels=hot_ch)
    ctx = CoachContext(store, record=hot_lap, cmap=cmap, track=hot_lap.track,
                       car=hot_lap.car, on_track=True)
    summary = ctx.get_lap_summary("latest")
    assert "100%" in summary
    for pct in re.findall(r"(\d+)%", summary):
        assert int(pct) <= 100, summary


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
    ctx, _store, lap_id = _ctx(tmp_path, synthetic_lap)
    status = ctx.get_session_status()
    assert "test_track" in status and "Complete laps" in status
    listing = ctx.list_recent_laps()
    assert f"lap {lap_id}:" in listing


def test_session_status_reports_class_and_aid_availability(tmp_path, synthetic_lap):
    # #108 item 5: class + ABS/TC come from data (car_class + the setup's
    # availability flags), never asserted from memory.
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    ctx.car_class = "LMGT3"
    ctx.setup = {
        "VM_ANTILOCK_BRAKES": {"stringValue": "Available"},
        "VM_TRACTION_CONTROL": {"stringValue": "N/A"},
    }
    status = ctx.get_session_status()
    assert "Class: LMGT3" in status
    assert "ABS: available" in status
    assert "TC: not fitted" in status


def test_session_status_class_and_aids_unknown_without_data(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    status = ctx.get_session_status()
    assert "Class: unknown" in status
    assert "ABS: unknown" in status
    assert "TC: unknown" in status


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
    assert "session: 8" in status


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


def test_race_position_in_recent_laps_lap_line_and_status(tmp_path, synthetic_lap):
    # #134: P8 -> P25 -> P20 -> P11 (a lap-2 spin, then a recovery) must surface in
    # list_recent_laps, the Lap: identity line, and get_session_status -- all read
    # from the stored `laps.position` column, no LLM guess.
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    positions = [8, 25, 20, 11]
    ids = [store.save(LapRecord(**{**synthetic_lap.__dict__,
                                   "session_id": "s1", "phase": "race",
                                   "timestamp": float(i + 1), "lap_no": i + 1,
                                   "position": p}))
           for i, p in enumerate(positions)]
    ctx = CoachContext(
        store,
        record=LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "race"}),
        lap_id=ids[-1], session_id="s1", cmap=cmap,
        track=synthetic_lap.track, car=synthetic_lap.car,
    )

    listing = ctx.list_recent_laps()
    for label, pos in zip(("race lap 1", "race lap 2", "race lap 3", "race lap 4"), positions):
        line = next(l for l in listing.splitlines() if l.startswith(label + ":"))
        assert line.endswith(f"P{pos} overall"), line  # #162: never a bare P

    # The spin's cost: lap 3 (P20) shown against the previous race lap's position (P25).
    lap_line = ctx.get_lap_summary("race lap 3").splitlines()[0]
    assert "position P20 overall (from P25 overall)" in lap_line

    status = ctx.get_session_status()
    # #67: the first captured lap is never called a start.
    assert ("Position: P11 overall (P8 overall on the first captured lap, low P25 "
            "overall)") in status
    assert "started" not in status.lower()


def test_practice_session_shows_no_position(tmp_path, synthetic_lap):
    # A position value stored on a non-race lap must never surface -- position is
    # only meaningful (and only asked about) during a race.
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    lap_id = store.save(LapRecord(**{**synthetic_lap.__dict__,
                                     "session_id": "s1", "phase": "practice",
                                     "timestamp": 1.0, "lap_no": 1, "position": 5}))
    ctx = CoachContext(
        store,
        record=LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1", "phase": "practice"}),
        lap_id=lap_id, session_id="s1", cmap=cmap,
        track=synthetic_lap.track, car=synthetic_lap.car,
    )

    assert "P5" not in ctx.list_recent_laps()
    assert "position P" not in ctx.get_lap_summary("practice lap 1")
    assert "Position:" not in ctx.get_session_status()


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
    # #162: the log lists issued AND served lines -- entries, not a penalty count.
    assert "Penalty log (1 entry)" in out and "Track limits DT" in out

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
    assert "P5 overall on the first captured lap" in out and "as low as P10 overall" in out
    assert "P3 overall on the most recent" in out
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

    assert "Started P9 overall on the grid" in out
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
    assert "P18 overall on the first captured lap" in out
    assert "P16 overall on the most recent" in out
    assert "grid position was not captured" in out


def test_no_lap_context_is_honest(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, track="spa", car="car", on_track=False)
    assert "no lap captured yet" in ctx.get_session_status().lower()
    assert "no lap" in ctx.get_corner("1").lower()


def test_list_track_corners_entries_end_with_descriptor(tmp_path, synthetic_lap):
    """#108 item 12: each corner line ends with the same spoken locator get_corner
    gives, computed from the latest lap's analysis when one is available."""
    from simcoach.format import corner_descriptor
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    out = ctx.list_track_corners()
    analysis = ctx._latest_analysis()
    lines = out.split("\n")[1:]
    assert len(lines) == len(ctx.cmap.corners)
    for c, line in zip(ctx.cmap.corners, lines):
        assert line.endswith(corner_descriptor(ctx.cmap, c.number, analysis=analysis))


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
    assert "no notes on file" in ctx.get_track_notes().lower()


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
    assert "no notes on file" in ctx.get_track_notes().lower()


def test_record_driver_learning_writes_pending(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    out = ctx.record_driver_learning("consistently late on throttle")
    assert "review" in out.lower()
    rows = store.list_learnings(status="pending")
    assert any(r["text"] == "consistently late on throttle" and r["source"] == "coach_tool"
               for r in rows)
    # not active -> not returned by get_driver_memories
    assert "consistently late on throttle" not in ctx.get_driver_memories()


# --- #139: replaces updates a note in place instead of appending a correction -

def test_record_driver_learning_first_save_returns_the_id(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    out = ctx.record_driver_learning("contact exiting Turn 15")
    m = re.search(r"note (\d+)", out)
    assert m, out
    row = store.get_learning(int(m.group(1)))
    assert row["text"] == "contact exiting Turn 15"


def test_record_driver_learning_replaces_updates_the_row_in_place(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    first = ctx.record_driver_learning("contact exiting Turn 15")
    note_id = int(re.search(r"note (\d+)", first).group(1))

    out = ctx.record_driver_learning("contact at Turn 13, not 15", replaces=note_id)

    assert f"note {note_id}" in out
    row = store.get_learning(note_id)
    assert row["text"] == "contact at Turn 13, not 15"
    assert row["status"] == "pending"
    # no new row -- the correction replaced, it didn't append
    assert len(store.list_learnings(status="pending")) == 1


def test_record_driver_learning_replaces_a_nonqualifying_id_appends_instead(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    # a note from a different, already-reviewed conversation
    other_id = store.add_learning("unrelated fact", status="active", track="t", car="c",
                                   source="coach_tool", session_id="some-other-session")

    out = ctx.record_driver_learning("new correction", replaces=other_id)

    assert "wasn't from this conversation" in out
    assert store.get_learning(other_id)["text"] == "unrelated fact"     # untouched
    pending = store.list_learnings(status="pending")
    assert any(r["text"] == "new correction" for r in pending)          # appended


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
    # #128: no session, so no in-session label -- falls back to the bare lap id.
    assert re.search(r"Live values from the last sample of lap id \d+\.", out)
    assert "Rear Wing: 6.5 deg (step 6 of 0-9)" in out

    store = ctx.store
    store.save_session_setup("prior--offline", synthetic_lap.track, synthetic_lap.car, {
        "VM_TRACTIONCONTROLMAP": {"stringValue": "6", "value": 6, "isFreeSetting": True,
                                  "minValue": 0, "maxValue": 11},
    })
    last = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car).get_last_setup()
    assert "live on-track" not in last


def _electronics_session(tmp_path, synthetic_lap, laps):
    """A practice session of stored laps, each `(kind, tc_index, tc_max, ts)` --
    kind None for a complete lap, else 'out'/'in'/'partial'. tc_max None leaves
    the TC channel unpopulated (never captured this lap). Returns (store, ids)."""
    import dataclasses

    import numpy as np
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    n = len(synthetic_lap.channels["lap_distance"])
    ids = []
    for i, (kind, tc, tc_max, ts) in enumerate(laps, start=1):
        ch = {**synthetic_lap.channels,
              "tc_map": np.full(n, np.nan), "tc_map_max": np.full(n, np.nan)}
        if tc_max is not None:
            ch["tc_map"][-1] = tc
            ch["tc_map_max"][-1] = tc_max
        lap = dataclasses.replace(synthetic_lap, channels=ch, session_id="s1",
                                  phase="practice", partial=kind,
                                  valid=kind is None, lap_time=None if kind else 90.0,
                                  lap_no=i, timestamp=ts)
        ids.append(store.save(lap))
    return store, ids


def test_get_setup_live_electronics_from_newest_lap_of_any_kind(tmp_path, synthetic_lap):
    """Issue #128: a complete lap at TC 5 followed by a later in-lap whose last
    sample is TC 10 -- get_setup must report 10 (the newest stored lap of any
    kind), naming the in-lap, not stay pinned to the last complete lap's TC 5."""
    store, _ids = _electronics_session(tmp_path, synthetic_lap, [
        (None, 5, 11, 100.0),
        (None, 5, 11, 200.0),
        ("in", 10, 11, 20.51 * 3600),  # 20:51:00 local -- distinguishable timestamp
    ])
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True)
    ctx.setup = {
        "VM_TRACTIONCONTROLMAP": {"stringValue": "6", "value": 6, "isFreeSetting": True,
                                  "minValue": 0, "maxValue": 11},
    }
    out = ctx.get_setup()
    assert "Tractioncontrolmap: 10 of 0-11 [live on-track; garage 6]" in out
    assert "practice lap 3 (in-lap)" in out
    assert "Live values from the last sample of practice lap 3 (in-lap)" in out


def test_get_setup_live_electronics_unchanged_with_only_a_complete_lap(tmp_path, synthetic_lap):
    """No in-lap yet -- behaviour matches before #128: the one complete lap's TC 5."""
    store, _ids = _electronics_session(tmp_path, synthetic_lap, [
        (None, 5, 11, 100.0),
    ])
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True)
    ctx.setup = {
        "VM_TRACTIONCONTROLMAP": {"stringValue": "6", "value": 6, "isFreeSetting": True,
                                  "minValue": 0, "maxValue": 11},
    }
    out = ctx.get_setup()
    assert "Tractioncontrolmap: 5 of 0-11 [live on-track; garage 6]" in out
    assert "practice lap 1" in out


def test_get_setup_falls_back_to_complete_lap_when_newest_lap_has_no_electronics(
        tmp_path, synthetic_lap):
    """The newest lap (an in-lap) never populated the TC block (Max == 0, i.e.
    unset here) -- get_setup must fall back to the newest COMPLETE lap rather
    than go quiet or silently report stale garage values, and say so."""
    store, _ids = _electronics_session(tmp_path, synthetic_lap, [
        (None, 5, 11, 100.0),
        ("in", None, None, 200.0),   # TC channel never populated this lap
    ])
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True)
    ctx.setup = {
        "VM_TRACTIONCONTROLMAP": {"stringValue": "6", "value": 6, "isFreeSetting": True,
                                  "minValue": 0, "maxValue": 11},
    }
    out = ctx.get_setup()
    assert "Tractioncontrolmap: 5 of 0-11 [live on-track; garage 6]" in out
    assert "Live values from the last sample of practice lap 1" in out
    assert "practice lap 2 (in-lap) had no electronics captured." in out


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


def test_get_personal_best_set_this_session_names_lap_and_age(tmp_path, synthetic_lap):
    # #130: the all-time best IS the lap just driven this session -- say so with
    # its label and how long ago, not a bare date that reads as history.
    import time as time_mod

    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    now = time_mod.time()
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1--online",
                            "phase": "qualifying", "timestamp": now - 200, "lap_time": 95.0}))
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1--online",
                            "phase": "qualifying", "timestamp": now - 13, "lap_time": 91.0}))
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1--online")
    out = ctx.dispatch("get_personal_best", {})
    assert "1:31.000" in out
    assert "this session's qualifying lap 2" in out
    m = re.search(r"set (\d+) s ago", out)
    assert m and 5 <= int(m.group(1)) <= 30, out   # ~13s, tolerant of test runtime


def test_get_personal_best_older_session_keeps_date_wording(tmp_path, synthetic_lap):
    # PB was set in a PAST session, not this one -- wording stays unchanged (date,
    # plus the session-best contrast line), same as before #130.
    import time as time_mod

    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1--online",
                            "timestamp": 1000.0, "lap_time": 91.0}))
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s2--online")   # different session -- no laps in it
    out = ctx.dispatch("get_personal_best", {})
    expected_date = time_mod.strftime("%Y-%m-%d", time_mod.localtime(1000.0))
    assert "1:31.000" in out
    assert f", set {expected_date}." in out
    assert "s ago" not in out
    assert "this session's" not in out


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


# #182: real-shaped Sarthe rows (copied from the driver's DB). Live AND result-log laps
# store the same raw team ENTRY name; only car_lookup's result-log table maps it to a
# model/class, and a brand word can match a car with no timed laps (Corvette GTE).
_SARTHE = "Circuit de la Sarthe"
_Z06_ENTRY = "Racing Team Turkey by TF 2026 #34:WEC"
_ENTRIES = {
    _Z06_ENTRY: ("Chevrolet Corvette Z06 LMGT3.R", "GT3"),
    "United Autosports 2025 #95:LM": ("McLaren 720S LMGT3 Evo", "GT3"),
    "Corvette Racing #33:LM": ("Corvette C8.R GTE", "GTE"),
    "Cadillac Racing #3:LM": ("Cadillac V-Series.R", "Hyper"),
}


def _sarthe_store(tmp_path, synthetic_lap, monkeypatch):
    from simcoach import car_lookup
    from simcoach.model_types import LapRecord
    monkeypatch.setattr(car_lookup, "_entry_table", lambda: _ENTRIES)
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save(LapRecord(**{**synthetic_lap.__dict__, "track": _SARTHE, "car": _Z06_ENTRY,
                            "session_id": "Circuit de la Sarthe|x--offline",
                            "timestamp": 1790000000.0, "lap_time": 242.14111328125}))
    # lap 5964's shape: source='result', telemetry_path NULL, mapping 0, pit/partial NULL
    store.conn.executemany(
        "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path, phase, "
        "session_id, source, s1, s2, s3, mapping) "
        "VALUES (?, 'lmu', ?, ?, ?, ?, NULL, 'practice', ?, 'result', NULL, NULL, NULL, 0)",
        [(1785303279.0, _SARTHE, _Z06_ENTRY, 240.0089, 1, "2026_07_29_00_54_18-57P1"),
         (1785303000.0, _SARTHE, _Z06_ENTRY, 239.0, 0, "2026_07_29_00_54_18-57P1"),  # invalid
         (1782300000.0, _SARTHE, "United Autosports 2025 #95:LM", 244.4586, 1, "R2"),
         (1782300000.0, _SARTHE, "Corvette Racing #33:LM", None, 0, "R3"),
         (1782300000.0, _SARTHE, "Cadillac Racing #3:LM", 200.3782, 1, "R4")])
    store.conn.commit()
    return store


def test_personal_best_counts_result_log_lap_and_flags_no_telemetry(
        tmp_path, synthetic_lap, monkeypatch):
    store = _sarthe_store(tmp_path, synthetic_lap, monkeypatch)
    z06 = "Chevrolet Corvette Z06 LMGT3.R"
    out = CoachContext(store, track=_SARTHE, car=z06).dispatch("get_personal_best", {})
    assert "4:00.009" in out and "no telemetry" in out, out
    assert "3:59.000" not in out   # the invalid result lap is still excluded
    # best_lap is unchanged: telemetry-only, so analysis still gets the live lap
    assert store.best_lap(_SARTHE, z06).lap_time == pytest.approx(242.1411, abs=1e-3)


def test_personal_best_class_mode_lists_one_best_per_car(tmp_path, synthetic_lap, monkeypatch):
    store = _sarthe_store(tmp_path, synthetic_lap, monkeypatch)
    ctx = CoachContext(store, track=_SARTHE, car="Chevrolet Corvette Z06 LMGT3.R")
    out = ctx.dispatch("get_personal_best", {"car_class": "LMGT3"})
    assert out.count("Chevrolet Corvette Z06 LMGT3.R") == 1, out
    assert "Chevrolet Corvette Z06 LMGT3.R 4:00.009" in out
    assert "McLaren 720S LMGT3 Evo 4:04.459" in out
    assert "Cadillac" not in out and "3:20.378" not in out   # Hyper, not GT3


def test_personal_best_resolves_a_brand_word(tmp_path, synthetic_lap, monkeypatch):
    store = _sarthe_store(tmp_path, synthetic_lap, monkeypatch)
    ctx = CoachContext(store, track=_SARTHE, car="Cadillac V-Series.R")
    mclaren = ctx.dispatch("get_personal_best", {"car": "mclaren"})
    assert "in McLaren 720S LMGT3 Evo: 4:04.459" in mclaren, mclaren
    # 'Corvette' also names the GTE car, which has no timed laps here
    assert "in Chevrolet Corvette Z06 LMGT3.R: 4:00.009" in ctx.dispatch(
        "get_personal_best", {"car": "Corvette"})
    # an unmatched word lists what IS here, so the coach can retry without asking
    miss = ctx.dispatch("get_personal_best", {"car": "any GT3"})
    assert "McLaren 720S LMGT3 Evo (GT3)" in miss, miss


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
    assert "finished P4 overall" in out
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


def test_get_lap_summary_no_name_stutter_on_an_unnamed_map(tmp_path, synthetic_lap):
    """#141: a map whose corner names are just 'Turn N' must render the tag
    once ('T1'), not 'T1 Turn 1', anywhere in the time-loss block."""
    import dataclasses

    from simcoach.corners import Corner, CornerMap
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap([
        Corner(number=1, name="Turn 1", direction="left",
               start_dist=800.0, apex_dist=1000.0, end_dist=1200.0),
        Corner(number=2, name="Turn 2", direction="right",
               start_dist=2000.0, apex_dist=2200.0, end_dist=2400.0),
    ])

    def clone(lap_time, slow_in_t1=False):
        ch = {k: v.copy() for k, v in synthetic_lap.channels.items()}
        if slow_in_t1:
            d = ch["lap_distance"]
            ch["speed"][(d >= 900) & (d <= 1100)] = 15.0
        return dataclasses.replace(synthetic_lap, lap_time=lap_time, channels=ch,
                                   session_id="s1--online", phase="race", timestamp=lap_time)

    store.save(clone(90.0))
    cur = clone(95.0, slow_in_t1=True)
    cur_id = store.save(cur)
    ctx = CoachContext(store, record=cur, lap_id=cur_id, cmap=cmap,
                       track=cur.track, car=cur.car, session_id="s1--online", on_track=True)

    out = ctx.get_lap_summary("latest")
    assert "T1 Turn 1" not in out
    assert "T2 Turn 2" not in out
    assert "T1" in out and "T2" in out


def test_get_lap_summary_unchanged_without_a_reference(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)    # only one lap stored -- no reference
    out = ctx.get_lap_summary("latest")
    assert out.splitlines()[0].startswith("Lap: ")   # #108 item 1: lap identity first
    assert out.splitlines()[1].startswith("Events this lap: ")   # #108 items 3+4
    assert out.splitlines()[2] == "Corner facts (authoritative, from telemetry):"


def test_get_time_loss_on_the_best_lap_compares_to_next_best_not_itself(tmp_path, synthetic_lap):
    ctx, _ = _time_loss_ctx(tmp_path, synthetic_lap)
    # #173: 'best' used to resolve no lap id, so it was compared against itself (a
    # flat +0.00) or refused. It now resolves its id and meets its next-best lap,
    # labelled so -- never "best".
    out = ctx.get_time_loss(which="best")
    assert "itself" not in out.lower()
    assert "vs your next-best lap (race lap 2, +5.00 s)" in out
    assert "vs next-best: race lap 2" in out and "vs best" not in out


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
    # #169: the subject is the faster lap, so its reference is its NEXT-best lap --
    # never labelled "best".
    assert " vs next-best: " in out and " vs best: " not in out
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


# --- damage / contact (#143) --------------------------------------------------

def _damage_session(tmp_path, synthetic_lap):
    """3 race laps: lap 1 is pre-#143 (no damage channels at all), lap 2 has
    damage channels but no impact (clean), lap 3 takes one hit."""
    import dataclasses

    import numpy as np

    def zeroed(base):
        ch = dict(base.channels)
        n = len(np.asarray(ch["lap_distance"], float))
        for key in ("last_impact_et", "last_impact_mag", "last_impact_x",
                    "last_impact_z", "dent_max", "dent_total", "overheating",
                    "detached"):
            ch[key] = np.zeros(n)
        for w in ("fl", "fr", "rl", "rr"):
            ch[f"flat_{w}"] = np.zeros(n)
            ch[f"wheel_detached_{w}"] = np.zeros(n)
        return dataclasses.replace(base, channels=ch)

    def hit_at(base, dist=1240.0, et=2178.5, mag=365.47, x=-1.02, z=1.75):
        clean = zeroed(base)
        ch = dict(clean.channels)
        d = np.asarray(ch["lap_distance"], float)
        idx = int(np.argmin(np.abs(d - dist)))
        ch["last_impact_et"][idx:] = et
        ch["last_impact_mag"][idx:] = mag
        ch["last_impact_x"][idx:] = x
        ch["last_impact_z"][idx:] = z
        ch["dent_max"][idx:] = 1.0
        ch["dent_total"][idx:] = 1.0
        return dataclasses.replace(clean, channels=ch)

    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    session_id = "s1--online"
    laps = [synthetic_lap, zeroed(synthetic_lap), hit_at(synthetic_lap)]
    ids = [store.save(dataclasses.replace(lap, session_id=session_id, phase="race",
                                          lap_time=90.0 + i, timestamp=90.0 + i))
           for i, lap in enumerate(laps)]
    cur = store.load(ids[-1])
    ctx = CoachContext(store, cmap=cmap, track=cur.track, car=cur.car,
                       session_id=session_id, on_track=True)
    return ctx


def test_get_stint_events_prints_contact_for_the_damaged_lap_only(tmp_path, synthetic_lap):
    ctx = _damage_session(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_stint_events", {})
    assert out.count("Contact:") == 1
    assert "race lap 3" in out.split("Contact:")[0].splitlines()[-3]
    line = next(line for line in out.splitlines() if "Contact:" in line)
    assert "1 impact" in line
    assert "365" in line
    assert "1,240 m" in line
    assert "front-left" in line
    assert "dent severity max 1" in line


def test_get_stint_events_is_silent_on_a_clean_or_pre_143_lap(tmp_path, synthetic_lap):
    ctx = _damage_session(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_stint_events", {})
    lap1_block, lap2_block = out.split("Lap: race lap 2")[0], \
        out.split("Lap: race lap 2")[1].split("Lap: race lap 3")[0]
    assert "Contact:" not in lap1_block
    assert "Contact:" not in lap2_block


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


# --- issue #77: partial laps are listed and readable, never the default subject --

def _partial_session(tmp_path, synthetic_lap):
    """A practice run of: lap 1 out-lap (partial), lap 2 timed, lap 3 timed,
    lap 4 in-lap (partial) -- the shape the rig log recorded. Returns
    (ctx, store, ids-by-game-lap-number)."""
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    base = dict(synthetic_lap.__dict__, session_id="s1", phase="practice")
    ids = {}
    for n, (lt, kind, ts) in enumerate(
            [(None, "out", 10.0), (95.0, None, 20.0), (92.0, None, 30.0),
             (None, "in", 40.0)], start=1):
        ids[n] = store.save(LapRecord(**{**base, "lap_time": lt, "valid": kind is None,
                                         "partial": kind, "lap_no": n, "timestamp": ts}))
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True)
    return ctx, store, ids


def test_latest_lap_is_the_latest_COMPLETE_lap_not_the_in_lap(tmp_path, synthetic_lap):
    """The controller's ruling: a garage return must not make the in-lap the coach's
    default subject."""
    ctx, _, ids = _partial_session(tmp_path, synthetic_lap)
    record, lap_id = ctx._current_lap()
    assert lap_id == ids[3]
    assert record.lap_time == 92.0
    assert "practice lap 3" in ctx.get_lap_summary("latest")


def test_a_partial_lap_is_reachable_by_its_game_lap_number(tmp_path, synthetic_lap):
    """No new tool: the in-session numbering already covers every stored lap, and
    the identity line names the kind so the model can't read it as a timed lap."""
    ctx, _, ids = _partial_session(tmp_path, synthetic_lap)
    record, lap_id = ctx._resolve_lap("practice lap 4")
    assert lap_id == ids[4]
    assert record.partial == "in"
    assert ctx._lap_line(record, lap_id).endswith(" — in-lap")
    assert "practice lap 4" in ctx.get_lap_summary("practice lap 4")


def test_list_recent_laps_shows_partials_with_their_kind(tmp_path, synthetic_lap):
    """Greyed out in the UI, named in the tool output -- the numbering runs 1..4
    with no gap, which is the whole point of storing them."""
    ctx, _, _ = _partial_session(tmp_path, synthetic_lap)
    out = ctx.list_recent_laps()
    assert "practice lap 1: out lap" in out
    assert "practice lap 4: in lap" in out
    assert "practice lap 2: 1:35.000" in out
    for n in (1, 2, 3, 4):
        assert f"practice lap {n}:" in out


def test_consistency_and_time_loss_refuse_partial_laps(tmp_path, synthetic_lap):
    """Best-lap / consistency math must ignore them (they are valid = 0, so
    `_stint_analyses` drops them with no change), and a partial can never be
    compared against a reference."""
    ctx, _, _ = _partial_session(tmp_path, synthetic_lap)
    analyses, lap_times, _ = ctx._stint_analyses()
    assert lap_times == [95.0, 92.0]
    assert len(analyses) == 2
    assert "can't be compared" in ctx.get_time_loss("practice lap 4")


def test_session_status_counts_complete_laps_only(tmp_path, synthetic_lap):
    """A partial is a row in the lap list, not a lap of data -- counting it would
    inflate every 'laps captured' number the day partials started being stored."""
    ctx, _, _ = _partial_session(tmp_path, synthetic_lap)
    out = ctx.get_session_status()
    assert "this practice: 2, session: 2, all-time this track/car: 2" in out


def test_session_status_names_the_latest_lap_and_labels_counts_as_counts(tmp_path, synthetic_lap):
    """#126: the coach read 'this practice: 1' (a COUNT of complete laps) as a lap
    NUMBER and narrated a session reset. The status line must say what the latest
    STORED lap is (any kind) by its own label, and never use the bare word 'Laps'
    for a count. Game lap numbers have a gap at 3 (a garage return), same shape as
    the rig log, so the last row's label is 'practice lap 5', not 'lap 4'."""
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    base = dict(synthetic_lap.__dict__, session_id="s1", phase="practice")
    for n, (lt, kind, ts) in enumerate(
            [(None, "out", 10.0), (95.0, None, 20.0), (None, "partial", 30.0),
             (None, "partial", 40.0)], start=1):
        lap_no = n if n < 3 else n + 1   # gap at game lap 3
        store.save(LapRecord(**{**base, "lap_time": lt, "valid": kind is None,
                                 "partial": kind, "lap_no": lap_no, "timestamp": ts}))
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True)
    out = ctx.get_session_status()
    assert "Latest stored lap: practice lap 5 (partial)" in out
    assert "Complete laps (this practice: 1" in out
    assert "Other laps this session (out/in/partial): 3" in out


def test_pit_lap_flagged_in_lap_line_and_recent_laps(tmp_path, synthetic_lap):
    """#138: a pit-stop lap's time/deltas are the stop, not the driving -- both the
    `Lap: …` identity line and the recent-laps list must say so, and a normal lap
    must not."""
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    base = dict(synthetic_lap.__dict__, session_id="s1", phase="race")
    pit_id = store.save(LapRecord(**{**base, "lap_time": 140.0, "lap_no": 4,
                                     "timestamp": 10.0, "pit": True}))
    clean_id = store.save(LapRecord(**{**base, "lap_time": 95.0, "lap_no": 5,
                                       "timestamp": 20.0, "pit": False}))
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True)

    pit_record, _ = ctx._resolve_lap("race lap 4")
    clean_record, _ = ctx._resolve_lap("race lap 5")
    assert "pit stop this lap" in ctx._lap_line(pit_record, pit_id)
    assert "pit stop this lap" not in ctx._lap_line(clean_record, clean_id)

    listing = ctx.list_recent_laps()
    pit_line = next(l for l in listing.splitlines() if l.startswith("race lap 4:"))
    clean_line = next(l for l in listing.splitlines() if l.startswith("race lap 5:"))
    assert pit_line.endswith("(pit)")
    assert not clean_line.endswith("(pit)")


def test_lap_identity_prompt_explains_the_pit_flag():
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "pit stop this lap" in AGENTIC_SYSTEM_PROMPT
    assert "not a pace lap" in AGENTIC_SYSTEM_PROMPT


# --- #136: race boundaries inside one session -------------------------------

_T0 = 1_700_000_000.0


def _two_race_session(store, base, sid="s1"):
    """Tonight's shape (#136): race 1, a qualifying lap, then race 2 -- all under one
    session id, because the instance only splits after 6 h. Returns (race1, race2) lap
    ids."""
    from simcoach.model_types import LapRecord

    def _save(ts, phase, position=None):
        return store.save(LapRecord(**{**base.__dict__, "session_id": sid,
                                       "phase": phase, "timestamp": ts,
                                       "position": position}))

    # Race 1 is the LONGER one on purpose: an unqualified 'lap 3' then has a lap it
    # could wrongly resolve to in the earlier race, which is the failure being guarded.
    race1 = [_save(_T0 + i * 100, "race", 20 + i) for i in range(3)]
    _save(_T0 + 300, "qualifying")
    race2 = [_save(_T0 + 400 + i * 100, "race", 5 + i) for i in range(2)]
    return race1, race2


def _hhmm(ts):
    import time
    return time.strftime("%H:%M", time.localtime(ts))


def test_lap_labels_name_the_race_when_a_session_holds_more_than_one(tmp_path, synthetic_lap):
    """#136: 'race lap 2' is ambiguous across three races in one evening, and that
    ambiguity is what let the coach answer about race 3 with race 1's lap times."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    race1, race2 = _two_race_session(store, synthetic_lap)
    ctx = CoachContext(store, session_id="s1", track=synthetic_lap.track,
                       car=synthetic_lap.car)

    label_by_id, _ = ctx._lap_numbering()
    assert label_by_id[race1[0]] == "race 1 lap 1"
    assert label_by_id[race1[2]] == "race 1 lap 3"
    # Race 2 counts from 1 again -- it does not carry on from race 1's total.
    assert label_by_id[race2[0]] == "race 2 lap 1"
    assert label_by_id[race2[1]] == "race 2 lap 2"
    listing = ctx.list_recent_laps()
    assert "race 1 lap 1" in listing and "race 2 lap 1" in listing


def test_a_bare_lap_reference_resolves_in_the_current_race(tmp_path, synthetic_lap):
    """An unqualified reference means THIS race; an earlier one must be asked for by
    name ('race 1 lap 2' / 'lap 2 of race 1')."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    race1, race2 = _two_race_session(store, synthetic_lap)
    ctx = CoachContext(store, session_id="s1", track=synthetic_lap.track,
                       car=synthetic_lap.car)

    assert ctx._resolve_lap("lap 2")[1] == race2[1]
    assert ctx._resolve_lap("2")[1] == race2[1]
    assert ctx._resolve_lap("race lap 2")[1] == race2[1]
    assert ctx._resolve_lap("race 1 lap 2")[1] == race1[1]
    assert ctx._resolve_lap("lap 2 of race 1")[1] == race1[1]
    assert ctx._resolve_lap("race 2 lap 1")[1] == race2[0]
    # This race has two laps. An unqualified 'lap 3' must NOT reach back and hand
    # over race 1's third lap -- silently answering about the wrong race is #136.
    assert ctx._resolve_lap("lap 3") == (None, None)
    assert ctx._resolve_lap("race 1 lap 3")[1] == race1[2]


def test_the_second_race_counts_from_lap_1_across_a_gap_only_boundary(
        tmp_path, synthetic_lap):
    """Two races back to back with no other phase captured between them: the numbering
    has to restart at the boundary too, or race 2's first lap is called 'lap 4'."""
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ids = [store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1",
                                   "phase": "race", "timestamp": ts}))
           for ts in (_T0, _T0 + 100, _T0 + 200, _T0 + 1800, _T0 + 1900)]
    ctx = CoachContext(store, session_id="s1", track=synthetic_lap.track,
                       car=synthetic_lap.car)

    label_by_id, _ = ctx._lap_numbering()
    assert label_by_id[ids[2]] == "race 1 lap 3"
    assert label_by_id[ids[3]] == "race 2 lap 1"
    assert ctx._resolve_lap("lap 1")[1] == ids[3]


def test_one_race_keeps_the_plain_lap_label(tmp_path, synthetic_lap):
    """Nothing changes for the ordinary single-race session -- no race number to
    disambiguate, and 'race 1 lap 1' would just be noise."""
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ids = [store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1",
                                   "phase": "race", "timestamp": _T0 + i * 100}))
           for i in range(2)]
    ctx = CoachContext(store, session_id="s1", track=synthetic_lap.track,
                       car=synthetic_lap.car)

    label_by_id, _ = ctx._lap_numbering()
    assert label_by_id[ids[0]] == "race lap 1"
    assert "race 1 lap" not in ctx.list_recent_laps()
    assert ctx._resolve_lap("race lap 1")[1] == ids[0]


def test_get_session_status_counts_the_races_and_scopes_the_race_to_this_one(
        tmp_path, synthetic_lap):
    """'8 laps into the race' must be THIS race's laps, and the position arc this
    race's places -- unbounded, both spanned every race of the sitting."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    _two_race_session(store, synthetic_lap)
    ctx = CoachContext(store, session_id="s1", track=synthetic_lap.track,
                       car=synthetic_lap.car)

    out = ctx.get_session_status()
    assert f"Races this session: 2 (current: race 2, started {_hhmm(_T0 + 400)})" in out
    assert "this race: 2," in out          # race 2's two laps, not all four
    assert "Position: P6 overall (P5 overall on the first captured lap, low P6 overall)" in out   # race 2's places only


def test_get_race_result_names_which_race_of_the_sitting_it_reports(
        tmp_path, synthetic_lap):
    """The arc is already bounded to one race; without a name on it, an answer about
    race 2 reads exactly like an answer about race 1."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    _two_race_session(store, synthetic_lap)
    ctx = CoachContext(store, session_id="s1", track=synthetic_lap.track,
                       car=synthetic_lap.car)

    out = ctx.get_race_result()
    assert f"Race 2 of this session (started {_hhmm(_T0 + 400)}, 2 laps)." in out
    # And the figures under that heading are race 2's, not all four laps'.
    assert "over 2 captured lap(s)" in out
    assert "P5 overall on the first captured lap" in out
    assert "P6 overall on the most recent" in out


def test_get_race_result_says_nothing_about_races_in_a_single_race_session(
        tmp_path, synthetic_lap):
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for i in range(2):
        store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1",
                                "phase": "race", "timestamp": _T0 + i * 100,
                                "position": 4}))
    ctx = CoachContext(store, session_id="s1", track=synthetic_lap.track,
                       car=synthetic_lap.car)

    out = ctx.get_race_result()
    assert "of this session (started" not in out
    assert "Races this session" not in ctx.get_session_status()


def test_get_stint_events_does_not_walk_back_into_the_previous_race(
        tmp_path, synthetic_lap):
    """'that spin a few laps ago' asked in race 2 came back with race 1's spin: the
    scan counts back N laps from the newest one and walked straight out of this race."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    _two_race_session(store, synthetic_lap)
    ctx = CoachContext(store, session_id="s1", track=synthetic_lap.track,
                       car=synthetic_lap.car,
                       cmap=CornerMap.load("simcoach/tracks/test_track.json"))

    out = ctx.get_stint_events(laps=10)
    assert "race 2 lap 1" in out and "race 2 lap 2" in out
    assert "race 1 lap" not in out


def test_race_turn_note_marks_only_turns_from_an_earlier_race():
    """History annotation on READ, from stored timestamps (#136) -- the same shape as
    scrub_stored_answer, which never rewrites the driver's stored conversation."""
    from simcoach.coach_tools import race_turn_note
    windows = [{"index": 1, "start_ts": _T0, "end_ts": _T0 + 200, "lap_ids": [1, 2],
                "laps": 2},
               {"index": 2, "start_ts": _T0 + 400, "end_ts": _T0 + 500,
                "lap_ids": [4, 5], "laps": 2}]

    note = race_turn_note(_T0 + 250, windows)
    assert note == (f"[About an earlier race in this session (race 1, started "
                    f"{_hhmm(_T0)}) — not the current one.] ")
    # A turn inside the current race, and one from before any race lap, are left alone.
    assert race_turn_note(_T0 + 450, windows) == ""
    assert race_turn_note(_T0 - 60, windows) == ""
    # One race: nothing to mistake it for.
    assert race_turn_note(_T0 + 250, windows[:1]) == ""


def test_current_race_fact_only_when_the_session_holds_several_races():
    from simcoach.coach_tools import current_race_fact
    windows = [{"index": 1, "start_ts": _T0, "end_ts": _T0 + 200, "lap_ids": [1, 2],
                "laps": 2},
               {"index": 2, "start_ts": _T0 + 400, "end_ts": _T0 + 500,
                "lap_ids": [4, 5], "laps": 2}]

    fact = current_race_fact(windows)
    assert fact.startswith(f"CURRENT RACE: race 2 of this session (started "
                           f"{_hhmm(_T0 + 400)}, 2 laps).")
    assert "Earlier turns marked as earlier races are history" in fact
    assert current_race_fact(windows[:1]) == ""
    assert current_race_fact([]) == ""


# --- #152: the race tools read the cached snapshot only -----------------------

def _race_snapshot():
    from simcoach.model_types import RaceSample
    from simcoach.race import LapFacts, build_snapshot
    rs = RaceSample(class_position=3, class_ahead="A. Rival (Rival Entry)",
                    class_gap_ahead=2.5, max_laps=24)
    stint = [LapFacts(lap_no=n, lap_time=100.0, clean=True, position=7, race=rs,
                      fuel_used=2.5, fuel_end=e, fuel_capacity=100.0)
             for n, e in ((11, 27.5), (12, 25.0))]
    return build_snapshot(stint, "s")


def test_race_tools_answer_no_live_race_off_track_or_before_a_race_lap(tmp_path, synthetic_lap):
    ctx, store, _ = _ctx(tmp_path, synthetic_lap)
    assert ctx.dispatch("get_race_state", {}).startswith("No live race")   # no snapshot yet
    off = CoachContext(store, on_track=False, race=_race_snapshot())
    for tool in ("get_race_state", "get_strategy", "get_battle"):
        assert off.dispatch(tool, {}).startswith("No live race")


def test_race_tools_read_the_cached_snapshot_and_lead_with_its_lap(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    snap = _race_snapshot()
    ctx.race = snap
    state = ctx.dispatch("get_race_state", {})
    strat = ctx.dispatch("get_strategy", {})
    assert state.startswith("As of lap 12 (race).") and "P3 in class" in state
    assert strat.startswith("As of lap 12.") and "Fuel: 2.50 L/lap" in strat
    assert ctx.race is snap and ctx.dispatch("get_race_state", {}) == state


def test_race_state_states_the_live_penalty_count(tmp_path, synthetic_lap):
    """#162: Road Atlanta 2026-09-23 -- a drive-through ("Out of position") was issued
    on lap 1 and the coach never mentioned it. get_race_state says the game's live
    count, with or without a race snapshot yet (lap 1 has none)."""
    import dataclasses

    from simcoach.lmu.live import empty_snapshot
    sess = dataclasses.replace(empty_snapshot().session, live=True, phase="race",
                               penalties=1)
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    ctx.race, ctx.live_session = _race_snapshot(), sess
    assert ("Penalties: 1 outstanding (the game's live count; what it is for is not "
            "reported until the race's result log).") in ctx.dispatch("get_race_state", {})
    ctx.race, ctx.race_note = None, "Not enough laps yet: x"
    assert ctx.dispatch("get_race_state", {}).startswith("Not enough laps yet: x\nPenalties: 1")
    ctx.live_session = dataclasses.replace(sess, penalties=0)
    # red-team: 0 is never said -- the channel is unverified (a dead one reads 0), and
    # a c_short can hold garbage.
    for n in (0, -1, 30000):
        ctx.live_session = dataclasses.replace(sess, penalties=n)
        assert "Penalties" not in ctx.dispatch("get_race_state", {}), n
    ctx.live_session = dataclasses.replace(sess, phase="qualifying")
    assert "Penalties" not in ctx.dispatch("get_race_state", {})


def test_race_tools_say_the_race_note_when_a_live_race_has_no_fresh_snapshot(tmp_path):
    """#152 red-team: a live race whose snapshot is stale/another race's -> the note."""
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, on_track=True, race=None,
                       race_note="Not enough laps yet: x")
    for tool in ("get_race_state", "get_battle"):
        assert ctx.dispatch(tool, {}) == "Not enough laps yet: x"
    # #161: get_strategy tries a provisional plan instead -- here there is nothing to
    # plan from, and it says so rather than serving any other race's figures.
    assert ctx.dispatch("get_strategy", {}).startswith("No live race projection yet")


# --- issue #153: the energy report reads the session's latest complete lap -------

def _energy_session(tmp_path, synthetic_lap, laps):
    """Store `laps` [(partial kind, channel overrides, slice)] for session s1 and build
    the context EXACTLY as the server does (app.py: record=None, lap_id=None)."""
    import numpy as np

    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    n = len(synthetic_lap.channels["lap_distance"])
    for i, (kind, over, sl) in enumerate(laps, start=1):
        ch = {**synthetic_lap.channels, **over,
              "virtual_energy": over.get("virtual_energy", np.linspace(0.90, 0.85, n))}
        ch = {k: np.asarray(v)[sl] for k, v in ch.items()}
        store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1",
                                "phase": "practice", "channels": ch,
                                "lap_time": None if kind else 92.5, "valid": kind is None,
                                "partial": kind, "lap_no": i, "timestamp": 10.0 * i}))
    return CoachContext(store, record=None, lap_id=None,
                        cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track=synthetic_lap.track, car=synthetic_lap.car,
                        session_id="s1", on_track=True)


def test_energy_report_built_like_the_server_reads_the_stored_lap(tmp_path, synthetic_lap):
    ctx = _energy_session(tmp_path, synthetic_lap, [(None, {}, slice(None))])
    rep = ctx.dispatch("get_energy_report", {})
    assert "No lap captured" not in rep
    assert "Fuel: 2.60 L used this lap; 77.4 L in tank" in rep
    assert "Virtual energy: 5.0% used this lap; 85% remaining (~17.0 laps left)" in rep


def test_energy_report_never_reads_a_newer_in_lap(tmp_path, synthetic_lap):
    import numpy as np
    n = len(synthetic_lap.channels["lap_distance"])
    in_lap = {"fuel": np.linspace(77.4, 76.0, n),
              "virtual_energy": np.linspace(0.85, 0.83, n)}
    ctx = _energy_session(tmp_path, synthetic_lap,
                          [(None, {}, slice(None)), ("in", in_lap, slice(None))])
    rep = ctx.dispatch("get_energy_report", {})
    assert "practice lap 1" in rep
    assert "77.4 L in tank" in rep and "76.0" not in rep and "83%" not in rep


def test_energy_report_gives_no_burn_for_a_lap_picked_up_mid_lap(tmp_path, synthetic_lap):
    # Capture joined at ~1500 m: the fuel it saw burn is half a lap's, not a lap's.
    ctx = _energy_session(tmp_path, synthetic_lap, [(None, {}, slice(500, None))])
    rep = ctx.dispatch("get_energy_report", {})
    assert "used this lap" not in rep and "laps left" not in rep
    assert "Fuel: 77.4 L in tank at the end of that lap (burn not measured on a full lap" in rep
    assert "Virtual energy: 85% remaining (burn not measured on a full lap" in rep


def test_energy_report_skips_a_dead_virtual_energy_channel(tmp_path, synthetic_lap):
    # A car with no virtual energy captures a flat 0 -- a dead channel, never "0% remaining".
    import numpy as np
    n = len(synthetic_lap.channels["lap_distance"])
    ctx = _energy_session(tmp_path, synthetic_lap,
                          [(None, {"virtual_energy": np.zeros(n)}, slice(None))])
    rep = ctx.dispatch("get_energy_report", {})
    assert "Virtual energy" not in rep
    assert "Fuel: 2.60 L used this lap; 77.4 L in tank at the end of that lap" in rep


def test_get_battle_reads_the_cached_battle_and_takes_a_named_rival(tmp_path, synthetic_lap):
    from simcoach.battle import CarLap, build_battle
    from simcoach.corners import Corner
    from simcoach.race import LapFacts, build_snapshot
    t1 = Corner(1, "La Source", "right", 200.0, 250.0, 300.0, label="1")
    me = CarLap(1, "You", "E1", 3, True, entry={1: 104.0}, exit={1: 106.0})
    rival = CarLap(2, "A. Rival", "E2", 2, False, entry={1: 103.0}, exit={1: 105.3},
                   model="Ferrari 296 GT3")
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    assert ctx.dispatch("get_battle", {}).startswith("No live race")
    ctx.race = build_snapshot([LapFacts(lap_no=12, lap_time=100.0, clean=True)], "s",
                              battle=build_battle([me, rival], [t1]))
    out = ctx.dispatch("get_battle", {})
    assert out.startswith("As of lap 12.")
    assert "You are quicker: Turn 1 (La Source) by 0.30 s." in out
    named = ctx.dispatch("get_battle", {"rival": "P2"})
    assert "Rival (the car you named): A. Rival (Ferrari 296 GT3)" in named
    ctx.race = build_snapshot([LapFacts(lap_no=12, lap_time=100.0, clean=True)], "s")
    assert ctx.dispatch("get_battle", {}).startswith("As of lap 12. No battle read")
    ctx.race = build_snapshot([LapFacts(lap_no=12, lap_time=100.0, clean=True)], "s",
                              battle_note="there is no corner map for this track")
    assert ctx.dispatch("get_battle", {}) == (
        "As of lap 12. No battle read for lap 12: there is no corner map for this track.")


# --- #174: get_battle after the race, from the stored battle_laps rows -----------

def _post_race(tmp_path, synthetic_lap, on_track=False):
    """Race laps 1-5 of session s1 stored through the real store. Lap 1 (the start) has
    no battle rows; lap 5 has Momo 10 s away. On laps 2-4 Momo is 0.5 s behind at T1 and
    his per-lap deltas (his time minus yours; + = you quicker) are:
      T1           +0.05, +0.05, +0.60   median 0.05 (a SUM, 0.70, would list it)
      T1 exit->T2  +0.25, +0.30, +0.20   median 0.25 -> you are quicker
      T2           -0.30, -0.40, +0.50   median -0.30 -> he is quicker; lap 4 was
                                         YOURS, so only laps 2-3 are named for him"""
    from simcoach.battle import CarLap
    from simcoach.corners import Corner
    from simcoach.model_types import LapRecord
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap([Corner(1, "La Source", "right", 200.0, 250.0, 300.0, label="1"),
                      Corner(2, "Eau Rouge", "left", 600.0, 650.0, 700.0, label="2")])
    deltas = {2: (0.05, 0.25, 0.30), 3: (0.05, 0.30, 0.40), 4: (0.60, 0.20, -0.50)}
    # Not race laps of their own: a grid row (game lap 0) and lap 5's in-lap fragment.
    for n, ts, kind in ((0, 1000.0, "partial"), (5, 1590.0, "in")):
        store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1",
                                "phase": "race", "lap_no": n, "timestamp": ts,
                                "partial": kind, "lap_time": None, "valid": False}))
    for n in range(1, 6):
        lid = store.save(LapRecord(**{**synthetic_lap.__dict__, "session_id": "s1",
                                      "phase": "race", "lap_no": n,
                                      "timestamp": 1000.0 + 100.0 * n}))
        if n == 1:
            continue
        me = CarLap(1, "You", "E1", 4, True, entry={1: 10.0, 2: 20.0},
                    exit={1: 12.0, 2: 22.0})
        other = CarLap(3, "B. Other", "E3", 6, False, entry={1: 70.0, 2: 80.0},
                       exit={1: 72.0, 2: 82.0}, model="Porsche 911 GT3 R")
        d1, s, d2 = deltas.get(n, (0.0, 0.0, 0.0))
        t0 = 10.5 if n in deltas else 20.5    # lap 5: never within 2 s
        ex1 = t0 + 2.0 + d1
        en2 = ex1 + 8.0 + s
        momo = CarLap(2, "Momo Rossi", "E2", 3, False, entry={1: t0, 2: en2},
                      exit={1: ex1, 2: en2 + 2.0 - d2}, model="Ferrari 296 GT3")
        store.save_battle(lid, [me, momo, other])
    return CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                        session_id="s1", on_track=on_track)


def test_post_race_battle_is_the_median_per_zone_over_the_race_laps(tmp_path, synthetic_lap):
    ctx = _post_race(tmp_path, synthetic_lap)
    for args in ({}, {"rival": "momo"}, {"which": "race"}):
        out = ctx.dispatch("get_battle", args)
        assert "Momo Rossi (Ferrari 296 GT3), P3 in class" in out, args
        assert ("Compared on 3 of 5 race laps (race lap 2, race lap 3, race lap 4); skipped "
                "1 with no stored battle read, 1 with him never within 2 s of you.") in out
        assert ("That car is quicker: Turn 2 (Eau Rouge) by 0.30 s (median over 3 laps; "
                "beyond the noise on race lap 2, race lap 3).") in out
        assert ("You are quicker: Turn 1 (La Source) exit to Turn 2 (Eau Rouge) by 0.25 s "
                "(median over 3 laps; beyond the noise on race lap 2, race lap 3, "
                "race lap 4).") in out
        # T1's one big lap (0.60) never makes it a place he is slower: its median is noise.
        assert "Turn 1 (La Source) by" not in out
    assert "Rival (nearest to you on the most race laps)" in ctx.dispatch("get_battle", {})


def test_post_race_battle_for_one_stored_lap(tmp_path, synthetic_lap):
    ctx = _post_race(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_battle", {"rival": "Momo", "which": "race lap 4"})
    assert out.startswith("Stored read for race lap 4.\nAs of lap 4.")
    assert ("You are quicker: Turn 1 (La Source) by 0.60 s; Turn 2 (Eau Rouge) by 0.50 s; "
            "Turn 1 (La Source) exit to Turn 2 (Eau Rouge) by 0.20 s.") in out
    assert "That car is quicker: nowhere by more than the timing noise." in out
    assert ctx.dispatch("get_battle", {"which": "race lap 1"}).startswith(
        "No battle read is stored for race lap 1:")


def test_post_race_battle_names_the_field_when_the_rival_is_not_in_it(tmp_path, synthetic_lap):
    """Only the driver's class is recorded, so a car from another class (or a name
    nobody has) is not found -- and the stored field is listed, like live."""
    ctx = _post_race(tmp_path, synthetic_lap)
    field_txt = ("In your class: Momo Rossi (Ferrari 296 GT3), P3 in class; B. Other "
                 "(Porsche 911 GT3 R), P6 in class.")
    assert ctx.dispatch("get_battle", {"rival": "the Oreca"}) == (
        "No car in your class matches 'the Oreca'. " + field_txt)
    assert ctx.dispatch("get_battle", {"rival": "P9", "which": "race lap 2"}).endswith(
        "No car in your class matches 'P9'. " + field_txt)
    assert ctx.dispatch("get_battle", {"rival": "you"}) == "you is you, not a rival."
    far = ctx.dispatch("get_battle", {"rival": "B. Other"})
    assert "Compared on 0 of 5 race laps; skipped 1 with no stored battle read, 4 with " \
           "him never within 2 s of you" in far and "No race lap to compare him on." in far


def test_a_live_race_still_reads_its_snapshot_and_a_new_race_its_note(tmp_path, synthetic_lap):
    from simcoach.battle import CarLap, build_battle
    from simcoach.corners import Corner
    from simcoach.race import LapFacts, build_snapshot
    ctx = _post_race(tmp_path, synthetic_lap, on_track=True)
    t1 = Corner(1, "La Source", "right", 200.0, 250.0, 300.0, label="1")
    live = build_battle([CarLap(1, "You", "E1", 3, True, entry={1: 104.0}, exit={1: 106.0}),
                         CarLap(9, "Live Guy", "E9", 2, False, entry={1: 103.0},
                                exit={1: 105.3})], [t1])
    ctx.race = build_snapshot([LapFacts(lap_no=12, lap_time=100.0, clean=True)], "s1",
                              battle=live)
    for which in ("latest", "last lap", "current", "last completed lap", None):
        assert ctx.dispatch("get_battle", {"which": which}).startswith(
            "As of lap 12.\nRival (nearest to you on lap 12): Live Guy"), which
    # An explicit stored lap / the whole race reads the store, even live.
    assert "Momo Rossi" in ctx.dispatch("get_battle", {"which": "race"})
    ctx.race, ctx.race_note = None, "Not enough laps yet: x"
    assert ctx.dispatch("get_battle", {}) == "Not enough laps yet: x"



def _add_race(ctx, t0, rows):
    """Another race window of session s1 (t0 > 20 min after the last): a grid row, then
    race laps 1..len(rows), each stored with the battle rows `rows[i]` (None = none)."""
    from simcoach.model_types import LapRecord
    ctx.store.save(LapRecord(**{**ctx._syn.__dict__, "session_id": "s1", "phase": "race",
                                "lap_no": 0, "timestamp": t0, "partial": "partial",
                                "lap_time": None, "valid": False}))
    for n, cars in enumerate(rows, start=1):
        lid = ctx.store.save(LapRecord(**{**ctx._syn.__dict__, "session_id": "s1",
                                          "phase": "race", "lap_no": n,
                                          "timestamp": t0 + 100.0 * n}))
        if cars:
            ctx.store.save_battle(lid, cars)
    return CoachContext(ctx.store, cmap=ctx.cmap, track=ctx.track, car=ctx.car,
                        session_id="s1", on_track=False)


def _race2(tmp_path, synthetic_lap):
    """Race 1 = _post_race (Momo). Race 2, no Momo: lap 1 no rows; lap 2 Z. New 0.30 s
    slower through T1; lap 3 Z. New in the pit lane the whole lap; lap 4 Z. New absent."""
    from simcoach.battle import CarLap
    ctx = _post_race(tmp_path, synthetic_lap)
    ctx._syn = synthetic_lap
    me = CarLap(1, "You", "E1", 4, True, entry={1: 10.0, 2: 20.0}, exit={1: 12.0, 2: 22.0})
    new = CarLap(5, "Z. New", "E5", 2, False, entry={1: 10.5, 2: 20.5},
                 exit={1: 12.8, 2: 22.5}, model="Aston Martin Vantage")
    pitted = CarLap(5, "Z. New", "E5", 2, False, entry={1: 10.5, 2: 20.5},
                    exit={1: 12.8, 2: 22.5}, model="Aston Martin Vantage",
                    pit=[(0.0, 99.0)])
    else_ = CarLap(6, "Q. Else", "E6", 5, False, entry={1: 50.0}, exit={1: 52.0})
    out = _add_race(ctx, 5000.0, [None, [me, new], [me, pitted], [me, else_]])
    out._syn = synthetic_lap
    return out


def test_a_named_rival_not_in_the_latest_race_is_read_from_his_last_race(tmp_path,
                                                                        synthetic_lap):
    ctx = _race2(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_battle", {"rival": "Momo"})
    assert out.startswith("Race 1 of this session (started ")
    assert ("'Momo' was not in race 2 (the latest with a battle read); this is race 1, the "
            "most recent race he was in. Rival (the car you named): Momo Rossi") in out
    assert "Compared on 3 of 5 race laps (race 1 lap 2, race 1 lap 3, race 1 lap 4)" in out
    # With no rival named, the latest race is read, and nothing says 'was not in'.
    latest = ctx.dispatch("get_battle", {})
    assert latest.startswith("Race 2 of this session") and "was not in" not in latest
    assert "Z. New" in latest


def test_race_n_in_a_multi_race_session_is_that_whole_race(tmp_path, synthetic_lap):
    ctx = _race2(tmp_path, synthetic_lap)
    one = ctx.dispatch("get_battle", {"which": "race 1"})
    assert one.startswith("Race 1 of this session") and "Momo Rossi" in one
    assert "Compared on 3 of 5 race laps (race 1 lap 2, race 1 lap 3, race 1 lap 4)" in one
    assert "was not in" not in one
    two = ctx.dispatch("get_battle", {"rival": "Z. New", "which": "the race 2"})
    assert ("Compared on 1 of 4 race laps (race 2 lap 2); skipped 1 with no stored battle "
            "read, 1 with him not recorded, 1 with you or him in the pit lane.") in two
    assert "You are quicker: Turn 1 (La Source) by 0.30 s" in two
    # One lap of an earlier race still reads that lap.
    assert ctx.dispatch("get_battle", {"which": "race 1 lap 4"}).startswith(
        "Stored read for race 1 lap 4.\nAs of lap 4.")


def test_a_grid_only_latest_race_falls_back_to_the_last_race_with_a_read(tmp_path,
                                                                         synthetic_lap):
    ctx = _add_race(_race2(tmp_path, synthetic_lap), 9000.0, [])   # race 3: grid row only
    out = ctx.dispatch("get_battle", {})
    assert out.startswith("Race 2 of this session") and "Z. New" in out


def test_a_name_matching_several_cars_says_so_like_live(tmp_path, synthetic_lap):
    ctx = _post_race(tmp_path, synthetic_lap)
    out = ctx.dispatch("get_battle", {"rival": "GT3"})
    assert ("Rival (2 cars in your class match 'GT3'; this is the one nearest you on the "
            "most laps): Momo Rossi") in out


# --- #161: incident laps stay out of the tire trend's pace slope ----------------

def test_get_tire_trend_leaves_contact_laps_out_of_the_pace_trend(tmp_path, synthetic_lap):
    """Road Atlanta 2026-09-23: two slow incident laps in the window read as 'deg-limited'.
    A lap with an impact (the #143 rising edge get_stint_events reports) is left out."""
    import dataclasses

    import numpy as np
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    n = len(synthetic_lap.channels["lap_distance"])
    ids = []
    for i in range(6):
        ch = dict(synthetic_lap.channels)
        t = 90.0
        if i in (3, 5):
            t += 12.0
            et = np.zeros(n)
            et[n // 2:] = 500.0 + i          # one hit mid-lap
            ch.update(last_impact_et=et, last_impact_mag=np.full(n, 900.0),
                      last_impact_x=np.zeros(n), last_impact_z=np.ones(n))
        lap = dataclasses.replace(synthetic_lap, channels=ch, lap_time=t,
                                  session_id="s1", phase="race", timestamp=90.0 + i)
        ids.append(store.save(lap))
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True)
    out = ctx.dispatch("get_tire_trend", {})
    assert "deg-limited" not in out and "pace +" not in out, out
    assert "2 incident laps (spin/off/contact) left out of the pace trend" in out


# --- #161 provisional pre-race plan / #164 pit-menu stops ------------------------

def _live(phase="race", max_laps=None, elapsed=0.0, end_time=0.0, current_lap=1):
    from simcoach.lmu.live import SessionState
    return SessionState(live=True, phase=phase, current_lap=current_lap, max_laps=max_laps,
                        track_len=3000.0, elapsed=elapsed, end_time=end_time, cur_s1=None,
                        cur_s2=None, best_s1=None, best_s2=None, last_s1=None, last_s2=None,
                        last_lap=None, best_lap=None)


def _weekend(tmp_path, synthetic_lap, phases=("practice",) * 3 + ("qualifying",)):
    """This session's practice/qualy laps: 2.5 L and 12.5 % virtual energy a lap, 101 s."""
    import dataclasses

    import numpy as np
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    n = len(synthetic_lap.channels["lap_distance"])
    for i, ph in enumerate(phases):
        ch = dict(synthetic_lap.channels)
        ch.update(fuel=np.linspace(80.0 - 2.5 * i, 77.5 - 2.5 * i, n),
                  fuel_capacity=np.full(n, 100.0),
                  virtual_energy=np.linspace(1.0 - 0.125 * i, 0.875 - 0.125 * i, n))
        store.save(dataclasses.replace(synthetic_lap, channels=ch, lap_time=101.0,
                                       session_id="s1", phase=ph, timestamp=10.0 + i,
                                       lap_no=i + 1))
    return store


def test_get_strategy_on_the_grid_gives_a_provisional_plan_not_a_refusal(tmp_path,
                                                                        synthetic_lap):
    """Road Atlanta 2026-09-23: 'when do I pit / how much to refuel' before the race got
    'not enough laps yet' until lap 11."""
    from simcoach.race import NO_RACE_LAP_YET
    store = _weekend(tmp_path, synthetic_lap)
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True, race=None, race_note=NO_RACE_LAP_YET,
                       live_session=_live(max_laps=14))
    out = ctx.dispatch("get_strategy", {})
    assert out.startswith("PROVISIONAL — from your last 4 clean practice/qualifying laps"), out
    assert "Race length: 14 laps (the live race's lap count)." in out
    assert "Pit menu: set virtual energy to 92% for a stop at the end of lap 7" in out
    assert "crew" not in out.lower()


def test_get_strategy_in_practice_plans_from_the_race_length_the_driver_gives(tmp_path,
                                                                             synthetic_lap):
    store = _weekend(tmp_path, synthetic_lap)
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True, live_session=_live("practice"))
    ask = ctx.dispatch("get_strategy", {})
    assert "race length" in ask.lower() and "race_minutes" in ask
    out = ctx.dispatch("get_strategy", {"race_minutes": 25})
    # 25 min at the 101.0 s practice pace -> ceil(1500 / 101) = 15 laps.
    assert "Race length: 25 minutes (as you said) at your practice pace of 1:41.000 " \
           "= about 15 laps." in out, out
    assert "Pit window: laps 7 to 7" in out


def test_get_strategy_provisional_needs_two_clean_practice_laps(tmp_path, synthetic_lap):
    store = _weekend(tmp_path, synthetic_lap, phases=("practice",))
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True, live_session=_live(max_laps=15))
    out = ctx.dispatch("get_strategy", {})
    assert out.startswith("No live race projection yet") and "2 clean" in out, out


def test_prompt_says_lmu_has_no_crew_radio(tmp_path, synthetic_lap):
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT as AGENTIC_SYSTEM
    assert "no crew radio" in AGENTIC_SYSTEM and "never say 'tell the crew'" in AGENTIC_SYSTEM


def test_get_tire_trend_starts_after_the_last_pit_stop(tmp_path, synthetic_lap):
    """#161 red-team, Road Atlanta 2026-09-23 race laps 16-23 (ids 7553-7560): an FL
    puncture, pit on laps 19 AND 20 (FL tread 0.900 -> 0.0 -> 0.941 new), read 'FL wear
    +1.6%/lap ... deg-limited' across the change. The stint restarts after the stop."""
    import dataclasses

    import numpy as np
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    n = len(synthetic_lap.channels["lap_distance"])
    real = [  # lap, time, pit, FL tread start -> end, FR start -> end
        (16, 84.19, False, 0.913, 0.909, 0.900, 0.896),
        (17, 82.75, False, 0.909, 0.904, 0.896, 0.891),
        (18, 87.17, False, 0.904, 0.900, 0.891, 0.888),
        (19, 173.26, True, 0.900, 0.0, 0.888, 0.885),
        (20, 254.10, True, 0.0, 0.941, 0.885, 0.881),
        (21, 96.09, False, 0.941, 0.933, 0.881, 0.862),
        (22, 83.72, False, 0.933, 0.927, 0.862, 0.858),
        (23, 83.25, False, 0.927, 0.921, 0.858, 0.854)]
    for lap, t, pit, fl0, fl1, fr0, fr1 in real:
        ch = dict(synthetic_lap.channels)
        ch.update(tire_wear_fl=np.linspace(fl0, fl1, n), tire_wear_fr=np.linspace(fr0, fr1, n),
                  tire_wear_rl=np.linspace(fr0, fr1, n), tire_wear_rr=np.linspace(fr0, fr1, n))
        store.save(dataclasses.replace(synthetic_lap, channels=ch, lap_time=t, pit=pit,
                                       session_id="s1", phase="race", lap_no=lap,
                                       timestamp=float(lap)))
    ctx = CoachContext(store, cmap=cmap, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True)
    out = ctx.dispatch("get_tire_trend", {})
    assert out.startswith("Over 3 laps:"), out           # laps 21-23 only
    assert "deg-limited" not in out


def test_get_strategy_mid_race_without_a_snapshot_never_plans_from_a_full_fill(
        tmp_path, synthetic_lap):
    """#161 red-team, Road Atlanta race lap 23 after a sidecar restart (race=None,
    1650 s left): it printed 'Assumes full fill ... Laps to go 20 ... no stop needed'."""
    from simcoach.race import NO_RACE_LAP_YET
    store = _weekend(tmp_path, synthetic_lap)
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True, race=None, race_note=NO_RACE_LAP_YET,
                       live_session=_live(end_time=3599.6, elapsed=3599.6 - 1650,
                                          current_lap=24))
    out = ctx.dispatch("get_strategy", {})
    assert out == NO_RACE_LAP_YET
    assert "PROVISIONAL" not in out and "Assumes" not in out


def test_provisional_plan_reads_only_the_live_track_and_car(tmp_path, synthetic_lap):
    """#161 red-team (t10): live at Road Atlanta in qualifying while the chat carried the
    Fuji session the UI had selected -- the plan came from Fuji's laps."""
    import dataclasses

    import numpy as np
    store = _weekend(tmp_path, synthetic_lap)
    n = len(synthetic_lap.channels["lap_distance"])
    for i in range(4):   # another track, 5 L a lap, in the chat's session
        ch = dict(synthetic_lap.channels)
        ch.update(fuel=np.linspace(90.0 - 5 * i, 85.0 - 5 * i, n),
                  fuel_capacity=np.full(n, 100.0))
        store.save(dataclasses.replace(synthetic_lap, channels=ch, lap_time=101.0,
                                       track="Fuji Speedway", session_id="fuji",
                                       phase="practice", timestamp=100.0 + i, lap_no=i + 1))
    for i in range(4):   # this weekend's session, but another car
        ch = dict(synthetic_lap.channels)
        ch.update(fuel=np.linspace(90.0 - 5 * i, 85.0 - 5 * i, n),
                  fuel_capacity=np.full(n, 100.0))
        store.save(dataclasses.replace(synthetic_lap, channels=ch, lap_time=101.0,
                                       car="Other Car", session_id="s1",
                                       phase="practice", timestamp=200.0 + i, lap_no=10 + i))
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="fuji", on_track=True,
                       live_lap={"session_id": "s1", "lap_no": 5, "start_ts": 300.0},
                       live_session=_live("qualifying"))
    out = ctx.dispatch("get_strategy", {"race_laps": 14})
    assert out.startswith("PROVISIONAL — from your last 4 clean"), out
    assert "Fuel: 2.50 L/lap measured" in out and "5.00" not in out


def test_get_strategy_rejects_a_bool_or_fractional_race_length(tmp_path, synthetic_lap):
    store = _weekend(tmp_path, synthetic_lap)
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True, live_session=_live("practice"))
    for bad in ({"race_laps": True}, {"race_laps": 43.9}, {"race_minutes": True}):
        assert "must be a positive number" in ctx.dispatch("get_strategy", bad), bad
    assert "about 1 lap)" not in ctx.dispatch("get_strategy", {"race_minutes": 0.5})
    assert "= about 1 lap." in ctx.dispatch("get_strategy", {"race_minutes": 0.5})


def _repeat_weekend(tmp_path, synthetic_lap):
    """A deterministic session on its SECOND weekend, as the real DB holds them (e.g.
    algarve): an older race under the bare base -- lap 1 with a flat FL -- and this
    weekend's laps, which the store files under `base~YYYYMMDDTHHMM` (the 6 h gap).
    Returns (store, base, this weekend's instance, T)."""
    import dataclasses

    import numpy as np
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    base = "michelin-raceway-road-atlanta--13-autosport-2026-13-lm--online"
    T = 1_790_000_000.0
    n = len(synthetic_lap.channels["lap_distance"])

    def lap(ts, phase, lap_no, fuel, ve, flat=False, burn=2.3):
        ch = dict(synthetic_lap.channels)
        ch.update(fuel=np.linspace(fuel, fuel - burn, n), fuel_capacity=np.full(n, 100.0),
                  virtual_energy=np.linspace(ve, ve - 0.034, n),
                  dent_max=np.full(n, 40.0 if flat else 0.0), dent_total=np.zeros(n),
                  detached=np.zeros(n), overheating=np.zeros(n),
                  flat_fl=np.full(n, 1.0 if flat else 0.0))
        return store.save(dataclasses.replace(synthetic_lap, channels=ch, lap_time=95.0,
                                              session_id=base, phase=phase,
                                              lap_no=lap_no, timestamp=ts))
    for i in range(3):     # the older weekend (three days earlier): 5 L a lap in qualy
        lap(T - 3 * 86400 - 1000 + 100 * i, "qualifying", i + 1, 90.0 - 5 * i,
            1.0 - 0.05 * i, burn=5.0)
    old = lap(T - 3 * 86400, "race", 1, 30.0, 0.40, flat=True)
    for i in range(4):                                             # this weekend's qualy
        lap(T + 100 * i, "qualifying", i + 1, 80.0 - 2.3 * i, 1.0 - 0.034 * i)
    lap(T + 1000, "race", 1, 73.8, 1.0)                            # real race-lap-1 shape
    lap(T + 1100, "race", 2, 71.5, 0.966)
    inst = store.load(old + 1).session_id   # the first lap saved after the old race
    assert inst.startswith(base + "~") and store.load(old).session_id == base
    return store, base, inst, T


def test_provisional_plan_mid_race_starts_from_this_weekends_race_lap(tmp_path,
                                                                     synthetic_lap):
    """#161 red-team rounds 1-2 (t4, t13): the plan must start from what was on board at
    the snapshot's OWN race lap -- of THIS weekend. The live lap carries the bare base id;
    reading it unresolved stated another race's lap 1 (a flat FL, 30 L) as fact."""
    from simcoach.race import LapFacts, build_snapshot
    store, base, inst, T = _repeat_weekend(tmp_path, synthetic_lap)
    snap = build_snapshot([LapFacts(lap_no=1, lap_time=95.0, clean=False)], inst)
    assert snap.strategy.note is not None                 # 1 lap: no race projection yet
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id=inst, on_track=True, race=snap,
                       live_lap={"session_id": base, "lap_no": 3, "start_ts": T + 1200},
                       live_session=_live(max_laps=14, current_lap=3))
    out = ctx.dispatch("get_strategy", {})
    assert "As of race lap 1: what was on board at the line" in out, out
    assert "71.5 L on board" in out                        # this lap 1's line: 73.8 - 2.3
    assert "Fuel: 2.30 L/lap" in out                       # this weekend's qualy burn
    assert "flat" not in out and "27.7 L" not in out       # never the older race's lap 1


def test_provisional_pre_race_plan_reads_this_weekends_laps_from_the_bare_base(
        tmp_path, synthetic_lap):
    store, base, inst, T = _repeat_weekend(tmp_path, synthetic_lap)
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id=inst, on_track=True,
                       live_lap={"session_id": base, "lap_no": 5, "start_ts": T + 400},
                       live_session=_live("qualifying"))
    out = ctx.dispatch("get_strategy", {"race_laps": 14})
    assert out.startswith("PROVISIONAL — from your last 4 clean"), out
    assert "Fuel: 2.30 L/lap measured" in out and "5.00" not in out   # this weekend's


def test_an_old_weekend_chat_as_newest_activity_never_serves_the_old_race(
        tmp_path, synthetic_lap, monkeypatch):
    """#161 red-team round 3 (t15 POISON=1): a chat stamped under the OLD weekend's bare
    id was the newest activity, so resolving the base returned the old weekend -- and its
    race lap 1 (a flat FL) was stated as this race's. Rows must fall inside the store's
    session gap before the live lap started, whatever id they resolve under. #165 fixed
    that resolve at the root; this stays as defense in depth, so the mis-resolve is
    forced here."""
    from simcoach.race import LapFacts, build_snapshot
    store, base, inst, T = _repeat_weekend(tmp_path, synthetic_lap)
    store.save_chat("chat-old-selected", base, None, None, None, "q", "a", ts=T + 1150)
    assert store.resolve_current_instance(base, T + 1200) == inst      # #165: no poison
    monkeypatch.setattr(store, "resolve_current_instance", lambda b, ts=None: base)
    snap = build_snapshot([LapFacts(lap_no=1, lap_time=95.0, clean=False)], inst)
    ctx = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id=inst, on_track=True, race=snap,
                       live_lap={"session_id": base, "lap_no": 3, "start_ts": T + 1200},
                       live_session=_live(max_laps=14, current_lap=3))
    out = ctx.dispatch("get_strategy", {})
    assert "flat" not in out and "dent severity" not in out, out
    assert "5.00" not in out                  # nor the old weekend's qualifying burn
    pre = CoachContext(store, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id=inst, on_track=True,
                       live_lap={"session_id": base, "lap_no": 5, "start_ts": T + 1200},
                       live_session=_live("qualifying"))
    assert "5.00" not in pre.dispatch("get_strategy", {"race_laps": 14})


# --- #157: lap-level reports don't need a corner map --------------------------

def _no_map_ctx(tmp_path, lap):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    lap_id = store.save(lap)
    return CoachContext(store, record=lap, lap_id=lap_id, cmap=None,
                        track=lap.track, car=lap.car, on_track=True)


def test_tire_and_wheel_reports_answer_without_a_corner_map(tmp_path, synthetic_lap):
    ctx = _no_map_ctx(tmp_path, synthetic_lap)
    tire = ctx.get_tire_report()
    assert tire.startswith("Lap: ") and "Tire temps" in tire
    wheel = ctx.get_wheel_report()
    assert wheel.startswith("Lap: ")
    assert "Brake temps" in wheel and "Tire wear remaining" in wheel
    # the per-corner part is named as needing a map, not silently dropped
    assert "corner map" in wheel
    for out in (tire, wheel):
        assert "No lap captured" not in out


def test_tire_and_wheel_reports_no_lap_is_not_a_map_problem(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for cmap in (None, CornerMap.load("simcoach/tracks/test_track.json")):
        ctx = CoachContext(store, cmap=cmap, track="Nowhere", car="GT3", on_track=True)
        for out in (ctx.get_tire_report(), ctx.get_wheel_report()):
            assert out == "No lap captured yet."


def test_get_consistency_reads_lap_times_without_a_corner_map(tmp_path, synthetic_lap):
    import dataclasses
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for i in range(3):
        store.save(dataclasses.replace(synthetic_lap, lap_time=90.0 + i, session_id="s1",
                                       phase="practice", timestamp=90.0 + i))
    ctx = CoachContext(store, cmap=None, track=synthetic_lap.track, car=synthetic_lap.car,
                       session_id="s1", on_track=True)
    out = ctx.get_consistency()
    assert "Lap time" in out and "over 3 laps" in out
    assert "corner map" in out and "Least consistent" not in out
