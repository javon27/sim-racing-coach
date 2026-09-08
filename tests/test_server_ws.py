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
                            tool_script=[("get_corner", {"corner": "1"})]),
                  "simcoach/knowledge")
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
    app = create_app(store, Coach(factory.LazyModel(), "simcoach/knowledge"))
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
    app = create_app(store, Coach(factory.LazyModel(), "simcoach/knowledge"))
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
    coach2 = Coach(FakeModel(reply="x"), "simcoach/knowledge")
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
    app = create_app(store, Coach(model, "simcoach/knowledge"))
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
    coach = Coach(_DeltaModel(), "simcoach/knowledge")
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
    coach = Coach(FakeModel(reply="x"), "simcoach/knowledge")
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
    coach = Coach(fake, "simcoach/knowledge")
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


def test_ws_coach_no_history_for_new_chat(tmp_path):
    # A brand-new chat (no chat_id) threads no history -> just the question.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel(reply="Answer.")
    coach = Coach(fake, "simcoach/knowledge")
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
    coach = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
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
    coach = Coach(fake, "simcoach/knowledge")
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
