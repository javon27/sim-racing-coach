"""#183: per-turn latency + routing on the coach_chat row, and the classifier's 1.5 s
budget. Fake models only -- no network."""
import inspect
import sqlite3
import time

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.base import Model
from simcoach.model.claude import ClaudeModel
from simcoach.model.fake import FAKE_MODEL_ID, FakeModel
from simcoach.server.app import create_app
from simcoach.store import SCHEMA_VERSION, LapStore
from simcoach.thinking import CLASSIFY_TIMEOUT_S, route

_USAGE = {"type": "usage", "usage": {"input": 10, "output": 5, "cache_read": 0,
                                     "cache_write": 0}, "model": FAKE_MODEL_ID}


class _TwoRounds(FakeModel):
    """Round 1 calls a tool; round 2 answers in two sentences, 0.2 s apart."""

    def run_stream(self, system, messages, tools, dispatch, force_tool=False,
                   thinking="standard"):
        self.thinking.append(thinking)
        yield {"type": "tool", "name": "list_recent_laps", "input": {}}
        dispatch("list_recent_laps", {})
        yield _USAGE
        time.sleep(0.15)
        yield {"type": "text", "text": "Brake later. "}
        time.sleep(0.2)
        yield {"type": "text", "text": "Carry more speed."}
        yield _USAGE


def _app(tmp_path, monkeypatch, model, classifier=None):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(model, classifier_for=(lambda name: classifier) if classifier else None)
    return TestClient(create_app(store, coach), base_url="http://127.0.0.1"), store


def _ask(client, question, **extra):
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": question, **extra})
        frames = [ws.receive_json()]
        while frames[-1]["type"] not in ("done", "error"):
            frames.append(ws.receive_json())
    return frames[-1]


def test_turn_latency_is_stored_and_returned(tmp_path, monkeypatch, mem_keyring):
    c, store = _app(tmp_path, monkeypatch, _TwoRounds())
    done = _ask(c, "where am I losing time?")
    lat = done["latency"]
    assert lat["tier"] == "standard" and lat["tier_via"] == "rules"
    assert lat["classify_ms"] is None                      # no classifier configured
    assert (lat["rounds"], lat["tools"]) == (2, 1)
    assert 150 <= lat["first_text_ms"] < lat["total_ms"]
    assert lat["total_ms"] - lat["first_text_ms"] >= 200   # the second sentence's wait
    assert store.chat_messages(done["chat_id"])[0]["latency"] == lat


def test_a_repeat_turn_is_marked_and_makes_no_model_round(tmp_path, monkeypatch,
                                                          mem_keyring):
    c, _ = _app(tmp_path, monkeypatch, FakeModel(reply="Brake later."))
    chat = _ask(c, "where am I losing time?")["chat_id"]
    lat = _ask(c, "say again", chat_id=chat)["latency"]
    assert (lat["tier"], lat["tier_via"], lat["rounds"], lat["tools"]) == \
        (None, "repeat", 0, 0)
    assert lat["first_text_ms"] is not None


def test_a_router_failure_is_recorded(tmp_path, monkeypatch, mem_keyring):
    from simcoach.server import coach_ws

    def boom(*a, **kw):
        raise RuntimeError("router bug")
    monkeypatch.setattr(coach_ws, "route", boom)
    c, _ = _app(tmp_path, monkeypatch, FakeModel(reply="Brake later."))
    lat = _ask(c, "where am I losing time?")["latency"]
    assert (lat["tier"], lat["tier_via"], lat["rounds"]) == ("standard", "router error", 1)


def test_v26_db_gains_coach_chat_latency(tmp_path):
    db = str(tmp_path / "c.sqlite")
    s = LapStore(db, str(tmp_path / "data"))
    s.save_chat("chat-1", "sid", "T", "C", None, "q", "a")
    s.conn.close()
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE coach_chat DROP COLUMN latency")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    conn.close()
    store = LapStore(db, str(tmp_path / "data"))
    assert "latency" in [r[1] for r in store.conn.execute("PRAGMA table_info(coach_chat)")]
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store.chat_messages("chat-1")[0]["latency"] is None     # old rows stay NULL
    store.save_chat("chat-1", "sid", "T", "C", None, "q2", "a2", latency={"rounds": 3})
    assert store.chat_messages("chat-1")[1]["latency"] == {"rounds": 3}


# --- how the tier was chosen: every path ------------------------------------------

class _Boom:
    def classify(self, prompt, schema, *, timeout):
        raise RuntimeError("no key")


@pytest.mark.parametrize("kw, clf, via, asked", [
    ({"moving": True}, FakeModel(classify_result={"tier": "deep"}), "gate", False),
    ({}, FakeModel(classify_result={"tier": "deep"}), "classifier", True),
    ({}, None, "rules", False),                 # coach.classifier_model "none"
    ({}, FakeModel(classify_result={"tier": "deep"}, classify_delay=2.0),
     "rules: classifier timeout", True),
    ({}, FakeModel(classify_result=None), "rules: classifier failed", True),
    ({}, FakeModel(classify_result={"tier": "banana"}), "rules: bad classifier output", True),
    ({}, _Boom(), "rules: classifier error", True),
])
def test_route_reports_how_the_tier_was_chosen(kw, clf, via, asked):
    meta = route("what's my fuel?", classifier=lambda: clf, **kw)[4]
    assert meta["via"] == via
    assert (meta["classify_ms"] is not None) == asked


def test_route_without_a_classifier_factory_is_rules():
    assert route("what's my fuel?")[4] == {"via": "rules", "classify_ms": None}


# --- #183: a voice question with an unknown speed never waits on the classifier -----

@pytest.mark.parametrize("voice, status, gated", [
    ("conversation", None, True),                               # no status read
    ("ptt", {"state": "track", "speed": None}, True),           # no telemetry row
    ("conversation", {"state": "pits", "speed": None}, True),
    ("ptt", {"state": "garage", "speed": None}, False),         # in the garage: ask
    (None, None, False),                                        # typed: unchanged
    (None, {"state": "track", "speed": None}, False),
])
def test_ws_voice_with_unknown_speed_is_gated(tmp_path, monkeypatch, mem_keyring,
                                              voice, status, gated):
    from simcoach.lmu import smm
    monkeypatch.setattr(smm, "read_session_track_car", lambda: ("T", "C"))
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: status and {"track": "T", "car": "C", **status})
    clf = FakeModel(classify_result={"tier": "standard"})
    c, _ = _app(tmp_path, monkeypatch, FakeModel(reply="Brake later."), classifier=clf)
    lat = _ask(c, "what's my fuel?", **({"voice": voice} if voice else {}))["latency"]
    if gated:
        assert (lat["tier"], lat["tier_via"], clf.classified) == ("quick", "gate", [])
    else:
        assert lat["tier_via"] == "classifier" and len(clf.classified) == 1


# --- #183 part 3: the classifier's budget ------------------------------------------

def test_a_rig_speed_classifier_lands_within_the_budget():
    """The effect, measured with a fake: a 1.0 s classifier round trip (the rig's is
    ~0.85 s; the old 0.8 s budget timed out 5 of 5) now decides the tier."""
    slow = FakeModel(classify_result={"tier": "deep", "intent": "setup"}, classify_delay=1.0)
    tier, _, _, _, meta = route("what's my fuel?", classifier=lambda: slow)
    assert (tier, meta["via"]) == ("deep", "classifier")
    assert CLASSIFY_TIMEOUT_S * 1000 > meta["classify_ms"] >= 1000
    assert slow.classify_timeouts == [CLASSIFY_TIMEOUT_S]


@pytest.mark.parametrize("voice", [None, "ptt", "conversation"])
def test_ws_every_consulted_question_gets_the_same_budget(tmp_path, monkeypatch,
                                                          mem_keyring, voice):
    clf = FakeModel(classify_result={"tier": "standard"})
    c, _ = _app(tmp_path, monkeypatch, FakeModel(reply="Brake later."), classifier=clf)
    lat = _ask(c, "what's my fuel?", **({"voice": voice} if voice else {}))["latency"]
    assert clf.classify_timeouts == [CLASSIFY_TIMEOUT_S] == [1.5]
    assert lat["tier_via"] == "classifier" and lat["classify_ms"] is not None


def test_claude_classify_sends_the_budget_to_the_wire():
    """Injected transport, no network: the router's budget reaches the HTTP request."""
    seen = []

    def handler(req):
        seen.append(req.extensions["timeout"]["read"])
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": '{"tier": "deep"}'}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 50, "output_tokens": 12}})
    m = ClaudeModel(model="claude-haiku-4-5", api_key="sk-test")
    m.client = anthropic.Anthropic(api_key="sk-test", http_client=httpx2.Client(
        transport=httpx2.MockTransport(handler)))
    assert m.classify("q", {}, timeout=CLASSIFY_TIMEOUT_S)[0] == {"tier": "deep"}
    assert m.classify("q", {}, timeout=0.3)[0] == {"tier": "deep"}
    assert seen == [CLASSIFY_TIMEOUT_S, 0.3]       # whatever the caller passes
    with pytest.raises(TypeError):                 # no default to fall back on
        m.classify("q", {})


@pytest.mark.parametrize("cls", [Model, ClaudeModel, FakeModel])
def test_classify_timeout_is_keyword_only_with_no_default(cls):
    p = inspect.signature(cls.classify).parameters["timeout"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
