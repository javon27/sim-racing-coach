"""Agentic tool layer: the tools return the right on-demand summaries, and the
Coach agentic loop dispatches the model's tool calls (via FakeModel scripting)."""
from simcoach.coach import Coach
from simcoach.coach_tools import TOOLS, CoachContext
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
    assert out.startswith("T1 ")
    assert "\nT2" not in out  # just the one corner, not the whole lap
    # name lookup works too
    assert ctx.get_corner("Left Hairpin").startswith("T1 ")
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
    assert ctx.get_corner("1", which="race lap 1").startswith("T1 ")
    assert ctx.dispatch("get_corner", {"corner": "1", "which": "race lap 1"}).startswith("T1 ")
    # a lap that doesn't exist is honest, not silently the latest lap
    assert "no lap" in ctx.get_corner("1", which="race lap 9").lower()


def test_get_lap_summary_and_tires(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    summary = ctx.get_lap_summary("latest")
    assert "Corner facts" in summary and "T1 " in summary
    assert "Tire temps" in ctx.get_tire_report()


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
    assert "started P5" in out and "as low as P10" in out and "P3" in out
    assert "P14" not in out  # practice position excluded


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


def test_dispatch_routes_and_handles_unknown(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    assert ctx.dispatch("get_corner", {"corner": "1"}).startswith("T1 ")
    assert "unknown tool" in ctx.dispatch("bogus", {})


def test_agentic_loop_dispatches_tool_calls(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    # Script the fake model to call get_corner, then answer.
    model = FakeModel(reply="Turn 1 looks good.",
                      tool_script=[("get_corner", {"corner": "1"})])
    coach = Coach(model, "simcoach/knowledge")
    answer = coach.ask_agentic(ctx, "how's turn 1?", "test_car")
    assert answer == "Turn 1 looks good."
    # the tool actually ran and produced the corner line
    assert model.dispatched[0][0] == "get_corner"
    assert model.dispatched[0][2].startswith("T1 ")
    # tools were advertised to the model
    assert model.last_tools == TOOLS


def test_agentic_system_prompt_has_units_and_tools_guidance(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    model = FakeModel(reply="ok")
    Coach(model, "simcoach/knowledge").ask_agentic(ctx, "q", "test_car", units="imperial")
    assert "mph" in model.last_system and "°F" in model.last_system
    assert "tool" in model.last_system.lower()


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
    assert ctx.get_corner("1").startswith("T1 ")
    assert "Current lap: yes" in ctx.get_session_status()
