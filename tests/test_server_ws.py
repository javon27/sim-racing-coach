import importlib.util

from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


def test_ws_protocol_library_installed():
    # Starlette's TestClient has its own in-process WS impl, so every test above
    # passes even when uvicorn has NO WebSocket library -- in which case the real
    # sidecar answers /coach with "404 Unsupported upgrade request" and the UI
    # shows "lost connection". Guard the runtime dependency explicitly.
    assert (importlib.util.find_spec("websockets")
            or importlib.util.find_spec("wsproto")), \
        "uvicorn needs 'websockets' or 'wsproto' to serve WS /coach"


def _client(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(reply="Brake later into Turn 1.",
                            tool_script=[("get_corner", {"corner": "1"})]))
    app = create_app(store, coach)
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_ws_coach_streams_events_and_persists(tmp_path):
    client, store = _client(tmp_path)
    # starlette's TestClient.websocket_connect hardcodes Host: testserver
    # regardless of base_url (unlike its HTTP methods) — override explicitly
    # so this "good" request clears the Host guard.
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "where do I lose time?", "session_id": "s1"})
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in ("done", "error"):
                break
    types = [e["type"] for e in events]
    assert "tool" in types and "text" in types and types[-1] == "done"
    chat_id = events[-1]["chat_id"]
    msgs = store.chat_messages(chat_id)
    assert msgs and msgs[0]["question"] == "where do I lose time?"
    assert msgs[0]["answer"] == "Brake later into Turn 1."


def test_ws_coach_rejects_bad_host(tmp_path):
    client, _ = _client(tmp_path)
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/coach", headers={"host": "evil.com"}) as ws:
            ws.receive_json()


def test_ws_coach_rejects_hostile_origin_good_host(tmp_path):
    # The real cross-site-WS attack: valid Host (TestClient sends 127.0.0.1) but a
    # hostile browser Origin. Must be rejected before accept.
    client, _ = _client(tmp_path)
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/coach", headers={"host": "127.0.0.1",
                                                          "origin": "http://evil.com"}) as ws:
            ws.receive_json()


def test_ws_coach_rejects_another_localhost_port(tmp_path):
    # #60 on the WS path (its own _bad_origin call site, not covered by the HTTP
    # middleware): a page on any other localhost port is exactly the cross-site-WS
    # attacker this guard exists for, and the hostname-only compare let it in.
    # RED before the fix -- the connect used to be accepted.
    # The send_text is what keeps this test HONEST: pre-fix the socket is accepted,
    # so a bare receive_json() would block forever on a handler waiting for input
    # (a hang, not a failure). Sending first guarantees a reply on the broken path,
    # so the missing raise fails fast instead of wedging the suite.
    client, _ = _client(tmp_path)
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/coach", headers={"host": "127.0.0.1",
                               "origin": "http://localhost:3000"}) as ws:
        ws.send_text("not json at all")
        ws.receive_json()


def test_ws_coach_malformed_message_errors_not_crash(tmp_path):
    client, _ = _client(tmp_path)
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_text("not json at all")
        ev = ws.receive_json()
    assert ev["type"] == "error"


def test_ws_coach_through_lazymodel(tmp_path, monkeypatch):
    # Covers the production delegation path (Coach(LazyModel(...))), which the
    # FakeModel tests bypass — this is where the missing run_stream bit.
    from simcoach.coach import Coach
    from simcoach.model import factory
    from simcoach.model.fake import FakeModel
    from simcoach.store import LapStore
    monkeypatch.setattr(factory, "make_model",
                        lambda provider=None, model=None: FakeModel(reply="ok"))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(factory.LazyModel()))
    c = TestClient(app, base_url="http://127.0.0.1")
    with c.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "hi"})
        types = []
        while True:
            ev = ws.receive_json();  types.append(ev["type"])
            if ev["type"] in ("done", "error"): break
    assert types[-1] == "done" and "text" in types


def test_ws_coach_error_no_key_is_curated(tmp_path, monkeypatch):
    from simcoach.model import factory
    from simcoach.model.factory import CoachUserError

    # No API key -> CoachUserError -> the friendly message reaches the client verbatim.
    monkeypatch.setattr(factory, "make_model",
                        lambda provider=None, model=None: (_ for _ in ()).throw(
                            CoachUserError("No Anthropic API key set — add one in Settings.")))
    store = LapStore(str(tmp_path / "c1.sqlite"), str(tmp_path / "data1"))
    app = create_app(store, Coach(factory.LazyModel()))
    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "hi"})
        events = []
        while True:
            ev = ws.receive_json();  events.append(ev)
            if ev["type"] in ("done", "error"): break
    assert events[-1]["type"] == "error"
    assert "API key" in events[-1]["message"]

    # A generic internal error must NOT leak its text to the client.
    coach2 = Coach(FakeModel(reply="x"))
    monkeypatch.setattr(coach2, "ask_agentic_stream",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret internal detail")))
    store2 = LapStore(str(tmp_path / "c2.sqlite"), str(tmp_path / "data2"))
    app2 = create_app(store2, coach2)
    client2 = TestClient(app2, base_url="http://127.0.0.1")
    with client2.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "hi"})
        events2 = []
        while True:
            ev = ws.receive_json();  events2.append(ev)
            if ev["type"] in ("done", "error"): break
    assert events2[-1] == {"type": "error", "message": "coach error"}


def test_ws_coach_injects_concise_driver_profile(tmp_path, monkeypatch):
    # Static prefs (settings "driver" block) + the active driver_summary must reach
    # the coach's system prompt; a full memory (driver_learnings row) must NOT.
    from simcoach import config as config_mod

    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    cfg = config_mod.load_config()
    cfg["driver"]["braking_style"] = "trail-braker"
    config_mod.save_config(cfg)

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    store.set_driver_summary("Works on late apexes.", "active")
    store.add_learning("Locks the rears at Turn 3.", status="active", source="manual")

    model = FakeModel(reply="ok")
    app = create_app(store, Coach(model))
    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "hi"})
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"): break

    assert "trail-braker" in model.last_system
    assert "Works on late apexes" in model.last_system
    assert "Locks the rears at Turn 3" not in model.last_system


class _DeltaModel:
    """Fake model that yields the answer as multiple text deltas, like the real
    ClaudeModel.run_stream token-streaming — regression test for the worker's
    last-wins accumulation bug (see test_ws_coach_persists_full_streamed_answer)."""

    def complete(self, system, messages):
        return "Brake later into 1."

    def run(self, system, messages, tools, dispatch):
        return "Brake later into 1."

    def run_stream(self, system, messages, tools, dispatch):
        for t in ["Brake ", "later ", "into 1."]:
            yield {"type": "text", "text": t}


def test_ws_coach_persists_full_streamed_answer(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(_DeltaModel())
    app = create_app(store, coach)
    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "where do I lose time?", "session_id": "s1"})
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in ("done", "error"):
                break
    text_events = [e for e in events if e["type"] == "text"]
    assert len(text_events) == 3
    chat_id = events[-1]["chat_id"]
    msgs = store.chat_messages(chat_id)
    assert msgs[0]["answer"] == "Brake later into 1."


def test_ws_coach_allows_tauri_origin(tmp_path):
    client, _ = _client(tmp_path)   # reuse the module's FakeModel-backed helper
    with client.websocket_connect(
            "/coach", headers={"host": "127.0.0.1", "origin": "http://tauri.localhost"}) as ws:
        ws.send_json({"question": "hi"})
        seen = []
        while True:
            ev = ws.receive_json(); seen.append(ev["type"])
            if ev["type"] in ("done", "error"): break
    assert seen[-1] == "done"


def test_ws_coach_error_without_text_persists_nothing(tmp_path, monkeypatch):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(reply="x"))
    monkeypatch.setattr(coach, "ask_agentic_stream",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    app = create_app(store, coach)
    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "hi"})
        events = []
        while True:
            ev = ws.receive_json();  events.append(ev)
            if ev["type"] in ("done", "error"): break
    assert events[-1]["type"] == "error"
    assert "chat_id" not in events[-1]
    assert store.list_chats() == []


def test_ws_coach_threads_prior_chat_history(tmp_path):
    # Regression: the WS worker must pass the chat's prior turns as history so the
    # coach continues the conversation instead of treating each question standalone
    # ("lost thread mid conversation"). Verify via FakeModel.last_messages.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel(reply="Second answer.")
    coach = Coach(fake)
    app = create_app(store, coach)
    store.save_chat("c1", "s1", "trk", "car", None,
                    "first question", "first answer", None)

    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "second question", "chat_id": "c1"})
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"):
                break

    contents = [m["content"] for m in fake.last_messages]
    assert "first question" in contents and "first answer" in contents
    assert contents[-1] == "second question"
    assert contents.index("first question") < contents.index("second question")


def test_ws_coach_retracts_a_fabricated_grip_claim_in_resumed_history(tmp_path):
    # #72: four answers already in the driver's store assert "min grip reading 0.00"
    # as established fact. mGripFract is never populated by the game (flat 0.0 across
    # 173/173 captured laps, 5 cars), so the claim is false -- and resuming the chat
    # re-feeds it to the model as its OWN prior reasoning, which is harder to discount
    # than a raw number would be.
    #
    # Filtered on read, never on disk: the stored row and the UI keep what the coach
    # actually said. Only the model's copy carries the retraction.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel(reply="Second answer.")
    app = create_app(store, Coach(fake))
    poisoned = ("That's a lockup -- min grip reading 0.00 there confirms the tires "
                "weren't gripping at all through that phase.")
    store.save_chat("c1", "s1", "trk", "car", None, "why did I lock up?", poisoned, None)

    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "second question", "chat_id": "c1"})
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"):
                break

    sent = [m["content"] for m in fake.last_messages]
    carried = [m for m in sent if "min grip" in m]
    assert carried, "the prior answer should still be threaded, not dropped"
    assert all("fabricated" in m and "Disregard it" in m for m in carried), \
        f"the grip claim reached the model unretracted: {carried}"
    # ...and the stored record is untouched: rewriting the driver's own conversation
    # history would be a worse wrong than leaving a bad number in it.
    assert store.chat_messages("c1")[0]["answer"] == poisoned


def test_ws_coach_leaves_ordinary_grip_prose_alone(tmp_path):
    # GUARD against over-matching. The authored driving prose in simcoach/knowledge/*.md
    # and simcoach/cars/*.json is full of legitimate uses of "grip", and it reaches the
    # model through other tools; a blanket filter on the word would gut it. Only the
    # specific fabricated metric is matched.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel(reply="Second answer.")
    app = create_app(store, Coach(fake))
    legit = "You ran out of front grip on entry; the mechanical grip balance is rearward."
    store.save_chat("c1", "s1", "trk", "car", None, "why understeer?", legit, None)

    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "second question", "chat_id": "c1"})
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"):
                break

    assert legit in [m["content"] for m in fake.last_messages]


def test_ws_coach_no_history_for_new_chat(tmp_path):
    # A brand-new chat (no chat_id) threads no history -> just the question.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel(reply="Answer.")
    coach = Coach(fake)
    app = create_app(store, coach)

    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "only question"})
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"):
                break

    assert [m["content"] for m in fake.last_messages] == ["only question"]


def test_ws_coach_auto_anchors_to_live_session(tmp_path, synthetic_lap):
    # With no session_id/lap_id in the request, the coach must anchor to the LIVE
    # session (app.state.latest) so it knows the current laps without the driver
    # selecting one. Verify via the persisted chat's session stamp.
    from simcoach.model_types import LapRecord

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    rec = LapRecord(**{**synthetic_lap.__dict__, "session_id": "live-s"})
    lap_id = store.save(rec)
    coach = Coach(FakeModel(reply="ok"))
    app = create_app(store, coach)
    app.state.latest = {"record": rec, "lap_id": lap_id}

    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "how's it going"})  # no session_id / lap_id
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"):
                break

    msgs = store.chat_messages(ev["chat_id"])
    assert msgs[0]["session_id"] == "live-s"          # anchored to the live stint
    assert msgs[0]["lap_id"] is None                  # but NOT pinned to a lap — the
    # coach fetches laps on demand via its tools, so a turn isn't tied to one lap.


def test_ws_coach_respects_imperial_units(tmp_path, monkeypatch):
    # Regression: the coach's unit labels + tool data must follow the user's
    # config (imperial), not silently default to metric.
    from simcoach import config
    monkeypatch.setattr(config, "load_config",
                        lambda *a, **k: {**config.DEFAULTS, "units": "imperial"})
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel(reply="ok")
    coach = Coach(fake)
    app = create_app(store, coach)

    client = TestClient(app, base_url="http://127.0.0.1")
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json({"question": "how am I doing?"})
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"):
                break

    assert fake.last_system is not None
    assert "mph" in fake.last_system and "°F" in fake.last_system
    assert "km/h" not in fake.last_system


# --- token usage / cost meter (issue #39) ---------------------------------

def _ask(client, payload: dict) -> list[dict]:
    """Ask one question over /coach and collect every frame through the terminal."""
    with client.websocket_connect("/coach", headers={"host": "127.0.0.1"}) as ws:
        ws.send_json(payload)
        frames = []
        while True:
            ev = ws.receive_json()
            frames.append(ev)
            if ev["type"] in ("done", "error"):
                return frames


def test_coach_ws_done_carries_cumulative_usage_and_cost(tmp_path):
    client, _ = _client(tmp_path)
    frames = _ask(client, {"question": "where am I losing time?"})
    done = frames[-1]
    assert done["type"] == "done"
    assert done["usage"]["total"] > 0
    assert done["usage"]["messages"] == 1
    assert done["cost"] is not None and done["cost_complete"] is True
    assert done["model"] == "fake"
    # the per-turn breakdown stays server-side -- it's noise on the wire
    assert not any(f["type"] == "usage" for f in frames)


def test_coach_ws_second_question_usage_accumulates(tmp_path):
    client, _ = _client(tmp_path)
    first = _ask(client, {"question": "q1"})[-1]
    second = _ask(client, {"question": "q2", "chat_id": first["chat_id"]})[-1]
    assert second["usage"]["total"] > first["usage"]["total"]
    assert second["usage"]["messages"] == 2


def test_coach_ws_persists_usage_onto_the_chat_row(tmp_path):
    client, store = _client(tmp_path)
    done = _ask(client, {"question": "q"})[-1]
    assert store.chat_usage(done["chat_id"])["total"] == done["usage"]["total"]
    row = store.conn.execute(
        "SELECT model, input_tokens, output_tokens FROM coach_chat WHERE chat_id = ?",
        (done["chat_id"],)).fetchone()
    assert row[0] == "fake" and row[1] > 0 and row[2] > 0


def test_coach_ws_reports_the_model_from_the_usage_event_not_config(tmp_path, monkeypatch):
    """The id must come from the event the billed call emitted -- config may have
    changed since the sidecar resolved its model."""
    from simcoach import config
    monkeypatch.setattr(config, "load_config",
                        lambda *a, **k: {**config.DEFAULTS, "model": "opus"})
    client, _ = _client(tmp_path)
    done = _ask(client, {"question": "q"})[-1]
    assert done["model"] == "fake"            # what actually billed, not "opus"


def test_get_chat_usage_endpoint_seeds_the_meter(tmp_path):
    client, _ = _client(tmp_path)
    done = _ask(client, {"question": "q"})[-1]
    r = client.get(f"/chats/{done['chat_id']}/usage").json()
    assert r["usage"]["total"] == done["usage"]["total"]
    assert r["cost"] == done["cost"] and r["cost_complete"] is True


def test_get_chat_usage_is_zeroed_for_an_unknown_chat(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/chats/nope/usage").json()
    assert r["usage"]["total"] == 0 and r["usage"]["messages"] == 0
    assert r["cost"] == 0.0 and r["cost_complete"] is True


def test_get_chat_usage_flags_incomplete_for_an_all_legacy_chat(tmp_path):
    """Every pre-v12 row billed real money but stores NULL usage. A chat that is
    ALL such rows must never render as 'total=0, complete=True' -- that reads as
    a free chat when it plainly was not."""
    client, store = _client(tmp_path)
    cid = store.create_chat()
    for i in range(3):
        store.save_chat(cid, "s", "Monza", "GT3", None, f"q{i}", f"a{i}")
    r = client.get(f"/chats/{cid}/usage").json()
    assert r["usage"]["total"] == 0
    assert r["cost_complete"] is False


def test_get_chat_usage_flags_incomplete_for_a_mixed_legacy_and_new_chat(tmp_path):
    """The real-world case (chat-20260908T204046-6de917 in the live DB has 30
    legacy rows): 29 legacy rows + one new, priced turn must still show the total
    as incomplete, not '18.9k tokens * $0.045' with no caveat."""
    client, store = _client(tmp_path)
    cid = store.create_chat()
    for i in range(29):
        store.save_chat(cid, "s", "Monza", "GT3", None, f"q{i}", f"a{i}")
    store.save_chat(cid, "s", "Monza", "GT3", None, "q30", "a30",
                     usage={"input": 15000, "output": 3900}, model="claude-sonnet-5")
    r = client.get(f"/chats/{cid}/usage").json()
    assert r["usage"]["total"] == 18900
    assert r["usage"]["messages"] == 30
    assert r["cost_complete"] is False


def test_get_chat_usage_is_complete_for_an_all_new_chat(tmp_path):
    """Guard against overcorrecting: a chat with no legacy rows at all must still
    report complete=True."""
    client, store = _client(tmp_path)
    cid = store.create_chat()
    store.save_chat(cid, "s", "Monza", "GT3", None, "q", "a",
                     usage={"input": 100, "output": 20}, model="claude-sonnet-5")
    r = client.get(f"/chats/{cid}/usage").json()
    assert r["cost_complete"] is True


def test_coach_ws_usage_missing_degrades_the_meter_not_the_answer(tmp_path):
    """A model that emits no usage event (e.g. _DeltaModel) must still answer and
    persist; the meter just reads zero."""
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(_DeltaModel()))
    client = TestClient(app, base_url="http://127.0.0.1")
    done = _ask(client, {"question": "q"})[-1]
    assert done["type"] == "done"
    assert store.chat_messages(done["chat_id"])[0]["answer"] == "Brake later into 1."
    assert done["usage"]["total"] == 0


def test_coach_ws_long_chat_is_false_under_the_soft_limit(tmp_path):
    client, _ = _client(tmp_path)
    done = _ask(client, {"question": "q"})[-1]
    assert done["usage"]["total"] < 150_000      # the default threshold
    assert done["long_chat"] is False


def test_coach_ws_long_chat_fires_past_the_soft_limit(tmp_path, monkeypatch):
    """The core owns the threshold decision; the UI only renders the nudge."""
    from simcoach import config
    monkeypatch.setattr(config, "load_config",
                        lambda *a, **k: {**config.DEFAULTS, "chat_token_soft_limit": 1})
    client, _ = _client(tmp_path)
    done = _ask(client, {"question": "q"})[-1]
    assert done["usage"]["total"] >= 1
    assert done["long_chat"] is True


def test_coach_ws_long_chat_threshold_of_zero_disables_the_nudge(tmp_path, monkeypatch):
    from simcoach import config
    monkeypatch.setattr(config, "load_config",
                        lambda *a, **k: {**config.DEFAULTS, "chat_token_soft_limit": 0})
    client, _ = _client(tmp_path)
    assert _ask(client, {"question": "q"})[-1]["long_chat"] is False


def test_coach_ws_errored_turn_still_closes_clean(tmp_path, monkeypatch):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(reply="x"))
    monkeypatch.setattr(coach, "ask_agentic_stream",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    app = create_app(store, coach)
    client = TestClient(app, base_url="http://127.0.0.1")
    frames = _ask(client, {"question": "q"})
    assert frames[-1]["type"] == "error"
    assert "usage" not in frames[-1]          # nothing persisted, nothing to report
