def test_coach_injects_corner_facts(synthetic_lap):
    from simcoach.model.fake import FakeModel
    from simcoach.coach import Coach
    from simcoach.corners import CornerMap
    from simcoach.analysis import analyze_lap
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    a = analyze_lap(synthetic_lap, cmap)
    m = FakeModel(reply="trail-brake deeper")
    c = Coach(m, "simcoach/knowledge")
    out = c.ask(a, cmap, "test_car", "where am I losing time?")
    assert out == "trail-brake deeper"
    sent = m.last_messages[-1]["content"]
    assert "Left Hairpin" in sent and "left" in sent   # fact injected
    assert "test_car" in m.last_system or "corner" in m.last_system.lower()
