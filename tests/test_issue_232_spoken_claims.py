"""#232: two tool-free answers (rig, Bahrain Outer, turns 10-11) passed the #159 guard
while fabricating facts in spoken style: amounts with no digits ("a tenth and a half"),
rankings of the driver's own laps ("your quickest of the race") and corner time
attribution ("Turn 4 is where the time is"). Each must force a tool round."""
import pytest

from simcoach.coach import states_data

NEW_SINCE = "NEW SINCE YOUR LAST ANSWER: race lap 7 (1:14.908) landed just now."
KNOWN_T2 = f"{NEW_SINCE}\n\nI think that turn 2 was better."
KNOWN_T4 = f"{NEW_SINCE}\n\nI think my Turn 4 and set up into Turn 5 needs work."


# The rig answers, verbatim (coach_chat ids 329, 330).
RIG_1 = ("Yes it was — Turn 2 is now at your own best on that lap, nothing left there. "
         "And 1:14.908 is your quickest of the race.")
RIG_2 = ("The time that's left is Turn 10, the fast one late in the lap: about a tenth "
         "and a half, all on the exit.")
RIG_3 = ("Agreed — Turn 4 is where the time is on that lap now, a bit over a tenth, and "
         "it's all in the entry.")
# measured T2 trail was 0%: "like you do into Turn N" is a claim about the telemetry
RIG_4 = ("Ease the initial pressure and trail a little deeper, like you do into Turn 2 "
         "now, and both corners come to you.")


@pytest.mark.parametrize("text, known", [
    (RIG_1, KNOWN_T2), (RIG_2, KNOWN_T2), (RIG_3, KNOWN_T4), (RIG_4, KNOWN_T4),
])
def test_the_rig_sentences_flag(text, known):
    assert states_data(text, known), text


@pytest.mark.parametrize("text", [
    # (a) spoken amounts
    "You gave away half a second there.",
    "That's a couple of tenths in the braking zone.",
    "There's a few tenths in the last sector.",
    "You're a bit over a tenth off.",
    "About a tenth and a half.",
    "A couple of hundredths, nothing more.",
    "You lost a few seconds in traffic.",
    # (b) rank claims about the driver's own laps/corners
    "That was your fastest lap of the session.",
    "That's your best lap.",
    "Turn 7 is at your best now.",
    "That one's your quickest of the stint.",
    # (c) corner time attribution
    "Turn 4 is where the time is.",
    "The time that's left is in the final sector.",
    "It's all on the exit.",
    "The loss is all in the braking.",
    # red-team round 2: ranks without "your", positions, gaps
    "That's a new personal best.",
    "That's your PB.",
    "Fastest lap of the race, well done.",
    "That's your best yet.",
    "You're up to fourth.",
    "You're running fourth.",
    "You gained two places.",
    "The gap's about a second.",
    "You're a second clear.",
    "Couple of tenths on the exit.",
])
def test_spoken_claims_flag(text):
    assert states_data(text, known=""), text


@pytest.mark.parametrize("text", [
    "Nice one, that's your best effort yet.",          # praise, no measured claim
    "Give it your best shot.",
    "Do your best to stay patient on entry.",
    "Try to carry a bit more speed through the middle.",
    "A tenth here and there adds up over a stint.",    # generic, no corner or lap
    "Wait a second before you go back to throttle.",   # bare "a second" is idiom
    "Look for a second apex in the long right-hander.",
    "Brake a touch later and be patient with the throttle.",
    "Trail a little deeper, like you would on a dry track.",
    "Exit speed is what matters on this layout.",
    # red-team round 2: on a model that can't be forced, an over-trigger replaces
    # a good technique answer with UNCONFIRMED
    "You're second-guessing the brake point.",
    "Running second gear through there is fine.",
    "Wait about a second before you pick up the throttle.",
    "Take a second look at your line into Turn 4.",
    "You're running wide on exit.",
    "You're at your best when you're relaxed.",
    "All on the throttle out of the hairpin is the classic mistake.",
    "Your best lap will come when you stop overdriving.",
    "Your fastest lap comes from a smooth exit.",
    "Wait a few seconds after the green before you push.",
])
def test_technique_and_small_talk_still_pass(text):
    assert not states_data(text, known=""), text


def test_a_spoken_amount_the_driver_gave_is_not_a_claim():
    q = "i think i lost a couple of tenths in turn 2"
    assert not states_data("A couple of tenths in Turn 2 sounds right to chase.", known=q)
    assert not states_data("About a second, yes.", known="am i about a second off")
    assert states_data("About a second, yes.", known="")
