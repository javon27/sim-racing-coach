def test_agentic_system_prompt_has_scope_guard():
    """The agentic prompt must keep the coach in character and decline off-purpose
    requests (code, chit-chat, unrelated tasks) — this is prompt-only, no classifier."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "SCOPE:" in AGENTIC_SYSTEM_PROMPT
    assert "nothing else" in AGENTIC_SYSTEM_PROMPT


def test_system_prompts_describe_setup_steps_as_positions_not_values():
    """Issue #32: the setup suffix is an ordinal step position, not a physical
    range. The prompt must (a) say so explicitly, so the coach never restates
    the index as a spring rate/pressure/angle, and (b) still forbid quoting an
    exact increment, since no step-size field exists on the payload."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    prompt = AGENTIC_SYSTEM_PROMPT
    assert "SETUP STEPS:" in prompt
    assert "(step N of lo-hi)" in prompt
    assert "NOT a physical value" in prompt
    assert "exact increment" in prompt
    # the old framing asserted a legal *physical* range — it must be gone
    assert "SETUP RANGES:" not in prompt
    assert "legal (range lo-hi)" not in prompt


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
    """Issue #33: the prompt must instruct the coach to treat a flagged electronic as
    the garage setting to confirm, never as the driver's current one."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    prompt = AGENTIC_SYSTEM_PROMPT
    assert "COCKPIT ELECTRONICS:" in prompt
    assert "garage value" in prompt
    assert "Never assert" in prompt
    assert "live on-track" in prompt


def test_live_electronics_reads_last_finite_sample():
    """Issue #33: the coach must read the driver's LIVE on-track dial state from the
    latest lap's telemetry, not the (possibly stale) garage value. Values are chosen
    so last-finite != first-finite != max, so the test can't pass on the wrong read."""
    import numpy as np

    from simcoach.coach import live_electronics
    channels = {
        "tc_map": np.array([np.nan, np.nan, 6, 6, 3, 3]),
        "tc_map_max": np.array([np.nan, np.nan, 11, 11, 11, 11]),
        "rear_brake_bias": np.array([np.nan, 0.515, 0.495, 0.495]),
        # Max never populated (all zero) -- must be omitted, not read as "0 of 0-0".
        "abs_map": np.array([np.nan, 5, 5]),
        "abs_map_max": np.array([np.nan, 0, 0]),
        # All-NaN channel -- no live sample ever arrived.
        "motor_map": np.array([np.nan, np.nan]),
        "motor_map_max": np.array([np.nan, 3, 3]),
        # tc_slip_map is simply absent -- must be omitted, not KeyError.
    }
    out = live_electronics(channels)
    assert out == {
        "VM_TRACTIONCONTROLMAP": "3 of 0-11",
        "VM_BRAKE_BALANCE": "50.5:49.5",
    }


def test_setup_lines_with_live_value_drops_the_caveat_and_step():
    """A live-covered electronic states the on-track read as fact, tagged with the
    garage value for context -- no caveat, no ordinal step (the live index already
    carries its own range). A live-less electronic keeps the caveat. A mechanical
    param is unaffected either way."""
    from simcoach.coach import _setup_lines
    setup = {
        "VM_TRACTIONCONTROLMAP": {"stringValue": "6", "value": 6, "minValue": 0,
                                  "maxValue": 11, "isFreeSetting": True},
        "VM_ENGINE_MIXTURE": {"stringValue": "Race", "isFreeSetting": True},
        "VM_REAR_WING": {"stringValue": "6.5 deg", "value": 6, "minValue": 0,
                         "maxValue": 9, "isFreeSetting": True},
    }
    live = {"VM_TRACTIONCONTROLMAP": "3 of 0-11"}
    lines = {line.split(":")[0][2:]: line for line in _setup_lines(setup, live)}
    assert lines["Tractioncontrolmap"] == "- Tractioncontrolmap: 3 of 0-11 [live on-track; garage 6]"
    assert "(step" not in lines["Tractioncontrolmap"]
    assert CAVEAT not in lines["Tractioncontrolmap"]
    assert lines["Engine Mixture"] == f"- Engine Mixture: Race {CAVEAT}"
    assert lines["Rear Wing"] == "- Rear Wing: 6.5 deg (step 6 of 0-9)"


from simcoach.coach import first_paragraph, split_spoken


def test_split_spoken_extracts_and_strips_the_last_tag_line():
    text = "Brake **later** into T4.\n\n- point one\n- point two\n\n[spoken] Brake later into T4, you're giving up 2 tenths."
    answer, spoken = split_spoken(text)
    assert spoken == "Brake later into T4, you're giving up 2 tenths."
    assert answer == "Brake **later** into T4.\n\n- point one\n- point two"


def test_split_spoken_extracts_when_tag_is_first():
    text = "[spoken] Brake later into four.\n\nFull answer.\n\nMore."
    answer, spoken = split_spoken(text)
    assert spoken == "Brake later into four."
    assert answer == "Full answer.\n\nMore."


def test_split_spoken_tolerates_bold_tag_and_colon():
    answer, spoken = split_spoken("Full answer.\n**[Spoken]:** Short cue.")
    assert (answer, spoken) == ("Full answer.", "Short cue.")


def test_split_spoken_absent_or_malformed_returns_text_unchanged():
    assert split_spoken("Just an answer.") == ("Just an answer.", None)
    assert split_spoken("Answer.\n[spoken]") == ("Answer.\n[spoken]", None)     # empty capture = absent
    assert split_spoken("[spoken] only a summary") == ("", "only a summary")


def test_first_paragraph_is_the_fallback():
    assert first_paragraph("\n\nFirst block\nstill first.\n\nSecond.") == "First block\nstill first."
    assert first_paragraph("") == ""


def test_system_prompt_requires_lap_identity():
    """#108 item 1: the coach must say which lap a per-lap fact came from, and call
    out a later answer reading a newer lap than an earlier one."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "Lap:" in AGENTIC_SYSTEM_PROMPT


def test_system_prompt_calls_track_knowledge_the_track_notes():
    """#108 item 8: the coach must never call the authored track knowledge a wiki
    or a guide -- the driver never authored a wiki, they authored notes."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "track notes" in AGENTIC_SYSTEM_PROMPT
    assert "wiki" in AGENTIC_SYSTEM_PROMPT


def test_spoken_sentence_locates_corners_by_number_first():
    """#108 item 10: a spoken cue must lead with 'Turn N' -- a corner NAME alone
    ('Pouhon') isn't followable mid-session; the number is."""
    from simcoach.coach import SPOKEN_SENTENCE
    assert "Turn N" in SPOKEN_SENTENCE
    assert "never the name alone" in SPOKEN_SENTENCE


def test_spoken_sentence_points_to_the_locate_it_descriptor():
    """#108 item 12: the spoken cue must reuse the corner tools' own locator
    ('Locate it: Turn N, sector 2, the fast left after the long straight'),
    not a bare number the model has to invent context around."""
    from simcoach.coach import SPOKEN_SENTENCE
    assert "Locate it" in SPOKEN_SENTENCE


def test_prompt_routes_incidents_to_get_stint_events():
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "get_stint_events" in AGENTIC_SYSTEM_PROMPT
    assert "flat spot" in AGENTIC_SYSTEM_PROMPT
