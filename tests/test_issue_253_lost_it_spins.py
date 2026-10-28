"""#253 (Road Atlanta rig, 2026-10-27): "I completely lost it" -- three spins on the lap
in progress, no impacts. get_car_status said only "No impacts since the start of this
lap", and the coach answered "nothing hit you". Car status now carries the lap's
measured spins and stops too, so "no impacts" never reads as "nothing happened"."""
from simcoach.analysis import lap_events
from simcoach.coach import AGENTIC_SYSTEM_PROMPT
from simcoach.coach_tools import CoachContext
from tests.test_issue_210_event_location import _CMAP, _SPIN, _lap

_STATUS = ("Car status now (live), game lap 14 in progress (race):\nOn track.\n"
           "No impacts since the start of this lap.")


def _ctx(store, events):
    return CoachContext(store, cmap=_CMAP, track="test_track", car="test_car",
                        on_track=True, session_id="s", car_status=_STATUS,
                        live_lap={"lap_no": 14, "incidents": {"events": events,
                                                              "impacts": []}})


def test_car_status_lists_this_laps_spins_beside_no_impacts(tmp_path):
    from simcoach.store import LapStore
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "d"))
    rec = _lap([(30.0, 0.0, 180.0, 180.0), _SPIN, (5.0, 0.0, 30.0, 180.0)])
    events = lap_events(rec, _CMAP, None, live=True)
    assert [e["kind"] for e in events] == ["spin"]            # the fixture's premise
    out = _ctx(store, events).dispatch("get_car_status", {})
    assert "No impacts since the start of this lap." in out
    assert "Spins and stops since the start of this lap (1):" in out
    assert "\n  spin at 1,5" in out
    # none measured: nothing added (never "no spins" -- off-corner scans may lag)
    assert _ctx(store, []).dispatch("get_car_status", {}) == _STATUS


def test_the_prompt_reads_lost_it_as_a_spin_question():
    assert "'I lost it'" in AGENTIC_SYSTEM_PROMPT and "spins and stops" in AGENTIC_SYSTEM_PROMPT


