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


def test_get_corner_output_includes_authored_note(tmp_path, synthetic_lap):
    ctx, _, _ = _ctx(tmp_path, synthetic_lap)
    ctx.cmap.notes = {"corners": {"1": "brake earlier than it looks"}}
    out = ctx.get_corner("1")
    assert "Authored note: brake earlier than it looks" in out


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
    assert ctx.get_corner("1").startswith("T1 ")
    assert "Current lap: yes" in ctx.get_session_status()
