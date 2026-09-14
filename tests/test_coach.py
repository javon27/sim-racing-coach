def test_coach_injects_corner_facts(synthetic_lap):
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    m = FakeModel(reply="trail-brake deeper")
    c = Coach(m)
    out = c.ask(a, "test_car", "where am I losing time?")
    assert out == "trail-brake deeper"
    sent = m.last_messages[-1]["content"]
    assert "Left Hairpin" in sent and "left" in sent   # fact injected
    assert "test_car" in m.last_system or "corner" in m.last_system.lower()


def test_coach_brake_point_is_relative_to_apex(synthetic_lap):
    """Braking is shown RELATIVE to the apex (driver-meaningful) and derives from the
    analysis corner — the absolute lap-distance apex position must NOT leak to the driver."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"))
    _, user_content = c.build_prompt(a, "test_car", "where am I losing time?")
    c1 = a.corners[0]
    before = c1.apex_dist - c1.brake_point
    assert f"brake {before:.0f}m before apex" in user_content
    assert f"apex {c1.apex_dist}m" not in user_content  # no absolute distance leak


def test_coach_degenerate_corner_says_no_telemetry(synthetic_lap):
    """A corner with no telemetry (nan min_spd) is flagged, not rendered with nan speeds
    and fake defaulted brake/throttle distances the coach would critique."""
    import math
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    a.corners[0].min_spd = math.nan
    c = Coach(FakeModel(reply="ok"))
    _, user_content = c.build_prompt(a, "test_car", "q")
    t1 = [l for l in user_content.splitlines() if l.startswith("T1 ")][0]
    assert "no telemetry" in t1
    assert "nan" not in t1 and "before apex" not in t1


def test_coach_system_prompt_asserts_ground_truth(synthetic_lap):
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"))
    system, _ = c.build_prompt(a, "test_car", "where am I losing time?")
    assert "ground truth" in system.lower()


def test_agentic_system_prompt_has_scope_guard():
    """The agentic prompt must keep the coach in character and decline off-purpose
    requests (code, chit-chat, unrelated tasks) — this is prompt-only, no classifier."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "SCOPE:" in AGENTIC_SYSTEM_PROMPT
    assert "nothing else" in AGENTIC_SYSTEM_PROMPT


def test_system_prompts_describe_setup_steps_as_positions_not_values():
    """Issue #32: the setup suffix is an ordinal step position, not a physical
    range. Both prompts must (a) say so explicitly, so the coach never restates
    the index as a spring rate/pressure/angle, and (b) still forbid quoting an
    exact increment, since no step-size field exists on the payload."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT, SYSTEM_PROMPT
    for prompt in (SYSTEM_PROMPT, AGENTIC_SYSTEM_PROMPT):
        assert "SETUP STEPS:" in prompt
        assert "(step N of lo-hi)" in prompt
        assert "NOT a physical value" in prompt
        assert "exact increment" in prompt
        # the old framing asserted a legal *physical* range — it must be gone
        assert "SETUP RANGES:" not in prompt
        assert "legal (range lo-hi)" not in prompt


def test_coach_shows_delta_with_reference(synthetic_lap):
    """With a reference lap, the prompt surfaces the min_spd delta vs best."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    import dataclasses

    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    # A DISTINCT best lap, 5 km/h faster through T1 and identical through T2. With
    # synthetic_lap as its own reference every delta was 0.0, so "+0.0 km/h" appeared
    # whichever corner the numbers came from (#70 review).
    d = synthetic_lap.channels["lap_distance"]
    faster_t1 = synthetic_lap.channels["speed"] + 5.0 * ((d >= 800) & (d <= 1200))
    best = dataclasses.replace(synthetic_lap, channels={**synthetic_lap.channels,
                                                        "speed": faster_t1})
    a = analyze_lap(synthetic_lap, cmap, reference=best)
    c = Coach(FakeModel(reply="ok"))
    _, user_content = c.build_prompt(a, "test_car", "where am I losing time?")
    lines = user_content.splitlines()
    t1 = next(line for line in lines if line.startswith("T1 "))
    t2 = next(line for line in lines if line.startswith("T2 "))
    assert "min vs best: -5.0 km/h" in t1     # the delta is on the corner it belongs to
    assert "min vs best: +0.0 km/h" in t2


def test_coach_no_delta_without_reference(synthetic_lap):
    """Without a reference lap, no delta text is shown."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"))
    _, user_content = c.build_prompt(a, "test_car", "where am I losing time?")
    assert "vs best" not in user_content


def test_coach_with_allowed_adjustments_adds_constraint(synthetic_lap):
    """When allowed_adjustments is given, the prompt names the legal items and a constraint phrase."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"))
    system, user_content = c.build_prompt(
        a, "test_car", "should I change the anti-roll bars?",
        allowed_adjustments=["brake bias", "ABS"],
    )
    combined = system + user_content
    assert "brake bias" in combined
    assert "only" in combined.lower() or "legal" in combined.lower()


def test_coach_without_allowed_adjustments_has_no_constraint(synthetic_lap):
    """Regression: allowed_adjustments=None leaves the prompt unconstrained (current behavior)."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"))
    system, user_content = c.build_prompt(a, "test_car", "where am I losing time?")
    combined = (system + user_content).lower()
    assert "legal setup adjustments" not in combined
    assert "setup constraint" not in combined


def test_coach_with_driver_profile_adds_personalization(synthetic_lap):
    """driver_profile facts show up in the system prompt to personalize advice."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    from simcoach.driver import DriverProfile
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"))
    profile = DriverProfile(risk_tolerance="conservative")
    system, _ = c.build_prompt(a, "test_car", "where am I losing time?", driver_profile=profile)
    assert "conservative" in system


def test_coach_without_driver_profile_has_no_personalization_text(synthetic_lap):
    """Regression: driver_profile=None leaves the system prompt unchanged (current behavior)."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"))
    system, _ = c.build_prompt(a, "test_car", "where am I losing time?")
    assert "driver profile" not in system.lower()


def test_coach_ask_passes_driver_profile(synthetic_lap):
    """ask() forwards driver_profile through to the model call."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    from simcoach.driver import DriverProfile
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    m = FakeModel(reply="ok")
    c = Coach(m)
    profile = DriverProfile(braking_style="late")
    c.ask(a, "test_car", "where am I losing time?", driver_profile=profile)
    assert "late" in m.last_system


def test_coach_skips_delta_text_when_delta_is_none(synthetic_lap):
    """A corner with min_spd_delta=None (degenerate) shows no delta text and doesn't crash."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap, reference=synthetic_lap)
    a.corners[0].min_spd_delta = None
    c = Coach(FakeModel(reply="ok"))
    _, user_content = c.build_prompt(a, "test_car", "where am I losing time?")
    c1_line = [l for l in user_content.splitlines() if l.startswith("T1 ")][0]
    assert "vs best" not in c1_line


CAVEAT = "[garage value — confirm what's dialed on-track]"

NO_INFO = ("N/A", "Non-adjustable", "None", "")

# Every cockpit-adjustable electronic present in the real payload, and nothing else.
# The first nine are the LMU TelemInfoV01 dial fields as they appear in the garage
# setup; the last three are cockpit controls LMU exposes no live source for at all,
# which makes their garage value less verifiable, not more.
EXPECTED_ELECTRONICS = {
    "VM_ANTILOCKBRAKESYSTEMMAP",       # mABS
    "VM_TRACTIONCONTROLMAP",           # mTC
    "VM_TRACTIONCONTROLSLIPANGLEMAP",  # mTCSlip
    "VM_TRACTIONCONTROLPOWERCUTMAP",   # mTCCut
    "VM_ELECTRIC_MOTOR_MAP",           # mMotorMap
    "VM_BRAKE_MIGRATION",              # mMigration
    "VM_FRONT_ANTISWAY",               # mFrontAntiSway
    "VM_REAR_ANTISWAY",                # mRearAntiSway
    "VM_BRAKE_BALANCE",                # mRearBrakeBias
    "VM_ENGINE_MIXTURE",               # no live field
    "VM_REGEN_LEVEL",                  # mRegen is instantaneous flow, not the dial
    "VM_ENGINE_BRAKEMAP",              # no live field
}


def _real_setup_keys():
    """The 170 keys of a real captured LMU garage payload (key -> stringValue)."""
    import json
    import pathlib
    p = pathlib.Path(__file__).parent / "fixtures" / "lmu_setup_keys.json"
    return json.loads(p.read_text(encoding="utf-8"))["keys"]


def test_electronics_caveat_matches_exactly_the_cockpit_dials_in_a_real_payload():
    """Issue #33: LMU's REST reports the GARAGE setup, and ANY electronics change the
    driver makes on track is discarded when the car returns to the garage (driver-stated,
    2026-09-09). So every cockpit-adjustable electronic must carry the garage caveat --
    and nothing else may. Enumerated over a real 170-key payload rather than a hand-picked
    dict, so a term that also catches an availability flag or a mechanical param fails
    here: hedging a value the driver genuinely cannot change on track would make the
    coach hedge a fact."""
    from simcoach.coach import _ELECTRONICS_TERMS, _SETUP_TERMS
    keys = _real_setup_keys()
    assert len(keys) == 170

    def packed(key):  # the same name _setup_lines matches its term lists against
        return key.removeprefix("VM_").lower()

    matched = {k for k in keys if any(t in packed(k) for t in _ELECTRONICS_TERMS)}
    assert matched == EXPECTED_ELECTRONICS

    # The caveat is only reachable if the param survives _SETUP_TERMS -- a caveat on a
    # param the coach never sees is dead code. (This is how ANTISWAY, ELECTRIC_MOTOR_MAP,
    # ENGINE_MIXTURE and REGEN_LEVEL were found to be dropped from the prompt entirely.)
    for key in EXPECTED_ELECTRONICS:
        assert any(t in packed(key) for t in _SETUP_TERMS), f"{key} never reaches the prompt"

    # ...and then survives the no-info filter, which is per-car: this LMH exposes no
    # engine-brake rotary, so VM_ENGINE_BRAKEMAP reads "N/A" and is correctly dropped
    # here. Its term still earns its place for cars that do expose one.
    assert {k for k in EXPECTED_ELECTRONICS if keys[k] not in NO_INFO} == (
        EXPECTED_ELECTRONICS - {"VM_ENGINE_BRAKEMAP"})
    assert keys["VM_ENGINE_MIXTURE"] == "Race" and keys["VM_REGEN_LEVEL"] == "0%"

    # Availability flags read "Available" -- state, not a dial. Must stay uncaveated.
    for flag in ("VM_ANTILOCK_BRAKES", "VM_TRACTION_CONTROL"):
        assert keys[flag] == "Available"
        assert flag not in matched

    # Mechanical params: the driver cannot touch these on track, so the garage value
    # IS the truth. A caveat on any of them is a regression.
    mechanical = [k for k in keys if any(t in k for t in (
        "SPRING", "RIDEHEIGHT", "CAMBER", "WING", "PACKERS", "BUMP", "REBOUND",
        "TOEIN", "CASTER", "PRESSURE", "GEAR_", "WEIGHT_", "DIFF", "DUCTS"))]
    assert len(mechanical) > 60                      # the sweep really covers the payload
    assert not (set(mechanical) & matched)


def test_setup_lines_render_the_caveat_on_electronics_only():
    """The term list above is applied by _setup_lines: the caveat is appended, #32's
    ordinal suffix survives it, and a mechanical param alongside stays clean."""
    from simcoach.coach import _setup_lines
    setup = {
        "VM_ANTILOCKBRAKESYSTEMMAP": {"stringValue": "9 (Understeer)", "value": 9,
                                      "minValue": 0, "maxValue": 10, "isFreeSetting": True},
        "VM_BRAKE_BALANCE": {"stringValue": "51.5:48.5", "value": 22,
                             "minValue": 0, "maxValue": 57, "isFreeSetting": True},
        "VM_FRONT_ANTISWAY": {"stringValue": "P28 (hard)", "value": 4,
                              "minValue": 0, "maxValue": 7, "isFreeSetting": True},
        # A named (non-numeric) electronic: caveated, and with no step suffix to lean on.
        "VM_ENGINE_MIXTURE": {"stringValue": "Race", "isFreeSetting": True},
        "VM_ANTILOCK_BRAKES": {"stringValue": "Available", "isFreeSetting": True},
        "VM_REAR_WING": {"stringValue": "6.5 deg", "value": 6,
                         "minValue": 0, "maxValue": 9, "isFreeSetting": True},
    }
    lines = {line.split(":")[0][2:]: line for line in _setup_lines(setup)}
    assert lines["Engine Mixture"] == f"- Engine Mixture: Race {CAVEAT}"
    for dial in ("Antilockbrakesystemmap", "Brake Balance", "Front Antisway"):
        assert lines[dial].endswith(CAVEAT), lines[dial]
        assert "(step " in lines[dial]          # #32's ordinal suffix survives
    for plain in ("Antilock Brakes", "Rear Wing"):
        assert CAVEAT not in lines[plain], lines[plain]


def test_system_prompts_tell_the_coach_to_flag_not_assert_electronics():
    """Issue #33: both prompts must instruct the coach to treat a flagged electronic as
    the garage setting to confirm, never as the driver's current one."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT, SYSTEM_PROMPT
    for prompt in (SYSTEM_PROMPT, AGENTIC_SYSTEM_PROMPT):
        assert "COCKPIT ELECTRONICS:" in prompt
        assert "garage value" in prompt
        assert "Never assert" in prompt
