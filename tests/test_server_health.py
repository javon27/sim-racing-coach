from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _client(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel())
    return TestClient(create_app(store, coach), base_url="http://127.0.0.1")


def test_health(tmp_path):
    r = _client(tmp_path).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_spoofed_host_forbidden(tmp_path):
    r = _client(tmp_path).get("/health", headers={"host": "evil.com"})
    assert r.status_code == 403


def test_missing_host_forbidden(tmp_path):
    # TestClient sends a default Host, so force an empty one to simulate missing.
    r = _client(tmp_path).get("/health", headers={"host": ""})
    assert r.status_code == 403


def test_uppercase_localhost_allowed(tmp_path):
    r = _client(tmp_path).get("/health", headers={"host": "LOCALHOST"})
    assert r.status_code == 200
