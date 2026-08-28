def test_coach_injects_corner_facts(synthetic_lap):
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    m = FakeModel(reply="trail-brake deeper")
    c = Coach(m, "simcoach/knowledge")
    out = c.ask(a, "test_car", "where am I losing time?")
    assert out == "trail-brake deeper"
    sent = m.last_messages[-1]["content"]
    assert "Left Hairpin" in sent and "left" in sent   # fact injected
    assert "test_car" in m.last_system or "corner" in m.last_system.lower()


def test_coach_apex_comes_from_analysis(synthetic_lap):
    """Apex shown in the prompt must equal the analysis corner's apex_dist (single source of truth)."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    _, user_content = c.build_prompt(a, "test_car", "where am I losing time?")
    c1 = a.corners[0]
    assert f"apex {c1.apex_dist}m" in user_content


def test_coach_system_prompt_asserts_ground_truth(synthetic_lap):
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    system, _ = c.build_prompt(a, "test_car", "where am I losing time?")
    assert "ground truth" in system.lower()


def test_coach_rejects_path_traversal_in_car(synthetic_lap):
    """A crafted car value must not escape knowledge_dir; notes are simply omitted."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    _, user_content = c.build_prompt(a, "../secret", "where am I losing time?")
    assert "Left Hairpin" in user_content
    assert "Knowledge notes" not in user_content


def test_coach_missing_knowledge_file_still_builds_prompt(synthetic_lap):
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    _, user_content = c.build_prompt(a, "no_such_car", "where am I losing time?")
    assert "Left Hairpin" in user_content
    assert "Knowledge notes" not in user_content


def test_coach_shows_delta_with_reference(synthetic_lap):
    """With a reference lap, the prompt surfaces the min_spd delta vs best."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap, reference=synthetic_lap)
    c = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    _, user_content = c.build_prompt(a, "test_car", "where am I losing time?")
    assert "vs best" in user_content
    assert "+0.0 km/h" in user_content or "-0.0 km/h" in user_content


def test_coach_no_delta_without_reference(synthetic_lap):
    """Without a reference lap, no delta text is shown."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    c = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
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
    c = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
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
    c = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    system, user_content = c.build_prompt(a, "test_car", "where am I losing time?")
    combined = (system + user_content).lower()
    assert "legal setup adjustments" not in combined
    assert "setup constraint" not in combined


def test_coach_skips_delta_text_when_delta_is_none(synthetic_lap):
    """A corner with min_spd_delta=None (degenerate) shows no delta text and doesn't crash."""
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap, reference=synthetic_lap)
    a.corners[0].min_spd_delta = None
    c = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    _, user_content = c.build_prompt(a, "test_car", "where am I losing time?")
    c1_line = [l for l in user_content.splitlines() if l.startswith("C1 ")][0]
    assert "vs best" not in c1_line
