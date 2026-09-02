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

    class FakeRest:
        def get_setup(self):
            return {"carClass": "GT3"}

    fake_cmap = object()

    monkeypatch.setattr(m, "read_session_track_car", lambda: ("test_track", "test_car"))
    monkeypatch.setattr(m, "LmuRest", FakeRest)
    monkeypatch.setattr(m, "resolve_track_map", lambda track, tracks_dir: "fake/path.json")
    monkeypatch.setattr(m, "CornerMap", type("CM", (), {"load": staticmethod(lambda p: fake_cmap)}))

    from simcoach.server.__main__ import build_app
    app = build_app()
    assert app.state.cmap is fake_cmap
    assert isinstance(app.state.rest, FakeRest)


def test_build_app_lmu_rest_failure_still_resolves_cmap(tmp_path, monkeypatch, mem_keyring):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import simcoach.__main__ as m

    class BrokenRest:
        def get_setup(self):
            raise RuntimeError("LMU closed")

    fake_cmap = object()

    monkeypatch.setattr(m, "read_session_track_car", lambda: ("test_track", "test_car"))
    monkeypatch.setattr(m, "LmuRest", BrokenRest)
    monkeypatch.setattr(m, "resolve_track_map", lambda track, tracks_dir: "fake/path.json")
    monkeypatch.setattr(m, "CornerMap", type("CM", (), {"load": staticmethod(lambda p: fake_cmap)}))

    from simcoach.server.__main__ import build_app
    app = build_app()
    assert app.state.rest is None
    assert app.state.cmap is fake_cmap
