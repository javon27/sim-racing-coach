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
