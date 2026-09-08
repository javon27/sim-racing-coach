import logging

from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


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
    r = c.post("/settings/test-key")
    assert r.status_code == 200
    assert r.json()["ok"] is False


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
