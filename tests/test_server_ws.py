from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


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
    from simcoach.store import LapStore
    from simcoach.coach import Coach
    from simcoach.model import factory
    from simcoach.model.fake import FakeModel
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
