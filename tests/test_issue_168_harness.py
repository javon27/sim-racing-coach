"""#168: "it still feels like I'm talking to a brick wall" -- Long Beach/Algarve,
2026-09-24 (chat-20260924T223706-cca7c5, chat-20260924T232917-c69dd5)."""
from simcoach.coach import AGENTIC_SYSTEM_PROMPT


def test_prompt_takes_the_drivers_word_as_given():
    # #237/#240 corrected his lap numbering; #211/#212 (09-23) refused to "just agree"
    # with where he finished.
    p = AGENTIC_SYSTEM_PROMPT
    assert "TAKEN AS GIVEN" in p
    assert "never correct the driver's lap numbering" in p
    assert "The driver's own figures" in p
    assert "If the driver says it was a race, it was" in p
    # ...while the coach still never invents its own figures.
    assert "YOU introduce comes from a tool you called THIS turn -- never invent one" in p


def test_empty_track_notes_license_framed_general_knowledge(tmp_path):
    # #236: "first time at Long Beach, any tips?" -> "I've got nothing loaded".
    from simcoach.coach_tools import CoachContext
    from simcoach.store import LapStore
    ctx = CoachContext(LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data")),
                       track="Nowhere Street Circuit", car="C")
    notes = ctx.dispatch("get_track_notes", {})
    assert notes.startswith("No notes on file for Nowhere Street Circuit")
    assert "general knowledge of the track is fine" in notes
    assert "no turn numbers, names or directions from memory" in notes
    assert "General knowledge of the track's character is fine" in ctx.dispatch(
        "list_track_corners", {})
    p = AGENTIC_SYSTEM_PROMPT
    assert "you MAY give general, well-known knowledge of the track" in p
    assert "NEVER recall corner names, numbers, or directions" in p   # still owned data
