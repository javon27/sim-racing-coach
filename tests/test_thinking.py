"""#175: thinking tiers -- the router (gates, classifier, keyword floor), the tier on
the Model seam, the Anthropic mapping, the deep-tier ack, settings and usage."""
import logging
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.corners import Corner, CornerMap
from simcoach.model.base import TokenUsage
from simcoach.model.claude import ClaudeModel
from simcoach.model.factory import make_classifier
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.thinking import CLASSIFY_TIMEOUT_S, route, thinking_tier

# --- the pure gates + keyword floor ------------------------------------------------

@pytest.mark.parametrize("question, kw, tier", [
    # on track AND moving: quick, whatever is asked (even "take your time")
    ("take your time, why is the rear loose?", {"moving": True}, "quick"),
    # on track but stationary is NOT the moving gate: the garage default applies
    ("what pressure are my fronts at?", {"moving": False}, "standard"),
    ("quick answer: brake bias?", {}, "quick"),
    ("think about it -- why does the rear step out?", {}, "deep"),
    ("take your time with this one", {}, "deep"),
    # precedence: "quick answer" is checked before "think about it"
    ("quick answer, don't think about it", {}, "quick"),
    ("tyre temps?", {"setting": "off"}, "quick"),
    ("take your time: the rear?", {"setting": "off"}, "deep"),   # driver's words win
    ("tyre temps?", {"setting": "always"}, "standard"),
    ("analyze my braking in detail", {"setting": "always"}, "standard"),
    ("think about it", {"setting": "always"}, "deep"),
    # post-race review: deep automatically once the race is over...
    ("how was that race?", {"race_finished": True}, "deep"),
    # ...not before (keyword floor: no deep word -> standard)
    ("how was that race?", {"race_finished": False}, "standard"),
    # keyword floor
    ("analyze my braking in detail", {}, "deep"),
    ("walk me through the stint", {}, "deep"),
    ("what's my fuel?", {}, "standard"),
    ("tyre temps?", {"setting": "banana"}, "standard"),   # unknown setting -> auto
])
def test_thinking_tier_gates_and_floor(question, kw, tier):
    assert thinking_tier(question, **kw) == tier


def test_a_valid_classification_decides_only_when_no_gate_did():
    deep = {"tier": "deep", "intent": "setup"}
    assert thinking_tier("why is the rear loose?", classified=deep) == "deep"
    assert thinking_tier("why is the rear loose?", moving=True, classified=deep) == "quick"
    assert thinking_tier("analyze it in detail", classified={"tier": "standard"}) == "standard"


# --- the router with a classifier behind the seam ----------------------------------

CMAP = CornerMap([Corner(5, "Eau Rouge", "left", 100, 150, 200),
                  Corner(8, "Les Combes", "right", 900, 950, 1000, label="8A")])


def _route(q, clf, **kw):
    return route(q, classifier=clf, cmap=CMAP, state="garage", **kw)


def test_router_uses_a_valid_classification_and_matches_the_corner_by_code():
    clf = FakeModel(classify_result={"tier": "deep", "intent": "setup", "corner": "turn 5",
                                     "lap": None, "rival": "Kevin Fischer",
                                     "wants_detail": True})
    tier, intent, hint, usage = _route("why does the rear step out?", clf)
    assert (tier, intent) == ("deep", "setup")
    assert "about Turn 5 (Eau Rouge)" in hint and "setup" in hint and "detail" in hint
    assert "Kevin Fischer" not in hint         # never a model-named driver as context
    assert usage == TokenUsage(input=len(clf.classified[0]) // 4, output=20)
    assert "why does the rear step out?" in clf.classified[0]


@pytest.mark.parametrize("name, expect", [
    ("8A", "Turn 8A (Les Combes)"), ("T8A", "Turn 8A (Les Combes)"),
    ("eau rouge", "Turn 5 (Eau Rouge)"), ("Turn 99", None), ("Raidillon", None),
])
def test_classifier_corner_matched_to_the_owned_map(name, expect):
    clf = FakeModel(classify_result={"tier": "standard", "intent": "corner", "corner": name})
    hint = _route("how's that corner?", clf)[2]
    if expect:
        assert f"about {expect}" in hint
    else:
        assert "about" not in hint and name not in hint   # dropped, not passed through


@pytest.mark.parametrize("bad", [None, {"tier": "banana"}, {"intent": "setup"}, ["deep"]])
def test_malformed_classification_falls_back_to_the_rules(bad):
    clf = FakeModel(classify_result=bad)
    assert _route("analyze my braking in detail", clf)[:3] == ("deep", None, "")
    assert _route("what's my fuel?", clf)[:3] == ("standard", None, "")


def test_unknown_intent_is_dropped_but_the_tier_is_used():
    clf = FakeModel(classify_result={"tier": "deep", "intent": "hack the planet"})
    assert _route("hmm?", clf)[:3] == ("deep", None, "")


def test_slow_classifier_falls_back_within_the_budget():
    clf = FakeModel(classify_result={"tier": "deep", "intent": "setup"}, classify_delay=3)
    t0 = time.monotonic()
    tier, intent, hint, usage = _route("what's my fuel?", clf)
    assert time.monotonic() - t0 < CLASSIFY_TIMEOUT_S + 0.4
    assert (tier, intent, hint, usage) == ("standard", None, "", TokenUsage())


def test_raising_classifier_falls_back():
    class Boom:
        def classify(self, prompt, schema):
            raise RuntimeError("no key")
    assert _route("analyze the stint in detail", Boom())[0] == "deep"


def test_gates_and_settings_never_call_the_classifier():
    clf = FakeModel(classify_result={"tier": "deep"})
    _route("tyres?", clf, moving=True)
    _route("quick answer: tyres?", clf)
    _route("tyres?", clf, setting="off")
    _route("tyres?", clf, setting="always")
    _route("how was that race?", clf, race_finished=True)
    assert clf.classified == []


def test_classifier_model_none_is_rules_only():
    assert make_classifier("none") is None
    assert make_classifier(None) is None


def test_every_classification_is_logged(caplog):
    clf = FakeModel(classify_result={"tier": "deep", "intent": "setup"})
    with caplog.at_level(logging.INFO, logger="simcoach.thinking"):
        _route("why is it loose?", clf)
        _route("tyres?", clf, moving=True)
    lines = [r.getMessage() for r in caplog.records]
    assert any("tier=deep via=classifier" in m and "why is it loose?" in m for m in lines)
    assert any("tier=quick via=gate" in m for m in lines)


# --- the tier on the seam, through the coach ---------------------------------------

def _ctx(tmp_path):
    return CoachContext(LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "d")),
                        track="T", car="C")


@pytest.mark.parametrize("tier", ["quick", "standard", "deep"])
def test_tier_reaches_the_model_via_ask_agentic_stream(tmp_path, tier):
    model = FakeModel(reply="Carry more speed.")
    list(Coach(model).ask_agentic_stream(_ctx(tmp_path), "q?", car="C", thinking=tier))
    assert model.thinking == [tier]


def test_deep_ack_is_streamed_before_the_model_is_called(tmp_path):
    model = FakeModel(reply="Carry more speed.")
    stream = Coach(model).ask_agentic_stream(_ctx(tmp_path), "how was that race?", car="C",
                                             thinking="deep", intent="race_review")
    first = next(stream)
    assert first == {"type": "ack",
                     "text": "Give me a moment — going through the race lap by lap."}
    assert model.thinking == []                  # the model was not called yet
    rest = list(stream)
    assert model.thinking == ["deep"]
    # the ack is not answer text: the answer is the model's alone
    assert "".join(e["text"] for e in rest if e["type"] == "text") == "Carry more speed."


def test_no_ack_below_deep(tmp_path):
    events = list(Coach(FakeModel()).ask_agentic_stream(_ctx(tmp_path), "q?", car="C",
                                                        thinking="standard"))
    assert not any(e["type"] == "ack" for e in events)


def test_thinking_event_is_no_paragraph_break():
    # #133 inserts a break after a TOOL round; a thinking start is not one.
    events = list(Coach._separated(iter([{"type": "text", "text": "First."},
                                         {"type": "thinking"},
                                         {"type": "text", "text": "Then."}])))
    assert "".join(e["text"] for e in events if e["type"] == "text") == "First.Then."


# --- the Anthropic mapping (SDK mocked; no network) ---------------------------------

class _Client:
    """messages.create double: a tool_use turn, then a final text turn."""
    def __init__(self):
        self.messages = self
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            return NS(content=[NS(type="tool_use", id="t1", name="get_lap", input={})],
                      stop_reason="tool_use")
        return NS(content=[NS(type="text", text="ok")], stop_reason="end_turn")


def _claude(model="claude-sonnet-5"):
    m = ClaudeModel.__new__(ClaudeModel)      # no real anthropic client
    m.model, m.max_tokens, m.client = model, 4096, _Client()
    return m


def _kw(m, tier, force=False):
    list(m.run_stream("sys", [{"role": "user", "content": "q"}], [{"name": "get_lap"}],
                      lambda n, i: "r", force_tool=force, thinking=tier))
    return m.client.calls


ADAPTIVE = {"type": "adaptive", "display": "summarized"}


def test_claude_tier_params():
    for call in _kw(_claude(), "quick"):
        assert call["thinking"] == {"type": "disabled"}
        assert "output_config" not in call and call["max_tokens"] == 4096
    for call in _kw(_claude(), "standard"):
        assert call["thinking"] == ADAPTIVE
        assert "output_config" not in call and call["max_tokens"] == 4096
    for call in _kw(_claude(), "deep"):
        assert call["thinking"] == ADAPTIVE
        assert call["output_config"] == {"effort": "max"} and call["max_tokens"] == 8192


def test_claude_forced_round_runs_without_thinking_or_effort_at_any_tier():
    for tier in ("standard", "deep"):
        first, second = _kw(_claude(), tier, force=True)
        assert first["tool_choice"] == {"type": "any"}
        assert first["thinking"] == {"type": "disabled"} and "output_config" not in first
        assert "tool_choice" not in second and second["thinking"] == ADAPTIVE


def test_claude_quick_on_a_family_that_rejects_disabled_thinking():
    for call in _kw(_claude("claude-opus-5-5"), "quick"):
        assert call["thinking"] == ADAPTIVE and call["output_config"] == {"effort": "low"}


def test_claude_haiku_runs_every_tier_plain():
    for call in _kw(_claude("claude-haiku-4-5-20251001"), "deep"):
        assert call["thinking"] == {"type": "disabled"} and "output_config" not in call


def test_claude_thinking_is_logged_never_yielded_and_billed_in_usage(caplog):
    final = NS(content=[NS(type="thinking", thinking="The fronts are hot, so..."),
                        NS(type="text", text="Drop a click of front wing.")],
               stop_reason="end_turn",
               usage=NS(input_tokens=100, output_tokens=900, cache_read_input_tokens=0,
                        cache_creation_input_tokens=0))

    class CM:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def __iter__(self):
            yield NS(type="content_block_start", content_block=NS(type="thinking"))
            yield NS(type="thinking", thinking="The fronts are hot, so...")
            yield NS(type="content_block_start", content_block=NS(type="text"))
            yield NS(type="text", text="Drop a click of front wing.")
        def get_final_message(self): return final

    m = _claude()
    m.client.stream = lambda **kw: CM()
    with caplog.at_level(logging.INFO, logger="simcoach.model.claude"):
        events = list(m.run_stream("sys", [{"role": "user", "content": "q"}], [],
                                   lambda n, i: "r", thinking="deep"))
    assert events == [{"type": "thinking"},
                      {"type": "text", "text": "Drop a click of front wing."},
                      {"type": "usage", "model": "claude-sonnet-5",
                       "usage": {"input": 100, "output": 900, "cache_read": 0,
                                 "cache_write": 0}}]
    assert any("The fronts are hot" in r.getMessage() for r in caplog.records)


class _ClassifyClient:
    def __init__(self, reply=None, exc=None):
        self.messages = self
        self.reply, self.exc, self.options, self.calls = reply, exc, None, []

    def with_options(self, **kw):
        self.options = kw
        return self

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return NS(content=[NS(type="text", text=self.reply)],
                  usage=NS(input_tokens=50, output_tokens=12, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0))


def test_claude_classify_parses_json_and_bills():
    m = _claude("claude-haiku-4-5-20251001")
    m.client = _ClassifyClient('```json\n{"tier": "deep", "intent": "setup"}\n```')
    obj, usage = m.classify("q", {"type": "object"})
    assert obj == {"tier": "deep", "intent": "setup"}
    assert usage == TokenUsage(input=50, output=12)
    assert m.client.options == {"timeout": 0.8, "max_retries": 0}
    assert "thinking" not in m.client.calls[0]


def test_claude_classify_failures_are_none():
    m = _claude()
    m.client = _ClassifyClient("not json at all")
    assert m.classify("q", {})[0] is None
    m.client = _ClassifyClient(exc=TimeoutError("slow"))
    assert m.classify("q", {}) == (None, TokenUsage())


# --- settings + the usage meter over the websocket ---------------------------------

def _app(tmp_path, monkeypatch, classifier=None):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    model = FakeModel(reply="Brake later.")
    coach = Coach(model, classifier_for=(lambda name: classifier) if classifier else None)
    return TestClient(create_app(store, coach), base_url="http://127.0.0.1"), store, model


def test_coach_thinking_settings_round_trip(tmp_path, monkeypatch, mem_keyring):
    c, _, _ = _app(tmp_path, monkeypatch)
    assert c.get("/settings").json()["coach"] == {"thinking": "auto",
                                                 "classifier_model": "cheapest"}
    c.put("/settings", json={"coach": {"thinking": "off", "classifier_model": "none",
                                       "api_key": "sk-nope"}})
    assert c.get("/settings").json()["coach"] == {"thinking": "off",
                                                 "classifier_model": "none"}


def _ask(client, question):
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": question})
        frames = []
        while frames[-1:] == [] or frames[-1]["type"] not in ("done", "error"):
            frames.append(ws.receive_json())
    return frames


def test_ws_turn_bills_the_classifier_and_threads_the_tier(tmp_path, monkeypatch,
                                                           mem_keyring):
    clf = FakeModel(classify_result={"tier": "deep", "intent": "setup"})
    c, store, model = _app(tmp_path, monkeypatch, classifier=clf)
    frames = _ask(c, "why does the rear step out on exit?")
    assert model.thinking == ["deep"]
    assert frames[0] == {"type": "ack", "text": "Give me a moment — thinking through the setup."}
    # the answer (and so the stored row) is the model's alone
    assert frames[-1]["answer"] == "Brake later."
    coach_usage = model._usage(model.last_messages)
    classifier_usage = TokenUsage(input=len(clf.classified[0]) // 4, output=20)
    assert frames[-1]["usage"]["total"] == (coach_usage + classifier_usage).total
    # the router's one line of context rides in the user turn, the stored question doesn't
    assert "question type: setup" in model.last_messages[-1]["content"]
    assert store.chat_messages(frames[-1]["chat_id"])[0]["question"] == \
        "why does the rear step out on exit?"


def test_ws_without_a_classifier_uses_the_rules(tmp_path, monkeypatch, mem_keyring):
    c, _, model = _app(tmp_path, monkeypatch)
    _ask(c, "what's my fuel?")
    _ask(c, "analyze my braking in detail")
    assert model.thinking == ["standard", "deep"]


@pytest.mark.parametrize("state, speed, tier", [
    ("track", 140.0, "quick"),       # on track AND moving: the gate, no classifier
    ("track", 0.0, "deep"),          # on track, stationary: the classifier decides
    ("garage", 0.0, "deep"),
])
def test_ws_moving_gate_reads_live_speed(tmp_path, monkeypatch, mem_keyring, state, speed,
                                         tier):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_session_track_car", lambda: ("T", "C"))
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": "T", "car": "C", "state": state, "speed": speed})
    clf = FakeModel(classify_result={"tier": "deep"})
    c, _, model = _app(tmp_path, monkeypatch, classifier=clf)
    _ask(c, "what's my fuel?")
    assert model.thinking == [tier]
    assert (clf.classified == []) == (tier == "quick")

