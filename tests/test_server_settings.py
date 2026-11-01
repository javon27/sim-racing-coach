import logging

from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _client(tmp_path, monkeypatch, mem_keyring):  # mem_keyring: conftest fixture (Task 3)
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_get_settings_hides_key(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    body = c.get("/settings").json()
    assert body["api_key_set"] is False
    assert "key" not in body and "api_key" not in body
    assert body["units"] == "metric"


def test_put_settings_persists_and_rejects_secret(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    # include a bogus secret + echoed non-config keys; server must drop them all
    c.put("/settings", json={"units": "imperial", "api_key": "sk-should-not-persist",
                             "api_key_set": True, "data_dir": "/tmp"})
    body = c.get("/settings").json()
    assert body["units"] == "imperial"
    assert body["api_key_set"] is False  # the secret never reached storage
    # and it never landed in the JSON file
    from simcoach import paths
    assert "sk-should-not-persist" not in paths.config_path().read_text()


def test_put_show_cost_round_trips_through_the_whitelist(tmp_path, monkeypatch, mem_keyring):
    """The cost estimate is a Settings toggle; it rides the existing DEFAULTS
    whitelist with no special-casing in put_settings."""
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    assert c.get("/settings").json()["show_cost"] is True      # default on
    assert c.put("/settings", json={"show_cost": False}).json()["show_cost"] is False
    assert c.get("/settings").json()["show_cost"] is False
    assert c.put("/settings", json={"show_cost": True}).json()["show_cost"] is True


def test_put_voice_ptt_focus_gate_round_trips_through_the_settings_endpoint(tmp_path, monkeypatch, mem_keyring):
    """#112 escape hatch: same nested-voice-key round trip as ptt_key's neighbours."""
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    assert c.get("/settings").json()["voice"]["ptt_focus_gate"] is True       # default on
    body = c.put("/settings", json={"voice": {"ptt_focus_gate": False}}).json()
    assert body["voice"]["ptt_focus_gate"] is False
    assert c.get("/settings").json()["voice"]["ptt_focus_gate"] is False


def test_put_settings_rejects_a_nested_injected_key(tmp_path, monkeypatch, mem_keyring):
    # #10: the whitelist only checked TOP-level keys, so {"voice": {...}} passed
    # any nested keys and any nested values straight through save_config's
    # nested merge onto disk -- an injected voice.api_key never reaches the file.
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    c.put("/settings", json={"voice": {"enabled": True, "api_key": "sk-nested-leak"}})
    body = c.get("/settings").json()
    assert body["voice"]["enabled"] is True          # legit nested key still saved
    assert "api_key" not in body["voice"]
    from simcoach import paths
    assert "sk-nested-leak" not in paths.config_path().read_text()


def test_put_settings_413_on_oversize_body(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    r = c.put("/settings", json={"units": "x" * 70_000})
    assert r.status_code == 413


def test_delete_requires_confirmation(tmp_path, monkeypatch, mem_keyring, synthetic_lap):
    c, store = _client(tmp_path, monkeypatch, mem_keyring)
    store.save(synthetic_lap)
    r = c.request("DELETE", "/data")  # no confirm body
    assert r.status_code == 400
    assert store.list_lap_ids(synthetic_lap.track, synthetic_lap.car)  # untouched


def test_delete_data_clears_with_confirm(tmp_path, monkeypatch, mem_keyring, synthetic_lap):
    c, store = _client(tmp_path, monkeypatch, mem_keyring)
    store.save(synthetic_lap)
    r = c.request("DELETE", "/data", json={"confirm": True})
    assert r.json() == {"cleared": True}
    assert store.list_lap_ids(synthetic_lap.track, synthetic_lap.car) == []


def test_test_key_never_raises(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    # no key set -> make_model raises -> endpoint returns ok:false, not 500
    r = c.post("/settings/test-key", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    # #259: the plain message, not raw SDK text
    assert r.json()["error"] == "No Anthropic API key set — add one in Settings."


def test_test_key_hides_an_unexpected_error_text(tmp_path, monkeypatch, mem_keyring, caplog):
    import simcoach.server.settings_routes as sr

    def boom(**kw):
        raise RuntimeError("Error code: 400 - {'type': 'error', 'error': {...raw sdk...}}")
    monkeypatch.setattr(sr, "make_model", boom)
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    r = c.post("/settings/test-key", json={})
    assert r.json() == {"ok": False, "error": "The key test failed — the log has the details."}
    assert "raw sdk" in caplog.text                 # the detail is in the log


def test_test_key_requires_json_body(tmp_path, monkeypatch, mem_keyring):
    # /settings/test-key makes a real (billed) Anthropic call, so it must not be
    # reachable cross-origin: requiring a JSON body stops it being a CORS "simple
    # request", forcing a preflight the origin allowlist blocks. Same guard as
    # /ingest and /mapcapture/*. A bodyless POST and a body with no Content-Type
    # (both CORS-simple) must be rejected before the handler runs.
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    assert c.post("/settings/test-key").status_code == 422
    assert c.post("/settings/test-key", content=b"{}").status_code == 422


def test_post_api_key_sets_it(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    r = c.post("/settings/api-key", json={"key": "sk-real-key"})
    assert r.status_code == 200
    assert r.json() == {"api_key_set": True}
    assert c.get("/settings").json()["api_key_set"] is True


def test_post_api_key_rejects_empty(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    r = c.post("/settings/api-key", json={"key": ""})
    assert r.status_code == 400
    r2 = c.post("/settings/api-key", json={})
    assert r2.status_code == 400


def test_put_settings_log_level_persists_and_applies_live(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    c.put("/settings", json={"log_level": "DEBUG"})
    body = c.get("/settings").json()
    assert body["log_level"] == "DEBUG"
    assert logging.getLogger().level == logging.DEBUG


def test_put_partial_driver_preserves_siblings(tmp_path, monkeypatch, mem_keyring):
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    c.put("/settings", json={"driver": {"braking_style": "late", "balance_preference": "neutral"}})
    c.put("/settings", json={"driver": {"risk_tolerance": "high"}})
    body = c.get("/settings").json()
    assert body["driver"] == {
        "braking_style": "late",
        "balance_preference": "neutral",
        "risk_tolerance": "high",
    }
