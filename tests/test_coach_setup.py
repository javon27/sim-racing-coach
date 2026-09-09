from simcoach.analysis import analyze_lap
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel


def test_setup_values_included_and_metadata_skipped(synthetic_lap):
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel())
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


def test_setup_step_position_shown_when_exposed(synthetic_lap):
    # Issue #32: minValue/maxValue on the LMU garage payload are ORDINAL OPTION
    # INDICES, not physical bounds. Real captured shape (session_setups):
    # VM_REAR_WING = stringValue "6.5 deg", value 6, minValue 0, maxValue 9 --
    # wing *position* 6 of 0-9. So render the step position; stringValue stays
    # the physical value.
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel())
    setup = {
        "VM_REAR_WING": {"stringValue": "6.5 deg", "value": 6, "isFreeSetting": True,
                         "minValue": 0, "maxValue": 9},
    }
    _system, user = coach.build_prompt(a, "test_car", "help me", setup=setup)
    assert "Rear Wing: 6.5 deg (step 6 of 0-9)" in user


def test_physical_value_never_rendered_as_bounded_by_the_index_range(synthetic_lap):
    """Regression for the #32 defect: a 1200 lbf/in spring whose option indices
    run 0-4 must NEVER render as '1200lbf/in (range 0-4)'. A coach reading that
    concludes 1200 is 300x over the limit and tells the driver so. The suffix
    must name a step position, and the physical value must never sit next to
    bounds it is not measured in."""
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel())
    setup = {
        "WM_SPRING-W_FL": {"stringValue": "1200lbf/in", "value": 3, "isFreeSetting": True,
                           "minValue": 0, "maxValue": 4},
    }
    _system, user = coach.build_prompt(a, "test_car", "help me", setup=setup)
    assert "WM_SPRING-W_FL: 1200lbf/in (step 3 of 0-4)" in user
    assert "(range" not in user          # the broken framing, in any form
    assert "1200lbf/in (0-4)" not in user  # bare bounds read as physical too


def test_setup_step_omitted_when_ordinal_missing(synthetic_lap):
    """Never print bounds without the matching ordinal -- printing bounds alone
    IS the unit-mismatch bug. No `value` on the param -> no suffix at all."""
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel())
    setup = {
        "VM_REAR_WING": {"stringValue": "6.5 deg", "isFreeSetting": True,
                         "minValue": 0, "maxValue": 9},
    }
    _system, user = coach.build_prompt(a, "test_car", "help me", setup=setup)
    assert "Rear Wing: 6.5 deg" in user
    assert "6.5 deg (" not in user   # no suffix opened at all
    assert "0-9" not in user


def test_setup_no_suffix_when_bounds_absent(synthetic_lap):
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel())
    setup = {"VM_REARWING": {"stringValue": "6", "value": 6, "isFreeSetting": True}}
    _system, user = coach.build_prompt(a, "test_car", "help me", setup=setup)
    assert "Rearwing: 6" in user
    assert "step" not in user


def test_no_setup_section_when_absent(synthetic_lap):
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel())
    _system, user = coach.build_prompt(a, "test_car", "help me")
    assert "Current car setup" not in user


def test_prelap_question_without_analysis():
    """A question with no lap yet: no corner facts, still answerable from knowledge."""
    coach = Coach(FakeModel(reply="ok"))
    _system, user = coach.build_prompt(None, "test_car", "how do I approach this track?")
    assert "No lap captured yet" in user
    assert "Corner facts" not in user
    assert coach.ask(None, "test_car", "how do I approach this track?") == "ok"


def test_controller_caveat_in_system(synthetic_lap):
    a = analyze_lap(synthetic_lap, CornerMap.load("simcoach/tracks/test_track.json"))
    coach = Coach(FakeModel())
    sys_ctrl, _ = coach.build_prompt(a, "test_car", "q", input_device="controller")
    sys_wheel, _ = coach.build_prompt(a, "test_car", "q", input_device="wheel")
    assert "input-device artifacts" in sys_ctrl and "controller" in sys_ctrl
    assert "using a wheel" in sys_wheel and "input-device artifacts" not in sys_wheel
