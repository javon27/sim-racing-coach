# tests/test_server_tracks.py
import json
import logging
import shutil
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

import simcoach
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
    coach = Coach(FakeModel(model_reply))
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
    coach = Coach(FakeModel("{}"))
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
    coach = Coach(FakeModel("{}"))
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


def _written(tracks_dir):
    return json.loads((tracks_dir / "algarve_international_circuit.json").read_text())


def _put_notes(client, corners):
    return client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "rhythm track", "corners": corners,
                  "reference_times": {"GT3": "1:32.500"}, "source": "me"}})


def test_save_track_map_by_slug_preserves_authored_notes(tmp_path):
    # Issue #37: re-saving a track's layout (PUT .../map) must not silently wipe
    # the authored notes overlay -- it's expensive to author. The no-op case: same
    # corner count, same labels -> everything is carried across unchanged.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    _put_notes(client, {"1": "brake late"})
    r = client.put("/tracks/algarve_international_circuit/map", json={
        "corners": [
            {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 50.0},
        ]})
    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["overview"] == "rhythm track"
    assert written["notes"]["corners"] == {"1": "brake late"}
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}


def test_save_track_map_drops_corner_notes_when_corner_count_changes(tmp_path, caplog):
    # Issue #37 (rework): a rebuild may re-derive a chicane as TWO corners and
    # renumber everything after it, so a label-keyed note would silently rebind to
    # a DIFFERENT corner -- and the coach states authored notes as owned fact.
    # A changed corner count -> drop per-corner notes; keep the fields that are
    # not corner-keyed (overview / reference_times / source).
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    _put_notes(client, {"1": "the slow left hairpin - huge patience"})
    with caplog.at_level(logging.WARNING, logger="simcoach.server.app"):
        r = client.put("/tracks/algarve_international_circuit/map", json={
            "corners": [
                {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 18.0},
                {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 24.0},
            ]})
    assert r.status_code == 200, r.text
    # Not silent, and the dropped text is in the log so it can be recovered.
    assert "huge patience" in caplog.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {}          # not rebound to a new T1/T2
    assert written["notes"]["overview"] == "rhythm track"
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}
    assert written["notes"]["source"] == "me"


def test_save_track_map_drops_orphaned_corner_notes_on_relabel(tmp_path):
    # Issue #37 (rework): same corner count, but GP-style relabelling ('2' -> '2A'
    # /'2B') leaves note keys that no longer name any corner. Those are silently
    # unreachable -- drop them, keep the labels that still exist.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    three = [{"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 20.0},
             {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 50.0},
             {"number": 3, "label": "3", "name": "Turn 3", "apex_dist": 80.0}]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": three}).status_code == 200
    _put_notes(client, {"1": "brake late", "2": "middle of the chicane", "3": "flat in 6th"})
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": [
        {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 20.0},
        {"number": 2, "label": "2A", "name": "Turn 2A", "apex_dist": 50.0},
        {"number": 3, "label": "2B", "name": "Turn 2B", "apex_dist": 80.0},
    ]})
    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {"1": "brake late"}   # '2'/'3' orphaned
    assert written["notes"]["overview"] == "rhythm track"


def _four_corners():
    return [{"number": n, "label": str(n), "name": f"Turn {n}", "apex_dist": 20.0 * n}
            for n in (1, 2, 3, 4)]


def test_save_track_map_keeps_range_keyed_notes(tmp_path):
    # A per-corner note key may name a RANGE ('2-4') -- that is how a complex gets
    # annotated, and 16 of the 135 per-corner keys across the shipped maps look like
    # this. '2-4' is never itself a corner label, so testing the raw key against the
    # labels dropped every range-keyed note as "stale" on a plain no-op re-save,
    # even though every corner it names still existed.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    four = _four_corners()
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": four}).status_code == 200
    _put_notes(client, {"1": "brake late", "2-4": "the fast left-right-left"})
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": four}).status_code == 200
    assert _written(tracks_dir)["notes"]["corners"] == {
        "1": "brake late", "2-4": "the fast left-right-left"}


def test_save_track_map_keeps_range_note_when_only_some_of_its_corners_survive(tmp_path):
    # Partial survivor. '1-3' still names corners 1 and 3 after the middle one is
    # relabelled '2A', and the coach still serves the note for those, so keeping it
    # loses nothing. It is also no more exposed to renumbering than a plain '1' key,
    # which the same rule already keeps -- so a range is judged the same way a single
    # label is: keep it while it names a corner that exists.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    three = [{"number": n, "label": str(n), "name": f"Turn {n}", "apex_dist": 20.0 * n}
             for n in (1, 2, 3)]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": three}).status_code == 200
    _put_notes(client, {"1-3": "the downhill complex"})
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": [
        {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 20.0},
        {"number": 2, "label": "2A", "name": "Turn 2A", "apex_dist": 40.0},
        {"number": 3, "label": "3", "name": "Turn 3", "apex_dist": 60.0},
    ]})
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {"1-3": "the downhill complex"}


def test_save_track_map_drops_range_note_when_no_corner_it_names_survives(tmp_path):
    # GUARD (passes before the fix too -- the old code dropped every range key
    # unconditionally, so this case cannot be made red). Pins the other side of the
    # rule: a range naming nothing that still exists is unreachable, so it goes.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    three = [{"number": n, "label": str(n), "name": f"Turn {n}", "apex_dist": 20.0 * n}
             for n in (1, 2, 3)]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": three}).status_code == 200
    _put_notes(client, {"2-3": "the chicane"})
    r = client.put("/tracks/algarve_international_circuit/map", json={"corners": [
        {"number": 1, "label": "1A", "name": "Turn 1A", "apex_dist": 20.0},
        {"number": 2, "label": "2A", "name": "Turn 2A", "apex_dist": 40.0},
        {"number": 3, "label": "3A", "name": "Turn 3A", "apex_dist": 60.0},
    ]})
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {}


def test_save_track_map_drops_range_keyed_notes_when_corner_count_changes(tmp_path, caplog):
    # GUARD (passes before the fix too). #37's renumbering-safety arm must treat a
    # range key no differently: a changed corner count drops per-corner notes
    # wholesale, because every label downstream of a re-split complex shifts.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    four = _four_corners()
    assert client.put("/tracks/algarve_international_circuit/map",
                      json={"corners": four}).status_code == 200
    _put_notes(client, {"2-4": "the fast left-right-left"})
    with caplog.at_level(logging.WARNING, logger="simcoach.server.app"):
        r = client.put("/tracks/algarve_international_circuit/map",
                       json={"corners": four[:3]})
    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {}
    assert "the fast left-right-left" in caplog.text   # recoverable from the log
    assert "corner count changed" in caplog.text
    assert written["notes"]["overview"] == "rhythm track"


def test_self_save_of_shipped_imola_keeps_every_authored_note(tmp_path):
    # The sharpest check available: hand the editor Imola's OWN shipped map straight
    # back. Imola's notes are range-keyed ('2-4', '5-6', '11-13', '14-15', '17-18'),
    # and this self-save silently deleted all five -- 1000 characters of authored
    # prose -- on every map edit made through the app.
    slug = "autodromo_enzo_e_dino_ferrari"
    src = Path(simcoach.__file__).parent / "tracks" / f"{slug}.json"
    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir()
    shutil.copy(src, tracks_dir / f"{slug}.json")   # a temp copy; never the repo file
    before = json.loads(src.read_text(encoding="utf-8"))
    assert any("-" in k for k in before["notes"]["corners"]), "fixture lost its ranges"
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    client = TestClient(create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks_dir)),
                        base_url="http://127.0.0.1")
    r = client.put(f"/tracks/{slug}/map", json={
        "corners": before["corners"], "sectors": before.get("sectors") or [],
        "sections": before.get("sections") or [], "name": before.get("name")})
    assert r.status_code == 200, r.text
    after = json.loads((tracks_dir / f"{slug}.json").read_text(encoding="utf-8"))
    assert after["notes"] == before["notes"]        # every authored note, verbatim
    # Otherwise a no-op. Not byte-identical only because a self-save re-bakes
    # radius_m on 7 corners to different values than the shipped file carries --
    # a separate, pre-existing geometry drift, unrelated to notes.
    assert sorted(k for k in set(before) | set(after)
                  if before.get(k) != after.get(k)) == ["corners"]
    assert {f for b, a in zip(before["corners"], after["corners"])
            for f in set(b) | set(a) if b.get(f) != a.get(f)} == {"radius_m"}


def test_save_track_map_has_notes_false_once_only_orphans_remained(tmp_path):
    # The Tracks screen must not keep claiming a track has notes when every
    # authored per-corner note was dropped as unbindable.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "", "corners": {"1": "brake late"}}})
    assert client.get("/tracks").json()[0]["has_notes"] is True
    assert client.put("/tracks/algarve_international_circuit/map", json={"corners": [
        {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 18.0},
        {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 24.0},
    ]}).status_code == 200
    assert client.get("/tracks").json()[0]["has_notes"] is False
    assert "notes" not in _written(tracks_dir)   # nothing authored left to keep


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


def test_save_track_map_bakes_geometry_and_digest_uses_it(tmp_path):
    slug = "algarve_international_circuit"
    n = 120
    R = 60.0
    t = np.linspace(0.0, np.pi, n)                       # semicircle -> radius ~R
    path = [[float(R * np.cos(a)), float(R * np.sin(a))] for a in t]
    dist = [float(R * a) for a in t]                     # arc length
    width = [12.0] * n
    bank = [float(np.radians(2.0))] * n
    s, apex, e = dist[n // 4], dist[n // 2], dist[3 * n // 4]

    tracks_dir = tmp_path / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)
    (tracks_dir / f"{slug}.json").write_text(json.dumps({
        "track": slug, "path": path, "path_dist": dist, "length": dist[-1],
        "ribbon": {"left": path, "right": path, "center": path, "width": width, "bank": bank},
        "corners": [{"number": 1, "label": "1", "name": "Turn 1", "direction": "left",
                     "start_dist": s, "apex_dist": apex, "end_dist": e, "apexes": [apex]}],
    }))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    fake = FakeModel('{"overview": "o", "corners": {}, "source": ""}')
    app = create_app(store, Coach(fake), tracks_dir=str(tracks_dir))
    c = TestClient(app, base_url="http://127.0.0.1")

    r = c.put(f"/tracks/{slug}/map", json={"corners": [
        {"number": 1, "label": "1", "name": "Turn 1",
         "apex_dist": apex, "apexes": [apex], "start_dist": s, "end_dist": e}]})
    assert r.status_code == 200, r.text
    cc = json.loads((tracks_dir / f"{slug}.json").read_text())["corners"][0]
    assert abs(cc["radius_m"] - R) < 8.0                 # radius from path curvature
    assert cc["width_m"] == 12.0                         # from the ribbon
    assert abs(cc["banking_deg"] - 2.0) < 0.2            # radians -> degrees

    d = c.post(f"/tracks/{slug}/draft-notes", json={"track": slug})
    assert d.status_code == 200
    prompt = fake.last_messages[0]["content"]            # the digest sent to the model
    assert "radius ~" in prompt and "width 12m" in prompt
