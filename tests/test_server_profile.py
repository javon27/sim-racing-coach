import dataclasses
import json

from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _client(tmp_path, monkeypatch, mem_keyring, model_reply="{}"):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(model_reply)))
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_get_profile_shape(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    body = c.get("/profile").json()
    assert body == {
        "prefs": {"braking_style": None, "balance_preference": None, "risk_tolerance": None},
        "summary": {"text": "", "status": "active"},
        "memories": {"active": [], "pending": []},
    }


def test_put_profile_saves_prefs(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    r = c.put("/profile", json={"prefs": {"braking_style": "late", "risk_tolerance": "aggressive"}})
    assert r.status_code == 200
    body = c.get("/profile").json()
    assert body["prefs"] == {"braking_style": "late", "balance_preference": None,
                              "risk_tolerance": "aggressive"}


def test_put_profile_summary_roundtrip(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    r = c.put("/profile/summary", json={"text": "Brakes late, trims oversteer.", "status": "active"})
    assert r.status_code == 200
    body = c.get("/profile").json()
    assert body["summary"] == {"text": "Brakes late, trims oversteer.", "status": "active"}


def test_manual_memory_add_and_approve(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    r = c.post("/profile/memories", json={"text": "Consistently early on the brake pedal at T1."})
    assert r.status_code == 200
    mid = r.json()["id"]
    active = c.get("/profile").json()["memories"]["active"]
    assert any(m["id"] == mid and m["text"].startswith("Consistently early") for m in active)


def test_put_memory_approves_pending_to_active(tmp_path, monkeypatch, mem_keyring):
    c, store = _client(tmp_path, monkeypatch, mem_keyring)
    mid = store.add_learning("Test pending memory", status="pending", source="distill")
    r = c.put(f"/profile/memories/{mid}", json={"status": "active"})
    assert r.status_code == 200
    body = c.get("/profile").json()["memories"]
    assert not any(m["id"] == mid for m in body["pending"])
    assert any(m["id"] == mid for m in body["active"])


def test_put_memory_edits_text(tmp_path, monkeypatch, mem_keyring):
    c, store = _client(tmp_path, monkeypatch, mem_keyring)
    mid = store.add_learning("Old text", status="active", source="manual")
    r = c.put(f"/profile/memories/{mid}", json={"text": "New text"})
    assert r.status_code == 200
    active = c.get("/profile").json()["memories"]["active"]
    assert any(m["id"] == mid and m["text"] == "New text" for m in active)


def _seed_session(store, synthetic_lap, session_id="sess-1", n=2):
    for i in range(n):
        lap = dataclasses.replace(synthetic_lap, session_id=session_id, phase="race",
                                   timestamp=float(i))
        store.save(lap)


def test_distill_writes_only_pending_and_returns_proposals(tmp_path, monkeypatch, mem_keyring,
                                                             synthetic_lap):
    reply = json.dumps({"memories": ["Brakes consistently early into slow corners."],
                         "summary": "Conservative braker, smooth on throttle."})
    c, store = _client(tmp_path, monkeypatch, mem_keyring, model_reply=reply)
    _seed_session(store, synthetic_lap, session_id="sess-1")
    # Pre-existing active summary must survive the distill untouched.
    store.set_driver_summary("Existing approved summary.", "active")

    r = c.post("/profile/distill", json={"session_id": "sess-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["memories"][0]["text"] == "Brakes consistently early into slow corners."
    assert body["memories"][0]["status"] == "pending"
    assert body["summary"] == "Conservative braker, smooth on throttle."

    # Only a pending row was created; nothing active was added or changed.
    pending = store.list_learnings(status="pending")
    active = store.list_learnings(status="active")
    assert len(pending) == 1
    assert active == []
    assert store.get_driver_summary() == {"text": "Existing approved summary.", "status": "active"}


def test_distill_no_such_session_404(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring, model_reply="{}")
    r = c.post("/profile/distill", json={"session_id": "does-not-exist"})
    assert r.status_code == 404


def test_distill_bad_model_json_502(tmp_path, monkeypatch, mem_keyring, synthetic_lap):
    c, store = _client(tmp_path, monkeypatch, mem_keyring, model_reply="not json")
    _seed_session(store, synthetic_lap, session_id="sess-1")
    r = c.post("/profile/distill", json={"session_id": "sess-1"})
    assert r.status_code == 502
    assert store.list_learnings(status="pending") == []
