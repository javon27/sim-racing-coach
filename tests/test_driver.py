from simcoach.driver import DriverProfile


def test_empty_profile_has_no_prompt_text():
    p = DriverProfile()
    assert p.to_prompt_text() is None


def test_profile_facts_appear_in_prompt_text():
    p = DriverProfile(
        braking_style="late", balance_preference="understeer", risk_tolerance="conservative"
    )
    text = p.to_prompt_text()
    assert "late" in text
    assert "understeer" in text
    assert "conservative" in text


def test_learned_notes_appear_in_prompt_text():
    p = DriverProfile(learned_notes=["brakes early into hairpins"])
    assert "brakes early into hairpins" in p.to_prompt_text()


def test_partial_profile_omits_unset_fields():
    p = DriverProfile(risk_tolerance="aggressive")
    text = p.to_prompt_text()
    assert "aggressive" in text
    assert "braking style" not in text
