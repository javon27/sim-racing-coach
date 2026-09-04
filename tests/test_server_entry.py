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
    tc = TestClient(app, base_url="http://127.0.0.1")
    # Live SMM track drives has_map through the resolver; /setup lazily connects.
    st = tc.get("/status").json()
    assert st["track"] == "test_track" and st["has_map"] is True
    assert tc.get("/setup").json() == {"carClass": "GT3"}
    assert tc.get("/status").json()["lmu_connected"] is True


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
    tc = TestClient(app, base_url="http://127.0.0.1")
    # Broken REST -> never connects; map still resolves per-track independently.
    assert tc.get("/setup").json() == {}
    st = tc.get("/status").json()
    assert st["lmu_connected"] is False and st["has_map"] is True
