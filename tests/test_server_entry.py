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
