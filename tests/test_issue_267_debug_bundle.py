"""#267: export one session as a debug .zip a user can attach to an issue -- its rows,
lap telemetry and log slice, and never the API key or another session's data."""
import dataclasses
import json
import time
import zipfile

from fastapi.testclient import TestClient

from simcoach import debug_bundle, paths, secrets
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore

KEY = "sk-ant-api03-THISISAFAKEKEYFORTHETEST0123456789"


def _setup(tmp_path, monkeypatch, synthetic_lap):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    now = time.time()
    mine = store.save(dataclasses.replace(synthetic_lap, session_id="sess-a", timestamp=now))
    store.save(dataclasses.replace(synthetic_lap, session_id="sess-b", timestamp=now))
    sid = store.conn.execute("SELECT session_id FROM laps WHERE id = ?", (mine,)).fetchone()[0]
    store.save_chat("chat-1", sid, synthetic_lap.track, synthetic_lap.car, mine,
                    "where am I losing time?", f"T1. (and a pasted {KEY})", ts=now)
    secrets.set_api_key(KEY)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S,000", time.localtime(now))
    old = time.strftime("%Y-%m-%d %H:%M:%S,000", time.localtime(now - 86400))
    (paths.logs_dir() / "server.log").write_text(
        f"{old} INFO simcoach: yesterday, another session\n"
        f"{stamp} WARNING simcoach.server.coach_ws: coach turn failed key={KEY}\n"
        "Traceback (most recent call last):\n  boom\n", encoding="utf-8")
    # load_config keeps only known keys: a secret-shaped one is injected to prove redaction
    real = debug_bundle.config.load_config
    monkeypatch.setattr(debug_bundle.config, "load_config", lambda: {
        **real(), "some_api_token": "tok-123", "lmu_player_names": ["Jane Q Driver"],
        "lmu_dir": str(tmp_path / "LMU")})
    # result-log races at this track: this car's, near the session (in) and another
    # car's at the same time (out -- review: race_sessions over-matched by track only)
    for rid, car in (("2026_R1", synthetic_lap.car), ("2026_OTHER", "Other Car")):
        store.conn.execute("INSERT INTO race_sessions (session_id, track, car, ts, phase) "
                           "VALUES (?, ?, ?, ?, 'race')", (rid, synthetic_lap.track, car, now))
    store.conn.commit()
    c = TestClient(create_app(store, Coach(FakeModel())), base_url="http://127.0.0.1")
    return c, sid, mine


def test_a_session_bundle_has_its_rows_telemetry_and_log_slice_and_no_key(
        tmp_path, monkeypatch, mem_keyring, synthetic_lap):
    c, sid, lap_id = _setup(tmp_path, monkeypatch, synthetic_lap)
    dest = tmp_path / "out" / "bundle.zip"
    dest.parent.mkdir()
    r = c.post(f"/sessions/{sid}/debug-bundle", json={"path": str(dest)})
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
        blob = b"".join(z.read(n) for n in names)
        laps = json.loads(z.read("db/laps.json"))
        manifest = json.loads(z.read("manifest.json"))
        log = z.read("logs/server.log").decode()
        settings = json.loads(z.read("settings.json"))
    assert KEY.encode() not in blob and b"tok-123" not in blob          # no secret anywhere
    assert [row["id"] for row in laps] == [lap_id]                       # this session only
    assert f"telemetry/{lap_id}.npz" in names
    assert manifest["session_id"] == sid and manifest["tables"]["coach_chat"] == 1
    assert "coach turn failed key=[redacted key]" in log and "boom" in log
    assert "yesterday" not in log                                       # outside the window
    assert settings["some_api_token"] == "[redacted]" and settings["units"] == "metric"
    assert "db/driver_learnings.json" not in names                      # no cross-session profile
    # review: no personal data -- the driver's names, local paths, the profile prefs
    assert b"Jane Q Driver" not in blob and str(tmp_path).encode() not in blob
    assert str(tmp_path).replace("\\", "\\\\").encode() not in blob     # JSON-escaped too
    assert settings["driver"] == "[redacted]"
    assert laps[0]["telemetry_path"] == f"telemetry/{lap_id}.npz"
    races = json.loads(zipfile.ZipFile(dest).read("db/race_sessions.json"))
    assert [r["session_id"] for r in races] == ["2026_R1"]
    assert manifest["contains_other_drivers_names"] is True


def test_telemetry_over_the_budget_is_left_out_and_named(
        tmp_path, monkeypatch, mem_keyring, synthetic_lap):
    # review: a real session's .npz set was 47-97 MB, over GitHub's 25 MB attachment cap
    c, sid, lap_id = _setup(tmp_path, monkeypatch, synthetic_lap)
    monkeypatch.setattr(debug_bundle, "TELEMETRY_BUDGET", 10)
    manifest = c.post(f"/sessions/{sid}/debug-bundle", json={}).json()["manifest"]
    assert manifest["telemetry"] == [] and manifest["telemetry_omitted_lap_ids"] == [lap_id]


def test_no_path_lands_under_app_data_and_bad_input_is_refused(
        tmp_path, monkeypatch, mem_keyring, synthetic_lap):
    c, sid, _ = _setup(tmp_path, monkeypatch, synthetic_lap)
    r = c.post(f"/sessions/{sid}/debug-bundle", json={})
    assert r.status_code == 200 and zipfile.is_zipfile(r.json()["path"])
    assert str(paths.app_data_dir() / "exports") in r.json()["path"]
    assert c.post(f"/sessions/{sid}/debug-bundle",
                  json={"path": str(tmp_path / "x.txt")}).status_code == 400
    assert c.post(f"/sessions/{sid}/debug-bundle",
                  json={"path": str(tmp_path / "nope" / "x.zip")}).status_code == 400
    assert c.post("/sessions/no-such-session/debug-bundle", json={}).status_code == 404
    # review: only a local absolute drive path -- no UNC (an SMB login), relative path,
    # NTFS stream, folder or non-string
    for bad in ("rel.zip", str(tmp_path / "note.txt:ads.zip"), str(tmp_path), 123):
        assert c.post(f"/sessions/{sid}/debug-bundle", json={"path": bad}).status_code == 400, bad
    # a UNC path is refused by its shape alone, before any filesystem call (is_dir() on
    # it would send an SMB login): every folder "exists" here, and it's still refused
    import pathlib
    monkeypatch.setattr(pathlib.Path, "is_dir", lambda self: not str(self).endswith(".zip"))
    for unc in (r"\\attacker\share\x.zip", "//attacker/share/x.zip"):
        assert c.post(f"/sessions/{sid}/debug-bundle", json={"path": unc}).status_code == 400, unc
    assert c.post(f"/sessions/{sid}/debug-bundle").status_code == 422     # CSRF: JSON body
