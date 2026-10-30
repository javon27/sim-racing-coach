"""#159: the coach answered race questions with invented numbers (and an invented rival)
without calling a single tool. These pin the guard: tool results are persisted, a
data-shaped answer with no tool this turn is discarded and re-asked with a forced tool,
and a named other driver the data never mentioned is cut."""
import pytest
from fastapi.testclient import TestClient

from simcoach.coach import (
    AGENTIC_SYSTEM_PROMPT,
    UNCONFIRMED,
    Coach,
    ground_names,
    states_data,
)
from simcoach.coach_tools import CoachContext
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore

# The shapes of the incident answers (Road Atlanta, 2026-09-23, turns #231/#234).
RIVAL = ("Let me pull the gaps. Kevin Fischer is 8.4 s behind, closing 0.6 s/lap, "
         "so he catches you around lap 37.")
TIRES = "Let me pull your tires. Fronts are at 141 °F and you're losing 3.36 s/lap."


def _store(tmp_path):
    return LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))


# --- the detector -------------------------------------------------------------

def test_states_data_true_positives_from_the_incident():
    assert states_data(RIVAL, known="")
    assert states_data(TIRES, known="")
    assert states_data("Let me check that for you.", known="")          # lookup, no tool
    assert states_data("You're P4 in class with 12 laps to go.", known="")
    assert states_data("Your best is 1:18.402.", known="")
    assert states_data("You're about three tenths off in the esses.", known="")
    # a named rival with a race cue, not in the data
    assert states_data("Conner Mallett is right behind you.", known="")


def test_states_data_true_negatives():
    for text in (
        "Hey! Ready when you are.",
        "Trail-brake into the corner and release as you add steering.",
        "I can't see that yet -- there's no lap captured.",
        "Which lap do you mean, the out lap or the last flyer?",
        "Soften the front anti-roll bar a step or two to calm the entry.",
        "Turn 5 wants patience: wait for the rotation before throttle.",
    ):
        assert not states_data(text, known=""), text


def test_states_data_accepts_a_name_the_driver_used():
    assert not states_data("Conner Mallett is right behind you.",
                           known="is Conner Mallett close?")


def test_ground_names_cuts_only_the_ungrounded_sentence():
    text = "As of lap 30 you're P3.\n\nKevin Fischer is 8.4 s behind. Keep pushing!"
    out, names = ground_names(text, known="As of lap 30 (race). Position: P3")
    assert names == ["Kevin Fischer"]
    assert "Kevin" not in out
    assert out == "As of lap 30 you're P3.\n\nKeep pushing!"   # #168: dropped, not spliced


def test_ground_names_keeps_a_grounded_rival_and_corner_names():
    known = "Car behind in class: Conner Mallett (Porsche 963), 2.1 s."
    text = "Conner Mallett is 2.1 s behind. Big Bend is where you gain."
    assert ground_names(text, known) == (text, [])


# --- 1. tool RESULTS persisted ------------------------------------------------

def test_dispatch_records_each_result(tmp_path):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    out = ctx.dispatch("get_race_state", {})
    assert ctx.tool_calls == [{"name": "get_race_state", "input": {}, "result": out}]
    ctx.dispatch("nope", {})
    assert ctx.tool_calls[-1]["result"].startswith("error: unknown tool")


def test_dispatch_records_a_raising_tool(tmp_path, monkeypatch):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    monkeypatch.setattr(ctx, "get_setup", lambda: 1 / 0)
    try:
        ctx.dispatch("get_setup", {})
    except ZeroDivisionError:
        pass
    assert ctx.tool_calls[-1]["result"] == "error: ZeroDivisionError: division by zero"


def test_save_chat_caps_the_stored_result_and_reads_it_back(tmp_path):
    from simcoach.store import TOOL_RESULT_CAP
    store = _store(tmp_path)
    big = "x" * (TOOL_RESULT_CAP * 3)
    calls = [{"name": "get_lap_summary", "input": {}, "result": big}]
    store.save_chat("c1", "s1", "T", "C", None, "q", "a", calls)
    got = store.chat_messages("c1")[0]["tool_calls"][0]
    assert got["name"] == "get_lap_summary"
    assert got["result"].startswith("x" * TOOL_RESULT_CAP)
    assert len(got["result"]) < TOOL_RESULT_CAP + 40
    assert got["result"].endswith("[truncated]")
    assert calls[0]["result"] == big      # the caller's list is not mutated


# --- 2/3. the stream guard ----------------------------------------------------

def test_ungrounded_draft_is_discarded_and_a_tool_is_forced(tmp_path):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    model = FakeModel(stream_script=[("text", RIVAL)],
                      forced_stream_script=[("tool", "get_race_state", {}),
                                            ("text", "No live race read yet.")])
    events = list(Coach(model).ask_agentic_stream(ctx, "can I catch him?", car="C"))
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "Kevin" not in text and "Let me pull" not in text   # the draft never streamed
    assert text == "No live race read yet."
    assert model.forced == [False, True]
    assert [c["name"] for c in ctx.tool_calls] == ["get_race_state"]
    assert len([e for e in events if e["type"] == "usage"]) == 2   # both rounds billed


def test_tool_free_small_talk_is_not_forced(tmp_path):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    model = FakeModel(reply="Evening! Ready when you are.")
    events = list(Coach(model).ask_agentic_stream(ctx, "hey", car="C"))
    assert [e["text"] for e in events if e["type"] == "text"] == ["Evening! Ready when you are."]
    assert model.forced == [False]


def test_forced_round_that_still_calls_no_tool_answers_unconfirmed(tmp_path):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    model = FakeModel(stream_script=[("text", RIVAL)], forced_stream_script=[("text", TIRES)])
    events = list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    assert [e["text"] for e in events if e["type"] == "text"] == [UNCONFIRMED]


def test_invented_rival_after_a_tool_is_never_streamed(tmp_path, caplog):
    # Red-team MAJOR 1: conversation voice speaks each streamed sentence, so the cut
    # must happen BEFORE a sentence is emitted, not as a correction afterwards.
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    model = FakeModel(stream_script=[("tool", "get_race_state", {}),
                                     ("text", "Good pace. Kevin Fischer is 8."),
                                     ("text", "4 s behind. Keep"), ("text", " pushing!")])
    with caplog.at_level("INFO", logger="simcoach.coach"):
        events = list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert not any("Kevin" in t for t in texts)
    assert "".join(texts) == "Good pace. Keep pushing!"   # #168: dropped, not spliced
    assert texts[0] == "Good pace."          # a clean sentence goes out before the rest
    assert not [e for e in events if e["type"] == "replace"]
    assert "Kevin Fischer" in caplog.text     # every cut is logged with its reason


def test_discarded_draft_is_logged(tmp_path, caplog):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    model = FakeModel(stream_script=[("text", RIVAL)],
                      forced_stream_script=[("tool", "get_race_state", {}), ("text", "ok")])
    with caplog.at_level("INFO", logger="simcoach.coach"):
        list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    assert "discarded" in caplog.text and "Let me pull" in caplog.text


# --- red-team MAJOR 2: false positives from the 72 real tool-free answers --------

# Real answers (coach_chat replay) that offered a FUTURE lookup -- not a claim.
OFFERS = [
    ("Copy that, radio check's good on my end too. What do you want to look at? Let me "
    "know and I'll pull it up."),
    "Give me a direction and I'll pull the data.",
    ("Go get some laps in and let me know how it feels. I'll pull the corner data "
    "whenever you want a check."),
    ("I'll be here once you've got data; just call me back after a run or two and "
    "I'll dig into corner times, tire warm-up, whatever you need."),
    "Just say the word when you want a lap read, and I'll pull the live data.",
    ("When you've got a couple of laps in, call me back and I'll dig into how it's "
    "behaving."),
    "Still with you -- I'm reading you fine on this end, no dropouts on my side.",
]

# The four incident turns (#229/#231/#233/#234), opening as they really did.
INCIDENT = [
    "Let me pull that lap 19 incident and the current strategy numbers together.",
    "Let me get the current gap and trend as of that lap 23.",
    "Let me pull that lap 25 and check the incident log for it.",
    "Let me check the tire trend and confirm what's happening with that front-left.",
]


def test_future_offers_are_not_lookups():
    for text in OFFERS:
        assert not states_data(text, known=""), text


def test_all_four_incident_turns_still_flag():
    for text in INCIDENT:
        assert states_data(text, known=""), text


def test_a_figure_the_driver_or_context_gave_is_not_a_claim():
    q = "It was the contact with Lynn in lap 2. We both went into the chicane."
    a = "Got it: contact at the chicane on lap 2, front-left puncture, forced stop."
    assert not states_data(a, known=q)
    assert states_data(a, known="")
    # the injected CURRENT RACE line rides in the user turn, so it's known too
    assert not states_data("That's lap 12 of the race.",
                           known="CURRENT RACE: race lap 12.\n\nhow far in am I?")


def test_spoken_figures_flag():
    for text in ("You're running fifth place.", "Thirteen laps to go.",
                 "Your best is a one twenty-three point four.",
                 "About a minute twenty-three a lap.", "You're sixteen seconds clear."):
        assert states_data(text, known=""), text


def test_repeat_requests():
    from simcoach.coach import is_repeat_request
    for q in ("Repeat that.", "What?", "Say again?", "Hello? Hello?", "come again",
              "Sorry?", "Can you repeat that?", "say that again please"):
        assert is_repeat_request(q), q
    for q in ("What's my best?", "Hello, how's my pace?", "repeat the tire numbers",
              "What happened at Turn 1?", ""):
        assert not is_repeat_request(q), q


def test_repeat_request_replays_the_last_answer_without_a_model_call(tmp_path):
    store = _store(tmp_path)
    store.save_chat("c1", "s1", "T", "C", None, "how are my tires?", "Fronts are hot.", None)
    model = FakeModel(reply="SHOULD NOT RUN")
    client = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    events = _ask(client, {"question": "Say again?", "session_id": "s1", "chat_id": "c1",
                           "voice": "ptt"})
    assert events[-1]["type"] == "done" and events[-1]["answer"] == "Fronts are hot."
    assert model.forced == []                       # no model call, nothing billed
    assert store.chat_messages("c1")[-1]["answer"] == "Fronts are hot."


# --- MINOR 3: models that 400 on a forced tool never get one ---------------------

def test_claude_models_that_cannot_be_forced():
    from simcoach.model.claude import ClaudeModel
    for mid, ok in (("claude-sonnet-5", True), ("claude-opus-5", True),
                    ("claude-haiku-4-5-20251001", True), ("claude-opus-5-5", False),
                    ("claude-fable-5-1", False), ("claude-fable-5", False),
                    ("claude-mythos-5-1", False)):
        m = ClaudeModel.__new__(ClaudeModel)
        m.model = mid
        assert m.can_force_tool is ok, mid


def test_unforceable_model_skips_the_forced_round(tmp_path):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    model = FakeModel(stream_script=[("text", RIVAL)])
    model.can_force_tool = False
    events = list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    assert model.forced == [False]                  # no second (400-bound) call
    assert [e["text"] for e in events if e["type"] == "text"] == [UNCONFIRMED]


def test_prompt_forbids_restating_and_announcing_without_a_tool():
    p = AGENTIC_SYSTEM_PROMPT
    assert "earlier in the conversation" in p
    assert "unless you call a tool in the same turn" in p


# --- the incident, end to end through /coach ----------------------------------

def _ask(client, req):
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json(req)
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in ("done", "error"):
                return events


def _incident(tmp_path, forced_answer):
    store = _store(tmp_path)
    # Earlier race answers in the chat: figures the model can parrot back.
    store.save_chat("c1", "s1", "Road Atlanta", "C", None, "how are my tires?",
                    "As of lap 28 your fronts are at 138 °F, dropping 3.53 s/lap.", None)
    store.save_chat("c1", "s1", "Road Atlanta", "C", None, "who's behind me?",
                    "As of lap 29, Conner Mallett is behind in class; gap not measured.", None)
    model = FakeModel(stream_script=[("text", RIVAL)],
                      forced_stream_script=[("tool", "get_race_state", {}),
                                            ("text", forced_answer)])
    client = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    events = _ask(client, {"question": "He's still in the pit, do I have a chance of "
                                       "catching up?", "session_id": "s1", "chat_id": "c1"})
    return store, model, events


def test_incident_replay_forces_get_race_state_and_never_persists_the_rival(tmp_path):
    store, model, events = _incident(tmp_path, "I can't read the race live right now.")
    assert model.forced == [False, True]
    done = events[-1]
    assert done["type"] == "done" and "Kevin" not in done["answer"]
    assert not any("Kevin" in e.get("text", "") for e in events)
    row = store.chat_messages("c1")[-1]
    assert "Kevin" not in row["answer"]
    assert row["tool_calls"][0]["name"] == "get_race_state"
    assert "No live race" in row["tool_calls"][0]["result"]     # auditable


def test_incident_replay_rival_repeated_after_the_tool_is_cut(tmp_path):
    store, _, events = _incident(tmp_path, RIVAL)
    # #168: the rival sentence is dropped, not spliced -- and the narration left over is
    # not an answer on its own (red-team round 1), so the whole answer is UNCONFIRMED.
    want = UNCONFIRMED
    assert not any("Kevin" in e.get("text", "") for e in events)   # never spoken
    assert "".join(e["text"] for e in events if e["type"] == "text") == want
    assert events[-1]["answer"] == want
    assert store.chat_messages("c1")[-1]["answer"] == want


# --- the swappable-Model seam: Claude implements force_tool -------------------

class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Client:
    """Turn 1 calls a tool, turn 2 answers. No `.stream`: the create path."""

    def __init__(self):
        self.messages = self
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            return _Block(content=[_Block(type="tool_use", id="t1", name="get_race_state",
                                          input={})], stop_reason="tool_use")
        return _Block(content=[_Block(type="text", text="ok")], stop_reason="end_turn")


def _claude(client):
    from simcoach.model.claude import ClaudeModel
    m = ClaudeModel.__new__(ClaudeModel)      # no real anthropic client
    m.model, m.max_tokens, m.client = "claude-sonnet-5", 100, client
    return m


def test_claude_force_tool_forces_only_the_first_turn():
    client = _Client()
    list(_claude(client).run_stream(
        "sys", [{"role": "user", "content": "q"}], [{"name": "get_race_state"}],
        lambda n, i: "r", force_tool=True, thinking="standard"))
    first, second = client.calls
    assert first["tool_choice"] == {"type": "any"}
    assert first["thinking"] == {"type": "disabled"}     # API rejects forced + thinking
    assert "tool_choice" not in second
    assert second["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_claude_default_sends_no_tool_choice():
    client = _Client()
    list(_claude(client).run_stream(

        "sys", [{"role": "user", "content": "q"}], [{"name": "get_race_state"}],
        lambda n, i: "r"))
    assert all("tool_choice" not in kw for kw in client.calls)


# --- coordinator round 2: earlier turns' stored tool RESULTS count as known --------

def _chat_with_result(tmp_path, result, reply):
    store = _store(tmp_path)
    store.save_chat("c1", "s1", "T", "C", None, "how are my brakes?",
                    "ABS is busy into Turn 10.",   # an ANSWER: never known on its own
                    [{"name": "get_handling", "input": {}, "result": result}])
    model = FakeModel(reply=reply, forced_stream_script=[("tool", "get_handling", {}),
                                                         ("text", "Re-read.")])
    client = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    events = _ask(client, {"question": "why is that?", "session_id": "s1", "chat_id": "c1"})
    return model, events


def test_a_figure_from_an_earlier_turns_tool_result_is_known(tmp_path):
    model, events = _chat_with_result(
        tmp_path, "Turn 10: ABS active 50% of the braking zone; fronts 138 °F.",
        "That 50% ABS spike is the blind crest.")
    assert model.forced == [False]
    assert events[-1]["answer"] == "That 50% ABS spike is the blind crest."


def test_a_drifted_figure_still_forces_a_re_read(tmp_path):
    # #234's shape: the earlier tool said 138 °F; restating it as 141 °F is a new claim.
    model, events = _chat_with_result(
        tmp_path, "Turn 10: ABS active 50% of the braking zone; fronts 138 °F.",
        "Fronts are sitting at 141°F now.")
    assert model.forced == [False, True]
    assert events[-1]["answer"] == "Re-read."


def test_an_earlier_answers_figure_is_not_known(tmp_path):
    store = _store(tmp_path)
    store.save_chat("c1", "s1", "T", "C", None, "tires?", "Fronts at 138 °F.", None)
    model = FakeModel(reply="Still 138 °F up front.",
                      forced_stream_script=[("tool", "get_tire_report", {}), ("text", "ok")])
    client = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    _ask(client, {"question": "and now?", "session_id": "s1", "chat_id": "c1"})
    assert model.forced == [False, True]      # no laundering a past answer


def test_in_the_first_place_is_not_a_position():
    # replay #195: the idiom, not a race position
    assert not states_data("That's why the sawing started in the first place.", known="")
    assert states_data("You're up to first place.", known="")


# --- #168: an unverifiable sentence is dropped, never spliced -------------------

def test_168_one_bad_sentence_loses_only_that_sentence(tmp_path):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    answer = ("That confirms it, ignore that Turn 2 gain.\n\nKevin Fischer is closing "
              "behind you. Get back to throttle earlier after the apex.")
    model = FakeModel(stream_script=[("tool", "get_lap_summary", {}), ("text", answer)])
    events = list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert text == ("That confirms it, ignore that Turn 2 gain.\n\n"
                    "Get back to throttle earlier after the apex.")
    assert "confirm that" not in text and "can't confirm" not in text


def test_168_every_sentence_cut_says_the_fallback_once(tmp_path):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    model = FakeModel(stream_script=[("tool", "get_race_state", {}),
                                     ("text", "Kevin Fischer is behind. Momo Racer is ahead.")])
    events = list(Coach(model).ask_agentic_stream(ctx, "q", car="C"))
    assert [e["text"] for e in events if e["type"] == "text"] == [UNCONFIRMED]


def test_168_the_stock_phrase_is_gone():
    assert "can't confirm" not in UNCONFIRMED.lower()


# --- #168 red-team round 1 -----------------------------------------------------

RIV168 = "Kevin Fischer is 8.4 s behind, closing 0.6 s/lap."


def _say(tmp_path, script, voice=None):
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    events = list(Coach(FakeModel(stream_script=script)).ask_agentic_stream(
        ctx, "who's behind?", car="C", voice=voice))
    return "".join(e["text"] for e in events if e["type"] == "text")


@pytest.mark.parametrize("voice", [None, "conversation", "ptt"])
@pytest.mark.parametrize("script", [
    [("tool", "get_race_state", {}), ("text", "Let me pull the gaps. " + RIV168)],
    [("tool", "get_race_state", {}), ("text", "Copy that. " + RIV168)],
    [("tool", "get_race_state", {}),
     ("text", "Here is the field:\n\n- " + RIV168 + "\n- Momo Racer is ahead.")],
])
def test_leftover_narration_is_not_an_answer(tmp_path, script, voice):
    assert _say(tmp_path, script, voice) == UNCONFIRMED


@pytest.mark.parametrize("voice", [None, "conversation", "ptt"])
def test_uncut_narration_and_short_answers_still_go_out(tmp_path, voice):
    assert _say(tmp_path, [("tool", "get_race_state", {}), ("text", "Copy that.")],
                voice) == "Copy that."
    said = _say(tmp_path, [("text", "Let me check."), ("tool", "get_race_state", {}),
                           ("text", "You're P3.")], voice)
    assert said.endswith("You're P3.") and UNCONFIRMED not in said


def test_a_cut_last_sentence_leaves_no_trailing_break(tmp_path):
    said = _say(tmp_path, [("tool", "get_race_state", {}),
                           ("text", "Keep pushing.\n\n" + RIV168)])
    assert said == "Keep pushing."


def test_a_cut_numbered_item_takes_its_marker(tmp_path):
    said = _say(tmp_path, [("tool", "get_race_state", {}),
                           ("text", "Two things:\n1. " + RIV168 + "\n2. Push on exit.")])
    assert said == "Two things:\n2. Push on exit."
    assert ground_names("Two things:\n1. " + RIV168 + "\n2. Push on exit.", "")[0] == (
        "Two things:\n2. Push on exit.")


def test_a_preposition_is_not_a_first_name():
    for text in ("At Turn Seven the gap closes.", "On Lap Five you were ahead.",
                 "After Big Bend you gain on the car behind."):
        assert ground_names(text, "") == (text, []), text


# --- #168 red-team round 2 -----------------------------------------------------

@pytest.mark.parametrize("voice", [None, "conversation"])
def test_narration_before_the_tool_goes_out_when_the_tool_starts(tmp_path, voice):
    """Not held through the tool round (silence while it runs); if every sentence
    after it is cut, the fallback follows it rather than standing in for it."""
    ctx = CoachContext(_store(tmp_path), track="T", car="C")
    model = FakeModel(stream_script=[("text", "Let me pull the gaps."),
                                     ("tool", "get_race_state", {}), ("text", RIV168)])
    events = list(Coach(model).ask_agentic_stream(ctx, "who's behind?", car="C",
                                                  voice=voice))
    kinds = [e["type"] for e in events if e["type"] in ("text", "tool")]
    assert kinds[:2] == ["text", "tool"]
    assert "".join(e["text"] for e in events if e["type"] == "text") == (
        "Let me pull the gaps.\n\n" + UNCONFIRMED)


def test_ptt_narration_before_the_tool_is_never_the_answer(tmp_path):
    said = _say(tmp_path, [("text", "Let me pull the gaps."),
                           ("tool", "get_race_state", {}), ("text", RIV168)], "ptt")
    assert said == UNCONFIRMED


@pytest.mark.parametrize("voice", [None, "conversation", "ptt"])
def test_a_lookup_phrase_that_goes_on_to_answer_is_not_filler(tmp_path, voice):
    said = _say(tmp_path, [("tool", "get_lap_summary", {}),
                           ("text", "Brake later. " + RIV168
                            + " Let me see, you lost 0.3 s at Turn 5.")], voice)
    assert said == "Brake later. Let me see, you lost 0.3 s at Turn 5."
    said = _say(tmp_path, [("tool", "get_lap_summary", {}),
                           ("text", RIV168 + " Let me see, you lost 0.3 s at Turn 5.")],
                voice)
    assert said == "Let me see, you lost 0.3 s at Turn 5."


@pytest.mark.parametrize("voice", [None, "conversation", "ptt"])
def test_a_cut_numbered_item_keeps_the_next_item(tmp_path, voice):
    said = _say(tmp_path, [("tool", "get_race_state", {}),
                           ("text", "Two things:\n1. " + RIV168 + "\n2. Push on exit.")],
                voice)
    assert said.endswith("Push on exit.") and "Kevin" not in said
    assert "1." not in said and UNCONFIRMED not in said


def test_a_possessive_name_matches_the_grounded_name():
    # #252 (rig 2026-10-27): "Matt Bell's four tenths behind" was cut although the
    # tool result named "Matt Bell (13 Autosport 2026 #13:LM), 0.4 s behind".
    known = "Car behind in class now (live, P6): Matt Bell (13 Autosport 2026 #13:LM), 0.4 s behind."
    for text in ("Matt Bell's four tenths behind you.", "Matt Bell’s four tenths behind you."):
        assert ground_names(text, known) == (text, []), text
    # a possessive of an ungrounded driver is still cut
    assert ground_names("Joe Bloggs's car is closing.", known)[1] == ["Joe Bloggs"]


@pytest.mark.parametrize("name", ["Conor O'Brien", "Conor O’Brien"])
def test_a_capital_apostrophe_capital_name_is_checked(name):
    # #258: _WORD needed a lowercase letter after the first capital, so "O'Brien" never
    # matched and a fabricated rival by that name passed the guard untouched.
    text = f"{name} is right behind you."
    assert ground_names(text, "")[1] == [name]
    # grounded either way round: a straight apostrophe in the data, a curly one spoken
    known = "Car behind in class now (live, P6): Conor O'Brien (Team 2026 #9:LM), 0.4 s behind."
    assert ground_names(text, known) == (text, [])
    assert ground_names(f"{name}'s car is closing.", known)[1] == []
    curly = known.replace("O'Brien", "O’Brien")       # a curly apostrophe in the data
    assert ground_names(text, curly) == (text, [])


def test_a_name_is_grounded_only_by_the_whole_name():
    # #258: the lookup was a substring match -- a known "Matt Bellamy" grounded "Matt Bell"
    known = "Car ahead in class now (live, P5): Matt Bellamy (Team 2026 #4:LM), 1.2 s ahead."
    assert ground_names("Matt Bell is right behind you.", known)[1] == ["Matt Bell"]
    assert ground_names("Matt Bellamy is 1.2 s ahead.", known)[1] == []


def test_review_258_whole_name_edges():
    known = "Car ahead in class now (live, P5): Matt Bell-Smith (Team 2026 #4:LM), 1.2 s ahead."
    assert ground_names("Matt Bell is right behind you.", known)[1] == ["Matt Bell"]
    # a possessive in the data still grounds the bare name
    assert ground_names("Matt Bell is 1.2 s ahead.", "Matt Bell's car, P5")[1] == []
    # a contraction before a name never pairs with it ("Here's Matt" was cut)
    assert ground_names("Here's Matt Bell, 1.2 s behind.", "Matt Bell P6")[1] == []
    assert ground_names("That's Joe Bloggs behind you.", "Matt Bell P6")[1] == ["Joe Bloggs"]
    # Spa: the model's "L'Eau Rouge" is grounded by the map's "Eau Rouge"
    text = "L'Eau Rouge is where you lose the gap."
    assert ground_names(text, "T3 Eau Rouge: left") == (text, [])
    # only the article l' is optional: a made-up O'Neil / D'Angelo is not grounded
    # by a real Neil / Angelo (#258 re-review)
    assert ground_names("O'Neil Hart is behind you.", "Neil Hart P4")[1] == ["O'Neil Hart"]
    assert ground_names("D'Angelo Russell is behind.", "Angelo Russell P4")[1] == ["D'Angelo Russell"]
