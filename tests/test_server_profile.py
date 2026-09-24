import dataclasses
import json

from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.lmu.results import ResultSession
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


def _distill_prompt(c, store, session_id="sess-1"):
    """The text that actually reached the model for a distill of `session_id`."""
    r = c.post("/profile/distill", json={"session_id": session_id})
    assert r.status_code == 200, r.text
    fake = c.app.state.coach.model
    return fake.last_system + "\n" + "\n".join(str(m) for m in fake.last_messages)


def test_distill_never_calls_a_captured_lap_a_starting_position(tmp_path, monkeypatch,
                                                                 mem_keyring, synthetic_lap):
    # #67, second surface. get_race_result was corrected to say "P{n} on the first
    # captured lap", but the profile digest kept its own copy -- "Race arc: started
    # P{n} ... finished P{n}" -- off an UNSCOPED arc, so it stated as fact the exact
    # thing the corrected path refuses to. Two copies meant fixing one left the other
    # lying. Both now route through coach_tools.race_result_text.
    #
    # The fixture makes the two failure modes distinguishable: the qualifying lap is
    # P2 and earlier, the race laps are P9 then P14. An unscoped arc reads start=P2
    # (wrong phase); a race-scoped arc reads P9 -- which is still NOT the grid slot,
    # because no result row exists here to supply one.
    c, store = _client(tmp_path, monkeypatch, mem_keyring, model_reply=json.dumps(
        {"memories": [], "summary": ""}))
    store.save(dataclasses.replace(synthetic_lap, session_id="sess-1", phase="qualifying",
                                    timestamp=1.0, position=2))
    store.save(dataclasses.replace(synthetic_lap, session_id="sess-1", phase="race",
                                    timestamp=2.0, position=9))
    store.save(dataclasses.replace(synthetic_lap, session_id="sess-1", phase="race",
                                    timestamp=3.0, position=14))

    prompt = _distill_prompt(c, store)
    assert "started P" not in prompt, "the digest asserted a starting position it cannot know"
    assert "P2" not in prompt, "the arc leaked the qualifying phase into the race arc"
    assert "P9 overall on the first captured lap" in prompt
    assert "is NOT the starting position" in prompt


def test_distill_reports_the_real_grid_slot_when_the_result_log_has_one(tmp_path, monkeypatch,
                                                                        mem_keyring,
                                                                        synthetic_lap):
    # The other side of the same coin: when the result log DOES carry a grid slot, the
    # digest must report it -- suppressing it would be the opposite bug. Without this,
    # a fix that simply deletes the word "started" everywhere would pass the test above.
    c, store = _client(tmp_path, monkeypatch, mem_keyring, model_reply=json.dumps(
        {"memories": [], "summary": ""}))
    store.save(dataclasses.replace(synthetic_lap, session_id="sess-1", phase="race",
                                    timestamp=2.0, position=14))
    store.save_result_session(ResultSession(
        sim="lmu", track=synthetic_lap.track, car=synthetic_lap.car, car_class="GT3",
        date_ts=1.0, session_type="race", position=18, class_position=18, best_lap=60.0,
        finish_status="Finished Normally", grid_position=9, class_grid_position=9,
        online=False, laps=[], incidents=[], penalties=[]), session_id="sess-1")

    prompt = _distill_prompt(c, store)
    assert "Started P9 overall on the grid" in prompt
    assert "Finished P18 overall" in prompt
    assert "is NOT the starting position" not in prompt


def test_distill_retracts_a_fabricated_grip_claim_in_the_chat_excerpt(tmp_path, monkeypatch,
                                                                       mem_keyring,
                                                                       synthetic_lap):
    # #72's other read path. The digest quotes the last 6 chat turns at 200 chars each,
    # so a poisoned answer reaches the model here too. The retraction is applied to the
    # SLICE, not the answer -- it appends at the end, so scrubbing first then truncating
    # would drop the correction and keep the claim.
    c, store = _client(tmp_path, monkeypatch, mem_keyring, model_reply=json.dumps(
        {"memories": [], "summary": ""}))
    _seed_session(store, synthetic_lap, session_id="sess-1")
    store.save_chat("c1", "sess-1", "trk", "car", None, "why did I lock up?",
                    "With min grip reading zero you're asking for more than the rear can give.",
                    None)

    prompt = _distill_prompt(c, store)
    # A phrase only the stored answer has: "min grip" is also in the retraction's text.
    assert "asking for more than the rear" in prompt, \
        "the excerpt should still be quoted, not dropped"
    assert "fabricated" in prompt and "Disregard it" in prompt


def test_distill_retracts_a_pre_fix_race_start_but_not_a_post_fix_one(tmp_path, monkeypatch,
                                                                       mem_keyring,
                                                                       synthetic_lap):
    # #67 residue on the digest's read path; see the ws test of the same name. The
    # post-fix answer matches the same pattern, so only the cutoff spares it.
    c, store = _client(tmp_path, monkeypatch, mem_keyring, model_reply=json.dumps(
        {"memories": [], "summary": ""}))
    _seed_session(store, synthetic_lap, session_id="sess-1")
    before = ("That one's tough to sugarcoat -- you started **P18** and crossed the line "
              "**P18**. No incidents or penalties logged.")
    after = "You started P9 on the grid and finished P18, with 15 incidents logged."
    store.save_chat("c1", "sess-1", "trk", "car", None, "how did it go?", before, None,
                    ts=1789018942.56)
    store.save_chat("c1", "sess-1", "trk", "car", None, "and now?", after, None,
                    ts=1789057327.0)

    prompt = _distill_prompt(c, store)
    assert "tough to sugarcoat" in prompt, "the excerpt should still be quoted, not dropped"
    assert prompt.count("never the grid slot") == 1, \
        "exactly the pre-fix answer carries the retraction, the post-fix one does not"
    assert "only if the track it names is trk" in prompt, \
        "the digest's retraction must name the stored answer's own track"
    # The chat excerpt closes the digest, so a retraction appended to the post-fix
    # answer would be the digest's tail.
    digest = c.app.state.coach.model.last_messages[0]["content"]
    assert digest.endswith(f"Coach answered: {after}")


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
