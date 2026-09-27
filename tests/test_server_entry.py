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

    from simcoach.lmu import smm
    from simcoach.server import __main__ as entry

    class FakeRest:
        def get_setup(self):
            return {"carClass": "GT3"}

    # /status reads live state via `from ..lmu.smm import read_live_status` at call
    # time, so patch it on smm (the source), not on the sidecar entry module.
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": "test_track", "car": "test_car", "state": "track"})
    monkeypatch.setattr(entry, "LmuRest", FakeRest)

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
    from simcoach.server import core as core_mod
    from simcoach.server.app import create_app
    from simcoach.store import LapStore

    # Probe on every call so the transition is observed synchronously (prod caps
    # the probe rate via REST_PROBE_TTL; here we want each /status to re-probe).
    monkeypatch.setattr(core_mod, "REST_PROBE_TTL", 0.0)

    class FakeRest:
        alive = True

        def get_setup(self):
            if not FakeRest.alive:
                raise RuntimeError("LMU closed")
            return {"carClass": "GT3"}

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(reply="ok"))
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
    from simcoach.lmu import smm
    from simcoach.model.fake import FakeModel
    from simcoach.model_types import LapRecord
    from simcoach.server.app import create_app
    from simcoach.store import LapStore

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(reply="ok"))
    app = create_app(store, coach, rest_factory=lambda: None)
    rec = LapRecord("lmu", "Algarve", "CarA", 90.0, True, 0.0, {},
                    session_id="algarve--cara--offline")
    app.state.latest = {"record": rec, "lap_id": 42}
    tc = TestClient(app, base_url="http://127.0.0.1")

    # Same track live -> the captured lap supplies the rich context.
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": "Algarve", "car": "CarA", "state": "track"})
    s = tc.get("/status", headers={"host": "127.0.0.1"}).json()
    assert s["track"] == "Algarve" and s["on_track"] is True and s["lap_id"] == 42

    # Driver moved to track B -> live read wins (track/car + live on_track), but the
    # stale lap context is dropped (no captured lap for B yet).
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": "Imola", "car": "CarB", "state": "track"})
    s = tc.get("/status", headers={"host": "127.0.0.1"}).json()
    assert s["track"] == "Imola" and s["car"] == "CarB"
    assert s["on_track"] is True and s["session_id"] is None and s["lap_id"] is None


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
    coach = Coach(FakeModel(reply="ok"))
    app = create_app(store, coach, rest_factory=CountingRest)
    tc = TestClient(app, base_url="http://127.0.0.1")

    for _ in range(5):
        tc.get("/status", headers={"host": "127.0.0.1"})
    assert calls["n"] == 1  # one connect probe; the rest are served from cache


def test_status_probe_is_rate_capped_while_disconnected_too(tmp_path):
    # Rig 2026-09-15: with LMU's REST port accepting but not answering, every
    # /status poll re-ran the 4 s probe because the cap only covered a CONNECTED
    # client. A failed probe must be remembered for the same TTL.
    from fastapi.testclient import TestClient

    from simcoach.coach import Coach
    from simcoach.model.fake import FakeModel
    from simcoach.server.app import create_app
    from simcoach.store import LapStore

    calls = {"n": 0}

    class HangingRest:
        def get_setup(self):
            calls["n"] += 1
            raise TimeoutError("LMU REST not answering")

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel(reply="ok")), rest_factory=HangingRest)
    tc = TestClient(app, base_url="http://127.0.0.1")

    for _ in range(5):
        assert tc.get("/status", headers={"host": "127.0.0.1"}).json()["lmu_connected"] is False
    assert calls["n"] == 1  # one failed probe per TTL, not one per poll


def test_build_app_lmu_rest_failure_still_resolves_cmap(tmp_path, monkeypatch, mem_keyring):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from simcoach.lmu import smm
    from simcoach.server import __main__ as entry

    class BrokenRest:
        def get_setup(self):
            raise RuntimeError("LMU closed")

    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": "test_track", "car": "test_car", "state": "track"})
    monkeypatch.setattr(entry, "LmuRest", BrokenRest)

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


def test_create_app_and_build_app_do_not_start_the_result_poller(tmp_path, monkeypatch, mem_keyring):
    # The poller reads the REAL LMU install (#81), and nearly every test builds an app.
    import threading

    from fastapi.testclient import TestClient

    from simcoach.coach import Coach
    from simcoach.lmu import ingest, install
    from simcoach.model.fake import FakeModel
    from simcoach.server.__main__ import build_app
    from simcoach.server.app import create_app
    from simcoach.store import LapStore

    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Should this ever regress, the poller it starts must still not see the real install.
    monkeypatch.setattr(install, "lmu_userdata_dir", lambda: None)
    targets = []
    real_start = threading.Thread.start

    def recording_start(self):
        targets.append(self._target)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", recording_start)

    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    for app in (create_app(store, Coach(FakeModel())), build_app()):
        with TestClient(app, base_url="http://127.0.0.1") as tc:   # runs startup too
            assert tc.get("/health").status_code == 200

    assert targets   # the recorder saw threads start (capture, test client), just not this
    assert ingest.poll_results not in targets


def test_main_starts_the_result_poller_on_a_daemon_thread_before_serving(tmp_path, monkeypatch,
                                                                         mem_keyring):
    import threading

    import uvicorn

    from simcoach.coach import Coach
    from simcoach.model.fake import FakeModel
    from simcoach.server import __main__ as entry
    from simcoach.server.app import create_app
    from simcoach.store import LapStore

    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))
    polled = {}
    running = threading.Event()

    def fake_poll(s):
        polled.update(store=s, thread=threading.current_thread())
        running.set()

    served = []

    def fake_run(a, **kw):
        # uvicorn.run blocks for the life of the process: started after it = never started.
        served.append((a, running.wait(5)))

    monkeypatch.setattr(entry, "poll_results", fake_poll)
    # #179: main() also warms car_lookup's entry table from the real install --
    # same reasoning as poll_results above, patched the same way.
    monkeypatch.setattr(entry, "warm_entry_table_async", lambda: None)
    monkeypatch.setattr(entry, "build_app", lambda: app)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    entry.main()

    assert served == [(app, True)]
    assert polled["store"] is store
    assert polled["thread"].daemon and polled["thread"] is not threading.main_thread()
