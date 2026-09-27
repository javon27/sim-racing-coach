# tests/test_server_tracks.py
import json
import logging
import os
import random
import shutil
import string
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import simcoach
from simcoach.analysis import corner_geometry, geometry_clause
from simcoach.coach import Coach
from simcoach.coach_tools import CoachContext
from simcoach.corners import CornerMap, in_window
from simcoach.model.fake import FakeModel
from simcoach.model_types import LapRecord
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.track_lookup import dump_track_json, load_map_data, note_key_binds
from tests.conftest import with_map_rev


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
        "track": "Algarve International Circuit", "rev": _rev(client),
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


def test_get_tracks_survives_a_map_file_name_with_nothing_to_slug(tmp_path):
    # A map file whose name slugs to "" ("---.json" in app-data tracks/) used to make the
    # capture lookup raise 400 for the WHOLE listing -- one odd file blanking the Tracks
    # screen, the same failure class as a corrupt lap file. It must count as no captures.
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    user_tracks = tmp_path / "data" / "tracks"
    user_tracks.mkdir(parents=True, exist_ok=True)
    (user_tracks / "---.json").write_text("{}", encoding="utf-8")
    r = client.get("/tracks")
    assert r.status_code == 200, r.text
    assert any(t["slug"] == "algarve_international_circuit" for t in r.json())


def test_put_notes_reference_times_roundtrip(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit", "rev": _rev(client),
        "notes": {"overview": "x", "reference_times": {"GT3": "1:32.500"}}})
    assert r.status_code == 200
    written = json.loads((tmp_path / "tracks" / "algarve_international_circuit.json").read_text())
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}


def test_get_notes_no_notes_returns_empty_shape(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/algarve_international_circuit/notes")
    assert r.status_code == 200
    assert {k: v for k, v in r.json().items() if k != "rev"} == {
        "overview": "", "corners": {}, "reference_times": {}, "source": ""}
    assert isinstance(r.json()["rev"], str)


def test_get_notes_missing_track_returns_empty_shape(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.get("/tracks/nonexistent_track_xyz/notes")
    assert r.status_code == 200
    assert {k: v for k, v in r.json().items() if k != "rev"} == {
        "overview": "", "corners": {}, "reference_times": {}, "source": ""}
    assert isinstance(r.json()["rev"], str)


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
    r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {
        "corners": [
            {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 50.0},
            {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 80.0},
        ]}))
    assert r.status_code == 200, r.text
    written = json.loads((tracks_dir / "algarve_international_circuit.json").read_text())
    assert len(written["corners"]) == 2
    # Direction is server-derived, never trusted from the client.
    assert all(c["direction"] in ("left", "right", "straight") for c in written["corners"])


def _written(tracks_dir):
    # UTF-8 explicitly: the writer emits raw UTF-8 (#145), and a bare read_text() is cp1252
    # on Windows.
    return json.loads((tracks_dir / "algarve_international_circuit.json").read_text(
        encoding="utf-8"))


_ALG = "/tracks/algarve_international_circuit"


def test_a_map_held_across_an_on_disk_rewrite_is_refused_not_reverted(tmp_path):
    # #166 F3: the editor loaded the map, the file was then rewritten on disk (a curation
    # script dropping corners), and Save sent the editor's OLD corner list back. The PUT
    # accepted it and the file silently reverted. Now the rev it was loaded with is stale.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    f = tracks_dir / "algarve_international_circuit.json"
    loaded = client.get(f"{_ALG}/map").json()
    old = [{"number": c["number"], "label": c["label"], "name": c["name"],
            "apex_dist": c["apex_dist"]} for c in loaded["corners"]]
    rewritten = json.loads(f.read_text())
    rewritten["corners"][0]["name"] = "Rewritten on disk"
    f.write_text(json.dumps(rewritten))
    before = f.read_bytes()
    for body in ({"corners": old, "rev": loaded["rev"]},   # stale -> 409
                 {"corners": old}):                        # absent -> 428
        r = client.put(f"{_ALG}/map", json=body)
        assert r.status_code == (409 if "rev" in body else 428), r.text
        if "rev" not in body:
            # Only an app window cached from before #166 omits it, and it shows this
            # detail verbatim: reopening the editor cannot fix that, a refresh does.
            assert "F5" in r.json()["detail"]
        assert f.read_bytes() == before                    # nothing written
    # Rotation writes the same file, so it takes the same guard.
    for body in ({"degrees": 90, "rev": loaded["rev"]}, {"degrees": 90}):
        r = client.post(f"{_ALG}/rotation", json=body)
        assert r.status_code == (409 if "rev" in body else 428), r.text
        assert f.read_bytes() == before
    # Reloading is the way out: the fresh rev saves.
    fresh = client.get(f"{_ALG}/map").json()
    assert fresh["rev"] != loaded["rev"]
    r = client.put(f"{_ALG}/map", json={"corners": old, "rev": fresh["rev"]})
    assert r.status_code == 200, r.text


def test_a_rewrite_landing_mid_build_stamps_the_payload_with_the_old_rev(tmp_path, monkeypatch):
    # The payload is built from several reads. A rewrite landing between them must leave
    # the payload with the rev read BEFORE the build -- so a save made from that payload
    # is refused -- never a stale corner list carrying the new file's rev.
    from simcoach.server import maps
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    f = tracks_dir / "algarve_international_circuit.json"
    base = json.loads(f.read_text())
    base["corners"] = [{"number": n, "label": str(n), "name": f"T{n}", "direction": "right",
                        "start_dist": a - 5, "apex_dist": a, "end_dist": a + 5}
                       for n, a in ((1, 20.0), (2, 60.0), (3, 90.0))]
    f.write_text(json.dumps(base))
    real = maps.build_track_map
    fired = []

    def rewrite_mid_build(channels, cmap, **kw):
        if not fired:
            fired.append(1)
            f.write_text(json.dumps({**base, "corners": base["corners"][:2]}))  # 3 -> 2
        return real(channels, cmap, **kw)

    monkeypatch.setattr(maps, "build_track_map", rewrite_mid_build)
    m = client.get(f"{_ALG}/map").json()
    monkeypatch.setattr(maps, "build_track_map", real)
    assert fired and len(m["corners"]) == 3            # the OLD map was served
    before = f.read_bytes()
    body = {"corners": [{k: c[k] for k in ("number", "label", "name", "apex_dist")}
                        for c in m["corners"]], "rev": m["rev"]}
    r = client.put(f"{_ALG}/map", json=body)
    assert r.status_code == 409, r.text
    assert f.read_bytes() == before                     # the 2-corner file survives


def test_each_map_write_hands_back_the_rev_the_next_one_needs(tmp_path):
    # The editor's Save is rotation THEN map: the map PUT must go out with the rev the
    # rotation returned, and a rev from before either write is stale after it.
    client, _ = _client_with_frozen_path(tmp_path)
    corners = [{"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 50.0}]
    r0 = client.get(f"{_ALG}/map").json()["rev"]
    r1 = client.post(f"{_ALG}/rotation", json={"degrees": 90, "rev": r0}).json()["rev"]
    assert r1 != r0
    assert client.put(f"{_ALG}/map", json={"corners": corners, "rev": r0}).status_code == 409
    r2 = client.put(f"{_ALG}/map", json={"corners": corners, "name": "Renamed", "rev": r1})
    assert r2.status_code == 200, r2.text
    assert r2.json()["rev"] == client.get(f"{_ALG}/map").json()["rev"]


def test_a_notes_save_does_not_stale_the_map_rev(tmp_path):
    # Every map writer carries the notes forward itself, so a notes save between loading
    # the map and saving it is not a conflict -- only a change to the map is.
    client, _ = _client_with_frozen_path(tmp_path)
    before = client.get(f"{_ALG}/map").json()["rev"]
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    assert client.get(f"{_ALG}/map").json()["rev"] == before


def _rev(client):
    """The notes rev PUT /notes requires -- what the Tracks screen got when it loaded them."""
    return client.get("/tracks/algarve_international_circuit/notes").json()["rev"]


def _put_notes(client, corners):
    return client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit", "rev": _rev(client),
        "notes": {"overview": "rhythm track", "corners": corners,
                  "reference_times": {"GT3": "1:32.500"}, "source": "me"}})


def test_save_track_map_by_slug_preserves_authored_notes(tmp_path):
    # Issue #37: re-saving a track's layout (PUT .../map) must not silently wipe
    # the authored notes overlay -- it's expensive to author. The no-op case: same
    # corner count, same labels -> everything is carried across unchanged.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    _put_notes(client, {"1": "brake late"})
    r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {
        "corners": [
            {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 50.0},
        ]}))
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
    # Both new corners sit more than 50 m from the old one's apex (50 m): no counterpart.
    # (They were at 18/24 m -- within the tolerance, so they would now be the same corner.)
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    _put_notes(client, {"1": "the slow left hairpin - huge patience"})
    with caplog.at_level(logging.WARNING, logger="simcoach.server.app"):
        r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {
            "corners": [
                {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 105.0},
                {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 115.0},
            ]}))
    assert r.status_code == 200, r.text
    # Not silent, and the dropped text is in the log so it can be recovered.
    assert "huge patience" in caplog.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {}          # not rebound to a new T1/T2
    assert written["notes"]["overview"] == "rhythm track"
    assert written["notes"]["reference_times"] == {"GT3": "1:32.500"}
    assert written["notes"]["source"] == "me"


def test_save_track_map_moves_corner_notes_onto_relabelled_corners(tmp_path):
    # Was "drops orphaned notes on relabel" (#37). GP-style relabelling ('2' -> '2A'/'2B')
    # leaves every corner where it was, so under #64 the corners are identified by apex
    # and the notes move WITH them -- the old rule deleted hand-authored prose about a
    # corner that still existed, just under a new name.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    three = [{"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 20.0},
             {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 50.0},
             {"number": 3, "label": "3", "name": "Turn 3", "apex_dist": 80.0}]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": three})).status_code == 200
    _put_notes(client, {"1": "brake late", "2": "middle of the chicane", "3": "flat in 6th"})
    r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": [
        {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 20.0},
        {"number": 2, "label": "2A", "name": "Turn 2A", "apex_dist": 50.0},
        {"number": 3, "label": "2B", "name": "Turn 2B", "apex_dist": 80.0},
    ]}))
    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {
        "1": "brake late", "2A": "middle of the chicane", "2B": "flat in 6th"}
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
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": four})).status_code == 200
    _put_notes(client, {"1": "brake late", "2-4": "the fast left-right-left"})
    assert client.put("/tracks/algarve_international_circuit/map",
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": four})).status_code == 200
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
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": three})).status_code == 200
    _put_notes(client, {"1-3": "the downhill complex"})
    r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": [
        {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 20.0},
        {"number": 2, "label": "2A", "name": "Turn 2A", "apex_dist": 40.0},
        {"number": 3, "label": "3", "name": "Turn 3", "apex_dist": 60.0},
    ]}))
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {"1-3": "the downhill complex"}


def test_save_track_map_narrows_a_range_note_whose_corners_were_all_renamed(tmp_path):
    # Was "drops a range note when no corner it names survives". Every corner it named
    # still exists (same apexes) under a non-numeric designation, which no single range
    # key can express -- so under #64 the note narrows onto the first of them instead of
    # being deleted. Under-serving authored prose is recoverable; deleting it is not.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    three = [{"number": n, "label": str(n), "name": f"Turn {n}", "apex_dist": 20.0 * n}
             for n in (1, 2, 3)]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": three})).status_code == 200
    _put_notes(client, {"2-3": "the chicane"})
    r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": [
        {"number": 1, "label": "1A", "name": "Turn 1A", "apex_dist": 20.0},
        {"number": 2, "label": "2A", "name": "Turn 2A", "apex_dist": 40.0},
        {"number": 3, "label": "3A", "name": "Turn 3A", "apex_dist": 60.0},
    ]}))
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {"2A": "the chicane"}


def test_save_track_map_drops_only_the_notes_of_a_deleted_corner(tmp_path, caplog):
    # Was "drops range-keyed notes when the corner count changes": a changed count
    # deleted every per-corner note on the track. Deleting corner 4 now drops only what
    # named corner 4 ALONE; '2-4' still names corners 2 and 3, the same apexes it was
    # written about, so it stays -- and a note about corner 4 alone goes, with its text.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    four = _four_corners()
    assert client.put("/tracks/algarve_international_circuit/map",
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": four})).status_code == 200
    _put_notes(client, {"2-4": "the fast left-right-left", "4": "the last corner"})
    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        r = client.put("/tracks/algarve_international_circuit/map",
                       json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": four[:3]}))
    assert r.status_code == 200, r.text
    written = _written(tracks_dir)
    assert written["notes"]["corners"] == {"2-4": "the fast left-right-left"}
    assert "the last corner" in caplog.text            # recoverable from the log
    assert "the corners they name no longer exist" in caplog.text
    assert written["notes"]["overview"] == "rhythm track"


def _put_map(client, pairs):
    """PUT the map as (label, apex) pairs, numbered in lap order -- the editor's shape."""
    return client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": [
        {"number": i, "label": lab, "name": f"Turn {lab}", "apex_dist": apex}
        for i, (lab, apex) in enumerate(pairs, 1)]}))


def test_deleting_a_middle_corner_logs_its_note_when_the_renumber_reuses_its_label(
        tmp_path, caplog):
    # Review blocker 1 on 5852270, the most ordinary editor flow: delete T2 and renumber
    # the rest down. T3's note correctly moves to '2' -- and the deleted T2's note, whose
    # old key is ALSO '2', vanished with no drop log, because the drops were computed by
    # comparing old keys against the new keys that landed. (The test above deletes the
    # LAST corner, so no label is reused -- the fixture shape that hid this.)
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_map(client, [(str(n), 20.0 * n) for n in range(1, 6)]).status_code == 200
    _put_notes(client, {"2": "T2 AT 40m - hairpin", "3": "T3 AT 60m - flat"})
    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        r = _put_map(client, [("1", 20.0), ("2", 60.0), ("3", 80.0), ("4", 100.0)])
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {"2": "T3 AT 60m - flat"}
    assert "dropped 1 authored" in caplog.text
    assert "T2 AT 40m - hairpin" in caplog.text


def test_a_deleted_corner_never_lends_its_note_to_a_corner_added_in_the_same_save(
        tmp_path, caplog):
    # Review blocker 2 on 5852270: delete a spurious T4 at 80 m and add a missing corner
    # at 10 m in ONE save, so the corner count is unchanged. The same-count position rule
    # handed T4's note to a live corner as authored fact -- (a) onto T3, (b) silently
    # replacing T3's own note, (c) with the labels renumbered, onto the 60 m corner, where
    # main does the same with no log at all. T4 has no counterpart now (its nearest corner
    # is already T3's exact match), so its note is dropped, loudly, in all three.
    four = [("1", 20.0), ("2", 40.0), ("3", 60.0), ("4", 80.0)]
    t4, t3 = "T4 AT 80m (being deleted)", "T3 AT 60m"
    untouched = [("0", 10.0), ("1", 20.0), ("2", 40.0), ("3", 60.0)]
    renumbered = [("1", 10.0), ("2", 20.0), ("3", 40.0), ("4", 60.0)]
    cases = {"a": (untouched, {"4": t4}, {}),
             "b": (untouched, {"4": t4, "3": t3}, {"3": t3}),
             "c": (renumbered, {"4": t4, "3": t3}, {"4": t3})}
    for name, (new_map, notes, expected) in cases.items():
        (tmp_path / name).mkdir()
        client, tracks_dir = _client_with_frozen_path(tmp_path / name)
        assert _put_map(client, four).status_code == 200
        _put_notes(client, notes)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
            r = _put_map(client, new_map)
        assert r.status_code == 200, (name, r.text)
        assert _written(tracks_dir)["notes"]["corners"] == expected, name
        assert "dropped 1 authored" in caplog.text, name
        assert t4 in caplog.text, name


def test_a_collision_keeps_the_note_already_on_that_corner_and_logs_the_other(
        tmp_path, caplog):
    # Two notes, one corner. Corner 3 (60 m) is deleted -- the new '3' at 110 m is a
    # different corner -- so '2-3' re-files onto '2', where T2's own note already is. The
    # note already filed under that corner keeps it; the one being moved there is dropped
    # AND logged. (5852270's position rule kept '2-3' bound to the 110 m corner instead.)
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_map(client, [("1", 20.0), ("2", 40.0), ("3", 60.0)]).status_code == 200
    _put_notes(client, {"2-3": "the 2-3 complex", "2": "T2 brake marker"})
    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        r = _put_map(client, [("1", 20.0), ("2", 40.0), ("3", 110.0)])
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {"2": "T2 brake marker"}
    assert "dropped 1 authored" in caplog.text
    assert "the 2-3 complex" in caplog.text


def test_a_dragged_apex_keeps_its_note_even_when_a_corner_is_added_elsewhere(tmp_path):
    # The curator drags T2's apex 4 m and adds a corner at 100 m in the same save. T2 is
    # still the same corner, so its note stays. 5852270 kept a moved corner's note only
    # when the corner count was unchanged, so the added corner cost T2 its note.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_map(client, [("1", 20.0), ("2", 40.0), ("3", 60.0)]).status_code == 200
    _put_notes(client, {"2": "late apex"})
    r = _put_map(client, [("1", 20.0), ("2", 44.0), ("3", 60.0), ("4", 100.0)])
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {"2": "late apex"}


def test_a_range_note_naming_only_deleted_corners_is_dropped_not_rebound(tmp_path, caplog):
    # Restores the server-level coverage 5852270 replaced. Corners 2 and 3 are deleted and
    # the lap renumbers, so '2-3' is a LIVE key again -- naming the 80 m and 100 m corners,
    # which it was never written about. It is dropped and logged; T4's note moves to '2'.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_map(client, [(str(n), 20.0 * n) for n in range(1, 6)]).status_code == 200
    _put_notes(client, {"2-3": "the chicane at 40-60 m", "4": "T4 at 80 m"})
    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        r = _put_map(client, [("1", 20.0), ("2", 80.0), ("3", 100.0)])
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {"2": "T4 at 80 m"}
    assert "the chicane at 40-60 m" in caplog.text
    assert "the corners they name no longer exist" in caplog.text


def _labels(tracks_dir):
    return [c["label"] for c in _written(tracks_dir)["corners"]]


def test_a_blank_label_added_after_a_delete_never_duplicates_a_curator_label(tmp_path):
    # #79, the editor's delete+add. Deleting T3 makes renumber() shift T4/T5 down to
    # numbers 3/4, but their labels stay "4"/"5". A corner added at 90 m is then number 4
    # with a blank label. The server filled that blank with str(number), a second "4".
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_map(client, [(str(n), 20.0 * n) for n in range(1, 6)]).status_code == 200
    _put_notes(client, {"2": "T2 note", "4": "T4 note", "5": "T5 note"})
    r = _put_map(client, [("1", 20.0), ("2", 40.0), ("4", 80.0), ("", 90.0), ("5", 100.0)])
    assert r.status_code == 200, r.text
    assert _labels(tracks_dir) == ["1", "2", "4", "4A", "5"]   # curator labels as sent
    # The curator's corners keep their notes, and the new corner gets none of them.
    assert _written(tracks_dir)["notes"]["corners"] == {
        "2": "T2 note", "4": "T4 note", "5": "T5 note"}
    assert r.json()["dropped_notes"] == {}
    cmap = CornerMap.load(str(tracks_dir / "algarve_international_circuit.json"))
    assert [c.apex_dist for c in cmap.corners if c.label == "4"] == [80.0]   # one T4
    ctx = CoachContext(store=None, cmap=cmap, track="Algarve", car="GT3")
    assert ctx._corner_note("4") == "T4 note"
    assert ctx._corner_note("4A") is None


def test_a_blank_label_does_not_join_a_range_note_about_other_corners(tmp_path):
    # T4 is deleted, and a corner added at 95 m takes its number. No corner is labelled
    # "4" any more, but '3-5' still names it. Filling "4" would put the new corner inside
    # a note written about T3-T5, and the guard would narrow that note to T3, so T5 loses
    # it. Old T4 (40 m) is 55 m from the new corner, beyond the 50 m same-corner
    # tolerance, so the outcome does not depend on the computed directions.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_map(client, [("1", 10.0), ("2", 20.0), ("3", 30.0), ("4", 40.0),
                             ("5", 110.0)]).status_code == 200
    _put_notes(client, {"3-5": "the complex"})
    r = _put_map(client, [("1", 10.0), ("2", 20.0), ("3", 30.0), ("", 95.0), ("5", 110.0)])
    assert r.status_code == 200, r.text
    assert _labels(tracks_dir) == ["1", "2", "3", "4A", "5"]
    assert _written(tracks_dir)["notes"]["corners"] == {"3-5": "the complex"}


def test_a_label_less_resave_keeps_number_labels_and_their_notes(tmp_path):
    # A blank label on a corner that has not moved still means "use the number". The
    # corner's own notes must not reserve its number away from it, or a re-save without
    # labels would relabel every noted corner 'NA' and move every note.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert client.put("/tracks/algarve_international_circuit/map",
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": _four_corners()})).status_code == 200
    _put_notes(client, {"1": "brake late", "2-4": "the fast left-right-left"})
    r = client.put("/tracks/algarve_international_circuit/map",
                   json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": [dict(c, label="") for c in _four_corners()]}))
    assert r.status_code == 200, r.text
    assert _labels(tracks_dir) == ["1", "2", "3", "4"]
    assert _written(tracks_dir)["notes"]["corners"] == {
        "1": "brake late", "2-4": "the fast left-right-left"}


def test_a_blank_label_with_no_free_candidate_is_refused_not_guessed(tmp_path):
    # "2" belongs to a curator corner and notes name "2A".."2Z", so no label is free.
    # The save must return a 422 asking for a label, not a 500 or a duplicate.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_map(client, [("1", 20.0), ("2", 40.0), ("3", 60.0)]).status_code == 200
    _put_notes(client, {f"2{ch}": "x" for ch in string.ascii_uppercase})
    before = _written(tracks_dir)
    r = _put_map(client, [("1", 20.0), ("", 30.0), ("2", 40.0), ("3", 60.0)])
    assert r.status_code == 422 and "give it a label" in r.text, r.text
    assert _written(tracks_dir) == before


def test_save_track_map_accepts_corner_labels_int_cannot_parse(tmp_path):
    # A corner label is a FREE-TEXT box in the map editor (TrackMapEditor.svelte) and
    # the endpoint only truncates it, so typing '²' U+00B2 is a pure-UI action. '²',
    # '₂' U+2082 and '⑥' U+2465 all satisfy str.isdigit() and RAISE on int(), so the
    # note guard's range check turned one into an HTTP 500 on save. Worse, whether you
    # got the 500 or a 200 that persisted the label depended on `set` iteration order
    # reaching an ASCII label first -- the label then poisoned every later read.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    odd = [{"number": n, "label": lab, "name": f"Turn {lab}", "apex_dist": 20.0 * n}
           for n, lab in ((1, "1"), (2, "2"), (3, "²"), (4, "₂"), (5, "⑥"))]
    assert client.put("/tracks/algarve_international_circuit/map",
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": odd})).status_code == 200
    # A NON-binding range scans every label, so it always reaches the odd ones: the
    # deterministic 500. ('1-3' below instead short-circuits on ASCII '1' and saved a
    # 200, which is the worse half of the same bug -- which you got depended on `set`
    # iteration order.)
    _put_notes(client, {"90-95": "nothing here"})
    r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": odd}))
    assert r.status_code == 200, r.text
    _put_notes(client, {"1-3": "the opening complex", "²": "the squared one"})
    r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": odd}))
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {
        "1-3": "the opening complex", "²": "the squared one"}
    # And the coach can still read every one of those labels back off the saved map.
    cmap = CornerMap.load(str(tracks_dir / "algarve_international_circuit.json"))
    ctx = CoachContext(store=None, cmap=cmap, track="Algarve", car="GT3")
    assert ctx._corner_note("²") == "the squared one"
    assert ctx._corner_note("2") == "the opening complex"
    assert ctx._corner_note("⑥") is None
    assert "the squared one" in ctx.get_track_notes(corner="²")


def test_save_track_map_survives_an_overlong_note_key_on_disk(tmp_path):
    # CPython caps int()/str() conversion at 4300 digits, so a 4301-digit note key made
    # every map save return 500 PERMANENTLY, with GET .../notes still 200 so the editor
    # looked fine. The key is reached before any comparison, so it also killed the
    # coach's read of corners that have nothing to do with it.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    four = _four_corners()
    assert client.put("/tracks/algarve_international_circuit/map",
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": four})).status_code == 200
    poison = "1-" + "9" * 5000          # well past the 4300-digit int() threshold
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    # ON DISK, behind the endpoint: `PUT /notes` rejects a key this long now (see
    # test_put_notes_rejects_unbounded_input), but a file written before that cap
    # existed -- which is how this got reported -- still has to be survivable.
    data = _written(tracks_dir)
    data["notes"]["corners"][poison] = "nonsense"
    # Same class, same line of defence: a note VALUE that isn't a str. `PUT /notes`
    # coerces with str(v), so these only arrive from a legacy, hand-edited or promoted
    # file -- and the drop-path log's `v[:200]` made one a 500 on every later map save.
    data["notes"]["corners"]["88"] = 7
    data["notes"]["corners"]["89"] = ["x" * 10_000] * 1000
    (tracks_dir / "algarve_international_circuit.json").write_text(json.dumps(data))
    r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": four}))
    assert r.status_code == 200, r.text
    # The poison key names nothing, so it is dropped; the real note is kept.
    assert _written(tracks_dir)["notes"]["corners"] == {"1": "brake late"}
    cmap = CornerMap.load(str(tracks_dir / "algarve_international_circuit.json"))
    ctx = CoachContext(store=None, cmap=cmap, track="Algarve", car="GT3")
    assert ctx._corner_note("1") == "brake late"    # ordinary labels still resolve
    assert ctx._corner_note("99") is None


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
    r = client.put(f"/tracks/{slug}/map", json=with_map_rev(client, f"/tracks/{slug}/map", {
        "corners": before["corners"], "sectors": before.get("sectors") or [],
        "sections": before.get("sections") or [], "name": before.get("name")}))
    assert r.status_code == 200, r.text
    after = json.loads((tracks_dir / f"{slug}.json").read_text(encoding="utf-8"))
    assert after["notes"] == before["notes"]        # every authored note, verbatim
    # And a no-op in FULL. This used to have to carve out radius_m, which the save
    # re-baked on 7 of Imola's corners (T13: 67.7 m -> 322.1 m) -- a separate defect
    # from the note loss, fixed in #62, so the carve-out is gone and the whole map
    # round-trips. tests/test_map_save_geometry.py owns that rule across every map.
    assert after == before


def test_editor_delete_and_blank_add_never_duplicates_a_label_on_shipped_maps(tmp_path):
    # #79's red-team probe, sampled. On every shipped map with notes: delete a corner, add
    # a blank-label corner near another, renumber by lap order as the editor does, and
    # save. Edit 1 is fixed and collides on main (delete T1, add 20 m past T4: the new
    # corner is number 4 next to the curator's "4"). Edit 2 is a seeded random pick.
    shipped = Path(simcoach.__file__).parent / "tracks"
    rng = random.Random(79)
    saved = []
    for src in sorted(shipped.glob("*.json")):
        data = json.loads(src.read_text(encoding="utf-8"))
        corners = data.get("corners") or []
        if not ((data.get("notes") or {}).get("corners") and data.get("path")):
            continue
        length = data["path_dist"][-1]
        edits = [(0, 3, 20.0), (rng.randrange(len(corners)), rng.randrange(len(corners)),
                                rng.choice((-35.0, -20.0, 20.0, 35.0)))]
        for n, (i, j, off) in enumerate(edits):
            apex = round(min(max(corners[j]["apex_dist"] + off, 0.0), length), 1)
            sent = sorted([dict(c) for k, c in enumerate(corners) if k != i]
                          + [{"label": "", "name": "", "apex_dist": apex}],
                          key=lambda c: c["apex_dist"])
            for num, c in enumerate(sent, 1):
                c["number"] = num
            tracks_dir = tmp_path / f"{src.stem}-{n}"
            tracks_dir.mkdir()
            shutil.copy(src, tracks_dir / src.name)
            store = LapStore(str(tracks_dir / "c.sqlite"), str(tracks_dir / "data"))
            client = TestClient(create_app(store, Coach(FakeModel("{}")),
                                           tracks_dir=str(tracks_dir)),
                                base_url="http://127.0.0.1")
            r = client.put(f"/tracks/{src.stem}/map", json=with_map_rev(client, f"/tracks/{src.stem}/map", {"corners": sent}))
            assert r.status_code == 200, (src.stem, i, j, off, r.text)
            after = json.loads((tracks_dir / src.name).read_text(encoding="utf-8"))
            labels = [c["label"] for c in after["corners"]]
            assert len(set(labels)) == len(labels), (src.stem, i, j, off, labels)
            # Curator labels untouched (by apex), and every note kept still names a corner.
            by_apex = {c["apex_dist"]: c["label"] for c in after["corners"]}
            assert all(by_apex[round(c["apex_dist"], 1)] == c["label"]
                       for c in sent if c["label"]), (src.stem, labels)
            for key in (after.get("notes") or {}).get("corners") or {}:
                assert note_key_binds(key, set(labels)), (src.stem, key)
            saved.append(src.stem)
    assert {"weathertech_raceway_laguna_seca", "bahrain_international_circuit",
            "algarve_international_circuit"} <= set(saved), "fixture lost the reported maps"


def test_save_track_map_has_notes_false_once_only_orphans_remained(tmp_path):
    # The Tracks screen must not keep claiming a track has notes when every
    # authored per-corner note was dropped as unbindable.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit", "rev": _rev(client),
        "notes": {"overview": "", "corners": {"1": "brake late"}}}).status_code == 200
    assert client.get("/tracks").json()[0]["has_notes"] is True
    # Both new corners more than 50 m from the old apex (50 m), so '1' has no counterpart.
    assert client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": [
        {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 105.0},
        {"number": 2, "label": "2", "name": "Turn 2", "apex_dist": 115.0},
    ]})).status_code == 200
    assert client.get("/tracks").json()[0]["has_notes"] is False
    assert "notes" not in _written(tracks_dir)   # nothing authored left to keep


def test_save_track_map_by_slug_rejects_empty_corners(tmp_path):
    client, _ = _client_with_frozen_path(tmp_path)
    r = client.put("/tracks/algarve_international_circuit/map", json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": []}))
    assert r.status_code == 422


def test_track_rotation_by_slug_persists(tmp_path):
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    r = client.post("/tracks/algarve_international_circuit/rotation", json=with_map_rev(client, "/tracks/algarve_international_circuit/rotation", {"degrees": 90}))
    assert r.status_code == 200
    written = json.loads((tracks_dir / "algarve_international_circuit.json").read_text())
    assert written["rotation"] == 90.0


def test_put_then_get_notes_roundtrip(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    assert client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit", "rev": _rev(client),
        "notes": {"overview": "rhythm track", "corners": {"1": "brake late"},
                   "reference_times": {"GT3": "1:32.500"}, "source": "me"}}).status_code == 200
    r = client.get("/tracks/algarve_international_circuit/notes")
    assert r.status_code == 200
    assert {k: v for k, v in r.json().items() if k != "rev"} == {
        "overview": "rhythm track", "corners": {"1": "brake late"},
        "reference_times": {"GT3": "1:32.500"}, "source": "me"}


def test_a_notes_save_needs_the_rev_the_notes_were_loaded_with(tmp_path):
    # PUT /notes writes its body verbatim, so it must know the body is not stale. No rev
    # is refused (428), a wrong one is refused (409), neither writes anything, and a good
    # save hands back the rev to send next time.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    before = _written(tracks_dir)
    body = {"track": "Algarve International Circuit",
            "notes": {"overview": "an edit", "corners": {}}}
    url = "/tracks/algarve_international_circuit/notes"
    assert client.put(url, json=body).status_code == 428
    assert client.put(url, json={**body, "rev": "0" * 16}).status_code == 409
    assert _written(tracks_dir) == before
    r = client.put(url, json={**body, "rev": _rev(client)})
    assert r.status_code == 200, r.text
    assert r.json()["rev"] == _rev(client)
    assert _written(tracks_dir)["notes"]["overview"] == "an edit"


def test_a_stale_tracks_screen_cannot_put_re_keyed_notes_back(tmp_path):
    # Red-team #3 through the real routes: the Tracks screen loads the notes, the map
    # editor (a modal on that same screen) relabels every corner +2 and the note moves
    # with its corner, then the screen saves the copy it loaded before -- with an
    # unrelated overview edit. That used to put the old key back, where it names the
    # first two corners of the lap as authored fact, with no orphan warning (the old key
    # matches the new labels). Refused now, and the moved note stays where it is.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_map(client, [("1", 20.0), ("2", 40.0), ("3", 60.0), ("4", 80.0)]).status_code == 200
    assert _put_notes(client, {"3-4": "THE LAST CHICANE"}).status_code == 200
    held = client.get("/tracks/algarve_international_circuit/notes").json()
    assert _put_map(client, [("3", 20.0), ("4", 40.0), ("5", 60.0), ("6", 80.0)]).status_code == 200
    assert _written(tracks_dir)["notes"]["corners"] == {"5-6": "THE LAST CHICANE"}
    held["overview"] = "an unrelated edit"
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit", "notes": held, "rev": held["rev"]})
    assert r.status_code == 409, r.text
    assert _written(tracks_dir)["notes"]["corners"] == {"5-6": "THE LAST CHICANE"}


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

    r = c.put(f"/tracks/{slug}/map", json=with_map_rev(c, f"/tracks/{slug}/map", {"corners": [
        {"number": 1, "label": "1", "name": "Turn 1",
         "apex_dist": apex, "apexes": [apex], "start_dist": s, "end_dist": e}]}))
    assert r.status_code == 200, r.text
    cc = json.loads((tracks_dir / f"{slug}.json").read_text())["corners"][0]
    assert abs(cc["radius_m"] - R) < 8.0                 # radius from path curvature
    assert cc["width_m"] == 12.0                         # from the ribbon
    assert abs(cc["banking_deg"] - 2.0) < 0.2            # radians -> degrees

    d = c.post(f"/tracks/{slug}/draft-notes", json={"track": slug})
    assert d.status_code == 200
    prompt = fake.last_messages[0]["content"]            # the digest sent to the model
    assert "banked" in prompt or "off-camber" in prompt   # banking still reaches the digest
    assert "radius ~" not in prompt and "width 12m" not in prompt   # #99: stale bakes cut


def test_put_notes_rejects_unbounded_input(tmp_path):
    # `PUT /tracks/{slug}/notes` validated NOTHING: no key count, no key length, no text
    # length, so a 2,000,000-character note body stored verbatim into the track file and
    # was carried across every later map save. The adjacent corner NAME has been capped
    # at 80 chars since this path was written -- this was the one endpoint on it with no
    # bounds at all. Over-limit input is REJECTED, never truncated: a silently shortened
    # note is the class of bug this whole area just got fixed for.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    keep = _written(tracks_dir)["notes"]

    over_limit = (
        {str(n): "x" for n in range(101)},     # 101 keys  (cap: 100, the corner cap)
        {"k" * 81: "x"},                       # 81-char key (cap: 80, the name cap)
        {"1": "x" * 2001},                     # 2001-char note (cap: 2000, ~300 words)
    )
    for corners in over_limit:
        r = _put_notes(client, corners)
        assert r.status_code == 422, r.text
        assert _written(tracks_dir)["notes"] == keep    # nothing persisted

    # overview/source are text on the same body, bounded the same way.
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "o" * 2001, "corners": {}}})
    assert r.status_code == 422, r.text
    assert _written(tracks_dir)["notes"] == keep

    # At the key-count, key-length and per-note limits SIMULTANEOUSLY still succeeds -- the
    # caps sit well clear of authored prose (the drafting prompt's budgets are 120 words of
    # overview, 50 words per corner). Not every cap at once, which is unsatisfiable by
    # design: the document cap below makes 100 maxed notes mutually exclusive with each
    # other. One maxed note on the longest allowed key, and 99 short ones.
    at_limit = {**{str(n): "y" for n in range(1, 100)}, "k" * 80: "z" * 2000}
    assert len(at_limit) == 100
    assert _put_notes(client, at_limit).status_code == 200
    assert _written(tracks_dir)["notes"]["corners"] == at_limit


def test_put_notes_refuses_an_oversized_notes_document(tmp_path):
    # Every field can pass its own cap and the DOCUMENT still be enormous: 100 corner keys
    # plus 100 reference times, each at the 2000-char field cap, put a 5 MB body on disk --
    # 2.5x the single-field abuse the per-field caps were added to stop, just spread across
    # 200 keys -- and every later map save re-read and re-wrote it.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    keep = _written(tracks_dir)["notes"]
    fat = {str(n): "x" * 2000 for n in range(1, 41)}   # 40 x 2000 > the 64,357-byte cap
    r = _put_notes(client, fat)
    assert r.status_code == 422, r.text
    assert _written(tracks_dir)["notes"] == keep       # authored notes untouched

    # The cap counts the UTF-8 bytes _write_track_json writes, not code points: an astral
    # char is 4 bytes, so 31 all-emoji notes (62,000 code points, under a code-point cap)
    # are 248,000+ bytes on disk. (Before #145 the writer escaped it to 12 bytes and wrote
    # 744,367.)
    emoji = {str(n): "\U0001f600" * 2000 for n in range(1, 32)}
    assert all(len(v) <= 2000 for v in emoji.values())   # every field within its own cap
    r = _put_notes(client, emoji)
    assert r.status_code == 422, r.text
    assert _written(tracks_dir)["notes"] == keep

    # ...and it counts the RAW UTF-8 the writer now emits, not the \uXXXX escapes it used
    # to (#145): 1000 x "é" is 2000 bytes on disk, so these 30 notes are byte-for-byte the
    # size of the ASCII ceiling below and fit. Measured escaped (6 bytes each) they would
    # be refused at ~184 KB -- a cap bounding bytes that are never written.
    accented = {str(n): "é" * 1000 for n in range(1, 31)}
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit", "rev": _rev(client),
        "notes": {"overview": "o" * 2000, "corners": accented,
                  "reference_times": {}, "source": "s" * 2000}})
    assert r.status_code == 200, r.text
    raw = (tracks_dir / "algarve_international_circuit.json").read_bytes()
    assert ("é" * 1000).encode("utf-8") in raw and b"\\u00e9" not in raw

    # And the ceiling the constant is derived from is genuinely accepted: 30 notes at the
    # full per-note cap plus overview and source at theirs. A round 64,000 would refuse it.
    ceiling = {str(n): "x" * 2000 for n in range(1, 31)}
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit", "rev": _rev(client),
        "notes": {"overview": "o" * 2000, "corners": ceiling,
                  "reference_times": {}, "source": "s" * 2000}})
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"]["corners"] == ceiling


def test_put_notes_refuses_a_non_dict_corners_map(tmp_path):
    # `corners` as a list used to raise AttributeError -- a 500 that wrote NOTHING. The
    # first cut of the caps above coerced a non-dict to empty instead, which returned 200
    # and wiped the authored notes already on disk: a crash that preserves data beats a
    # success that destroys it. This write REPLACES what is on disk, so it must refuse.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_notes(client, {"1": "brake late", "2": "turn in early"}).status_code == 200
    keep = _written(tracks_dir)["notes"]
    assert len(keep["corners"]) == 2
    for bad in ([["1", "brake late"]], "1: brake late", 7):
        r = _put_notes(client, bad)
        assert r.status_code == 422, r.text
        assert _written(tracks_dir)["notes"] == keep    # authored notes untouched


def test_dropped_note_log_is_bounded(tmp_path, caplog):
    # The drop-path warning json.dumps'd every dropped note IN FULL: a probe that stored
    # a large notes object made one warning() call write 4.6 MB. A log sink is not a
    # place unbounded user input gets to land, so it logs a bounded sample now -- the
    # exact count, plus enough of the keys/prose to re-author from.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    four = _four_corners()
    assert client.put("/tracks/algarve_international_circuit/map",
                      json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": four})).status_code == 200
    # THE most PUT /notes accepts at ~2000 chars a note: 31 of them, ~62 KB, just inside the
    # 64,357-byte document cap (32 is refused). That is the ceiling on how much this one log
    # call can ever be asked to write from this path -- and the bound is structural anyway,
    # a fixed 20 keys x 200 chars, so no accepted payload of any size or encoding moves it.
    assert _put_notes(client, {str(n): f"note {n} " + "x" * 1980
                               for n in range(1, 32)}).status_code == 200
    # The warning now comes from simcoach.track_lookup: the bound lives in
    # `guard_corner_notes`, which the promote script shares with this save path.
    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        r = client.put("/tracks/algarve_international_circuit/map",
                       json=with_map_rev(client, "/tracks/algarve_international_circuit/map", {"corners": four[:3]}))      # corner 4 deleted
    assert r.status_code == 200, r.text
    # Corners 1-3 kept their apexes, so their notes stay, TEXT AND ALL (#64); '4'..'31'
    # name nothing that exists and all 28 are dropped -- well past the 20-key bound.
    assert _written(tracks_dir)["notes"]["corners"] == {
        str(n): f"note {n} " + "x" * 1980 for n in (1, 2, 3)}
    (rec,) = [x for x in caplog.records if "dropped" in x.getMessage()]
    msg = rec.getMessage()
    assert len(msg) < 10_000, f"warning record is {len(msg)} chars"   # was the whole ~62 KB
    assert "dropped 28 authored" in msg           # the exact count still reported
    assert '"10": "note 10 ' in msg               # and a sample is still recoverable


# --- /tracks lists driven-but-unmapped tracks (#69) + capture coverage (#66) ----------

def _live_lap(store, name: str, n: int = 1, *, pos: bool = True, pos_y: bool = True) -> None:
    # A LIVE-captured lap: store.save writes the .npz and sets telemetry_path, which is
    # what separates a re-processable lap from a result-log import.
    #
    # pos=False is a PRE-POSITION lap, the shape this store really holds for laps recorded
    # before position capture landed: a real .npz with real channels and no pos_* at all.
    # It can never be rebuilt from, so it must not count toward the build gate.
    # pos_y=False is the other real shape: position but no elevation.
    for _ in range(n):
        ch = {"lap_distance": np.array([0.0, 1.0]), "speed": np.array([100.0, 100.0])}
        if pos:
            ch["pos_x"] = np.array([0.0, 1.0])
            ch["pos_z"] = np.array([0.0, 0.0])
            if pos_y:
                ch["pos_y"] = np.array([0.0, 0.0])
        store.save(LapRecord("lmu", name, "Oreca 07", 60.0, True, 0.0, ch))


def _result_lap(store, name: str, n: int = 1) -> None:
    # A result-log import: summary row, telemetry_path NULL, no point cloud. Inserted
    # directly so the test asserts on the column list_tracks actually reads.
    for _ in range(n):
        store.conn.execute(
            "INSERT INTO laps (ts, sim, track, car, lap_time, valid, telemetry_path) "
            "VALUES (0, 'lmu', ?, 'Oreca 07', 100.0, 1, NULL)", (name,))
    store.conn.commit()


def test_get_tracks_lists_driven_track_with_no_map_file(tmp_path):
    # The #69 case: laps driven live, point clouds on disk, no <slug>.json -- the row has
    # to exist (it is the only place a map can be built) with has_map False.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    _live_lap(store, "Silverstone National Circuit", 3)
    rows = client.get("/tracks").json()
    row = next(t for t in rows if t["slug"] == "silverstone_national_circuit")
    assert row["has_map"] is False
    assert row["captures"] == 3                  # not 0, and not collapsed to a bool
    assert row["name"] == "Silverstone National Circuit"
    assert row["has_notes"] is False and row["user_edited"] is False


def test_get_tracks_result_log_only_track_is_not_buildable(tmp_path):
    # Spa's 991 imported laps carry no cloud: a big lap count must NOT make a track look
    # buildable. The live track in the same store is the positive control -- without it
    # this test would also pass if the union were dropped entirely.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    _result_lap(store, "Circuit de Spa-Francorchamps", 991)
    _live_lap(store, "Silverstone National Circuit", 2)
    slugs = [t["slug"] for t in client.get("/tracks").json()]
    assert "silverstone_national_circuit" in slugs          # control: the union works
    assert "circuit_de_spa_francorchamps" not in slugs      # the claim


def test_get_tracks_mapped_track_row_shape_unchanged(tmp_path):
    # A track with a map file keeps the row it always had, and gains `captures` as an
    # int. Asserted as an exact key set so a renamed/dropped field is caught here.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    _result_lap(store, "Algarve International Circuit", 40)
    rows = client.get("/tracks").json()
    row = next(t for t in rows if t["slug"] == "algarve_international_circuit")
    assert set(row) == {"slug", "name", "has_map", "has_notes", "user_edited",
                        "captures", "live_laps", "capture_unreadable"}
    assert row["capture_unreadable"] is False
    assert row["has_map"] is True
    assert row["has_notes"] is False
    assert row["user_edited"] is False
    assert row["captures"] == 0        # 40 result laps are not captures
    assert row["live_laps"] == 0


def test_tracks_cloud_check_cache_skips_unchanged_files(tmp_path, monkeypatch):
    # #105: GET /tracks used to np.load every live lap's .npz on every open, however
    # often the screen is revisited. Cache the per-lap usable-cloud check on
    # (telemetry_path, mtime) so a re-open only reads files that changed.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    _live_lap(store, "Silverstone National Circuit", 2)

    calls: list[str] = []
    real_load = np.load

    def counting_load(path, *a, **kw):
        calls.append(str(path))
        return real_load(path, *a, **kw)

    monkeypatch.setattr("simcoach.server.maps.np.load", counting_load)

    r1 = client.get("/tracks")
    assert r1.status_code == 200
    assert len(calls) == 2   # both live laps read on the first open

    r2 = client.get("/tracks")
    assert r2.status_code == 200
    assert len(calls) == 2   # unchanged files: nothing re-read
    row = next(t for t in r2.json() if t["slug"] == "silverstone_national_circuit")
    assert row["captures"] == 2   # count semantics identical to the uncached path

    # Touch one lap's file forward so only it looks changed.
    (path,) = store.conn.execute(
        "SELECT telemetry_path FROM laps WHERE track = 'Silverstone National Circuit' "
        "ORDER BY id LIMIT 1").fetchone()
    touched = Path(path)
    new_mtime = touched.stat().st_mtime + 5
    os.utime(touched, (new_mtime, new_mtime))

    r3 = client.get("/tracks")
    assert r3.status_code == 200
    assert len(calls) == 3   # only the touched file was re-read


def test_get_tracks_driven_mapped_track_listed_once_with_count(tmp_path):
    # The union must dedupe: a track that has BOTH a map file and live laps is one row
    # carrying the count, never two rows.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    _live_lap(store, "Algarve International Circuit", 5)
    rows = [t for t in client.get("/tracks").json()
            if t["slug"] == "algarve_international_circuit"]
    assert len(rows) == 1
    assert rows[0]["has_map"] is True and rows[0]["captures"] == 5


def test_get_tracks_sums_captures_across_raw_names_of_one_slug(tmp_path):
    # Live names differ by championship suffix ("- ELMS") while track_slug collapses them
    # to one file/slug. The count is per SLUG, so 2 + 3 is one row of 5, not a row of 3
    # that silently drops the other session.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    _live_lap(store, "Paul Ricard", 2)
    _live_lap(store, "Paul Ricard - ELMS", 3)
    rows = [t for t in client.get("/tracks").json() if t["slug"] == "paul_ricard"]
    assert len(rows) == 1
    assert rows[0]["captures"] == 5


def test_get_tracks_counts_only_laps_whose_npz_really_has_position(tmp_path):
    # THE dead-button bug. Position capture landed partway through this store's history,
    # so pre-position laps are real .npz files with real channels and no pos_* at all.
    # A row-count badge made Sebring (15 live, 0 usable) offer "Build map from 15 laps",
    # which then 422'd -- the very error #69 exists to remove. `captures` is the gate and
    # must count USABLE laps; `live_laps` keeps the row explainable.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    _live_lap(store, "Sebring International Raceway", 15, pos=False)
    _live_lap(store, "Silverstone National Circuit", 4, pos=False)
    _live_lap(store, "Silverstone National Circuit", 3)          # 7 rows, 3 usable
    rows = client.get("/tracks").json()

    sebring = next(t for t in rows if t["slug"] == "sebring_international_raceway")
    assert sebring["live_laps"] == 15      # the row exists, and explains itself...
    assert sebring["captures"] == 0        # ...but offers nothing to build from

    silver = next(t for t in rows if t["slug"] == "silverstone_national_circuit")
    assert silver["live_laps"] == 7
    assert silver["captures"] == 3         # the usable subset, not the row count


def test_get_tracks_counts_laps_without_pos_y_as_buildable(tmp_path):
    # pos_y only feeds banking, which ribbon.py now writes as unknown wherever the cloud
    # lacks lateral spread -- so a lap without elevation is still one a Build consumes.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    _live_lap(store, "Monza Curva Grande Circuit", 4, pos_y=False)
    _live_lap(store, "Monza Curva Grande Circuit", 1)            # 5 usable, 1 with pos_y
    _live_lap(store, "Paul Ricard", 3, pos_y=False)              # 3 usable, 0 with pos_y
    rows = client.get("/tracks").json()

    monza = next(t for t in rows if t["slug"] == "monza_curva_grande_circuit")
    assert monza["captures"] == 5           # pos_y is NOT required to rebuild

    ricard = next(t for t in rows if t["slug"] == "paul_ricard")
    assert ricard["captures"] == 3


def test_get_tracks_counts_survive_a_deleted_npz(tmp_path):
    # A laps row can outlive its file. That lap is unusable, but it is still a live row.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    _live_lap(store, "Silverstone National Circuit", 3)
    (Path(store.data_dir) / "1.npz").unlink()
    row = next(t for t in client.get("/tracks").json()
               if t["slug"] == "silverstone_national_circuit")
    assert row["live_laps"] == 3
    assert row["captures"] == 2


# --- #69 round 2: bad files, badge == build, no frozen capture, the width/bank gate ----

_SLUG = "silverstone_national_circuit"
_NAME = "Silverstone National Circuit"


def _cloud_lap(store, zoff=0.0, y=None, *, n=2000):
    # A 400 m straight LIVE lap at lateral offset `zoff`, dense enough (5 samples per
    # 0.8 m rebuild bin) that every bin holds a sample of every lap. `y` = its pos_y.
    x = np.linspace(0.0, 400.0, n)
    ch = {"pos_x": x, "pos_z": np.full(n, float(zoff)), "lap_distance": x.copy(),
          "speed": np.full(n, 100.0)}
    if y is not None:
        ch["pos_y"] = np.full(n, float(y))
    return store.save(LapRecord("lmu", _NAME, "Oreca 07", 60.0, True, 0.0, ch))


def _row(client):
    r = client.get("/tracks")
    assert r.status_code == 200, r.text
    return next(t for t in r.json() if t["slug"] == _SLUG)


def _rebuild(client):
    r = client.post("/mapcapture/rebuild", json={"slug": _SLUG})
    assert r.status_code == 200, r.text
    return r.json()


def _corrupt_member(raw: bytes) -> bytes:
    # Damage pos_x's DEFLATE stream while the zip directory stays intact, so np.load
    # opens the file and the failure only surfaces reading the member: zlib.error.
    import io
    import zipfile
    zi = zipfile.ZipFile(io.BytesIO(raw)).getinfo("pos_x.npy")
    body = zi.header_offset + 30 + len(zi.filename) + len(zi.extra)
    out = bytearray(raw)
    for i in range(body + zi.compress_size // 4, body + zi.compress_size // 4 + 64):
        out[i] ^= 0xFF
    return bytes(out)


@pytest.mark.parametrize("shape", ["zero_byte", "truncated", "corrupt_member"])
def test_one_bad_lap_file_neither_blanks_tracks_nor_fails_rebuild(tmp_path, caplog, shape):
    # R1/R2: GET /tracks is the Tracks screen's ONLY data source. A zero-byte .npz raises
    # EOFError and a corrupt member zlib.error -- neither was in the caught tuple, so one
    # bad file 500'd the whole screen. It must skip that lap, log it, and carry on.
    import zipfile
    import zlib
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    ids = [_cloud_lap(store, z) for z in (-5.0, 5.0, 0.0)]
    bad = Path(store.data_dir) / f"{ids[2]}.npz"
    raw = bad.read_bytes()
    bad.write_bytes({"zero_byte": b"", "truncated": raw[: len(raw) // 2],
                     "corrupt_member": _corrupt_member(raw)}[shape])
    expect = {"zero_byte": EOFError, "truncated": zipfile.BadZipFile,
              "corrupt_member": zlib.error}[shape]
    with pytest.raises(expect), np.load(str(bad)) as z:   # the file really IS that shape
        z["pos_x"]

    with caplog.at_level(logging.WARNING, logger="simcoach.server.maps"):
        row = _row(client)
    assert row["live_laps"] == 3
    assert row["captures"] == 2                       # the bad lap is skipped, not fatal
    assert f"{ids[2]}.npz" in caplog.text             # ...and said so
    assert _rebuild(client)["lap_count"] == 2         # the build skips the same lap


@pytest.mark.parametrize("shape", ["object_array", "zero_length", "all_nan"])
def test_badge_count_equals_build_count_for_unusable_lap_shapes(tmp_path, shape):
    # R3(b)/(c): the badge used to count a lap from its channel NAMES. A pickled object
    # array was counted then skipped by the build; zero-length and all-NaN arrays were
    # counted AND handed to the build. One function now decides both.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    ids = [_cloud_lap(store, z) for z in (-5.0, 5.0, 0.0)]
    x = np.linspace(0.0, 400.0, 2000)
    nan = np.full(2000, np.nan)
    arrays = {
        "object_array": {"pos_x": np.array([None] * 2000, dtype=object),
                         "pos_z": x * 0.0, "lap_distance": x},
        "zero_length": {"pos_x": np.array([]), "pos_z": np.array([]),
                        "lap_distance": np.array([])},
        "all_nan": {"pos_x": nan, "pos_z": nan, "lap_distance": nan},
    }[shape]
    with open(Path(store.data_dir) / f"{ids[2]}.npz", "wb") as f:
        np.savez(f, **arrays)
    row = _row(client)
    assert row["live_laps"] == 3
    assert row["captures"] == _rebuild(client)["lap_count"] == 2


def test_badge_counts_the_capture_a_build_uses_not_the_live_laps_beside_it(tmp_path):
    # R3(a): /mapcapture/save-cloud writes captures/<slug>.npz and NO map JSON. The row
    # read "Build map from 7 laps" while the build (capture wins) used the capture's 2.
    from simcoach.server.app import _save_cloud
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    for z in (-5.0, 5.0, 0.0, 1.0, -1.0, 2.0, -2.0):
        _cloud_lap(store, z)
    x = np.linspace(0.0, 400.0, 2000)
    _save_cloud(Path(store.data_dir) / "captures" / f"{_SLUG}.npz",
                [{"pos_x": x, "pos_z": np.full(2000, s * 6.0), "lap_distance": x}
                 for s in (1, -1)])
    row = _row(client)
    assert row["has_map"] is False and row["live_laps"] == 7
    assert row["captures"] == _rebuild(client)["lap_count"] == 2
    assert row["capture_unreadable"] is False       # a capture that loads is not flagged


def _unusable_capture(store, shape):
    # captures/<slug>.npz that EXISTS and yields no usable cloud: garbage bytes (np.load
    # fails -> _load_cloud []), or a real npz whose laps carry no position data.
    from simcoach.server.app import _save_cloud
    cap = Path(store.data_dir) / "captures" / f"{_SLUG}.npz"
    if shape == "garbage":
        cap.parent.mkdir(parents=True, exist_ok=True)
        cap.write_bytes(b"not an npz")
    else:
        nan = np.full(2000, np.nan)
        _save_cloud(cap, [{"pos_x": nan, "pos_z": nan, "lap_distance": nan}])
        assert np.load(str(cap)).files                  # it really loads
    return cap


@pytest.mark.parametrize("shape", ["garbage", "no_usable_laps"])
def test_an_unusable_capture_is_reported_not_blamed_on_the_live_laps(tmp_path, shape):
    # #84: a capture that exists but yields nothing used to read as captures 0 beside
    # live_laps 3 -- "3 laps, none with position data", which is false -- and the rebuild
    # 422'd "saved capture is unreadable" with no way forward. Still refused (#69: a driver
    # who asked for the capture must not silently get geometry off other data), but named.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    for z in (-5.0, 5.0, 0.0):
        _cloud_lap(store, z)
    # Plus 2 laps with no position data: N in "build from your N laps" is what a build
    # would consume (3), never the live row count (5).
    _live_lap(store, _NAME, 2, pos=False)
    assert _rebuild(client)["lap_count"] == 3           # control: the live laps DO build
    cap = _unusable_capture(store, shape)

    row = _row(client)
    assert row["capture_unreadable"] is True
    assert row["captures"] == 0 and row["live_laps"] == 5
    r = client.post("/mapcapture/rebuild", json={"slug": _SLUG})
    assert r.status_code == 422, r.text
    # The FULL path: an installed build keeps it under %APPDATA%, which a bare file name
    # gives the driver no way to find.
    assert r.json()["detail"] == (f"the saved capture for this track can't be read "
                                  f"({cap}); delete it to build from your 3 laps")


@pytest.mark.parametrize("why", ["no_position_laps", "curated_map"])
def test_an_unusable_capture_never_points_at_a_delete_that_cannot_build(tmp_path, why):
    # "Delete it to build from your N laps" is only true when deleting it WOULD build: not
    # when every live lap predates position capture, and not over a curated corner map,
    # which a live-lap rebuild refuses. Those get the way out that does work.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    if why == "no_position_laps":
        _live_lap(store, _NAME, 4, pos=False)
    else:
        for z in (-5.0, 5.0, 0.0):
            _cloud_lap(store, z)
        (tmp_path / "tracks" / f"{_SLUG}.json").write_text(json.dumps({
            "track": _SLUG, "corners": [{"number": 1, "name": "Copse", "direction": "right",
                                         "start_dist": 100.0, "apex_dist": 150.0,
                                         "end_dist": 200.0}]}), encoding="utf-8")
    cap = _unusable_capture(store, "garbage")
    assert _row(client)["capture_unreadable"] is True
    r = client.post("/mapcapture/rebuild", json={"slug": _SLUG})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == (f"the saved capture for this track can't be read "
                                  f"({cap}); use Generate map layout to record a new one")


def test_live_lap_rebuild_does_not_freeze_a_capture_so_later_laps_reach_the_map(tmp_path):
    # Save after a live-lap rebuild used to write captures/<slug>.npz from those laps.
    # A capture WINS every later rebuild, so 3 more laps showed as "7" on the row and the
    # rebuild still used the frozen 4: more driving never improved the map.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    for z in (-5.0, 5.0, 0.0, 1.0):
        _cloud_lap(store, z)
    assert _rebuild(client)["lap_count"] == 4
    assert client.post("/mapcapture/save", json={}).json()["saved"] is True
    assert (tmp_path / "tracks" / f"{_SLUG}.json").exists()           # the map was saved
    assert not (Path(store.data_dir) / "captures" / f"{_SLUG}.npz").exists()

    for z in (-1.0, 2.0, -2.0):
        _cloud_lap(store, z)
    assert _row(client)["captures"] == 7
    assert _rebuild(client)["lap_count"] == 7                          # all of them


def _save_and_bake(client, tmp_path):
    # Rebuild -> Save -> an editor save (PUT) that bakes per-corner geometry off the
    # stored ribbon: the exact path by which ribbon width/bank reach geometry_clause.
    _rebuild(client)
    assert client.post("/mapcapture/save", json={}).json()["saved"] is True
    f = tmp_path / "tracks" / f"{_SLUG}.json"
    ribbon = json.loads(f.read_text())["ribbon"]
    r = client.put(f"/tracks/{_SLUG}/map", json=with_map_rev(client, f"/tracks/{_SLUG}/map", {"corners": [
        {"number": 1, "apex_dist": 200.0, "start_dist": 150.0, "end_dist": 250.0}]}))
    assert r.status_code == 200, r.text                # a NaN bake would 500 here
    return ribbon, json.loads(f.read_text())["corners"][0]


def test_same_line_laps_save_width_and_bank_as_unknown_and_bake_none(tmp_path):
    # Real racing-lap shape: 4 laps within ~1 m of one line, pos_y differing by a few cm
    # lap to lap. The old >1e-6 m gate fitted a slope across that 1 m (Silverstone:
    # 14.6-48.4 deg) and saved width ~1.4 m, which the editor save then BAKED into the
    # corner and geometry_clause read out as fact.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    for z, y in ((-0.4, 3.00), (0.1, 3.06), (0.5, 2.95), (-0.2, 3.04)):
        _cloud_lap(store, z, y)
    ribbon, c1 = _save_and_bake(client, tmp_path)
    assert set(ribbon["width"]) == {None}
    assert set(ribbon["bank"]) == {None}
    assert "width_m" not in c1 and "banking_deg" not in c1


def test_edge_laps_save_and_bake_a_real_width_and_bank(tmp_path):
    # Positive control for the test above, same route: laps spanning the width (edges
    # at +-5 m on a 0.05 cross-slope) DO produce width and bank, so the gate is not
    # simply "always unknown". On a pass along +x, +lateral is +z (#58).
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    for z in (-5.0, 0.0, 5.0):
        _cloud_lap(store, z, 3.0 + 0.05 * z)
    ribbon, c1 = _save_and_bake(client, tmp_path)
    assert None not in ribbon["width"][20:-20] and None not in ribbon["bank"][20:-20]
    assert abs(c1["width_m"] - 10.0) < 1.0
    assert abs(c1["banking_deg"] - np.degrees(np.arctan(0.05))) < 0.3


# --- #82: a track file with corners and no stored outline (shipped Spa) -----------------
#
# The Tracks screen draws a map by slug, and used to draw ONLY a stored outline, so Spa's
# shipped file (curated corners, no path) showed nothing there while the chat screen drew
# it from the laps. Fixtures mirror the real file: named corners, `length_m`, a provenance
# note, no path/path_dist/ribbon/name; laps stored under the LIVE name + car, as captured.

_SPA = "circuit_de_spa_francorchamps"
_SPA_NAME = "Circuit de Spa-Francorchamps"


def _spa_client(tmp_path):
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    f = tracks / f"{_SPA}.json"
    # T1 is "right" on laps that circle counter-clockwise (pure LEFT geometry), so a
    # direction taken from the laps instead of the file cannot pass for the file's.
    f.write_text(json.dumps({
        "track": _SPA,
        "_config_note": "curated from the track guide; T2 driver-confirmed",
        "length_m": 400,
        "corners": [
            {"number": 1, "name": "La Source", "direction": "right",
             "start_dist": 40, "apex_dist": 60.0, "end_dist": 80},
            {"number": 2, "name": "Eau Rouge", "direction": "left",
             "start_dist": 240, "apex_dist": 260.0, "end_dist": 280},
        ],
    }, indent=2), encoding="utf-8")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    client = TestClient(create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks)),
                        base_url="http://127.0.0.1")
    return client, store, f


def _circuit_lap(store, car, track=_SPA_NAME, lap_time=90.0, rz=60.0, position=True):
    # One counter-clockwise lap of a 60 m x `rz` m ellipse. position=False is a lap
    # recorded before position capture landed: telemetry, but no pos_* to draw from.
    t = np.linspace(0.0, 2 * np.pi, 200)
    ch = ({"pos_x": 60.0 * np.cos(t), "pos_z": rz * np.sin(t)} if position
          else {"speed": np.full(200, 50.0)})
    ch["lap_distance"] = np.linspace(0.0, 400.0, 200)
    return store.save(LapRecord("lmu", track, car, lap_time, True, 0.0, ch))


def test_track_map_by_slug_borrows_the_driven_outline_when_the_file_has_none(tmp_path):
    client, store, f = _spa_client(tmp_path)
    before = f.read_bytes()
    lap = _circuit_lap(store, "Manthey #91")
    _circuit_lap(store, "Manthey #91")
    # The NEWEST lap is an edge lap (a slow pass hugging the boundary, wider line). The
    # clean-lap rule (lap_time > 1.07x the car's best) is what keeps it off the outline.
    _circuit_lap(store, "Manthey #91", lap_time=260.0, rz=90.0)
    r = client.get(f"/tracks/{_SPA}/map")
    assert r.status_code == 200, r.text
    m = r.json()
    assert m.get("path")
    # The file's corners exactly -- numbers, names AND the curated directions.
    assert [(c["number"], c["name"], c["direction"]) for c in m["corners"]] == [
        (1, "La Source", "right"), (2, "Eau Rouge", "left")]
    assert m["track_name"] == _SPA_NAME                  # the live name, not the slug
    # A circle (screen rounding aside): averaging the 90 m edge lap in would give ~889 x 1000.
    assert abs(m["width"] - m["height"]) < 1.0
    # The very payload the chat screen gets for that track -- one construction, not two.
    assert m == client.get(f"/laps/{lap}/map").json()
    # A GET writes nothing: not the track file, not an app-data overlay.
    assert f.read_bytes() == before
    assert not (tmp_path / "data" / "tracks").exists()


def test_track_map_by_slug_borrows_the_most_recently_driven_cars_outline(tmp_path):
    client, store, _f = _spa_client(tmp_path)
    older = _circuit_lap(store, "Car A", rz=30.0)        # a different outline (ellipse)
    newer = _circuit_lap(store, "Car B")
    got = client.get(f"/tracks/{_SPA}/map").json()
    assert got == client.get(f"/laps/{newer}/map").json()
    assert got != client.get(f"/laps/{older}/map").json()


def test_track_map_by_slug_skips_a_newer_car_with_no_position_laps(tmp_path):
    client, store, _f = _spa_client(tmp_path)
    older = _circuit_lap(store, "Car A")
    _circuit_lap(store, "Car B", position=False)
    got = client.get(f"/tracks/{_SPA}/map").json()
    assert got.get("path") and got == client.get(f"/laps/{older}/map").json()


def test_track_map_by_slug_without_usable_laps_stays_empty(tmp_path):
    client, store, _f = _spa_client(tmp_path)
    r = client.get(f"/tracks/{_SPA}/map")                # fresh install: no laps at all
    assert r.status_code == 200 and r.json() == {}
    _circuit_lap(store, "Car A", position=False)         # a Spa lap with nothing to draw
    _circuit_lap(store, "Car A", track="Silverstone National Circuit")  # another track's
    r = client.get(f"/tracks/{_SPA}/map")
    assert r.status_code == 200 and r.json() == {}


def test_track_map_by_slug_borrows_nothing_for_a_track_with_no_corners(tmp_path):
    # Driven, never mapped (#69): there are no owned corners to draw, and a bare outline
    # would turn the pane's "Add map" -> build flow into an "Edit" of an empty layout.
    client, store, _f = _spa_client(tmp_path)
    _circuit_lap(store, "Car A", track="Silverstone National Circuit")
    r = client.get("/tracks/silverstone_national_circuit/map")
    assert r.status_code == 200 and r.json() == {}


def test_saving_a_borrowed_outline_by_slug_is_refused_and_writes_nothing(tmp_path):
    # Saving would store the laps' average as Spa's outline and re-derive every curated
    # direction from it (on the real laps: T19 Bus Stop right -> left). Refuse, say why,
    # write nothing -- and the #69 curated-map guard on rebuild still stands after it.
    client, store, f = _spa_client(tmp_path)
    before = f.read_bytes()
    for _ in range(3):
        _circuit_lap(store, "Car A")
    shown = client.get(f"/tracks/{_SPA}/map").json()
    assert shown.get("path")
    body = {"corners": [{k: c[k] for k in ("number", "name", "apex_dist", "start_dist", "end_dist")}
                        for c in shown["corners"]], "sectors": [], "sections": []}
    r = client.put(f"/tracks/{_SPA}/map", json=with_map_rev(client, f"/tracks/{_SPA}/map", body))
    assert r.status_code == 422, r.text
    assert "no stored outline" in r.json()["detail"]
    assert f.read_bytes() == before
    assert not (tmp_path / "data" / "tracks").exists()
    r = client.post("/mapcapture/rebuild", json={"slug": _SPA})
    assert r.status_code == 422 and "curated corner map" in r.json()["detail"]
    assert f.read_bytes() == before


def test_a_slug_write_refreshes_the_borrowed_outline(tmp_path):
    # The borrowed map is cached under the LIVE name + car; the Tracks screen writes by
    # slug. A slug-only cache clear left it stale until the sidecar restarted.
    client, store, _f = _spa_client(tmp_path)
    lap = _circuit_lap(store, "Car A", rz=30.0)
    assert client.get(f"/tracks/{_SPA}/map").json()["rotation"] == 0.0
    assert client.post(f"/tracks/{_SPA}/rotation", json=with_map_rev(client, f"/tracks/{_SPA}/rotation", {"degrees": 90})).status_code == 200
    assert client.get(f"/tracks/{_SPA}/map").json()["rotation"] == 90.0
    assert client.get(f"/laps/{lap}/map").json()["rotation"] == 90.0


def test_saving_a_borrowed_outline_by_lap_route_is_refused_and_writes_nothing(tmp_path):
    # #87: the chat screen's PUT /laps/{id}/map reaches the exact same file the Tracks
    # screen's PUT /tracks/{slug}/map does (#82) -- it needs the exact same refusal, not
    # just the slug route.
    client, store, f = _spa_client(tmp_path)
    before = f.read_bytes()
    lap = None
    for _ in range(3):
        lap = _circuit_lap(store, "Car A")
    shown = client.get(f"/laps/{lap}/map").json()
    assert shown.get("path")
    body = {"corners": [{k: c[k] for k in ("number", "name", "apex_dist", "start_dist", "end_dist")}
                        for c in shown["corners"]], "sectors": [], "sections": []}
    r = client.put(f"/laps/{lap}/map", json=with_map_rev(client, f"/laps/{lap}/map", body))
    assert r.status_code == 422, r.text
    assert "no stored outline" in r.json()["detail"]
    assert f.read_bytes() == before


def _spa_client_with_outline(tmp_path):
    """Same shape as `_spa_client` (curated corners, provenance keys) but WITH a stored
    outline (#87): bypasses the #82/#87 guard so a save actually reaches `_persist_map`'s
    direction carry-forward and top-level-key preservation on real curated data, instead
    of being refused before it gets there."""
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    f = tracks / f"{_SPA}.json"
    t = np.linspace(0.0, 2 * np.pi, 200)
    # The same CCW circle shape _circuit_lap draws -- pure LEFT geometry throughout (a
    # full lap only turns one way around a circle), so T1's curated "right" disagrees
    # with whatever a re-derivation off this path would say, same as the real Spa
    # chicane (#87): a divergent fixture is the only one that can tell carry-forward
    # apart from re-derivation.
    path = [[60.0 * float(x), 60.0 * float(z)] for x, z in zip(np.cos(t), np.sin(t))]
    dist = [float(v) for v in np.linspace(0.0, 400.0, 200)]
    f.write_text(json.dumps({
        "track": _SPA,
        "_config_note": "curated from the track guide; T2 driver-confirmed",
        "_distances_note": "apex from lap-averaged steering peaks",
        "length_m": 400,
        "path": path,
        "path_dist": dist,
        "corners": [
            {"number": 1, "name": "La Source", "direction": "right",
             "start_dist": 40, "apex_dist": 60.0, "end_dist": 80},
            {"number": 2, "name": "Eau Rouge", "direction": "left",
             "start_dist": 240, "apex_dist": 260.0, "end_dist": 280},
        ],
    }, indent=2), encoding="utf-8")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    client = TestClient(create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks)),
                        base_url="http://127.0.0.1")
    return client, store, f


def test_saving_an_unchanged_spa_shaped_fixture_by_lap_route_keeps_directions_and_provenance(tmp_path):
    # #87: with the guard bypassed (a stored outline present), an unchanged save must
    # leave every corner's curated direction and every top-level provenance key
    # byte-identical -- not re-derived from the frozen path (ambiguous/wrong for a
    # chicane-shaped average), not dropped by a writer that rebuilds from a fixed key set.
    client, store, f = _spa_client_with_outline(tmp_path)
    before = json.loads(f.read_text(encoding="utf-8"))
    # No position telemetry -> _clean_lap_channels finds nothing -> the save falls back
    # to the file's own stored path, same as a slug edit with no driven lap.
    lap = _circuit_lap(store, "Car A", track=_SPA_NAME, position=False)
    body = {"corners": [{k: c[k] for k in ("number", "name", "apex_dist", "start_dist", "end_dist")}
                        for c in before["corners"]], "sectors": [], "sections": []}
    r = client.put(f"/laps/{lap}/map", json=with_map_rev(client, f"/laps/{lap}/map", body))
    assert r.status_code == 200, r.text
    after = json.loads(f.read_text(encoding="utf-8"))
    assert [(c["number"], c["direction"]) for c in after["corners"]] == \
           [(c["number"], c["direction"]) for c in before["corners"]]
    assert after["_config_note"] == before["_config_note"]
    assert after["_distances_note"] == before["_distances_note"]
    assert after["length_m"] == before["length_m"]


def test_in_rejects_a_slug_containing_a_separator(tmp_path):
    # #18: `b in p.parents` alone accepts a slug with a path separator --
    # "sub/evil" resolves INSIDE base (base is an ancestor, just not the
    # immediate parent), so the old check let it through. Unreachable today
    # (every real caller passes track_slug's [a-z0-9_]-only output), but the
    # comment sold it as a guard this closed gap now actually provides.
    from simcoach.server.app import _in
    base = tmp_path / "tracks"
    base.mkdir()
    with pytest.raises(Exception) as exc:
        _in(base, "sub/evil")
    assert getattr(exc.value, "status_code", None) == 400
    # A plain slug still resolves as a direct child of base.
    p = _in(base, "circuit_de_spa_francorchamps")
    assert p == (base / "circuit_de_spa_francorchamps.json").resolve()


def test_an_unchanged_editor_save_of_every_shipped_map_is_a_byte_identical_no_op(tmp_path):
    # #145: the server's writer drifted from scripts/promote_map.py -- every save from the
    # Tracks screen re-encoded all IPA/accents as \uXXXX and moved `pronunciations` ahead
    # of `notes`, dirtying a shipped map with a diff that changed no data. The dict-level
    # self-save test above could not see it (json.loads of both compare equal); this is the
    # server-side twin of test_promote_map's byte-identity test. Temp copies only.
    shipped = Path(simcoach.__file__).parent / "tracks"
    maps = [p for p in sorted(shipped.glob("*.json"))
            if (d := json.loads(p.read_text(encoding="utf-8"))).get("length") and d.get("path")]
    assert len(maps) >= 11, "expected the shipped corner maps"
    for src in maps:
        before = json.loads(src.read_text(encoding="utf-8"))
        tracks_dir = tmp_path / src.stem
        tracks_dir.mkdir()
        shutil.copy(src, tracks_dir / src.name)
        store = LapStore(str(tracks_dir / "c.sqlite"), str(tracks_dir / "data"))
        client = TestClient(create_app(store, Coach(FakeModel("{}")),
                                       tracks_dir=str(tracks_dir)),
                            base_url="http://127.0.0.1")
        r = client.put(f"/tracks/{src.stem}/map", json=with_map_rev(client, f"/tracks/{src.stem}/map", {
            "corners": before["corners"], "sectors": before.get("sectors") or [],
            "sections": before.get("sections") or [], "name": before.get("name")}))
        assert r.status_code == 200, (src.name, r.text)
        assert (tracks_dir / src.name).read_bytes() == src.read_bytes(), \
            f"{src.name}: an unchanged editor save rewrote the file"


def test_an_unchanged_notes_save_of_every_shipped_map_is_a_byte_identical_no_op(tmp_path):
    # #145 red-team: the Tracks screen GETs notes padded to all four fields and PUTs them
    # back, which added `"reference_times": {}` to six shipped maps and a whole empty
    # notes block to Spa -- a dirty file from a save that changed nothing.
    shipped = Path(simcoach.__file__).parent / "tracks"
    # Every map already in the shipped format -- all but test_track.json, a compact-JSON
    # fixture that ANY writer reformats (the promote twin leaves it out too).
    maps = [p for p in sorted(shipped.glob("*.json"))
            if dump_track_json(json.loads(p.read_text(encoding="utf-8"))) == p.read_bytes()]
    assert len(maps) >= 12, "expected the shipped corner maps"
    for src in maps:
        tracks_dir = tmp_path / src.stem
        tracks_dir.mkdir()
        shutil.copy(src, tracks_dir / src.name)
        store = LapStore(str(tracks_dir / "c.sqlite"), str(tracks_dir / "data"))
        client = TestClient(create_app(store, Coach(FakeModel("{}")),
                                       tracks_dir=str(tracks_dir)),
                            base_url="http://127.0.0.1")
        got = client.get(f"/tracks/{src.stem}/notes").json()
        rev = got.pop("rev")
        r = client.put(f"/tracks/{src.stem}/notes",
                       json={"track": src.stem, "rev": rev, "notes": got})
        assert r.status_code == 200, (src.name, r.text)
        assert (tracks_dir / src.name).read_bytes() == src.read_bytes(), \
            f"{src.name}: an unchanged notes save rewrote the file"


_ALG_SLUG = "algarve_international_circuit"


def _writer_fixture() -> dict:
    # Inline, so a driver's uncommitted edit to a shipped map can't fail these tests (#171
    # round 1). Shipped-map shape: a stored outline (the map PUT refuses without one), no
    # ribbon (so baked width_m can only be carried, never re-derived), baked geometry, an
    # IPA table, notes, and a non-ASCII display name.
    t = np.linspace(0.0, 2 * np.pi, 121)[:-1]
    path = [[round(60.0 * np.cos(a), 1), round(40.0 * np.sin(a), 1)] for a in t]
    corners = [{"number": n, "label": str(n), "name": nm, "direction": dr,
                "start_dist": s, "apex_dist": a, "end_dist": e, "apexes": [a],
                "radius_m": r, "elev_delta_m": -1.2, "width_m": w, "banking_deg": 1.4}
               for n, nm, dr, s, a, e, r, w in [
                   (1, "Primeira", "left", 20.0, 50.0, 80.0, 40.5, 12.3),
                   (2, "Torre VIP", "left", 170.0, 200.0, 230.0, 38.1, 11.8)]]
    return {"track": _ALG_SLUG, "name": "Portimão", "corners": corners,
            "path": path, "path_dist": [i * 2.5 for i in range(120)],
            "sectors": [100.0, 200.0], "sections": [], "length": 297.5, "rotation": 0.0,
            "notes": {"overview": "Montanha russa.", "corners": {"1": "Travão cedo."}},
            "pronunciations": {"Torre VIP": "tˈɔːɹ vˈɪp"}}


def _writer_route_save(client, lap, route):
    # One UNCHANGED save of the fixture through `route`, the body each screen sends.
    d = _writer_fixture()
    base = f"/laps/{lap}" if route.startswith("laps") else f"/tracks/{_ALG_SLUG}"
    if route.endswith("notes"):
        got = client.get(f"/tracks/{_ALG_SLUG}/notes").json()
        return client.put(f"/tracks/{_ALG_SLUG}/notes",
                          json={"track": _ALG_SLUG, "rev": got.pop("rev"), "notes": got})
    if route.endswith("rotation"):
        return client.post(f"{base}/rotation",
                           json=with_map_rev(client, f"{base}/rotation", {"degrees": d["rotation"]}))
    return client.put(f"{base}/map", json=with_map_rev(client, f"{base}/map", {
        "corners": d["corners"], "sectors": d["sectors"], "sections": d["sections"],
        "name": d["name"]}))


def _writer_client(tmp_path):
    tracks = tmp_path / "tracks"
    tracks.mkdir()
    f = tracks / f"{_ALG_SLUG}.json"
    f.write_bytes(dump_track_json(_writer_fixture()))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    lap = store.save(LapRecord("lmu", "Algarve International Circuit", "Oreca 07", 90.0,
                               True, 0.0, {"lap_distance": np.linspace(0.0, 297.5, 200),
                                           "speed": np.full(200, 50.0)}))
    client = TestClient(create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks)),
                        base_url="http://127.0.0.1")
    return client, store, lap, f


_WRITER_ROUTES = ["tracks/map", "laps/map", "tracks/notes", "tracks/rotation", "laps/rotation"]


@pytest.mark.parametrize("route", _WRITER_ROUTES)
def test_every_track_writer_route_writes_dump_track_json_bytes(tmp_path, route):
    # #171: a driver's Laguna is byte-for-byte the pre-#145 writer's output (text mode,
    # ensure_ascii), and the shipped-map tests above only cover the map PUT. Pin EVERY
    # route that rewrites a track JSON: exactly dump_track_json's bytes (raw UTF-8, CRLF
    # only from the serializer), and an unchanged save is a byte-identical no-op -- which
    # is also what proves the carried width_m survived.
    assert all("width_m" in c for c in _writer_fixture()["corners"])   # premise
    client, _store, lap, f = _writer_client(tmp_path)
    before = f.read_bytes()
    r = _writer_route_save(client, lap, route)
    assert r.status_code == 200, r.text
    out = f.read_bytes()
    assert out == dump_track_json(json.loads(out.decode("utf-8")))   # canonical bytes
    assert b"\\u" not in out and "Portimão".encode() in out
    assert out.replace(b"\r\n", b"").count(b"\r") == 0 == out.replace(b"\r\n", b"").count(b"\n")
    assert out == before, f"{route}: an unchanged save rewrote the file"


@pytest.mark.parametrize("route", _WRITER_ROUTES)
def test_an_installed_build_writes_its_overlay_as_dump_track_json_bytes(tmp_path, monkeypatch,
                                                                        route):
    # The frozen sidecar never touches the bundled map: it writes an app-data overlay
    # (trackfile._write_track_json). That overlay is the same format, not a second one.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    client, store, lap, f = _writer_client(tmp_path)
    before = f.read_bytes()
    r = _writer_route_save(client, lap, route)
    assert r.status_code == 200, r.text
    assert f.read_bytes() == before                      # the bundle is untouched
    out = (Path(store.data_dir) / "tracks" / f.name).read_bytes()
    assert out == dump_track_json(json.loads(out.decode("utf-8")))
    assert b"\\u" not in out and "Portimão".encode() in out


def test_mapcapture_save_writes_dump_track_json_bytes(tmp_path):
    # #171, the writer route that rebuilds a map's geometry from a capture: same bytes rule, and
    # a kept non-ASCII display name lands raw, not \\u-escaped.
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    f = tmp_path / "tracks" / f"{_SLUG}.json"
    f.write_bytes(dump_track_json({"track": _SLUG, "name": "Portimão", "corners": []}))
    for z in (-5.0, 0.0, 5.0):
        _cloud_lap(store, z, 3.0)
    _rebuild(client)
    assert client.post("/mapcapture/save", json={}).json()["saved"] is True
    out = f.read_bytes()
    assert out == dump_track_json(json.loads(out.decode("utf-8")))
    assert b"\\u" not in out and "Portimão".encode() in out


def test_a_notes_save_that_clears_a_field_writes_it_empty(tmp_path):
    # The other half of the no-op rule: a field the file HAS is written even when the
    # author empties it -- dropping only fields the file never had must not make a
    # deliberate clear a no-op that leaves the old text on disk.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit", "rev": _rev(client),
        "notes": {"overview": "", "corners": {}, "reference_times": {}, "source": ""}})
    assert r.status_code == 200, r.text
    assert _written(tracks_dir)["notes"] == {
        "overview": "", "corners": {}, "reference_times": {}, "source": ""}


def test_a_lone_surrogate_is_refused_with_422_not_a_500(tmp_path):
    # JSON carries "\ud800" fine, but the file is raw UTF-8 now (#145) and encoding it
    # raised UnicodeEncodeError -> 500. Invalid input, so 422, and nothing written.
    client, tracks_dir = _client_with_frozen_path(tmp_path)
    assert _put_notes(client, {"1": "brake late"}).status_code == 200
    f = tracks_dir / "algarve_international_circuit.json"
    before = f.read_bytes()
    lone = "\ud800"

    def put(route, body):
        # Sent as the \ud800 ESCAPE, the way a browser's JSON.stringify sends it: httpx's
        # json= encodes raw UTF-8 and cannot carry a lone surrogate at all.
        url = f"/tracks/algarve_international_circuit/{route}"
        if route == "map":
            body = with_map_rev(client, url, body)   # reach the write, not the 428 (#166)
        r = client.put(url, content=json.dumps(body), headers={"content-type": "application/json"})
        assert r.status_code == 422, (route, body, r.text)

    def notes(**over):
        return {"track": "Algarve International Circuit", "rev": _rev(client),
                "notes": {"overview": "o", "corners": {}, "reference_times": {},
                          "source": "", **over}}

    put("notes", notes(overview=lone))
    put("notes", notes(corners={"1": lone}))
    put("notes", notes(corners={lone: "key"}))
    put("notes", notes(reference_times={"GT3": lone}))
    for corner in ({"name": lone}, {"label": lone}):
        put("map", {"corners": [
            {"number": 1, "label": "1", "name": "Turn 1", "apex_dist": 50.0, **corner}]})
    put("map", {"name": lone, "corners": [{"number": 1, "label": "1", "apex_dist": 50.0}]})
    # The backstop in _write_track_json: a section name no route checks.
    put("map", {"corners": [{"number": 1, "label": "1", "apex_dist": 50.0}],
                "sections": [{"name": lone, "start_dist": 10.0, "end_dist": 90.0}]})
    assert f.read_bytes() == before


def test_an_untimed_invalidated_whole_lap_stays_off_the_outline_and_the_cloud(tmp_path):
    """#181: an invalidated whole lap is complete (partial NULL) but untimed -- its off or
    cut is not the line. Kept off the averaged outline and the rebuild cloud, as it was
    while it was stored 'partial'."""
    import dataclasses
    client, store, _f = _spa_client(tmp_path)
    lap = _circuit_lap(store, "Manthey #91")
    _circuit_lap(store, "Manthey #91")
    wide = _circuit_lap(store, "Manthey #91", rz=90.0)
    store.conn.execute("UPDATE laps SET lap_time = NULL, valid = 0 WHERE id = ?", (wide,))
    assert dataclasses.asdict(store.load(wide))["partial"] is None
    m = client.get(f"/tracks/{_SPA}/map").json()
    assert abs(m["width"] - m["height"]) < 1.0       # averaging it in would stretch it
    assert m == client.get(f"/laps/{lap}/map").json()
    row = next(t for t in client.get("/tracks").json() if t["slug"] == _SPA)
    assert row["live_laps"] == 2


# --- #196: a corner whose window changed does not keep the old window's bake ---------

_LAGUNA = "weathertech_raceway_laguna_seca"
# Laguna's T1/T2 exactly as committed before the driver's merge edit (origin/main 46d85eb),
# inline so the driver's uncommitted re-edit of the shipped file can't move the premise.
# Old T2's width/banking are NOT the committed ones (10.3/-2.2, the same as T1's): they are
# made distinct so a carried value names the corner it came from.
_LAGUNA_OLD_T1_T2 = [
    {"number": 1, "label": "1", "name": "Turn 1", "direction": "left", "start_dist": 370.2,
     "apex_dist": 459.9, "end_dist": 495.8, "apexes": [459.9], "radius_m": 31.2,
     "elev_delta_m": -5.1, "width_m": 10.3, "banking_deg": -2.2},
    {"number": 2, "label": "2", "name": "Andretti Hairpin", "direction": "left",
     "start_dist": 495.8, "apex_dist": 531.8, "end_dist": 621.5, "apexes": [531.8],
     "radius_m": 34.8, "elev_delta_m": -0.8, "width_m": 9.1, "banking_deg": -0.7},
]
_EDITOR_KEYS = ("number", "label", "name", "apex_dist", "apexes", "start_dist", "end_dist")


def _laguna_client(tmp_path):
    # The real map (500-station frozen path, no ribbon, baked geometry, notes) with its
    # first two corners pinned to the pre-merge committed values.
    data = json.loads((Path(simcoach.__file__).parent / "tracks" / f"{_LAGUNA}.json")
                      .read_text(encoding="utf-8"))
    data["corners"] = [dict(c) for c in _LAGUNA_OLD_T1_T2] + data["corners"][2:]
    tracks = tmp_path / "tracks"
    tracks.mkdir()
    f = tracks / f"{_LAGUNA}.json"
    f.write_bytes(dump_track_json(data))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    client = TestClient(create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks)),
                        base_url="http://127.0.0.1")
    return client, store, data, f


def _put_laguna(client, corners, data):
    # The body TrackMapEditor sends: identity + window only, never geometry.
    return client.put(f"/tracks/{_LAGUNA}/map", json=with_map_rev(
        client, f"/tracks/{_LAGUNA}/map", {
            "corners": [{k: c[k] for k in _EDITOR_KEYS if k in c} for c in corners],
            "sectors": data.get("sectors") or [], "sections": data.get("sections") or [],
            "name": data.get("name")}))


def _by_apex(corners, apex):
    return next(c for c in corners if c["apex_dist"] == apex)


def _measured(data, c):
    # What corner_geometry measures for `c`'s own window on the map's path.
    return corner_geometry(data["path"], data["path_dist"], [dict(c, label="x")])["x"]


# The driver's real edit (#196): old T2 dragged onto old T1's apex 459.9 -- its window
# translated with it to 423.9-549.6 -- old T1 deleted, 531.8 re-added as a second apex,
# and a new T1 placed at 230.0.
_MERGED_T2 = {"number": 2, "label": "2", "name": "Andretti Hairpin", "start_dist": 423.9,
              "apex_dist": 459.9, "end_dist": 549.6, "apexes": [459.9, 531.8]}
_NEW_T1 = {"number": 1, "label": "1", "name": "Turn 1", "start_dist": 140.3,
           "apex_dist": 230.0, "end_dist": 265.9, "apexes": [230.0]}


def test_a_merged_corner_keeps_neither_old_corners_window_bake(tmp_path):
    client, _store, data, f = _laguna_client(tmp_path)
    r = _put_laguna(client, [_NEW_T1, _MERGED_T2] + data["corners"][2:], data)
    assert r.status_code == 200, r.text
    written = json.loads(f.read_text(encoding="utf-8"))
    t2 = _by_apex(written["corners"], 459.9)
    # Old T1's -5.1 was measured over 370.2-495.8, old T2's -0.8 over 495.8-621.5:
    # neither describes 423.9-549.6, and an editor save has no elevation source.
    assert "elev_delta_m" not in t2
    assert t2["radius_m"] == _measured(written, t2)["radius_m"]   # re-measured, own window
    # Read AT the apex, and the merged T2's apex 459.9 is old T1's, so old T1's values --
    # not old T2's 9.1/-0.7, measured at 531.8 (now only a secondary apex).
    assert (t2["width_m"], t2["banking_deg"]) == (10.3, -2.2)
    t1 = _by_apex(written["corners"], 230.0)                       # brand-new corner
    assert not {"elev_delta_m", "width_m", "banking_deg"} & t1.keys()
    assert t1["radius_m"] == _measured(written, t1)["radius_m"]
    # Untouched corners keep their bake (the Corkscrew's -16.7 m is the real one).
    assert _by_apex(written["corners"], 2515.4)["elev_delta_m"] == -16.7

    # And the coach no longer states it: get_corner's geometry line, from the map it loads.
    ctx = CoachContext(store=None, cmap=CornerMap.from_data(written), track="Laguna Seca",
                       car="GT3")
    clause = ctx._corner_geometry_clause("2", "left")
    assert clause == "2\N{DEGREE SIGN} banked" and "downhill" not in clause
    # The same save under the old carry-forward: this is what the coach used to say.
    assert geometry_clause(_LAGUNA_OLD_T1_T2[0], "left").startswith("downhill 5m")


def test_a_split_corner_re_measures_its_window_and_the_new_half_inherits_nothing(tmp_path):
    client, _store, data, f = _laguna_client(tmp_path)
    t1, t2 = (dict(c) for c in _LAGUNA_OLD_T1_T2)
    first = dict(t2, end_dist=560.0)                               # Andretti, entry half
    second = {"number": 3, "label": "2b", "name": "Andretti exit", "start_dist": 560.0,
              "apex_dist": 590.0, "end_dist": 621.5, "apexes": [590.0]}
    rest = [dict(c, number=c["number"] + 1) for c in data["corners"][2:]]
    r = _put_laguna(client, [t1, first, second] + rest, data)
    assert r.status_code == 200, r.text
    corners = json.loads(f.read_text(encoding="utf-8"))["corners"]
    a, b = _by_apex(corners, 531.8), _by_apex(corners, 590.0)
    assert "elev_delta_m" not in a and a["radius_m"] == _measured(data, a)["radius_m"]
    assert (a["width_m"], a["banking_deg"]) == (9.1, -0.7)        # old T2's own apex
    assert not {"elev_delta_m", "width_m", "banking_deg"} & b.keys()
    assert _by_apex(corners, 459.9)["elev_delta_m"] == -5.1       # T1 untouched: kept


@pytest.mark.parametrize("end, kept", [(2605.14, True), (2605.16, False), (2615.1, False)])
def test_a_window_move_beyond_the_1dp_tolerance_drops_the_window_bake(tmp_path, end, kept):
    # The Corkscrew (2479.4-2605.1, elev -16.7). Windows are written at 1 dp and the
    # editor sends them back verbatim, so the tolerance is that resolution: +0.04 is the
    # same stored window (and a byte-identical save), +0.06 is a different one.
    client, _store, data, f = _laguna_client(tmp_path)
    before = f.read_bytes()
    corners = [dict(c) for c in data["corners"]]
    cork = _by_apex(corners, 2515.4)
    cork["end_dist"] = end
    r = _put_laguna(client, corners, data)
    assert r.status_code == 200, r.text
    out = _by_apex(json.loads(f.read_text(encoding="utf-8"))["corners"], 2515.4)
    assert (out.get("elev_delta_m") == -16.7) is kept
    assert (f.read_bytes() == before) is kept
    if not kept:
        assert "elev_delta_m" not in out
        assert out["radius_m"] == _measured(data, out)["radius_m"]
        assert (out["width_m"], out["banking_deg"]) == (9.4, 4.1)


def test_an_installed_builds_read_does_not_put_the_shipped_bake_back(tmp_path, monkeypatch):
    # The frozen sidecar writes an app-data overlay and reads it merged over the shipped
    # map, where a shipped corner at the same apex used to win on every metric (#75) --
    # so the merged T2 picked old T1's -5.1 back up on the very next read.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    client, store, data, f = _laguna_client(tmp_path)
    r = _put_laguna(client, [_NEW_T1, _MERGED_T2] + data["corners"][2:], data)
    assert r.status_code == 200, r.text
    user = Path(store.data_dir) / "tracks"
    overlay_t2 = _by_apex(json.loads((user / f.name).read_text("utf-8"))["corners"], 459.9)
    assert "elev_delta_m" not in overlay_t2
    merged = load_map_data(_LAGUNA, str(f.parent), str(user))
    t2 = _by_apex(merged["corners"], 459.9)
    assert "elev_delta_m" not in t2 and t2["radius_m"] == overlay_t2["radius_m"]
    assert (t2["width_m"], t2["banking_deg"]) == (10.3, -2.2)
    # An unchanged window still takes the shipped bake (#75's re-bakes keep reaching it).
    assert _by_apex(merged["corners"], 2515.4)["elev_delta_m"] == -16.7
    # And the next unchanged save from that merged read is a no-op on the overlay.
    before = (user / f.name).read_bytes()
    assert _put_laguna(client, merged["corners"], data).status_code == 200
    assert (user / f.name).read_bytes() == before


def _station(data, near):
    # A click lands on a path station (TrackMapEditor: map.dist[nearestPathIndex]).
    return min(data["path_dist"], key=lambda d: abs(d - near))


def _click_added(apex):
    # TrackMapEditor's click-add: no start_dist/end_dist, blank label and name.
    return {"number": 0, "label": "", "name": "", "apex_dist": apex, "apexes": [apex]}


def _renumbered(corners):
    # The editor's renumber(): number = position in track order.
    return [dict(c, number=i + 1)
            for i, c in enumerate(sorted(corners, key=lambda c: c["apex_dist"]))]


def _assert_stored_windows_are_the_read_windows(written):
    # What the coach analyses (CornerMap.from_data re-clamps on read) is what was stored,
    # so every carried or re-measured metric is about the window actually read.
    read = {c.apex_dist: (c.start_dist, c.end_dist) for c in CornerMap.from_data(written).corners}
    assert {c["apex_dist"]: (c["start_dist"], c["end_dist"]) for c in written["corners"]} == read


def test_a_click_added_corner_inside_a_neighbour_clamps_it_and_drops_its_window_bake(tmp_path):
    # Red-team #196 round 1: click-adding at ~1600 m got apex +/- pad (1510.3-1689.7)
    # over T5 (1448.3-1627.7, elev +5.6). T5's STORED window was unchanged so +5.6
    # carried, but the clamp on read cut T5 to 1448.3-1569.0 -- the coach said "uphill
    # 6m" about a window whose capture re-bake is 3.7.
    client, _store, data, f = _laguna_client(tmp_path)
    new = _station(data, 1600.0)
    r = _put_laguna(client, _renumbered(data["corners"] + [_click_added(new)]), data)
    assert r.status_code == 200, r.text
    written = json.loads(f.read_text(encoding="utf-8"))
    _assert_stored_windows_are_the_read_windows(written)
    t5 = _by_apex(written["corners"], 1538.0)
    assert t5["end_dist"] < 1627.7                                 # premise: trimmed
    assert "elev_delta_m" not in t5
    assert t5["radius_m"] == _measured(written, t5)["radius_m"]
    assert (t5["width_m"], t5["banking_deg"]) == (10.7, -4.3)      # apex unchanged
    added = _by_apex(written["corners"], round(new, 1))
    assert not in_window(1538.0, added["start_dist"], added["end_dist"])
    assert not {"elev_delta_m", "width_m", "banking_deg"} & added.keys()
    assert added["radius_m"] == _measured(written, added)["radius_m"]
    ctx = CoachContext(store=None, cmap=CornerMap.from_data(written), track="Laguna Seca",
                       car="GT3")
    assert "uphill" not in ctx._corner_geometry_clause(t5["label"], "left")


@pytest.mark.parametrize("drag", [0, 1])
def test_an_editor_shaped_split_clamps_and_re_measures_both_halves(tmp_path, drag):
    # The split as the editor makes it: Andretti left alone (drag=0) or dragged one path
    # station (window translated with the apex, onPointerMove), plus an exit corner
    # click-added inside its window.
    client, _store, data, f = _laguna_client(tmp_path)
    pd = data["path_dist"]
    corners = [dict(c) for c in data["corners"]]
    andretti = _by_apex(corners, 531.8)
    if drag:
        to = pd[pd.index(_station(data, 531.8)) + drag]
        delta = to - andretti["apex_dist"]
        andretti.update(apex_dist=to, apexes=[to], start_dist=andretti["start_dist"] + delta,
                        end_dist=andretti["end_dist"] + delta)
    exit_apex = _station(data, 590.0)
    r = _put_laguna(client, _renumbered(corners + [_click_added(exit_apex)]), data)
    assert r.status_code == 200, r.text
    written = json.loads(f.read_text(encoding="utf-8"))
    _assert_stored_windows_are_the_read_windows(written)
    a = _by_apex(written["corners"], round(andretti["apex_dist"], 1))
    b = _by_apex(written["corners"], round(exit_apex, 1))
    assert a["end_dist"] == b["start_dist"] < 621.5                # clamped at the midpoint
    assert "elev_delta_m" not in a
    assert a["radius_m"] == _measured(written, a)["radius_m"]
    # Kept only while the apex it was measured at is unchanged; a dragged apex has no
    # ribbon to re-measure from.
    assert ({k: a[k] for k in ("width_m", "banking_deg") if k in a}
            == ({} if drag else {"width_m": 9.1, "banking_deg": -0.7}))
    assert not {"elev_delta_m", "width_m", "banking_deg"} & b.keys()
    assert _by_apex(written["corners"], 459.9)["elev_delta_m"] == -5.1   # T1 untouched


# --- #198: the lap route replaces the path, so a bake read off the old path is re-measured --


def test_a_lap_route_save_that_replaces_the_path_re_measures_radius_on_the_new_one(tmp_path):
    # Laguna has no ribbon, so PUT /laps/{id}/map re-averages its path from the clean laps.
    # The driven lap here is the real outline scaled 1.2x about its centroid, sampled at
    # 2000 points on its own lap-distance axis: every window, apex and label is unchanged,
    # only the path under them moved -- and every radius with it (x1.2).
    client, store, data, f = _laguna_client(tmp_path)
    p = np.asarray(data["path"], float)
    p = p.mean(axis=0) + 1.2 * (p - p.mean(axis=0))
    ld = np.linspace(0.0, data["path_dist"][-1], 2000)
    ch = {"pos_x": np.interp(ld, data["path_dist"], p[:, 0]),
          "pos_z": np.interp(ld, data["path_dist"], p[:, 1]), "lap_distance": ld}
    lap = store.save(LapRecord("lmu", "WeatherTech Raceway Laguna Seca", "Porsche 963",
                               82.0, True, 0.0, ch))
    body = {"corners": [{k: c[k] for k in _EDITOR_KEYS if k in c} for c in data["corners"]],
            "sectors": data.get("sectors") or [], "sections": data.get("sections") or [],
            "name": data.get("name")}
    r = client.put(f"/laps/{lap}/map", json=with_map_rev(client, f"/laps/{lap}/map", body))
    assert r.status_code == 200, r.text
    written = json.loads(f.read_text(encoding="utf-8"))
    assert written["path"] != data["path"]                        # premise: path replaced
    old = {c["apex_dist"]: c for c in data["corners"]}
    for c in written["corners"]:
        o = old[c["apex_dist"]]
        assert (c["start_dist"], c["end_dist"]) == (o["start_dist"], o["end_dist"])
        # Measured on the path it is stored with, not carried from the old one.
        assert c["radius_m"] == _measured(written, c)["radius_m"]
        # Keyed on lap distance (pos_y elevation, cloud width/banking), not on the path's
        # shape: the same window and apex still describe the same stretch of track.
        assert {k: c.get(k) for k in ("elev_delta_m", "width_m", "banking_deg")} == \
               {k: o.get(k) for k in ("elev_delta_m", "width_m", "banking_deg")}
    # T1's old 31.2 m, carried, would be the old path's radius on the new one.
    t1 = _by_apex(written["corners"], 459.9)
    assert t1["radius_m"] != 31.2 and abs(t1["radius_m"] / 31.2 - 1.2) < 0.05
