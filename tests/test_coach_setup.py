from simcoach.analysis import analyze_lap
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel


def test_setup_values_included_and_metadata_skipped(synthetic_lap):
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    setup = {
        "carClass": "GT3",  # metadata (not a dict-with-stringValue) — must be skipped
        "VM_REARWING": {"stringValue": "6", "isFreeSetting": True},
        "VM_BRAKE_PRESSURE": {"stringValue": "110", "isFreeSetting": False},  # locked
        "VM_CHASSIS_ADJ_07": {"stringValue": "3", "isFreeSetting": True},     # cryptic -> filtered
        "VM_DIFF_PUMP": {"stringValue": "N/A", "isFreeSetting": True},        # no-info -> filtered
    }
    _system, user = coach.build_prompt(a, "test_car", "help me", setup=setup)
    assert "Current car setup" in user
    assert "Rearwing: 6" in user               # coaching-relevant param kept
    assert "locked this tier" in user          # non-free param flagged
    assert "carClass" not in user              # metadata skipped
    assert "Chassis Adj 07" not in user        # cryptic indexed param filtered out
    assert "Diff Pump" not in user             # N/A value filtered out


def test_no_setup_section_when_absent(synthetic_lap):
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    _system, user = coach.build_prompt(a, "test_car", "help me")
    assert "Current car setup" not in user


def test_prelap_question_without_analysis():
    """A question with no lap yet: no corner facts, still answerable from knowledge."""
    coach = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    _system, user = coach.build_prompt(None, "test_car", "how do I approach this track?")
    assert "No lap captured yet" in user
    assert "Corner facts" not in user
    assert coach.ask(None, "test_car", "how do I approach this track?") == "ok"


def test_controller_caveat_in_system(synthetic_lap):
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    sys_ctrl, _ = coach.build_prompt(a, "test_car", "q", input_device="controller")
    sys_wheel, _ = coach.build_prompt(a, "test_car", "q", input_device="wheel")
    assert "input-device artifacts" in sys_ctrl and "controller" in sys_ctrl
    assert "using a wheel" in sys_wheel and "input-device artifacts" not in sys_wheel


def test_track_note_loaded_when_slug_given(synthetic_lap):
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel(), "simcoach/knowledge")
    _s, with_note = coach.build_prompt(a, "test_car", "q",
                                       track_note="silverstone_grand_prix_circuit")
    _s, without = coach.build_prompt(a, "test_car", "q")
    assert "Track notes" in with_note and "Track notes" not in without
