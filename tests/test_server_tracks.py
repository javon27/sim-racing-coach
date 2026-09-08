# tests/test_server_tracks.py
import json
from pathlib import Path

from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore


def _seed_track(tracks_dir: Path, slug: str = "algarve_international_circuit") -> None:
    # Minimal shipped-shape fixture -- just enough for CornerMap.from_data
    # (see simcoach/corners.py) so draft-notes/put-notes/coverage have a
    # "shipped" track to work with, sandboxed away from the real repo file.
    tracks_dir.mkdir(parents=True, exist_ok=True)
    (tracks_dir / f"{slug}.json").write_text(json.dumps({
        "track": slug,
        "corners": [{"number": 1, "name": "Turn 1", "direction": "right",
                     "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}],
    }))


def _make_client_with_fake_model(tmp_path, model_reply):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel(model_reply), "simcoach/knowledge")
    tracks_dir = tmp_path / "tracks"
    _seed_track(tracks_dir)
    client = TestClient(create_app(store, coach, tracks_dir=str(tracks_dir)),
                         base_url="http://127.0.0.1")
    return client, store


def test_draft_notes_returns_draft_without_writing(tmp_path):
    client, store = _make_client_with_fake_model(tmp_path, model_reply=json.dumps({
        "overview": "rhythm track", "corners": {"1": "brake late"}, "source": "draft"}))
    tracks_dir = Path(store.data_dir) / "tracks"
    before = set(tracks_dir.glob("*.json")) if tracks_dir.exists() else set()
    r = client.post("/tracks/algarve_international_circuit/draft-notes",
                     json={"track": "Algarve International Circuit", "guides": "outside line into T1"})
    assert r.status_code == 200
    draft = r.json()["draft"]
    assert draft["overview"] == "rhythm track"
    assert draft["corners"]["1"] == "brake late"
    after = set(tracks_dir.glob("*.json")) if tracks_dir.exists() else set()
    assert before == after                     # endpoint wrote nothing


def test_draft_notes_bad_slug_400(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.post("/tracks/..%2f..%2fetc/draft-notes", json={"track": "../../etc"})
    assert r.status_code in (400, 404)


def test_draft_notes_no_map_404(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.post("/tracks/nonexistent_track_xyz/draft-notes",
                     json={"track": "Nonexistent Track XYZ"})
    assert r.status_code == 404


def test_draft_notes_bad_model_json_502(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="not json")
    r = client.post("/tracks/algarve_international_circuit/draft-notes",
                     json={"track": "Algarve International Circuit"})
    assert r.status_code == 502


def test_put_notes_persists_and_reads_back(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "rhythm", "corners": {"1": "brake late"}, "source": "me"}})
    assert r.status_code == 200 and r.json()["ok"] is True
    # A fresh GET of the track's map data now carries the notes overlay.
    got = client.get("/tracks").json()
    row = next(t for t in got if t["slug"] == "algarve_international_circuit")
    assert row["has_notes"] is True


def test_get_tracks_lists_coverage(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    rows = client.get("/tracks").json()
    assert any(t["slug"] == "algarve_international_circuit" and t["has_map"] for t in rows)


def test_put_notes_reference_times_roundtrip(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "x", "reference_times": {"GT3": "1:32.500"}}})
    assert r.status_code == 200
    written = json.loads((tmp_path / "tracks" / "algarve_international_circuit.json").read_text())
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}


def test_get_notes_no_notes_returns_empty_shape(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/algarve_international_circuit/notes")
    assert r.status_code == 200
    assert r.json() == {"overview": "", "corners": {}, "reference_times": {}, "source": ""}


def test_get_notes_missing_track_returns_empty_shape(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/nonexistent_track_xyz/notes")
    assert r.status_code == 200
    assert r.json() == {"overview": "", "corners": {}, "reference_times": {}, "source": ""}


def test_track_map_by_slug_no_path_returns_empty(tmp_path):
    # The seed fixture has corners but no frozen path -> no canonical layout yet.
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/algarve_international_circuit/map")
    assert r.status_code == 200 and r.json() == {}


def test_track_map_by_slug_bad_slug_400(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/..%2f..%2fetc/map")
    assert r.status_code in (400, 404)


def test_track_map_by_slug_returns_layout(tmp_path):
    # A track file with a frozen path yields a built canonical map (path + corners),
    # no lap needed -- the Tracks-screen view path.
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel("{}"), "simcoach/knowledge")
    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    path = [[float(i), float(i % 3)] for i in range(120)]
    (tracks_dir / "algarve_international_circuit.json").write_text(json.dumps({
        "track": "algarve_international_circuit",
        "path": path,
        "path_dist": [float(i) for i in range(120)],
        "corners": [{"number": 1, "name": "Turn 1", "direction": "right",
                     "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}],
    }))
    client = TestClient(create_app(store, coach, tracks_dir=str(tracks_dir)),
                        base_url="http://127.0.0.1")
    r = client.get("/tracks/algarve_international_circuit/map")
    assert r.status_code == 200
    m = r.json()
    assert m.get("path") and len(m["corners"]) == 1


def _client_with_frozen_path(tmp_path):
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    coach = Coach(FakeModel("{}"), "simcoach/knowledge")
    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    (tracks_dir / "algarve_international_circuit.json").write_text(json.dumps({
        "track": "algarve_international_circuit",
        "path": [[float(i), float(i % 3)] for i in range(120)],
        "path_dist": [float(i) for i in range(120)],
        "corners": [{"number": 1, "name": "Turn 1", "direction": "right",
                     "start_dist": 0.0, "apex_dist": 50.0, "end_dist": 100.0}],
    }))
    client = TestClient(create_app(store, coach, tracks_dir=str(tracks_dir)),
                        base_url="http://127.0.0.1")
    return client, tracks_dir


def test_save_track_map_by_slug_persists(tmp_path):
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    r = client.put("/tracks/algarve_international_circuit/map", json={
        "corners": [
            {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 50.0},
            {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 80.0},
        ]})
    assert r.status_code == 200, r.text
    written = json.loads((tracks_dir / "algarve_international_circuit.json").read_text())
    assert len(written["corners"]) == 2
    # Direction is server-derived, never trusted from the client.
    assert all(c["direction"] in ("left", "right", "straight") for c in written["corners"])


def test_save_track_map_by_slug_rejects_empty_corners(tmp_path):
    client, _ = _client_with_frozen_path(tmp_path)
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": []})
    assert r.status_code == 422


def test_track_rotation_by_slug_persists(tmp_path):
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    r = client.post("/tracks/algarve_international_circuit/rotation", json={"degrees": 90})
    assert r.status_code == 200
    written = json.loads((tracks_dir / "algarve_international_circuit.json").read_text())
    assert written["rotation"] == 90.0


def test_put_then_get_notes_roundtrip(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "rhythm track", "corners": {"1": "brake late"},
                   "reference_times": {"GT3": "1:32.500"}, "source": "me"}})
    r = client.get("/tracks/algarve_international_circuit/notes")
    assert r.status_code == 200
    assert r.json() == {"overview": "rhythm track", "corners": {"1": "brake late"},
                         "reference_times": {"GT3": "1:32.500"}, "source": "me"}
