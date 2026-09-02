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


def test_no_lap_context_is_honest(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    ctx = CoachContext(store, track="spa", car="car", on_track=False)
    assert "no lap captured yet" in ctx.get_session_status().lower()
    assert "no lap" in ctx.get_corner("1").lower()


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
