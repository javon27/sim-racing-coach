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
