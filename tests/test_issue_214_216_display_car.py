"""#214/#216: the app's CoachContext.car is the resolved MODEL ('Chevrolet Corvette Z06
LMGT3.R'), laps store the raw ENTRY name. Cross-session reads must expand the model to
its entries -- in this car's class only (#190) -- or they find nothing."""
import dataclasses

import numpy as np
import pytest

from simcoach import car_lookup
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap
from simcoach.store import LapStore

Z06 = "Chevrolet Corvette Z06 LMGT3.R"
TURKEY = "Racing Team Turkey by TF 2026 #34:LM"     # this evening's entry
TF = "TF Sport 2026 #33:LM"                          # the same model, another night
GTE = "Corvette Racing Z06 #99:LM"                   # the same model, ANOTHER class
SID = "test_track|turkey--online"


def _wear_step(lap, drop=0.09, at_dist=1000.0):
    ch = dict(lap.channels)
    d = np.asarray(ch["lap_distance"], float)
    wear = np.asarray(ch["tire_wear_fr"], float).copy()
    wear[int(np.argmin(np.abs(d - at_dist))):] -= drop
    ch["tire_wear_fr"] = wear
    return dataclasses.replace(lap, channels=ch)


@pytest.fixture
def store(tmp_path, synthetic_lap, monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table", lambda: {
        TURKEY: (Z06, "GT3"), TF: (Z06, "GT3"), GTE: (Z06, "GTE")})
    s = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    laps = [(TF, "test_track|tf--online", synthetic_lap),
            (TURKEY, SID, synthetic_lap), (TURKEY, SID, synthetic_lap),
            (TURKEY, SID, _wear_step(synthetic_lap)),              # the flat spot
            (GTE, "test_track|gte--online", synthetic_lap)]        # newest of all
    for i, (car, sid, lap) in enumerate(laps):
        s.save(dataclasses.replace(lap, car=car, session_id=sid, phase="practice",
                                   lap_no=i + 1, lap_time=90.0 + i, timestamp=100.0 + i))
    return s


def _ctx(store, match_class="GT3"):
    # As coach_ws wires it: car = resolve_car(entry), match_class = its scraped class.
    return CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                        track="test_track", car=Z06, car_class=match_class,
                        match_class=match_class, session_id=SID, on_track=True)


def test_session_status_counts_all_time_laps_of_this_model_and_class(store):
    assert "all-time this track/car: 4" in _ctx(store).get_session_status()


def test_session_status_all_time_count_is_unknown_when_the_class_cant_be_told(store):
    assert "all-time this track/car: unknown" in _ctx(store, None).get_session_status()


def test_recent_records_reach_the_stored_laps_and_feed_the_grip_envelope(store):
    ctx = _ctx(store)
    assert [r.car for r in ctx._recent_records()] == [TURKEY, TURKEY, TURKEY, TF]
    assert "grip envelope still forming: 4 of 5 laps" in ctx.get_lap_summary("latest")


def test_single_lap_tool_flags_the_flat_spot_against_the_wear_baseline(store):
    ctx = _ctx(store)
    assert ctx._wear_baseline()
    assert "FR wear step" in ctx.get_wheel_report()


def test_same_model_in_another_class_is_not_counted(store):
    # #190: the GTE lap is the newest of all; it is neither recent nor counted.
    ctx = _ctx(store)
    assert {r.car for r in ctx._recent_records()} == {TURKEY, TF}
    assert "all-time this track/car: 5" not in ctx.get_session_status()


def test_distilled_entry_tagged_memories_reach_the_coach_by_model(store):
    # #219: distill tags a memory with the lap's raw entry name; the coach asks by model.
    store.add_learning("trail-brake into T1", status="active", car=TURKEY, source="distill")
    store.add_learning("coach-written, model tag", status="active", car=Z06, source="coach")
    store.add_learning("general", status="active", source="distill")
    store.add_learning("another model", status="active", car="BMW M4 LMGT3", source="distill")
    texts = {r["text"] for r in store.active_learnings(car=Z06)}
    assert texts == {"trail-brake into T1", "coach-written, model tag", "general"}
    assert {r["text"] for r in store.active_learnings(car=None)} == {"general"}


def test_a_raw_entry_name_still_matches_only_itself(store):
    # #214 review: map building / Tracks counts pass the raw entry; the model expansion
    # must not widen them to the same model's other entries.
    turkey = store.list_lap_ids("test_track", TURKEY)
    assert len(turkey) == 3
    assert {r["lap_id"] for r in store.recent_laps("test_track", TURKEY)} == set(turkey)
