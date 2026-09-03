from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


def _tc(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    return TestClient(create_app(store, Coach(FakeModel(), "simcoach/knowledge")),
                      base_url="http://127.0.0.1")


def test_cors_allows_dev_and_tauri_origins(tmp_path):
    c = _tc(tmp_path)
    for origin in ("http://localhost:5173", "http://tauri.localhost"):
        r = c.get("/health", headers={"host": "127.0.0.1", "origin": origin})
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == origin


def test_cors_omits_header_for_unknown_origin(tmp_path):
    r = _tc(tmp_path).get("/health", headers={"host": "127.0.0.1", "origin": "http://evil.com"})
    assert r.status_code == 200  # host-guard passed (127.0.0.1); CORS just withholds the header
    assert r.headers.get("access-control-allow-origin") in (None, "")
