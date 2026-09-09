from fastapi.testclient import TestClient
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel


def _tc(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    return TestClient(create_app(store, Coach(FakeModel())),
                      base_url="http://127.0.0.1")


def test_cors_allows_dev_and_tauri_origins(tmp_path):
    c = _tc(tmp_path)
    for origin in ("http://localhost:5173", "http://tauri.localhost"):
        r = c.get("/health", headers={"host": "127.0.0.1", "origin": origin})
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == origin


def test_preflight_still_succeeds_for_real_client_origins(tmp_path):
    # The origin guard runs OUTSIDE CORSMiddleware, so it sees the OPTIONS
    # preflight first. Every JSON POST the app makes depends on that preflight
    # succeeding -- if the guard ate it, the whole UI would break.
    c = _tc(tmp_path)
    for origin in ("http://localhost:5173", "http://tauri.localhost"):
        r = c.options("/ingest", headers={"host": "127.0.0.1", "origin": origin,
                                         "access-control-request-method": "POST",
                                         "access-control-request-headers": "content-type"})
        assert r.status_code == 200, f"{origin} preflight broke: {r.status_code}"
        assert r.headers.get("access-control-allow-origin") == origin


def test_unknown_origin_is_refused_not_just_uncorsed(tmp_path):
    # This used to assert 200 + no access-control-allow-origin, on the theory that
    # withholding the CORS header is enough. It isn't: the browser withholds the
    # RESPONSE from script, but the request was already sent and executed, so any
    # side effect has happened. The origin guard in _host_guard now refuses it
    # outright -- which is the only thing that can prevent the side effect.
    r = _tc(tmp_path).get("/health", headers={"host": "127.0.0.1", "origin": "http://evil.com"})
    assert r.status_code == 403
    assert r.headers.get("access-control-allow-origin") in (None, "")
