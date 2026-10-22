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


def test_system_prompt_says_electronics_come_from_the_newest_stored_lap():
    """Issue #128: the live cockpit dials come from the last sample of the newest
    stored lap of ANY kind, not "the latest captured lap" (which read, before the
    fix, as the latest COMPLETE lap and left a mid-stint change invisible)."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    section = AGENTIC_SYSTEM_PROMPT.split("COCKPIT ELECTRONICS:", 1)[1][:300]
    assert "last sample of the newest stored lap" in section
    assert "partial laps included" in section


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


def test_voice_ptt_locates_corners_by_number_and_the_locate_it_descriptor():
    """#119: the PTT voice rule must lead with 'Turn N' -- a corner NAME alone
    ('Pouhon') isn't followable mid-session; the number is -- and reuse the
    corner tools' own locator ('Locate it: Turn N, sector 2, the fast left
    after the long straight'), not a bare number the model has to invent
    context around."""
    from simcoach.coach import VOICE_PTT
    assert "Turn N" in VOICE_PTT
    assert "Locate it" in VOICE_PTT
    assert "only if the driver has used it themselves" in VOICE_PTT


def test_voice_conversation_locates_corners_the_same_way():
    """#119: the conversation voice rule carries the same corner-locator rule
    as PTT -- only one of the two is ever appended to a given turn's system
    block, so each must spell the rule out in full rather than cross-refer."""
    from simcoach.coach import VOICE_CONVERSATION
    assert "Turn N" in VOICE_CONVERSATION
    assert "Locate it" in VOICE_CONVERSATION
    assert "only if the driver has used it themselves" in VOICE_CONVERSATION


def test_prompt_routes_incidents_to_get_stint_events():
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "get_stint_events" in AGENTIC_SYSTEM_PROMPT
    assert "flat spot" in AGENTIC_SYSTEM_PROMPT


def test_system_prompt_grounds_car_facts_in_measured_data():
    """#108 item 5: the coach must never assert a car's class or aid availability
    from memory -- get_session_status states it from data, or "unknown"."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "CAR FACTS" in AGENTIC_SYSTEM_PROMPT
    assert "get_session_status" in AGENTIC_SYSTEM_PROMPT.split("CAR FACTS", 1)[1][:200]


def test_system_prompt_never_lets_damage_figures_be_guessed():
    """#143: damage figures come from a tool call (get_stint_events), never the
    model's own memory, and the dent scale is never attributed to a named panel."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    section = AGENTIC_SYSTEM_PROMPT.split("CAR FACTS", 1)[1][:450]
    assert "get_stint_events" in section
    assert "panel" in section
    assert "NEVER guess" in section


def test_system_prompt_flags_electronics_direction_as_unknown_by_default():
    """#108 item 6: which way a TC/ABS/motor map means MORE intervention is only
    known when get_setup says so from the car notes; otherwise it's unknown."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "ELECTRONICS DIRECTION" in AGENTIC_SYSTEM_PROMPT
    assert "get_setup" in AGENTIC_SYSTEM_PROMPT.split("ELECTRONICS DIRECTION", 1)[1][:200]


def test_system_prompt_resolves_that_lap_to_the_fresh_lap():
    """#131: when a NEW SINCE YOUR LAST ANSWER fact is present, 'that'/'that lap'/
    'how was that'/'the last one' must resolve to it without a clarifying question."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "REFERENCES:" in AGENTIC_SYSTEM_PROMPT
    refs = AGENTIC_SYSTEM_PROMPT.split("REFERENCES:", 1)[1][:400]
    assert "NEW SINCE YOUR LAST ANSWER" in refs
    assert "that lap" in refs
    assert "how was that" in refs
    assert "the last one" in refs
    assert "do not ask which" in refs


def test_driver_memory_prompt_confirms_before_saving_and_supports_replaces():
    """#139: an incident note waits for the driver to confirm lap+corner, and a
    correction inside the same chat uses replaces=<id> instead of a new row."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "DRIVER MEMORY" in AGENTIC_SYSTEM_PROMPT
    section = AGENTIC_SYSTEM_PROMPT.split("DRIVER MEMORY", 1)[1][:600]
    assert "confirmed the lap and corner" in section
    assert "neither a tool nor the driver stated" in section
    assert "replaces=<its id>" in section


def test_history_prompt_routes_cross_car_comparisons_through_car_argument():
    """#140: 'slower than the Corvette' must route through get_personal_best/
    get_progress/get_lap_summary with car= set to the other car, never a flat
    'no data' refusal."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "HISTORY:" in AGENTIC_SYSTEM_PROMPT
    section = AGENTIC_SYSTEM_PROMPT.split("HISTORY:", 1)[1][:800]
    assert "car= / track=" in section
    assert "Never claim you have no data for the other car without having asked" in section


def test_corner_identity_prompt_guards_the_turn_n_turn_n_stutter():
    """#141: on a map whose name is only the corner's own number, the coach must
    say the number once plus a descriptor, never 'Turn 16 (Turn 16)'."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "Turn 1 (Copse)" in AGENTIC_SYSTEM_PROMPT
    section = AGENTIC_SYSTEM_PROMPT.split("Turn 1 (Copse)", 1)[1][:300]
    assert "Turn 16, the heavy stop" in section
    assert "never 'Turn 16 (Turn 16)'" in section


def test_system_prompt_never_asks_permission_to_call_a_tool():
    """#131: 'want me to check X?' wastes a turn when the tool could just answer --
    the coach must call it and answer instead of asking permission."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    assert "Never ask permission to call a tool" in AGENTIC_SYSTEM_PROMPT


def test_system_prompt_notes_position_changes_never_gaps_or_traffic():
    """#134: a race lap call may note a position change when it matters, but must
    never invent gaps or traffic. #152: a class gap IS measured now -- stated only as
    get_race_state reports it; traffic is still never stated."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    racecraft = AGENTIC_SYSTEM_PROMPT.split("RACECRAFT:", 1)[1][:600]
    assert "position change" in racecraft
    assert "never state traffic" in racecraft
    assert "a gap only as get_race_state reports it" in racecraft


def test_system_prompt_forbids_reusing_an_earlier_races_numbers():
    """#136: a chat spans the races of an evening, so an earlier turn's spin, lap time
    or position is history -- this turn's numbers come from this turn's tool calls."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT
    identity = AGENTIC_SYSTEM_PROMPT.split("LAP IDENTITY:", 1)[1][:1400]
    assert "more than one race" in identity
    assert "a tool call in THIS turn" in identity
    assert "marked as an earlier race" in identity


def test_agentic_prompt_routes_race_questions_to_one_race_tool():
    """#152: one routing paragraph; the stamp is said; gaps are no longer 'not measured'."""
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT as p
    assert "→ get_race_state" in p and "→ get_strategy" in p
    assert "ONE call answers the question" in p and "As of lap N" in p
    assert "never state gaps or traffic" not in p
    assert "→ get_battle" in p
    # #213: a rival's lap times are a race-state / race-result answer, not get_battle's.
    assert ("a rival's best/last lap → get_race_state (live) / get_race_result (after the "
            "race)") in p
    p.format(car="x", speed_unit="km/h", temp_unit="°C")    # no stray braces


def test_prompt_wording_rules_235_236_237():
    from simcoach.coach import AGENTIC_SYSTEM_PROMPT as p
    # #235: the optimal is not a pace limit.
    assert "never as a limit on how fast he can go" in p
    # #236: a best lap goes to get_time_on_table, not get_lap_summary.
    refs = p.split("REFERENCES:", 1)[1][:900]
    assert "a best, a personal best or a breakthrough" in refs and "get_time_on_table" in refs
    # #237: supersession is an update, not a correction; phase always named.
    ident = p.split("LAP IDENTITY:", 1)[1][:1400]
    assert "('qualifying lap 4 now shows" in ident and "is not correcting him" in ident
    assert "as an update" in ident and "never as a correction" in ident
    assert "never a bare 'lap 3'" in ident
