def _seed_pos_lap(store, track, car):
    """Save one position-captured lap so the track has a canonical map outline."""
    import numpy as np

    from simcoach.model_types import LapRecord
    x = np.array([0.0, 100.0, 200.0, 200.0, 200.0])
    z = np.array([0.0, 0.0, 0.0, 100.0, 200.0])
    d = np.array([0.0, 100.0, 200.0, 300.0, 400.0])
    return store.save(LapRecord("lmu", track, car, 90.0, True, 0.0,
                                {"pos_x": x, "pos_z": z, "lap_distance": d}))


def test_build_app_offline_no_key(tmp_path, monkeypatch, mem_keyring):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SIMCOACH_MODEL_PROVIDER", raising=False)  # real default = anthropic
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)        # no key anywhere
    from simcoach.server.__main__ import build_app
    app = build_app()  # must NOT raise despite no key + no LMU
    from fastapi.testclient import TestClient
    tc = TestClient(app, base_url="http://127.0.0.1")
    assert tc.get("/health").json() == {"status": "ok"}
    assert tc.get("/settings").json()["api_key_set"] is False  # reachable to enter a key


def test_build_app_lmu_running(tmp_path, monkeypatch, mem_keyring):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import simcoach.__main__ as m
    from simcoach.lmu import smm

    class FakeRest:
        def get_setup(self):
            return {"carClass": "GT3"}

    fake_cmap = object()

    # /status reads the live track via `from ..lmu.smm import read_session_track_car`
    # at call time, so patch it on smm (the source), not on the __main__ re-export.
    monkeypatch.setattr(smm, "read_session_track_car", lambda: ("test_track", "test_car"))
    monkeypatch.setattr(m, "LmuRest", FakeRest)
    monkeypatch.setattr(m, "resolve_track_map", lambda track, tracks_dir: "fake/path.json")
    monkeypatch.setattr(m, "CornerMap", type("CM", (), {"load": staticmethod(lambda p: fake_cmap)}))

    from fastapi.testclient import TestClient

    from simcoach.server.__main__ import build_app
    app = build_app()
    # No boot-time resolution anymore: rest is lazy, map is per-track.
    assert app.state.rest is None
    _seed_pos_lap(app.state.store, "test_track", "test_car")  # canonical map source
    tc = TestClient(app, base_url="http://127.0.0.1")
    # Live SMM track + a position lap -> has_map (canonical outline available);
    # /setup lazily connects.
    st = tc.get("/status").json()
    assert st["track"] == "test_track" and st["has_map"] is True
    assert tc.get("/setup").json() == {"carClass": "GT3"}
    assert tc.get("/status").json()["lmu_connected"] is True


def test_status_lmu_connected_reflects_disconnect(tmp_path, monkeypatch):
    # /status.lmu_connected must go back to False when LMU closes mid-session,
    # not stick True after the first successful connect (regression guard).
    from fastapi.testclient import TestClient

    from simcoach.coach import Coach
    from simcoach.model.fake import FakeModel
    from simcoach.server import app as app_module
    from simcoach.server.app import create_app
    from simcoach.store import LapStore

    # Probe on every call so the transition is observed synchronously (prod caps
    # the probe rate via REST_PROBE_TTL; here we want each /status to re-probe).
    monkeypatch.setattr(app_module, "REST_PROBE_TTL", 0.0)

    class FakeRest:
        alive = True

        def get_setup(self):
            if not FakeRest.alive:
                raise RuntimeError("LMU closed")
            return {"carClass": "GT3"}

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    app = create_app(store, coach, rest_factory=FakeRest)
    tc = TestClient(app, base_url="http://127.0.0.1")

    assert tc.get("/status", headers={"host": "127.0.0.1"}).json()["lmu_connected"] is True
    FakeRest.alive = False
    assert tc.get("/status", headers={"host": "127.0.0.1"}).json()["lmu_connected"] is False
    FakeRest.alive = True
    assert tc.get("/status", headers={"host": "127.0.0.1"}).json()["lmu_connected"] is True


def test_status_detects_track_change_over_stale_latest(tmp_path, monkeypatch):
    # A captured lap pins app.state.latest to track A; when the driver moves to
    # track B the live SMM read must win — /status reports B (on_track False, no
    # stale lap ids), else the app shows the old track and would map B under A's
    # slug. Regression for "still shows Algarve after moving to Imola".
    from fastapi.testclient import TestClient

    from simcoach.coach import Coach
    from simcoach.model.fake import FakeModel
    from simcoach.server.app import create_app
    from simcoach.store import LapStore
    from simcoach.model_types import LapRecord
    from simcoach.lmu import smm

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    app = create_app(store, coach, rest_factory=lambda: None)
    rec = LapRecord("lmu", "Algarve", "CarA", 90.0, True, 0.0, {},
                    session_id="algarve--cara--offline")
    app.state.latest = {"record": rec, "lap_id": 42}
    tc = TestClient(app, base_url="http://127.0.0.1")

    # Same track live -> the captured lap supplies the rich context.
    monkeypatch.setattr(smm, "read_session_track_car", lambda: ("Algarve", "CarA"))
    s = tc.get("/status", headers={"host": "127.0.0.1"}).json()
    assert s["track"] == "Algarve" and s["on_track"] is True and s["lap_id"] == 42

    # Driver moved to track B -> live read wins, stale lap context dropped.
    monkeypatch.setattr(smm, "read_session_track_car", lambda: ("Imola", "CarB"))
    s = tc.get("/status", headers={"host": "127.0.0.1"}).json()
    assert s["track"] == "Imola" and s["car"] == "CarB"
    assert s["on_track"] is False and s["session_id"] is None and s["lap_id"] is None


def test_status_probe_is_rate_capped(tmp_path):
    # Rapid /status polls must not each hit LMU's REST (threadpool-saturation guard):
    # with the default REST_PROBE_TTL, a burst shares one liveness probe.
    from fastapi.testclient import TestClient

    from simcoach.coach import Coach
    from simcoach.model.fake import FakeModel
    from simcoach.server.app import create_app
    from simcoach.store import LapStore

    calls = {"n": 0}

    class CountingRest:
        def get_setup(self):
            calls["n"] += 1
            return {"x": 1}

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(reply="ok"), "simcoach/knowledge")
    app = create_app(store, coach, rest_factory=CountingRest)
    tc = TestClient(app, base_url="http://127.0.0.1")

    for _ in range(5):
        tc.get("/status", headers={"host": "127.0.0.1"})
    assert calls["n"] == 1  # one connect probe; the rest are served from cache


def test_build_app_lmu_rest_failure_still_resolves_cmap(tmp_path, monkeypatch, mem_keyring):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import simcoach.__main__ as m
    from simcoach.lmu import smm

    class BrokenRest:
        def get_setup(self):
            raise RuntimeError("LMU closed")

    fake_cmap = object()

    monkeypatch.setattr(smm, "read_session_track_car", lambda: ("test_track", "test_car"))
    monkeypatch.setattr(m, "LmuRest", BrokenRest)
    monkeypatch.setattr(m, "resolve_track_map", lambda track, tracks_dir: "fake/path.json")
    monkeypatch.setattr(m, "CornerMap", type("CM", (), {"load": staticmethod(lambda p: fake_cmap)}))

    from fastapi.testclient import TestClient

    from simcoach.server.__main__ import build_app
    app = build_app()
    _seed_pos_lap(app.state.store, "test_track", "test_car")  # canonical map source
    tc = TestClient(app, base_url="http://127.0.0.1")
    # Broken REST -> never connects; the canonical map still resolves per-track
    # independently (from the position lap, not from REST).
    assert tc.get("/setup").json() == {}
    st = tc.get("/status").json()
    assert st["lmu_connected"] is False and st["has_map"] is True
